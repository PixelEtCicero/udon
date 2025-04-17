#
# Copyright (c) 2019 Eric Faurot <eric@faurot.net>
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
import json
import threading
import time

try:
    import aiohttp
except ImportError:
    pass
else:
    AsyncHTTPError = aiohttp.client_exceptions.ClientResponseError

try:
    import requests
except ImportError:
    pass
else:
    HTTPError = requests.exceptions.HTTPError

import udon.util


def parse_jwt(value):
    """
    For some reason when using "base64.b64decode" this method fails with the following jwt issued by keycloak:
    eyJhbGciOiJSUzI1NiIsInR5cCIgOiAiSldUIiwia2lkIiA6ICJ3WnJwakktcnI3WXBYcjViU2F4WWt2bWQwaFZJaktQcHNQeXRNeUJDTUJrIn0.eyJleHAiOjE2ODAwODcxMTIsImlhdCI6MTY4MDA4NjgxMiwianRpIjoiNzM4ZjI3YTQtNDY5OS00NzA0LThhYTctMTVmYTMzOWMwN2FlIiwiaXNzIjoiaHR0cHM6Ly9vbW5pYm9vay5jb20vYXV0aC9yZWFsbXMvb21uaWJvb2siLCJhdWQiOiJhY2NvdW50Iiwic3ViIjoiMWFkNGMwNGMtMTEwYi00ZTEyLThkZjEtYjljY2YzYzA4OTIxIiwidHlwIjoiQmVhcmVyIiwiYXpwIjoib21uaWJvb2stYXBpIiwic2Vzc2lvbl9zdGF0ZSI6ImJjNjliYTljLWQzMTEtNDRlMi1hZjIxLWZjNGI5NWVhOGJlMSIsImFjciI6IjEiLCJhbGxvd2VkLW9yaWdpbnMiOlsiaHR0cHM6Ly9vbW5pYm9vay5jb20iXSwicmVhbG1fYWNjZXNzIjp7InJvbGVzIjpbIm9mZmxpbmVfYWNjZXNzIiwidW1hX2F1dGhvcml6YXRpb24iXX0sInJlc291cmNlX2FjY2VzcyI6eyJhY2NvdW50Ijp7InJvbGVzIjpbIm1hbmFnZS1hY2NvdW50IiwibWFuYWdlLWFjY291bnQtbGlua3MiLCJ2aWV3LXByb2ZpbGUiXX19LCJzY29wZSI6InByb2ZpbGUgb2ZmbGluZV9hY2Nlc3MgZW1haWwiLCJlbWFpbF92ZXJpZmllZCI6dHJ1ZSwibmFtZSI6IkF5xZ9lIEtPw4ciLCJwcmVmZXJyZWRfdXNlcm5hbWUiOiJheXNlLmtvY0BsY2RnYW5rYXJhLm9yZyIsImdpdmVuX25hbWUiOiJBecWfZSIsImZhbWlseV9uYW1lIjoiS0_DhyIsImVtYWlsIjoiYXlzZS5rb2NAbGNkZ2Fua2FyYS5vcmcifQ.xkzHo-Hdm81nQuADEA7VNojNXQI3aoV1VmQ7Z7xTS-3nLsqECMltuu8_OeH8U3E4EO3aU-JX_CmuZ5aUdOlDz93xjRe_umWnq9QLr22YIrVeA6JOhrlfC8O_GsHZk2jmK8PJK2wKNIWwBQgyPn8RtvJ79vyAUD9g4ahiseO4o4fxXqwfzVEflVwOQl-zjZqYCePbue8vE1gtD8FpKLBGRCgkzvmkr8kV365RZwQGkPsB4z3GYNL119Mk6Kj-45Bv9AwY8uaB_8GhYjXAd3O520kRBiLFvAtlAIsjh0vMQc6CsuwnsWIC0uIgrVbXjPYCrVcnSWKvJ9sjAr3hE9Eiww
    """
    def b64decode(buf):
        buf += "=" * ((4 - len(buf) % 4) % 4)
        # return base64.b64decode(buf)
        return base64.urlsafe_b64decode(buf)
    parts= value.split(".")
    return { 'header': json.loads(b64decode(parts[0])),
             'content': json.loads(b64decode(parts[1])),
             'signature': parts[2],
             'signed': '.'.join(parts[:1]) }


class AuthError(Exception):
    def __init__(self, message):
        super().__init__()
        self.message = message

class AuthRequired(AuthError):
    pass

class InvalidCredentials(AuthError):
    pass

class ExpiredCredentials(AuthError):
    pass


class ExpireCache:
    def __init__(self):
        self.cache = {}

    def purge(self, timestamp = None):
        if timestamp is None:
            timestamp = time.time()
        for key in [ key for (key, (timeout, _)) in self.cache.items() if timeout <= timestamp ]:
            del self.cache[key]

    def __len__(self):
        return len(self.cache)

    def __getitem__(self, key):
        return self.get(key)

    def get(self, key, timestamp = None):
        if timestamp is None:
            timestamp = time.time()
        timeout, value = self.cache.get(key, (None, None))
        if timeout is None:
            raise KeyError(key)
        if timeout <= timestamp:
            del self.cache[key]
            self.purge(timestamp)
            raise ExpiredCredentials(key)
        return value

    def set(self, key, value, timeout):
        self.cache[key] = timeout, value

    def unset(self, key):
        del self.cache[key]


class Portal:

    def __init__(self):
        self.cache = ExpireCache()

    def user_noauth(self, request):
        raise AuthRequired("Authorization required")

    def user_public(self, request):
        raise AuthRequired("Authorization required")

    def user_bearer(self, request, access_token, jwt):
        raise NotImplementedError

    def user_apikey(self, request, access_token):
        raise NotImplementedError

    def user(self, request):
        auth = request.headers.get("Authorization")
        if not auth:
            return self.user_noauth(request)

        parts = auth.split(' ', 1)
        scheme = parts[0]

        if scheme == 'Public':
            return self.user_public(request)

        if len(parts) < 2:
            raise InvalidCredentials("Invalid authorization header")

        access_token = parts[1].strip()

        if scheme == 'Bearer':
            try:
                return self.cache.get(access_token)
            except KeyError:
                try:
                    jwt = parse_jwt(access_token)
                    timeout = jwt['content']['exp']
                except:
                    raise InvalidCredentials("Invalid authorization token")

            if timeout <= time.time():
                raise ExpiredCredentials(access_token)

            user = self.user_bearer(request, access_token, jwt)
            self.cache.set(access_token, user, timeout)
            return user

        if scheme == 'ApiKey':
            return self.user_apikey(request, access_token)

        raise InvalidCredentials("Invalid authorization scheme")


class AsyncPortal:

    def __init__(self):
        self.cache = ExpireCache()

    async def user_noauth(self, request):
        raise AuthRequired("Authorization required")

    async def user_public(self, request):
        raise AuthRequired("Authorization required")

    async def user_bearer(self, request, access_token, jwt):
        raise NotImplementedError

    async def user_apikey(self, request, access_token):
        raise NotImplementedError

    async def user(self, request):
        auth = request.headers.get("Authorization")
        if not auth:
            return await self.user_noauth(request)

        parts = auth.split(' ', 1)
        scheme = parts[0]

        if scheme == 'Public':
            return await self.user_public(request)

        if len(parts) < 2:
            raise InvalidCredentials("Invalid authorization header")

        access_token = parts[1].strip()

        if scheme == 'Bearer':
            try:
                return self.cache.get(access_token)
            except KeyError:
                try:
                    jwt = parse_jwt(access_token)
                    timeout = jwt['content']['exp']
                except:
                    raise InvalidCredentials("Invalid authorization token")

            if timeout <= time.time():
                raise ExpiredCredentials(access_token)

            user = await self.user_bearer(request, access_token, jwt)
            self.cache.set(access_token, user, timeout)
            return user

        if scheme == 'ApiKey':
            return await self.user_apikey(request, access_token)

        raise InvalidCredentials("Invalid authorization scheme")


class OpenIDClient:

    def __init__(self, host, realm, client_id, client_secret, verify = True, scope = 'openid'):
        self.verify = verify
        self.realm = realm
        self.host = host
        self.scope = scope
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope

    def _url(self, action):
        return "%s/auth/realms/%s/protocol/openid-connect/%s" % (self.host, self.realm, action)

    def _request(self, method, action, data = None, headers = None):
        resp = requests.request(method, self._url(action), verify = self.verify, data = data, headers = headers)
        resp.raise_for_status()
        return resp

    def login(self, username, password):
        return self._request('POST', 'token', data = { 'client_id': self.client_id,
                                                       'client_secret': self.client_secret,
                                                       'scope': self.scope,
                                                       'grant_type': 'password',
                                                       'username': username,
                                                       'password': password }).json()

    def refresh(self, refresh_token):
        return self._request('POST', 'token', data = { 'client_id': self.client_id,
                                                       'client_secret': self.client_secret,
                                                       'grant_type': 'refresh_token',
                                                       'refresh_token': refresh_token }).json()

    def logout(self, refresh_token):
        return self._request('POST', 'logout', data = { 'client_id': self.client_id,
                                                        'client_secret': self.client_secret,
                                                        'refresh_token': refresh_token }).status_code == 204

    def userinfo(self, access_token):
        return self._request('GET', 'userinfo', headers = { 'Authorization': 'Bearer ' + access_token }).json()


class AsyncOpenIDClient:

    def __init__(self, host, realm, client_id, client_secret, verify = True, scope = 'openid', session = None):
        self.verify = verify
        self.realm = realm
        self.host = host
        self.scope = scope
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.session = session or aiohttp.ClientSession(raise_for_status=True)

    async def release(self):
        await self.session.close()

    def _url(self, action):
        return "%s/auth/realms/%s/protocol/openid-connect/%s" % (self.host, self.realm, action)

    async def _request(self, method, action, data = None, headers = None):
        return await self.session.request(method=method, url=self._url(action), data=data, headers=headers)

    async def login(self, username, password):
        response = await self._request('POST', 'token', data = { 'client_id': self.client_id,
                                                                 'client_secret': self.client_secret,
                                                                 'scope': self.scope,
                                                                 'grant_type': 'password',
                                                                 'username': username,
                                                                 'password': password })
        return await response.json()

    async def refresh(self, refresh_token):
        response = await self._request('POST', 'token', data = { 'client_id': self.client_id,
                                                                 'client_secret': self.client_secret,
                                                                 'grant_type': 'refresh_token',
                                                                 'refresh_token': refresh_token })
        return await response.json()

    async def logout(self, refresh_token):
        response = await self._request('POST', 'logout', data = { 'client_id': self.client_id,
                                                              'client_secret': self.client_secret,
                                                              'refresh_token': refresh_token })
        return response.status == 204

    async def userinfo(self, access_token):
        response = await self._request('GET', 'userinfo', headers = { 'Authorization': 'Bearer ' + access_token })
        return await response.json()


class Session:

    grant = None
    refresh_timeout = None
    access_timeout = None

    def __init__(self, client, username, password,
                 lock = None, access_dt = 0, refresh_dt = 0):
        self.client = client
        self.username = username
        self.password = password
        self.access_dt = access_dt
        self.refresh_dt = refresh_dt
        if lock is None:
            lock = udon.util.nullcontext()
        elif lock is True:
            lock = threading.Lock()
        self.lock = lock

    def token(self):
        # Make sure two threads do not try to login/refresh a token at the same time.
        with self.lock:
            if not self.grant:
                self._login()
            elif self.access_timeout < time.time():
                if self.refresh_timeout > time.time():
                    self._refresh()
                else:
                    self._clear()
                    self._login()
            return self.grant['access_token']

    def logout(self):
        if not self.grant:
            return
        grant, timeout = self.grant, self.refresh_timeout
        self._clear()
        if grant and time.time() < timeout:
            # XXX fail-safe
            self.client.logout(grant['refresh_token'])

    def _clear(self):
        del self.grant
        del self.access_timeout
        del self.refresh_timeout

    def _login(self):
        grant = self.client.login(self.username, self.password)
        self._granted(grant)

    def _refresh(self):
        grant = self.client.refresh(self.grant['refresh_token'])
        self._granted(grant)

    def _granted(self, grant):
        access_timeout = parse_jwt(grant['access_token'])['content']['exp']
        refresh_timeout = parse_jwt(grant['refresh_token'])['content'].get('exp')
        self.grant = grant
        self.access_timeout = access_timeout + self.access_dt
        self.refresh_timeout = refresh_timeout + self.refresh_dt if refresh_timeout else 0


class AsyncSession:

    grant = None
    refresh_timeout = None
    access_timeout = None

    def __init__(self, client, username, password,
                 lock = None, access_dt = 0, refresh_dt = 0):
        self.client = client
        self.username = username
        self.password = password
        self.access_dt = access_dt
        self.refresh_dt = refresh_dt
        if lock is None:
            lock = udon.util.nullcontext()
        elif lock is True:
            lock = threading.Lock()
        self.lock = lock

    async def release(self):
        await self.client.release()

    async def token(self):
        # Make sure two threads do not try to login/refresh a token at the same time.
        with self.lock:
            if not self.grant:
                await self._login()
            elif self.access_timeout < time.time():
                if self.refresh_timeout > time.time():
                    await self._refresh()
                else:
                    self._clear()
                    await self._login()
            return self.grant['access_token']

    async def logout(self):
        if not self.grant:
            return
        grant, timeout = self.grant, self.refresh_timeout
        self._clear()
        if grant and time.time() < timeout:
            # XXX fail-safe
            await self.client.logout(grant['refresh_token'])

    def _clear(self):
        del self.grant
        del self.access_timeout
        del self.refresh_timeout

    async def _login(self):
        grant = await self.client.login(self.username, self.password)
        self._granted(grant)

    async def _refresh(self):
        grant = await self.client.refresh(self.grant['refresh_token'])
        self._granted(grant)

    def _granted(self, grant):
        access_timeout = parse_jwt(grant['access_token'])['content']['exp']
        refresh_timeout = parse_jwt(grant['refresh_token'])['content'].get('exp')
        self.grant = grant
        self.access_timeout = access_timeout + self.access_dt
        self.refresh_timeout = refresh_timeout + self.refresh_dt if refresh_timeout else 0
