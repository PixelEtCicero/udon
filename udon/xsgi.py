import base64
import datetime
import hashlib
import logging
import os
import time


def fmt_time(timestamp = None):
    if timestamp is None:
        timestamp = time.time()
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(timestamp))


class ResourceView:

    def __init__(self, body, size, mtime, ctype = None, etag = None):
        self.body = body
        self.ctype = ctype if ctype is not None else 'application/octet-stream'
        self.size = size
        self.mtime = mtime
        self.etag = etag


class FileResourceView(ResourceView):

    def __init__(self, path, ctype = None, etag = None):
        fp = open(path, "rb")
        stat = os.fstat(fp.fileno())
        if etag is None:
            etag = make_etag(path, stat.st_size, stat.st_mtime)
        if ctype is None:
            ctype = guess_content_type(path)
        ResourceView.__init__(self,
                              fp,
                              stat.st_size,
                              stat.st_mtime,
                              ctype = ctype,
                              etag = etag)


def response_view(view, request, response_headers = {}):
    status_code = 200
    headers = {}
    body = None

    def _parse_range():
        value = request.headers.get("range", "")
        if not value.startswith("bytes="):
            return None

        for rng in value.split("=", 1)[1].split(","):
            if '-' not in rng:
                continue
            offset, end = rng.split('-', 1)
            if (offset, end) == ('', ''):
                continue
            if not offset:
                offset, end = max(0, view.size - int(end) + 1), view.size
            elif not end:
                offset, end = int(offset), view.size
            else:
                offset, end = int(offset), int(end) + 1
            if 0 <= offset < end <= view.size:
                return offset, end

        return None

    def _modified():
        return True

    def _read(fp, count, bufsize = 1024 * 1024):
        while count:
            data = fp.read(min(count, bufsize))
            if not data:
                break
            yield data
            count -= len(data)

    def _iter_range(body, offset, count, logger):
        try:
            if (offset):
                if body.seekable():
                    body.seek(offset, 1)
                else:
                    for _ in _read(body, offset):
                        pass
            for chunk in _read(body, count):
                yield chunk
        except GeneratorExit:
            # interrupted transfer
            pass
        except:
            logging.exception("EXCEPTION")
            raise
        finally:
            body.close()

    range = _parse_range()

    headers["Accept-Ranges"] = "bytes"
    headers["Content-Type"] = view.ctype
    if isinstance(view.mtime, str):
        headers["Last-Modified"] = view.mtime
    else:
        headers["Last-Modified"] = fmt_time(view.mtime)
    if view.etag is not None:
        headers["ETag"] = view.etag

    if request.method == "HEAD":
        headers["Content-Length"] = str(view.size)
        view.body.close()
    elif not _modified():
        headers["Content-Length"] = "0"
        status_code = 304
        view.body.close()
    elif range:
        offset, end = range
        length = end - offset
        headers["Content-Length"] = str(length)
        headers["Content-Range"] = "bytes %d-%d/%d" % (offset, end - 1, view.size)
        # HTTP clients often try to detect if a server supports partial content.
        # A common way to do this without wasting resources is to request a range
        # that represents the whole file. In this case, the recommendation from HTTP
        # is to respond with a 200 status to be consistant with the way HTTP caches work.
        # And clients should base the detection on the Accept-ranges/Content-Range headers
        # rather than the status.
        # Another reason to response 200: if a browser (chrome) receives a 206 response, which Content-Range
        # matches the Content-Length, it will consider the response valid for a subsequent GET
        # request WITHOUT range header, and this is counter-intuitive from the client point-of-view
        # to receive 206 status on a simple GET request.
        status_code = 206 if view.size > length else 200
        body = _iter_range(view.body, offset, end - offset, None)
    else:
        headers["Content-Length"] = str(view.size)
        # For some reason in asgi passing directly the fd is extremely slow to send to client (x10)
        # body = view.body
        # The fastapi exemple is also very slow
        # def iterfile():
        #     yield from view.body
        # body = iterfile()
        body = _iter_range(view.body, 0, view.size, None)

    for key in response_headers:
        headers[key] = response_headers[key]

    return (status_code, headers, body)


class Form(object):

    def __init__(self, request):
        self.request = request

    def raw(self, name):
        return self.request.forms.get(name)

    def string(self, name):
        return self.raw(name)

    def integer(self, name):
        return int(self.raw(name))

    def float(self, name):
        return float(self.raw(name))

    def date(self, name, fmt = "%d/%m/%Y"):
        return datetime.datetime.strptime(self.raw(name), fmt)

    def file(self, name):
        if name not in self.request.files:
            return None, None
        value = self.request.files[name]
        filename = os.path.basename(value.filename)
        return value.file, filename


_mandatory = object()
_unset = object()


class Parameters(object):

    def __init__(self, params):
        if not isinstance(params, dict):
            self.abort(400, 'Expect parameter object')
        self.params = params

    def abort(self, status_code, detail):
        raise NotImplementedError()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        if (type, value, traceback) == (None, None, None):
            if self.params:
                self.abort(400, 'Unexpected parameter(s): %s' % ', '.join(self.params.keys()))

    def get(self, name, default, validate):
        value = self.params.pop(name, _unset)

        if value is _unset:
            if default is not _mandatory:
                return default
            self.abort(400, 'Missing parameter %s' % (name, ))

        if validate:
            try:
                validate(value)
            except TypeError as e:
                self.abort(400, 'Invalid parameter type: %s: %s' % (name, str(e)))
            except ValueError as e:
                self.abort(400, 'Invalid parameter value: %s: %s' % (name, str(e)))
        return value

    def get_list(self, name, default, validate, maxlen):
        def _(v):
            if not isinstance(v, list):
                raise TypeError('expect list')
            if maxlen is not None and len(v) > maxlen:
                raise ValueError('list too long')
            if validate:
                for e in v:
                    validate(e)
        return self.get(name, default, _)

    def any(self, name, default = _mandatory, validate = None):
        return self.get(name, default, validate)

    def string(self, name, default = _mandatory, choice = None, validate = None):
        def _(v):
            if not isinstance(v, str):
                raise TypeError('expect string')
            if choice is not None and v not in choice:
                raise ValueError('not in set of possible values')
            if validate:
                validate(v)
        return self.get(name, default, _)

    def binary(self, name, default = _mandatory, maxlen = None, validate = None):
        def _(v):
            if not isinstance(v, str):
                raise TypeError('expect base64 string')
            if maxlen is not None and len(v) > maxlen * 4:
                raise ValueError('too long')
        v = self.get(name, default, _)
        try:
            v = base64.b64decode(v)
        except:
            raise ValueError('not properly base64-encoded')
        if maxlen is not None and len(v) > maxlen:
            raise ValueError('too long')
        if validate:
            validate(v)
        return v

    def integer(self, name, default = _mandatory, min = None, max = None):
        def _(v):
            if not isinstance(v, int):
                raise TypeError('expect integer')
            if min is not None and v < min:
                raise ValueError('too small')
            if max is not None and v > max:
                raise ValueError('too large')
        return self.get(name, default, _)

    def float(self, name, default = _mandatory, min = None, max = None):
        def _(v):
            if not isinstance(v, float):
                raise TypeError('expect float')
            if min is not None and v < min:
                raise ValueError('too small')
            if max is not None and v > max:
                raise ValueError('too large')
        return self.get(name, default, _)

    def boolean(self, name, default = _mandatory):
        def _(v):
            if not isinstance(v, bool):
                raise TypeError('expect boolean')
        return self.get(name, default, _)

    def string_list(self, name, default = _mandatory, maxlen = None, choice = None, validate = None):
        def _(v):
            if not isinstance(v, str):
                raise TypeError('expect list of strings')
            if choice is not None and v not in choice:
                raise ValueError('not in set of possible values')
            if validate:
                validate(v)
        return self.get_list(name, default, _, maxlen)

    def integer_list(self, name, default = _mandatory, maxlen = None):
        def _(v):
            if not isinstance(v, int):
                raise TypeError('expect list of integers')
        return self.get_list(name, default, _, maxlen)

    def any_list(self, name, default = _mandatory, validate = None, maxlen = None):
        return self.get_list(name, default, validate, maxlen)

    def timestamp(self, name, default = _mandatory, min = 0, max = None):
        if max is None:
            max = int(time.time()) + 3600 * 24 * 365
        return self.integer(name, default, min = min, max = max)

    def email(self, name, default = _mandatory):
        v = self.string(name, default)
        if v is not None:
            # XXX validate email?
            return v.strip().lower()


def make_etag(*parts):
    hash = hashlib.sha1()
    for part in parts:
        hash.update(str(part).encode('utf-8'))
    return hash.hexdigest()


DEFAULT_TYPE = {
    'aac': 'audio/mp4',
    'avif': 'image/avif',
    'css': 'text/css',
    'flac': 'audio/flac',
    'gif': 'image/gif',
    'html': 'text/html',
    'jpeg': 'image/jpeg',
    'jpg': 'image/jpeg',
    'js': 'application/javascript',
    'json': 'application/json',
    'm4a': 'audio/mp4',
    'map': 'application/json',
    'mp3': 'audio/mpeg',
    'mp4': 'video/mp4',
    'oga': 'audio/ogg',
    'ogg': 'audio/ogg',
    'ogv': 'video/ogg',
    'otf': 'application/vnd.ms-opentype',
    'pdf': 'application/pdf',
    'png': 'image/png',
    'svg': 'image/svg+xml',
    'ttf': 'application/x-font-ttf',
    'txt': 'text/plain',
    'wav': 'audio/wav',
    'webm': 'video/webm',
    'webp': 'image/webp',
    'woff': 'application/font-woff',
    'woff2': 'application/font-woff2',
    'xhtml': 'application/xhtml+xml',
}


def guess_content_type(filename, default = None):
    ext = filename.split('.')[-1].lower()
    return DEFAULT_TYPE.get(ext, default)
