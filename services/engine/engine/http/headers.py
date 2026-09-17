"""Header rules for the guarded HTTP client (2b design §5.4).

Two independent, pure functions -- pure in the sense that neither touches a socket or DNS, so both are
fully covered by tests/test_http_headers.py without a fake server:

- `sanitize_headers` runs once, on the tenant's original headers, before the first hop. It drops every
  header a tenant must never control -- the framing pair (Content-Length, Transfer-Encoding), the
  hop-by-hop set (Connection, Keep-Alive, Upgrade, TE, Trailer, Proxy-Authorization, Proxy-Authenticate),
  and the two this client pins itself (Host, Accept-Encoding) -- then validates every name and value
  that survives against RFC 7230's grammar. Nothing left in the result can inject a second header line,
  desync the request's framing from what httpx tells the server, or smuggle a value h11 would refuse in
  a way that looks like an unrelated transport failure.
- `redirect_headers` runs again on every redirect hop. It drops the headers that must not follow a
  request across an origin change -- Authorization, Cookie, and the common API-key convention
  X-API-Key -- unless the change is a same-host http-to-https upgrade, which is not a new party to
  trust less than the one the tenant already chose.

`client.py` calls `sanitize_headers` once and `redirect_headers` once per hop; see its module docstring
for the four httpx behaviours that make correct use of these two functions non-obvious from client.py
alone.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from engine.http.policy import DEFAULT_PORTS

# Headers that must not survive a redirect to a different origin: carrying a bearer token, API key or
# session cookie set for host A over to host B (possibly attacker-controlled once the allowlist permits
# it) would turn a single approved credential into a cross-tenant, cross-host leak. Mirrors httpx's own
# `_redirect_headers` (Authorization dropped on cross-origin, Cookie always re-evaluated), extended with
# the other common credential-header shape tenants actually use.
CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})

# Headers describing a body that must not survive the 301/302/303 -> GET conversion, once there is no
# body left for them to describe.
CONTENT_HEADERS = frozenset({"content-type", "content-encoding", "content-language", "content-md5"})

# Hop-by-hop and framing headers: never meaningful for a tenant to set, and dangerous if they are,
# because httpx computes Content-Length/Transfer-Encoding itself from `content=` -- a tenant-supplied
# value here can desync what httpx thinks the body boundary is from what it tells the server (RFC 7230
# §3.3.3), enabling request smuggling on a connection this client shares with other requests. Host is
# pinned by the client itself; Accept-Encoding is pinned to "identity" so the response-size cap runs
# against the bytes actually on the wire, not whatever a decompressor would expand them to.
UNSAFE_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "keep-alive", "expect", "upgrade", "te",
    "trailer", "proxy-authorization", "proxy-authenticate", "accept-encoding",
})

# RFC 7230 §3.2.6 token: a header *name* must be exactly this, or it is not a header, it is an attempt
# to smuggle something else into the request line.
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# RFC 7230 §3.2 field-value: VCHAR (0x21-0x7E) or SP/HTAB, any number of times, and nothing else.
# obs-text (0x80-0xFF) is deliberately excluded -- this client wants ASCII values only -- and so is
# every other C0 control: not just the CR/LF/NUL that would inject a second header line, but VT, FF,
# SOH and the rest of them, and DEL. h11 rejects some of these itself, as a LocalProtocolError that
# would otherwise reach a tenant as an unmapped transport failure instead of the clear
# EgressBlocked("header") this module exists to give them -- so this check has to be at least as strict
# as h11's, not merely "no CR, LF or NUL".
_VALID_VALUE = re.compile(r"^[\t\x20-\x7e]*$")


class HeaderRejected(Exception):
    """A header name or value was not well-formed, or an unsafe one was present. `client.py` catches
    this and raises `EgressBlocked("header")` -- this module has no opinion on node-facing categories,
    only on what is and is not an acceptable header."""


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop the headers in `UNSAFE_HEADERS` (case-insensitively) and validate everything that remains.

    A header name must be a non-empty RFC 7230 token; a value must contain only VCHAR, SP or HTAB (see
    `_VALID_VALUE`'s comment for exactly what that excludes and why). Raises `HeaderRejected` for
    anything else, including a non-`str` name or value -- a tenant who put a stray byte string or an
    integer in a headers mapping gets the same clear refusal as one who put Korean text in a header
    value on this Korean-facing product, rather than silent stripping or a crash three frames later.
    """
    clean: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise HeaderRejected("header name or value is not a string")
        if name.lower() in UNSAFE_HEADERS:
            continue
        if not _TOKEN.fullmatch(name) or not _VALID_VALUE.fullmatch(value):
            raise HeaderRejected(f"malformed header: {name!r}")
        clean[name] = value
    return clean


def redirect_headers(headers: dict[str, str], previous_url: str, next_url: str) -> dict[str, str]:
    """Strip credential-bearing headers that must not follow a request across an origin change.

    A same-origin redirect (the common case: a trailing slash, a path move) keeps every header --
    there is no new party being trusted. Crossing an origin drops Authorization, Cookie and X-API-Key,
    mirroring httpx's own `_redirect_headers`, with the same carve-out for a plain http-to-https upgrade
    of the *same* host: that is not a new origin to trust less than the one the tenant already chose.

    `.port` is a lazily-parsed property and can itself raise `ValueError` for a malformed port -- the
    caller in client.py wraps this call alongside the rest of its redirect-URL handling for that reason,
    so this function is free to let that propagate rather than catching it itself.
    """
    before, after = urlsplit(previous_url), urlsplit(next_url)
    before_port = before.port or DEFAULT_PORTS.get(before.scheme)
    after_port = after.port or DEFAULT_PORTS.get(after.scheme)
    if (before.scheme, before.hostname, before_port) == (after.scheme, after.hostname, after_port):
        return dict(headers)
    is_https_upgrade = (
        before.hostname == after.hostname and before.scheme == "http" and after.scheme == "https"
        and before_port == 80 and after_port == 443
    )
    drop = frozenset() if is_https_upgrade else CREDENTIAL_HEADERS
    return {name: value for name, value in headers.items() if name.lower() not in drop}
