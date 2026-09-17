"""The guarded HTTP client (2b design §5).

Order is the whole point: allowlist, then DNS, then every resolved address, then a connection pinned to an
address that passed — repeated in full for every redirect hop. Checking the allowlist before DNS means an
unapproved host name is never even looked up, so the name cannot be used as a DNS exfiltration channel.

Raises its own exceptions; `engine/nodes/http_request.py` turns them into node errors. Nothing here knows
about nodes, so the policy can be tested without one. The client's public contract is exactly four
exception types (`EgressBlocked`, `ResponseTooLarge`, `UnsupportedMedia`, `TransportFailed`) -- `request()`
guarantees it, see its docstring.
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from engine.http.policy import DEFAULT_PORTS, AllowEntry, find_entry, ip_category
from engine.jsondata import parse_json

log = logging.getLogger(__name__)

# allowPrivate exempts only the categories that mean "a machine on the operator's own network".
# link-local is deliberately absent: 169.254.169.254 is the cloud metadata endpoint, and an operator who
# opened one internal API did not ask for that (2b design §5.2).
ALLOW_PRIVATE_CATEGORIES = frozenset({"private", "loopback", "cgnat"})
REDIRECTS = {301, 302, 303, 307, 308}
BODYLESS = {301, 302, 303}  # these become a GET without a body, as every browser and client does
# Headers that must not survive a redirect to a different origin: carrying a bearer token, API key or
# session cookie set for host A over to host B (possibly attacker-controlled once the allowlist permits
# it) would turn a single approved credential into a cross-tenant, cross-host leak. Mirrors httpx's own
# `_redirect_headers` (Authorization dropped on cross-origin, Cookie always re-evaluated), extended with
# the other common credential-header shape tenants actually use.
_CREDENTIAL_HEADERS = {"authorization", "cookie", "x-api-key"}
# Headers describing a body that must not survive the 301/302/303 -> GET conversion, once there is no
# body left for them to describe.
_CONTENT_HEADERS = {"content-type", "content-encoding", "content-language", "content-md5"}
# Hop-by-hop and framing headers: never meaningful for a tenant to set, and dangerous if they are, because
# httpx computes Content-Length/Transfer-Encoding itself from `content=` -- a tenant-supplied value here
# can desync what httpx thinks the body boundary is from what it tells the server (RFC 7230 §3.3.3),
# enabling request smuggling on a connection this client shares with other requests. Host is pinned by
# `_send` itself; Accept-Encoding is pinned to "identity" (see I5 in the client's history) so the response
# size cap runs against the bytes actually on the wire, not whatever a decompressor would expand them to.
_UNSAFE_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "keep-alive", "expect", "upgrade", "te",
    "trailer", "proxy-authorization", "proxy-authenticate", "accept-encoding",
})
# RFC 7230 §3.2.6 token: a header *name* must be exactly this, or it is not a header, it is an attempt to
# smuggle something else into the request line.
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class EgressBlocked(Exception):
    """Policy refused the request. `category` is all a tenant is ever told (2b design §5.6)."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class ResponseTooLarge(Exception):
    """The response passed the byte cap and was abandoned unread."""


class UnsupportedMedia(Exception):
    """Neither JSON nor text: the DSL type system has nowhere to put it."""


class TransportFailed(Exception):
    """Connection, TLS, framing or read failure. Retryable, unlike everything else here. Also the
    catch-all `request()` converts any *other* exception into, so that a library detail (an h11 protocol
    error, an httpx.InvalidURL that is not an `HTTPError`) never reaches the node unmapped."""


class Resolver(Protocol):
    async def resolve(self, host: str) -> list[str]: ...


class SystemResolver:
    """getaddrinfo on the event loop's executor, with a cap so a burst of runs cannot take every thread
    (the same executor runs `analyze` for the API)."""

    def __init__(self, limit: int = 8) -> None:
        self._semaphore = asyncio.Semaphore(limit)

    async def resolve(self, host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        async with self._semaphore:
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(info[4][0] for info in infos))


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]  # lowercased names
    body: Any


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    path: str
    query: str  # kept apart from `path` until the request URL is built, so a log site can use path alone
    address: str  # the validated address to connect to


class GuardedClient:
    def __init__(self, allowlist: tuple[AllowEntry, ...], *, resolver: Resolver | None = None,
                 max_redirects: int = 3, max_request_bytes: int = 1_000_000,
                 max_response_bytes: int = 5_000_000, verify: ssl.SSLContext | bool = True) -> None:
        self._allowlist = allowlist
        self._resolver = resolver or SystemResolver()
        self._max_redirects = max_redirects
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        # follow_redirects=False: every hop is re-checked here. trust_env=False so an ambient HTTP_PROXY
        # cannot silently route pinned traffic through a proxy instead of the address we validated.
        # max_keepalive_connections=0: httpcore's pool key is (scheme, address, port) -- the SNI hostname
        # we pin is not part of it, so a kept-alive connection to one allowlisted host would be reused,
        # unverified, for a *different* hostname that resolves to the same address. A fresh handshake per
        # request is the right trade for a client that makes occasional API calls, not a high-rate
        # stream; correctness of certificate verification is the entire point of pinning.
        self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False, verify=verify,
                                         cookies=None, limits=httpx.Limits(max_keepalive_connections=0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, *, method: str, url: str, headers: dict[str, str], body: str | None,
                      timeout_sec: float) -> HttpResponse:
        """Run one logical request, following redirects internally. Raises only `EgressBlocked`,
        `ResponseTooLarge`, `UnsupportedMedia` or `TransportFailed` -- anything else that escapes the
        implementation below is a bug in this module, not something the node's error mapper (Task 9)
        should ever have to special-case, so it is caught here and reported as a transport failure
        instead of leaking an unmapped exception type up to tenant-facing code.
        """
        try:
            return await self._request(method=method, url=url, headers=headers, body=body,
                                       timeout_sec=timeout_sec)
        except (EgressBlocked, ResponseTooLarge, UnsupportedMedia, TransportFailed):
            raise
        except Exception as exc:  # noqa: BLE001 -- deliberate catch-all, see the docstring above
            log.error("http_request: unexpected %s escaped the guarded client", type(exc).__name__)
            raise TransportFailed(type(exc).__name__) from None

    async def _request(self, *, method: str, url: str, headers: dict[str, str], body: str | None,
                       timeout_sec: float) -> HttpResponse:
        if body is not None and len(body.encode("utf-8")) > self._max_request_bytes:
            raise EgressBlocked("request-too-large")
        current_headers = _sanitize_headers(headers)
        current_method, current_body, current_url = method.upper(), body, url
        for _ in range(self._max_redirects + 1):
            target = await self._check(current_url)
            response = await self._send(target, current_method, current_headers, current_body, timeout_sec)
            # Cookies are never stored (2b design §5.5): httpx's jar is still live underneath `cookies=
            # None` (it only seeds the *initial* jar empty) and just captured any Set-Cookie from this
            # hop. Discard it immediately so it cannot attach itself to the next hop of this redirect, or
            # to a later, unrelated request on this shared client. `_send` also refuses to trust the jar
            # for the *outgoing* Cookie header, so this is belt-and-suspenders against a race between two
            # concurrent requests on the same client, not the only thing standing between them.
            self._client.cookies.clear()
            if response.status_code not in REDIRECTS or "location" not in response.headers:
                return await self._read(response)
            await response.aclose()
            # Every URL touched below -- location, and inside _redirect_headers, both endpoints' .port
            # (a lazily-parsed property that raises ValueError just like .port did in _check) -- is
            # wrapped in the same try, so a malformed Location header fails as EgressBlocked("url")
            # instead of an unhandled ValueError.
            try:
                location = urljoin(current_url, response.headers["location"])
                is_downgrade = urlsplit(current_url).scheme == "https" and urlsplit(location).scheme == "http"
                next_headers = _redirect_headers(current_headers, current_url, location)
            except ValueError:
                raise EgressBlocked("url") from None
            if is_downgrade:
                raise EgressBlocked("downgrade")
            if response.status_code in BODYLESS and current_method not in ("GET", "HEAD"):
                current_method, current_body = "GET", None
                next_headers = {name: value for name, value in next_headers.items()
                                if name.lower() not in _CONTENT_HEADERS}
            current_headers = next_headers
            current_url = location
        raise EgressBlocked("too-many-redirects")

    async def _check(self, url: str) -> _Target:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            port = parts.port  # a lazily-parsed property: an out-of-range or non-numeric port raises here
        except ValueError:
            raise EgressBlocked("url") from None
        if parts.scheme not in DEFAULT_PORTS:
            raise EgressBlocked("scheme")
        if not host:
            raise EgressBlocked("allowlist")
        try:
            # find_entry requires an already-ASCII host (see its docstring); a Unicode host name has to
            # be IDNA-encoded here, the client's job, before it can ever match an allowlist entry.
            host = host if host.isascii() else host.encode("idna").decode("ascii")
        except UnicodeError:
            raise EgressBlocked("allowlist") from None
        port = port or DEFAULT_PORTS[parts.scheme]
        entry = find_entry(self._allowlist, parts.scheme, host, port)
        if entry is None:  # before DNS, deliberately
            raise EgressBlocked("allowlist")
        addresses = [host] if ip_category(host) != "invalid" else await self._resolve(host)
        for address in addresses:
            category = ip_category(address)
            if category is not None and not (entry.allow_private and category in ALLOW_PRIVATE_CATEGORIES):
                raise EgressBlocked(category)
        return _Target(parts.scheme, host, port, parts.path or "/", parts.query, addresses[0])

    async def _resolve(self, host: str) -> list[str]:
        try:
            addresses = await self._resolver.resolve(host)
        except OSError:
            raise EgressBlocked("dns") from None
        if not addresses:
            raise EgressBlocked("dns")
        return addresses

    async def _send(self, target: _Target, method: str, headers: dict[str, str], body: str | None,
                    timeout_sec: float) -> httpx.Response:
        literal = f"[{target.address}]" if ":" in target.address else target.address
        host_literal = f"[{target.host}]" if ":" in target.host else target.host
        authority = host_literal if target.port == DEFAULT_PORTS[target.scheme] else \
            f"{host_literal}:{target.port}"
        suffix = f"?{target.query}" if target.query else ""
        pinned = f"{target.scheme}://{literal}:{target.port}{target.path}{suffix}"
        request = self._client.build_request(
            method, pinned,
            # `headers` was sanitized once in `_request`: no Host, no framing/hop-by-hop headers, no
            # unsafe names or values. identity encoding is pinned here (not merely stripped from the
            # tenant's own headers above) so the response-size cap in `_read` runs against what is
            # actually on the wire.
            headers={**headers, "Host": authority, "Accept-Encoding": "identity"},
            content=body.encode("utf-8") if body is not None else None,
            timeout=httpx.Timeout(timeout_sec, connect=timeout_sec),
            # httpcore hands this to the TLS layer as `server_hostname`, so SNI *and* certificate
            # verification use the real name even though the socket goes to the validated address.
            extensions={"sni_hostname": target.host},
        )
        # httpx.Request.__init__ merges the client's cookie jar into a Cookie header regardless of the
        # `cookies=None` passed at construction (that only seeds the jar empty, see `request()`'s
        # cookie-clearing comment) -- force the wire to carry exactly the Cookie value, if any, that
        # survived `_sanitize_headers`/`_redirect_headers` above, never one httpx assembled on its own.
        request.headers.pop("Cookie", None)
        intended_cookie = next((value for name, value in headers.items() if name.lower() == "cookie"), None)
        if intended_cookie is not None:
            request.headers["Cookie"] = intended_cookie
        try:
            return await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            # Never let httpx's message out: it carries the full URL, query string included.
            log.warning("http_request transport failure for %s%s: %s", target.host, target.path,
                        type(exc).__name__)
            raise TransportFailed(type(exc).__name__) from None

    async def _read(self, response: httpx.Response) -> HttpResponse:
        chunks: list[bytes] = []
        total = 0
        try:
            # aiter_raw(), not aiter_bytes(): the latter transparently decompresses based on whatever
            # Content-Encoding the response declares, regardless of the identity we asked for in _send,
            # so the cap below would fire only after a compressed body had already been expanded in
            # memory -- a decompression bomb from any allowlisted server. Raw bytes are what the cap is
            # actually meant to bound.
            async for chunk in response.aiter_raw():
                total += len(chunk)
                if total > self._max_response_bytes:
                    raise ResponseTooLarge(str(self._max_response_bytes))
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise TransportFailed(type(exc).__name__) from None
        finally:
            await response.aclose()
        headers = {name.lower(): value for name, value in response.headers.items()}
        return HttpResponse(response.status_code, headers, self._decode(b"".join(chunks), headers))

    def _decode(self, raw: bytes, headers: dict[str, str]) -> Any:
        media = headers.get("content-type", "").split(";")[0].strip().lower()
        if not raw:
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise UnsupportedMedia(media or "binary") from None
        # No separate character cap here: `_read` already guarantees len(raw) <= max_response_bytes, and
        # UTF-8 never decodes to more characters than input bytes, so len(text) <= max_response_bytes
        # always holds -- a second, independent limit would either be dead code or a surprise ceiling
        # lower than what the caller configured (see this client's history).
        if media == "application/json" or media.endswith("+json"):
            try:
                return parse_json(text)  # the engine's strict parser: no NaN, no duplicate keys, bounded depth
            except ValueError as exc:
                raise UnsupportedMedia(f"invalid json: {exc}") from None
        if media.startswith("text/"):
            return text
        raise UnsupportedMedia(media or "unknown")


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop headers a tenant must not control (framing, hop-by-hop, Host, Accept-Encoding) and refuse
    anything left that is not a well-formed header. A name that is not an RFC 7230 token, or a value that
    is not ASCII or carries CR/LF/NUL, is not a header a tenant can legitimately want to send -- it is an
    attempt to inject a second header line or smuggle a request past whatever reads the response, and
    `EgressBlocked("header")` gives a tenant who did this by accident (e.g. Korean text in a header value
    on this Korean-facing product) a clear answer instead of silently stripping it.
    """
    clean: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise EgressBlocked("header")
        if name.lower() in _UNSAFE_HEADERS:
            continue
        if not _TOKEN.fullmatch(name) or not value.isascii() or _has_control_char(value):
            raise EgressBlocked("header")
        clean[name] = value
    return clean


def _has_control_char(value: str) -> bool:
    return "\r" in value or "\n" in value or "\x00" in value


def _redirect_headers(headers: dict[str, str], previous_url: str, next_url: str) -> dict[str, str]:
    """Strip credential-bearing headers that must not follow a request across an origin change.

    A same-origin redirect (the common case: a trailing slash, a path move) keeps every header --
    there is no new party being trusted. Crossing an origin drops Authorization, Cookie and X-API-Key,
    mirroring httpx's own `_redirect_headers`, with the same carve-out for a plain http-to-https upgrade
    of the *same* host: that is not a new origin to trust less than the one the tenant already chose.
    `.port` is a lazily-parsed property and can itself raise ValueError for a malformed port; the call
    site wraps this alongside the rest of the redirect-URL handling for that reason.
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
    drop = set() if is_https_upgrade else _CREDENTIAL_HEADERS
    return {name: value for name, value in headers.items() if name.lower() not in drop}
