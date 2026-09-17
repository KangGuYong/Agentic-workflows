"""The guarded HTTP client (2b design §5).

Order is the whole point: allowlist, then DNS, then every resolved address, then a connection pinned to an
address that passed — repeated in full for every redirect hop. Checking the allowlist before DNS means an
unapproved host name is never even looked up, so the name cannot be used as a DNS exfiltration channel.

Raises its own exceptions; `engine/nodes/http_request.py` turns them into node errors. Nothing here knows
about nodes, so the policy can be tested without one.
"""
from __future__ import annotations

import asyncio
import logging
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
# Headers that must not survive a redirect to a different origin: carrying a bearer token or session
# cookie set for host A over to host B (possibly attacker-controlled once the allowlist permits it) would
# turn a single approved credential into a cross-tenant, cross-host leak. Mirrors httpx's own
# `_redirect_headers` (Authorization dropped on cross-origin, Cookie always dropped on redirect).
_CREDENTIAL_HEADERS = {"authorization", "cookie"}


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
    """Connection, TLS or read failure. Retryable, unlike everything else here."""


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
        # follow_redirects=False: every hop is re-checked here. cookies are never stored; trust_env=False
        # so an ambient HTTP_PROXY cannot route around the pinning.
        self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False, verify=verify,
                                         cookies=None)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, *, method: str, url: str, headers: dict[str, str], body: str | None,
                      timeout_sec: float) -> HttpResponse:
        if body is not None and len(body.encode("utf-8")) > self._max_request_bytes:
            raise EgressBlocked("request-too-large")
        current_method, current_body, current_url = method.upper(), body, url
        current_headers = dict(headers)
        for _ in range(self._max_redirects + 1):
            target = await self._check(current_url)
            response = await self._send(target, current_method, current_headers, current_body, timeout_sec)
            if response.status_code not in REDIRECTS or "location" not in response.headers:
                return await self._read(response)
            await response.aclose()
            location = urljoin(current_url, response.headers["location"])
            if urlsplit(current_url).scheme == "https" and urlsplit(location).scheme == "http":
                raise EgressBlocked("downgrade")
            if response.status_code in BODYLESS and current_method not in ("GET", "HEAD"):
                current_method, current_body = "GET", None
            current_headers = _redirect_headers(current_headers, current_url, location)
            current_url = location
        raise EgressBlocked("too-many-redirects")

    async def _check(self, url: str) -> _Target:
        parts = urlsplit(url)
        if parts.scheme not in DEFAULT_PORTS:
            raise EgressBlocked("scheme")
        host = (parts.hostname or "").lower()
        if not host:
            raise EgressBlocked("allowlist")
        try:
            # find_entry requires an already-ASCII host (see its docstring); a Unicode host name has to
            # be IDNA-encoded here, the client's job, before it can ever match an allowlist entry.
            host = host if host.isascii() else host.encode("idna").decode("ascii")
        except UnicodeError:
            raise EgressBlocked("allowlist") from None
        port = parts.port or DEFAULT_PORTS[parts.scheme]
        entry = find_entry(self._allowlist, parts.scheme, host, port)
        if entry is None:  # before DNS, deliberately
            raise EgressBlocked("allowlist")
        addresses = [host] if ip_category(host) != "invalid" else await self._resolve(host)
        for address in addresses:
            category = ip_category(address)
            if category is not None and not (entry.allow_private and category in ALLOW_PRIVATE_CATEGORIES):
                raise EgressBlocked(category)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return _Target(parts.scheme, host, port, path, addresses[0])

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
        pinned = f"{target.scheme}://{literal}:{target.port}{target.path}"
        # Drop any caller-supplied Host header (any case) before adding ours: httpx.Headers does not
        # dedupe across case variants, so leaving one in would put two Host lines on the wire and hand a
        # tenant a way to smuggle a second Host past the one this client pins (2b design §5, Task 0).
        clean_headers = {name: value for name, value in headers.items() if name.lower() != "host"}
        request = self._client.build_request(
            method, pinned,
            headers={**clean_headers, "Host": authority},
            content=body.encode("utf-8") if body is not None else None,
            timeout=httpx.Timeout(timeout_sec, connect=timeout_sec),
            # httpcore hands this to the TLS layer as `server_hostname`, so SNI *and* certificate
            # verification use the real name even though the socket goes to the validated address.
            extensions={"sni_hostname": target.host},
        )
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
            async for chunk in response.aiter_bytes():
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
        # Decoded UTF-8 text can never hold more characters than the response had bytes, so this can
        # only ever be tighter than (never looser than, never surprisingly independent of) the byte cap
        # `_read` already enforced above -- there is no second, disconnected size limit to keep in sync.
        if len(text) > self._max_response_bytes:
            raise ResponseTooLarge(str(self._max_response_bytes))
        if media == "application/json" or media.endswith("+json"):
            try:
                return parse_json(text)  # the engine's strict parser: no NaN, no duplicate keys, bounded depth
            except ValueError as exc:
                raise UnsupportedMedia(f"invalid json: {exc}") from None
        if media.startswith("text/"):
            return text
        raise UnsupportedMedia(media or "unknown")


def _redirect_headers(headers: dict[str, str], previous_url: str, next_url: str) -> dict[str, str]:
    """Strip credential-bearing headers that must not follow a request across an origin change.

    A same-origin redirect (the common case: a trailing slash, a path move) keeps every header --
    there is no new party being trusted. Crossing an origin drops Cookie and Authorization, mirroring
    httpx's own `_redirect_headers`, with the same carve-out for a plain http-to-https upgrade of the
    *same* host: that is not a new origin to trust less than the one the tenant already chose.
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
    drop = {"cookie"} if is_https_upgrade else _CREDENTIAL_HEADERS
    return {name: value for name, value in headers.items() if name.lower() not in drop}
