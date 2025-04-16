from contextvars import ContextVar
import datetime
import email.utils
import importlib
import inspect
import json
import logging
import os.path
import socket
import time

import fastapi
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse as BaseStreamingResponse
from fastapi.utils import is_body_allowed_for_status_code
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.types import Receive, Scope, Send
import uvicorn

import udon.xsgi


_apis = {}


def api(path):
    def _(setup):
        _apis[setup] = API(path, setup)
    return _


class API:

    stack = None
    app = None

    def __init__(self, prefix, setup):
        self.prefix = prefix
        self.setup = setup

    def install(self, stack):
        assert self.stack is None
        mount_point = os.path.join(stack.prefix, self.prefix)
        if not mount_point.endswith("/"):
            mount_point += "/"
        app = stack.app_factory()
        # shared app state
        app.state = stack.app.state
        self.setup(app)
        stack.app.mount(mount_point, self)
        self.app = app
        self.stack = stack
        # propagate shutdown
        self.stack.on_before_shutdown(self.app.before_shutdown)

    async def __call__(self, scope, receive = None, send = None):
        return await self.app(scope, receive, send)


class APIStack:

    def __init__(self, prefix = "/", **options):
        options.setdefault("exception_handlers", {
            Exception: self.handle_error,
            HTTPException: self.handle_http_error,
        })
        self.prefix = prefix
        self.options = options
        self.app = self.app_factory()
        self.apis = []

    async def handle_error(self, request: fastapi.Request, exc: Exception):
        logging.error(str(exc))
        return response_json({"error": str(exc)}, status_code=500)

    async def handle_http_error(self, request: fastapi.Request, exc: HTTPException):
        logging.error(str(exc))
        headers = getattr(exc, "headers", None)
        if not is_body_allowed_for_status_code(exc.status_code):
            return Response(status_code=exc.status_code, headers=headers)

        data = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        return JSONResponse(data, status_code=exc.status_code, headers=headers)

    def app_factory(self):
        return App(**self.options)

    def install_func(self, prefix, setup):
        api = API(prefix, setup)
        api.install(self)
        self.apis.append(api)

    def install_module(self, module_name):
        importlib.import_module(module_name)
        for api in _apis.values():
            if not api.stack:
                api.install(self)

    def on_startup(self, method):
        self.app.on_startup(method)

    def on_shutdown(self, method):
        self.app.on_shutdown(method)

    def on_before_shutdown(self, method):
        self.app.on_before_shutdown(method)

    def before_shutdown(self):
        self.app.before_shutdown()

    def add_middleware(self, cls, **args):
        self.app.add_middleware(cls, **args)
        for api in self.apis:
            api.app.add_middleware(cls, **args)

    def add_http_middleware(self, method, **args):
        self.app.add_http_middleware(method, **args)
        for api in self.apis:
            api.app.add_http_middleware(method, **args)

    async def __call__(self, scope, receive = None, send = None):
        return await self.app(scope, receive, send)


class App(fastapi.FastAPI):

    def __init__(self, **kwargs):
        kwargs.setdefault("docs_url", None)
        kwargs.setdefault("redoc_url", None)
        kwargs.setdefault("debug", False)
        fastapi.FastAPI.__init__(self, **kwargs)
        self.before_shutdown_handlers = set()

    def get(self, path, endpoint, **args):
        fastapi.FastAPI.get(self, path, **args)(endpoint)

        # autohead
        async def wrapper(**args):
            response = await endpoint(**args)
            if response.status_code == 200:
                return Response(status_code=response.status_code,
                                headers=response.headers)
            return response
        wrapper.__signature__ = inspect.signature(endpoint)
        fastapi.FastAPI.head(self, path, **args)(wrapper)

    def post(self, path, endpoint, **args):
        fastapi.FastAPI.post(self, path, **args)(endpoint)

    def put(self, path, endpoint, **args):
        fastapi.FastAPI.put(self, path, **args)(endpoint)

    def patch(self, path, endpoint, **args):
        fastapi.FastAPI.patch(self, path, **args)(endpoint)

    def head(self, path, endpoint, **args):
        fastapi.FastAPI.head(self, path, **args)(endpoint)

    def delete(self, path, endpoint, **args):
        fastapi.FastAPI.delete(self, path, **args)(endpoint)

    def options(self, path, endpoint, **args):
        fastapi.FastAPI.options(self, path, **args)(endpoint)

    def on_startup(self, endpoint):
        fastapi.FastAPI.on_event(self, "startup")(endpoint)

    def on_shutdown(self, endpoint):
        fastapi.FastAPI.on_event(self, "shutdown")(endpoint)

    def on_before_shutdown(self, endpoint):
        self.before_shutdown_handlers.add(endpoint)

    def before_shutdown(self):
        for handler in self.before_shutdown_handlers:
            handler()

    def add_http_middleware(self, endpoint, **args):
        fastapi.FastAPI.middleware(self, "http")(endpoint, **args)

    async def __call__(self, scope: Scope, receive = Receive, send = Send):
        try:
            return await fastapi.FastAPI.__call__(self, scope, receive, send)
        except Exception as exc:
            logging.exception(exc)


class Config(uvicorn.Config):
    pass


class Server(uvicorn.Server):
    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self.config.app.before_shutdown()
        await uvicorn.Server.shutdown(self, sockets)


def run(app, **kwargs):
    # Use proxy headers to hydrate request
    kwargs.setdefault("proxy_headers", True)
    # Do not put uvicorn server header in all responses
    kwargs.setdefault("server_header", False)
    # Default is to disable all logging and use udon.asgi.log_http_middleware
    # Disable uvicorn error log
    kwargs.setdefault("log_config", {
        "version": 1,
        "level": "NOTSET",
        "handlers": "",
    })
    # Disable uvicorn access log
    kwargs.setdefault("access_log", False)

    config = Config(app, **kwargs)
    server = Server(config)

    try:
        server.run()
    except (SystemExit, KeyboardInterrupt):
        pass
    except:  # noqa: E722
        logging.exception("EXCEPTION")


###
# request
###


CONTEXTS = ContextVar("udon")

# default request when outside of api route (mimics bottle behavior)
CONTEXTS.set({"request": fastapi.Request(scope={"type": "http", "headers": {}})})


# per route request set by middleware
async def context_http_middleware(request: fastapi.Request, call_next):
    context = CONTEXTS.set({"request": request})
    response = await call_next(request)
    CONTEXTS.reset(context)

    return response


class LocalRequest():
    def __getattr__(self, name):
        return getattr(CONTEXTS.get()["request"], name)


# thread safe accessor for the *current* fastapi.Request
request = LocalRequest()
_request = request


###
# responses
###


Response = fastapi.responses.Response


def response_json(data, status_code = 200):
    return fastapi.responses.JSONResponse(jsonable_encoder(data), status_code=status_code)


def response_ok(status_code = fastapi.status.HTTP_204_NO_CONTENT):
    return fastapi.responses.Response(status_code=status_code)


def abort(status_code, detail = None):
    raise fastapi.HTTPException(status_code, detail)


async def request_to_stream_generator(req):
    # TODO: test/understand behavior of iter_* methods.
    # Eventually fallback to iter_chunked with a good chunk value ?
    async for data in req.content.iter_any():
        yield data


async def log_http_middleware(request: fastapi.Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = time.perf_counter() - start_time

    try:
        length = str(len(response.content))
    except:
        length = "-"

    logging.info(
        "%.3f %s %s %s %d %s %s" % (
            process_time,
            client_ip(request),
            request.headers.get("HTTP_X_FORWARDED_FOR", "-"),
            request.method,
            response.status_code,
            length,
            request.url
        )
    )

    return response


import typing
from starlette.background import BackgroundTask
from starlette.responses import ContentStream

class StreamingResponse(BaseStreamingResponse):
    def __init__(
        self,
        content: ContentStream,
        status_code: int = 200,
        headers: typing.Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        # starlette StreamingResponse wraps iterators into a thread (starlette.concurreny.iterate_in_threadpool)
        # but sometimes it just hangs, probably due to some anyio internal reasons.
        # Sync iterable must be used "as is", if a performance issue arises, then just reimpl the iterator itself.
        if not isinstance(content, typing.AsyncIterable):
            async def _(iterator):
                for a in iterator:
                    yield a
            content = _(content)
        BaseStreamingResponse.__init__(self, content, status_code, headers, media_type, background)


def streaming(request: fastapi.Request, size: int, fd, timestamp: int = None, etag: str = None, headers: dict = None):
    status = 200
    body = iter(())
    length = size
    range_ = parse_range(request, length)
    headers = {} if headers is None else headers

    if not none_match(etag, request) or not modified_since(timestamp, request):
        length = 0
        status = 304
    elif range_:
        offset, end = range_
        length = end - offset
        headers["Content-Range"] = "bytes %d-%d/%d" % (offset, end - 1, size)
        status = 206
        body = iter_range(fd, offset, end - offset, None)
    elif request.method != "HEAD":
        body = fd

    return StreamingResponse(
        content=body,
        status_code=status,
        # Caching logic here is to support both If-None-Match and If-Modified-Since
        # invalidation mechanism to allow a "small" caching age of one hour.
        headers = {
            "Cache-Control": "public, max-age=3600",
            "Content-Length": str(length),
            "Last-Modified": formatdate(timestamp),
            "Accept-Ranges": "bytes",
            # An Etag containing the mtime is pretty much useless, should be a real file hash.
            # TODO: use info.CRC from ZipInfo, and swift object MD5
            "Etag": etag,
            **headers})


def parse_range(request: fastapi.Request, size):
    value = request.headers.get("Range", "")
    if not value.startswith("bytes="):
        return None

    for rng in value.split("=", 1)[1].split(","):
        if "-" not in rng:
            continue
        offset, end = rng.split("-", 1)
        if (offset, end) == ("", ""):
            continue
        if not offset:
            offset, end = max(0, size - int(end) + 1), size
        elif not end:
            offset, end = int(offset), size
        else:
            offset, end = int(offset), int(end) + 1
        if 0 <= offset < end <= size:
            return offset, end

    return None


def iter_range(body, offset, count, logger):
    try:
        if (offset):
            if body.seekable():
                body.seek(offset, 1)
            else:
                for _ in iter_range_chunk(body, offset):
                    pass
        for chunk in iter_range_chunk(body, count):
            yield chunk
    except GeneratorExit:
        # interrupted transfer
        pass
    finally:
        body.close()


def iter_range_chunk(fp, count, bufsize = 1024 * 1024):
    while count:
        data = fp.read(min(count, bufsize))
        if not data:
            break
        yield data
        count -= len(data)


def client_ip(request = _request):
    return request.client.host if request.client is not None else request.headers.get("x-forwarded-for")


def formatdate(ts):
    """ Format an HTTP-Date """
    return email.utils.formatdate(timeval = ts, usegmt = True)


def parsedate(date: str):
    """ Parse an HTTP-Date into a datetime """
    return email.utils.parsedate_to_datetime(date)


def modified_since(ts: int, request: fastapi.Request):
    ims = request.headers.get("If-Modified-Since", "")
    if ims:
        # mdate is offset-naive, must make imsdate naive too
        mdate = datetime.datetime.utcfromtimestamp(ts)
        imsdate = parsedate(ims).replace(tzinfo = None)

        return (imsdate < mdate)

    return True


def none_match(etag: str, request: fastapi.Request):
    """
    etag: must already be enclosed with double quotes
    """
    # Get clean header
    inm = request.headers.get("If-None-Match", "").strip(" ")
    if inm:
        # Remove leading weak marker if needed
        if inm.startswith("W/"):
            inm = inm[2:]
        # List all etags, each double-quote encosed and coma separated
        etags = [part.strip(" ") for part in inm.split(",")]

        return (etag not in etags)

    return True


def access_control_route_handler(method, cors):
    options = {} if cors is True else cors

    def _(request: fastapi.Request, response: fastapi.Response):
        set_access_control_headers(response,
                                   method="GET",
                                   origin=options.get("origin", request.headers.get("Origin")),
                                   headers=options.get("headers", []))
    return _


def access_control_options_handler(method, cors):
    options = {} if cors is True else cors

    def _(request: fastapi.Request, response: fastapi.Response):
        set_access_control_headers(response,
                                   method=method,
                                   origin=options.get("origin", request.headers.get("Origin")),
                                   headers=options.get("headers", []))
    return _


def set_access_control_headers(response, method = "GET", origin = None, headers = []):
    for key, value in access_control_headers(method, origin, headers).items():
        response.headers[key] = value

    return response


def access_control_headers(method = "GET", origin = None, headers = []):
    return {
        "Access-Control-Allow-Origin": origin or "*",
        "Access-Control-Allow-Methods": method,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Headers": ", ".join(headers),
        "Access-Control-Max-Age": "600",
        # 1. The response will obviously vary with Origin because of the dynamic "Access-Control-Allow-Origin" header.
        # This is required so the CORS is not cached by browser for example when switching between editor and preview.
        # editor uses the main origin (ex: https://omnibook.com) and preview uses the subdomain origin (ex: https://<uuid>.omnibook.com)
        # and resources loaded cross-origin (ex: fonts from the theme) must not use the cached response from different origin
        # or the UA will emit a CORS error and the font wont load.
        # 2. The "Accept-Encoding" vary header is automatically added by haproxy but it seems that setting the Vary header
        # prevents haproxy to put his version, so put both here to prevent invalid responses.
        "Vary": "Origin, Accept-Encoding",
    }


###
# Expose pydantic model & wsgi params
###


Params = BaseModel


class Form(udon.xsgi.Form):

    def __init__(self, request = None):
        if request is None:
            request = _request
        udon.xsgi.Form.__init__(request)


class Parameters(udon.xsgi.Parameters):

    def abort(self, status_code, detail):
        abort(status_code, detail)


async def params(request = _request):
    return Parameters(await _request_json(request) or {})


async def _request_json(request):
    # wsgi BC: allow empty body
    body = await request.body()
    if not body:
        return {}

    try:
        return json.loads(body)
    except json.decoder.JSONDecodeError:
        abort(400, "Invalid JSON content")


###
# udon.content bridge
###


def response_content(fp, public = False, max_age = 0, response_headers = {}):
    view = udon.xsgi.ResourceView(fp,
                                  fp.info.size,
                                  fp.info.timestamp,
                                  ctype=fp.info.ctype,
                                  etag=fp.info.etag)
    response = response_view(view, response_headers=response_headers)

    visibility = "public" if public else "private"
    caching = "max-age={max_age}, must-revalidate" if max_age else "no-cache"
    response.headers["Cache-Control"] = f"{visibility}, {caching}"

    return response


def response_view(view, request = None, response_headers = {}):
    if request is None:
        request = _request

    (status_code, headers, body) = udon.xsgi.response_view(view, request, response_headers)

    return StreamingResponse(content=body,
                             status_code=status_code,
                             headers=headers)


def response_file(path, ctype = None, etag = None):
    return response_view(udon.xsgi.FileResourceView(path, ctype=ctype, etag=etag))
