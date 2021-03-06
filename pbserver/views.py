# coding: utf-8
#
# Copyright 2012 Alexandre Fiori
# based on the original Tornado by Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.


import cyclone.escape
import cyclone.locale
import cyclone.web

import socket
import struct

from twisted.internet import defer
from twisted.python import log

from pbserver import base62
from pbserver.utils import BaseHandler
from pbserver.utils import DatabaseMixin


# convert bytes to human readable strings
human_bytes = lambda s: [(s % 1024 ** i and "%.1f" % (s / 1024.0 ** i) or
                          str(s / 1024 ** i)) + x.strip() + "B"
                          for i, x in enumerate(' KMGTPEZY')
                          if s < 1024 ** (i + 1) or i == 8][0]


# convert seconds to human readable strings
def human_time(_, seconds):
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    z = []
    if h:
        z.append(_("%(n)d hour", "%(n)d hours", h) % {"n": h})
    if m:
        z.append(_("%(n)d minute", "%(n)d minutes", m) % {"n": m})
    if s:
        z.append(_("%(n)d seconds", "%(n)d seconds", s) % {"n": s})

    if len(z) == 3:
        return "%s, %s and %s" % (z[0], z[1], z[2])
    elif len(z) == 2:
        return " and ".join(z)
    else:
        return z[0]


class BashHandler(BaseHandler):
    def get(self):
        self.set_header("Content-Type", "text/plain")
        self.render("bash_profile.txt", url="%s://%s" %
                    (self.request.protocol, self.request.host))


class IndexHandler(BaseHandler, DatabaseMixin):
    @defer.inlineCallbacks
    def get(self, n):
        if n:
            self.set_header("Content-Type", "text/plain")

            # throttle
            k = "g:%d" % \
                struct.unpack('!I',
                              socket.inet_aton(self.request.remote_ip))[0]
            try:
                r = yield self.redis.get(k)
                assert r < self.settings.limits.throttle_get
                yield self.redis.incr(k)
                if not r:
                    yield self.redis.expire(k,
                                        self.settings.limits.throttle_interval)
            except AssertionError:
                raise cyclone.web.HTTPError(403)  # Forbidden
            except Exception, e:
                log.err("redis failed on get (throttle): %s" % e)
                raise cyclone.web.HTTPError(503)  # Service Unavailable

            try:
                k = "n:%s" % base62.base62_decode(n)
            except:
                raise cyclone.web.HTTPError(404)

            try:
                buf = yield self.redis.get(k)
            except Exception, e:
                log.err("redis failed on get: %s" % e)
                raise cyclone.web.HTTPError(503)  # Service Unavailable
            else:
                if buf:
                    self.finish(buf)
                else:
                    raise cyclone.web.HTTPError(404)
        else:
            if "text/html" in self.request.headers.get("Accept"):
                self.render("index.html",
                    url="%s://%s" % (self.request.protocol, self.request.host),
                    limit="Maximum of %d xpbpaste and %d xpbcopy every %s, "
                          "of %s each, expiring in %s." % (
                            self.settings.limits.throttle_get,
                            self.settings.limits.throttle_post,
                            human_time(self.locale.translate,
                                       self.settings.limits.throttle_interval),
                            human_bytes(self.settings.limits.pbsize),
                            human_time(self.locale.translate,
                                       self.settings.limits.pbexpire)))
            else:
                self.set_header("Content-Type", "text/plain")
                self.finish("Use: xpbpaste <pbid>\r\n")

    @defer.inlineCallbacks
    def post(self, *ign):
        self.set_header("Content-Type", "text/plain")

        blen = len(self.request.body)
        if blen > self.settings.limits.pbsize:
            raise cyclone.web.HTTPError(400,
                                        "text too large (%d bytes)" % blen)

        # throttle
        ip = struct.unpack('!I', socket.inet_aton(self.request.remote_ip))[0]
        k = "p:%d" % ip
        try:
            r = yield self.redis.get(k)
            assert r < self.settings.limits.throttle_post
            yield self.redis.incr(k)
            if not r:
                yield self.redis.expire(k,
                                        self.settings.limits.throttle_interval)
        except AssertionError:
            raise cyclone.web.HTTPError(403)  # Forbidden
        except Exception, e:
            log.err("redis failed on post (throttle): %s" % e)
            raise cyclone.web.HTTPError(503)  # Service Unavailable

        try:
            n = yield self.redis.incr("n")
            if n == 1:
                yield self.redis.expire("n",
                                self.settings.limits.pbexpire * 10)

            n += (ip + blen)
            k = "n:%d" % n
            yield self.redis.set(k, self.request.body)
            yield self.redis.expire(k, self.settings.limits.pbexpire)
        except Exception, e:
            log.err("redis failed on post: %s" % e)
            raise cyclone.web.HTTPError(503)  # Service Unavailable
        else:
            self.finish("xpbpaste %s\r\n" % base62.base62_encode(n))
            #self.finish("%s://%s/%s\r\n" % (self.request.protocol,
            #                                self.request.host,
            #                                base62.base62_encode(n)))

    def write_error(self, status_code, **kwargs):

        if status_code == 400:
            self.finish("Bad request: %s\r\n" %
                        kwargs["exception"].log_message)

        elif status_code == 403:
            self.finish("Forbidden: reached maximum service quotas. "
                        "Try again later.\r\n")

        elif status_code == 404:
            self.finish()

        elif status_code == 503:
            self.finish("Service temporarily unavailable. "
                        "Try again later.\r\n")

        else:
            BaseHandler.write_error(self, status_code, **kwargs)
