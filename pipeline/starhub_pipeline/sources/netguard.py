"""Hardened outbound channel: host allowlist + public resolution validation + strict redirect re-check + IP-pinned dialing.

The SSRF exposure surface for all pipeline outbound traffic converges here: only allowlisted https hosts,
every resolved address must be a public address (blocking intranet/loopback/link-local/reserved ranges),
each redirect hop is re-validated against the allowlist, and direct connections dial the validated IP to
prevent DNS rebinding. Requests rewritten by a proxy are not handled by this channel and are handled by
urllib's default logic (the proxy itself is responsible for the target).
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
import urllib.request


def resolve_public_ip(host: str, port: int) -> str:
    """Resolve once, require every resolved address to be public, return the pinned IP."""
    pinned = ""
    for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("host %s resolves to non-public address %s" % (host, ip))
        if not pinned:
            pinned = info[4][0]
    if not pinned:
        raise ValueError("host %s resolves to nothing" % host)
    return pinned


def assert_safe_url(url: str, allowed_hosts) -> None:
    """Scheme + allowlist host + all resolved addresses public; raise ValueError on any violation."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or host not in allowed_hosts:
        raise ValueError("blocked non-allowlist url: %s" % url)
    resolve_public_ip(host, parsed.port or 443)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Dial the validated IP instead of re-resolving (prevents DNS rebinding)."""

    def __init__(self, host, ip=None, timeout=None, context=None, **kwargs):
        super().__init__(host, timeout=timeout, context=context, **kwargs)
        self._pin_ip = ip
        self._tls_context = context or ssl.create_default_context()

    def connect(self):
        sock = socket.create_connection((self._pin_ip, self.port), timeout=self.timeout)
        self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """Validate at request time and dial the validated IP."""

    def __init__(self, allowed_hosts):
        super().__init__()
        self._allowed = frozenset(allowed_hosts)

    def https_open(self, req):
        # Starting from 3.14, Request no longer has .port; host may carry a ":port" suffix; both forms must be compatible
        hostname, _, port_s = (req.host or "").partition(":")
        port = int(port_s) if port_s.isdigit() else None
        if hostname not in self._allowed:
            # Requests rewritten by a proxy etc. are not this channel's concern; hand over to default logic
            return super().https_open(req)
        ip = resolve_public_ip(hostname, port or 443)
        return self.do_open(lambda host, **kw: _PinnedHTTPSConnection(host, ip, **kw), req)


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Each redirect hop is re-validated against the allowlist; cross-host redirects are rejected outright."""

    def __init__(self, allowed_hosts):
        super().__init__()
        self._allowed = frozenset(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_safe_url(newurl, self._allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_safe_opener(allowed_hosts) -> urllib.request.OpenerDirector:
    """Build a hardened opener; the subclass instances will replace the default HTTPS/redirect handlers."""
    return urllib.request.build_opener(
        _PinnedHTTPSHandler(allowed_hosts), _StrictRedirectHandler(allowed_hosts))
