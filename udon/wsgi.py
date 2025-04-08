#
# Copyright (c) 2018 Eric Faurot <eric@faurot.net>
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
#

import base64
import datetime
import hashlib
import importlib
import json
import logging
import os
import time

import bottle

import udon.xsgi


def _logger(logger):
    return logger if logger is not None else logging.getLogger(__name__)


def run_bottle(app, **kwargs):
    bottle.run(app = app, quiet = True, **kwargs)


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
        if not mount_point.endswith('/'):
            mount_point += '/'
        app = stack.app_factory()
        self.setup(app)
        stack.app.mount(mount_point, self)
        self.app = app
        self.stack = stack

    def __call__(self, environ, handler):
        environ['udon.api'] = self
        return self.app(environ, handler)


class APIStack(object):

    def __init__(self, prefix = "/", **options):
        self.prefix = prefix
        self.options = options
        self.app = self.app_factory()

    def app_factory(self):
        return bottle.Bottle(**self.options)

    def _install(self, mount_point, setup):
        # TODO deprecate
        return self.install_func(mount_point, setup)

    def install_func(self, prefix, setup):
        api = API(prefix, setup)
        api.install(self)

    def install(self, module_name):
        # TODO deprecate
        return self.install_module(module_name)

    def install_module(self, module_name):
        importlib.import_module(module_name)
        for api in _apis.values():
            if not api.stack:
                api.install(self)

    def __call__(self, environ, handler):
        return self.app(environ, handler)


class WSGIErrorStream:

    def __init__(self, stream, autoflush = False, logger = None):
        self.stream = stream
        self.autoflush = autoflush
        self.logger = _logger(logger)

    def write(self, err):
        try:
            self.stream.write("WSGI ERROR ---- %s\n%s" % (time.ctime(), err))
            if self.autoflush:
                self.stream.flush()
        except:
            self.logger.exception("Failed to write WSGI error")

    def flush(self):
        try:
            self.stream.flush()
        except:
            self.logger.exception("Failed to flush WSGI error")


class WSGIErrorLogger:

    def __init__(self, logger = None):
        self.logger = _logger(logger)

    def write(self, err):
        try:
            self.logger.error("WSGI ERROR: %s", err)
        except:
            self.logger.exception("Failed to write WSGI error")

    def flush(self):
        pass


class EnvMiddleware:

    def __init__(self, app, environ = None):
        self.app = app
        self.environ = {} if environ is None else environ

    def setenv(self, key, value):
        self.environ[key] = value

    def __call__(self, environ, handler):
        environ.update(self.environ)
        return self.app(environ, handler)


class LogMiddleware:

    def __init__(self, app, logger = None):
        self.app = app
        self.logger = _logger(logger)

    def __call__(self, environ, handler):
        t0 = time.time()
        ret = self.app(environ, handler)
        try:
            self.log(environ, ret, time.time() - t0)
        except:
            self.logger.exception("Failed to log result")
        return ret

    def log(self, environ, ret, dt):
        msg = self.format_message(environ, ret, dt)
        self.logger.info(msg)

    def format_message(self, environ, ret, dt):
        request = bottle.request
        response = bottle.response
        _scheme, host, _path, _query_string, _fragment = request.urlparts
        try:
            length = int(response.content_length)
        except:
            # For chunked requests, no content-length is set
            length = -1
        return "%.3f %s %s %s %d %d %s %s" % (dt,
                                              environ["REMOTE_ADDR"],
                                              environ.get("HTTP_X_FORWARDED_FOR", "-"),
                                              request.method,
                                              response.status_code,
                                              length,
                                              host,
                                              request.path)


def abort(code, message):
    bottle.abort(code, message)


class Form(udon.xsgi.Form):

    def __init__(self, request = None):
        if request is None:
            request = bottle.request
        udon.xsgi.Form.__init__(request)


class Parameters(udon.xsgi.Parameters):

    def abort(status_code, detail):
        abort(status_code, detail)


def _request_json(request):
    try:
        return request.json
    except ValueError:
        abort(400, 'Invalid JSON content')

def params(request = None, data = None):
    if data is None:
        if request is None:
            request = bottle.request
        data = _request_json(request) or {}
    return Parameters(data)

def no_params(request = None):
    if request is None:
        request = bottle.request
    if _request_json(request):
        abort(400, 'No parameter expected')

def _make_etag(*parts):
    hash = hashlib.sha1()
    for part in parts:
        hash.update(str(part).encode('utf-8'))
    return hash.hexdigest()


def response_ok(status = 204):
    response = bottle.response.copy(cls = bottle.HTTPResponse)
    response.status = status
    return response

def response_json(value):
    response = bottle.response.copy(cls = bottle.HTTPResponse)
    response.set_header('Content-Type', 'application/json')
    response.body = json.dumps(value)
    return response


def response_view(view, request = None, response_headers = {}):
    if request is None:
        request = bottle.request

    (status_code, headers, body) = udon.xsgi.response_view(view, request, response_headers)

    response = bottle.response.copy(cls=bottle.HTTPResponse)
    response.status = status_code
    for key in headers: 
        response.set_header(key, headers[key])
    response.body = body

    return response


def response_file(path, ctype = None, etag = None):
    fp = open(path, "rb")
    stat = os.fstat(fp.fileno())
    if etag is None:
        etag = _make_etag(path, stat.st_size, stat.st_mtime)
    if ctype is None:
        ctype = guess_content_type(path)
    view = udon.xsgi.ResourceView(fp,
                                  stat.st_size,
                                  stat.st_mtime,
                                  ctype = ctype,
                                  etag = etag)
    return response_view(view)


def response_content(fp):
    headers = { key: val for key, val in fp.info.headers }
    view = udon.xsgi.ResourceView(fp,
                                  fp.info.size,
                                  fp.info.timestamp,
                                  ctype = headers.get("Content-Type"),
                                  etag = headers.get('ETag'))
    return response_view(view)


def response_request(req, response_headers = {}):
    response = bottle.response.copy(cls = bottle.HTTPResponse)
    response.status = "%d %s" % (req.status_code, req.reason)
    for key, value in req.headers.items():
        if key not in ('Connection', ):
            response.set_header(key, value)
    for key in response_headers:
        if response_headers[key] is None:
            response.headers.pop(bottle._hkey(key), None)
        else:
            response.set_header(key, response_headers[key])

    response.body = req.raw
    return response


DEFAULT_TYPE = {
    'mp3': 'audio/mpeg',
    'mp4': 'video/mp4',
    'aac': 'audio/mp4',
    'webm': 'video/webm',
    'oga': 'audio/ogg',
    'ogg': 'audio/ogg',
    'ogv': 'video/ogg',
    'flac': 'audio/flac',
    'wav': 'audio/wav',
    'm4a': 'audio/mp4',
    'css': 'text/css',
    'gif': 'image/gif',
    'html': 'text/html',
    'js': 'application/javascript',
    'json': 'application/json',
    'jpeg': 'image/jpeg',
    'jpg': 'image/jpeg',
    'otf': 'application/vnd.ms-opentype',
    'pdf': 'application/pdf',
    'png': 'image/png',
    'svg': 'image/svg+xml',
    'ttf': 'application/x-font-ttf',
    'txt': 'text/plain',
    'woff': 'application/font-woff',
    'woff2': 'application/font-woff2',
    'xhtml': 'application/xhtml+xml',
    'map': 'application/json',
    'avif': 'image/avif',
    'webp': 'image/webp',
}

def guess_content_type(filename, default = None):
    ext = filename.split('.')[-1].lower()
    return DEFAULT_TYPE.get(ext, default)
