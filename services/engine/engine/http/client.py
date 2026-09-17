"""The guarded HTTP client (2b design §5).

Order is the whole point: allowlist, then DNS, then every resolved address, then a connection pinned to an
address that passed — repeated in full for every redirect hop. Checking the allowlist before DNS means an
unapproved host name is never even looked up, so the name cannot be used as a DNS exfiltration channel.
This order is the design; it is not to be reshuffled to make some other fix read more naturally.

Header rules -- what a tenant may set, what must not survive a redirect, and what must not survive the
301/302/303-to-GET conversion -- live in headers.py, not here; see its module docstring. What follows is
what httpx itself does that this module has to work around; read this once, because the workarounds are
scattered across the methods below by necessity and none of them makes sense in isolation:

- **The cookie jar is live even when constructed with `cookies=None`.** That argument only seeds the
  jar *empty*; `AsyncClient.send()` still calls `self.cookies.extract_cookies(response)` after every
  response, and `build_request()` still merges the jar into a `Cookie` header on the next one. `_send`
  never trusts that merge (it overwrites whatever httpx assembled with exactly the caller's own Cookie,
  if any), and `_follow_redirects` clears the jar after every hop as defence in depth -- see the
  comments at both call sites for which one is the actual guarantee.
- **httpcore's connection-pool key is `(scheme, address, port)` -- the SNI hostname this client pins is
  not part of it.** A kept-alive connection to one allowlisted host would be reused, with no new
  handshake and so no new verification, for a *different* hostname that happens to resolve to the same
  address. `GuardedClient.__init__` disables keep-alive entirely to close this.
- **`AsyncClient.send()` builds a redirect request internally -- to populate `response.next_request` --
  even when `follow_redirects=False`.** This client ignores that request (it re-derives the next hop
  itself from the `Location` header) but cannot stop httpx from *trying*: a `Location` value httpx's own
  stricter URL model rejects (an opaque, non-hierarchical scheme like `mailto:`) raises `httpx.InvalidURL`
  before this client ever gets to inspect the header itself. `request()`'s safety net is what catches
  that, not the scheme check in `_resolve_target`.
- **`httpx.Limits()` resets `max_connections` to unlimited if you construct it to change anything else.**
  The default `AsyncClient` pool caps both `max_connections` (100) and `max_keepalive_connections` (20);
  passing `limits=httpx.Limits(max_keepalive_connections=0)` alone silently drops the first cap along
  with lowering the second. Both need to be passed together.

Raises its own exceptions; `engine/nodes/http_request.py` turns them into node errors. Nothing here knows
about nodes, so the policy can be tested without one. The client's public contract is exactly four
exception types (`EgressBlocked`, `ResponseTooLarge`, `UnsupportedMedia`, `TransportFailed`) -- `request()`
guarantees it, see its docstring.

One functional trade, made deliberately: `_read` bounds the response cap against the bytes actually on
the wire (see its comment), which means a server that sends `Content-Encoding` regardless of the
`identity` this client asks for is no longer transparently decompressed -- its body now reports as
`UnsupportedMedia` instead of being parsed. An unbounded decompression is worse than a failed call, so
this is being kept; see test_a_compressed_response_is_capped_against_the_wire_bytes_not_the_decoded_size
and test_a_server_that_compresses_anyway_is_reported_as_unsupported_media in test_http_client.py for the
security case and the cost, side by side.
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

from engine.http.headers import HeaderRejected, bodyless_headers, headers_for_redirect, sanitize_headers
from engine.http.policy import DEFAULT_PORTS, AllowEntry, find_entry, has_hostname_syntax, ip_category
from engine.jsondata import parse_json

log = logging.getLogger(__name__)

# allowPrivate exempts only the categories that mean "a machine on the operator's own network".
# link-local is deliberately absent: 169.254.169.254 is the cloud metadata endpoint, and an operator who
# opened one internal API did not ask for that (2b design §5.2).
ALLOW_PRIVATE_CATEGORIES = frozenset({"private", "loopback", "cgnat"})
REDIRECTS = {301, 302, 303, 307, 308}
BODYLESS = {301, 302, 303}  # these become a GET without a body, as every browser and client does


class EgressBlocked(Exception):
    """Policy refused the request. `category` is all a tenant is ever told (2b design §5.6). This is
    also what `request()`'s safety net raises (as category "internal") for a bug in this module's own
    logic: per 2b design §5.6, an `EgressBlocked` is non-retryable and a `TransportFailed` is retryable,
    and a programming error in a policy check must fail the node once, loudly -- not be retried forever
    as if it were a flaky connection."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class ResponseTooLarge(Exception):
    """The response passed the byte cap and was abandoned unread."""


class UnsupportedMedia(Exception):
    """Neither JSON nor text: the DSL type system has nowhere to put it."""


class TransportFailed(Exception):
    """Connection, TLS, framing or read failure. Retryable, unlike everything else here."""


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
        # limits: keep-alive disabled -- see the module docstring for why, and for the max_connections
        # caveat that makes passing it alone unsafe.
        self._client = httpx.AsyncClient(
            follow_redirects=False, trust_env=False, verify=verify, cookies=None,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, *, method: str, url: str, headers: dict[str, str], body: str | None,
                      timeout_sec: float) -> HttpResponse:
        """Run one logical request, following redirects internally. Raises only `EgressBlocked`,
        `ResponseTooLarge`, `UnsupportedMedia` or `TransportFailed` -- anything else that escapes the
        implementation below is a bug in this module, not something the node's error mapper (Task 9)
        should ever have to special-case, so it is caught here and reported as `EgressBlocked("internal")`
        instead of leaking an unmapped exception type, or the wrong retry class, to tenant-facing code.
        """
        try:
            return await self._follow_redirects(method=method, url=url, headers=headers, body=body,
                                                timeout_sec=timeout_sec)
        except (EgressBlocked, ResponseTooLarge, UnsupportedMedia, TransportFailed):
            raise
        except Exception as exc:  # noqa: BLE001 -- deliberate catch-all, see the docstring above
            log.error("http_request: unexpected %s escaped the guarded client", type(exc).__name__)
            raise EgressBlocked("internal") from None

    async def _follow_redirects(self, *, method: str, url: str, headers: dict[str, str], body: str | None,
                                timeout_sec: float) -> HttpResponse:
        """Run `method url` and every redirect hop it produces, up to `max_redirects`, re-validating
        each hop's target from scratch via `_resolve_target` -- see the module docstring for why that
        order is not to be reshuffled. Raises `EgressBlocked("too-many-redirects")` once the budget is
        spent; every other exception this can raise is documented at its own call site below."""
        if body is not None and len(body.encode("utf-8")) > self._max_request_bytes:
            raise EgressBlocked("request-too-large")
        try:
            current_headers = sanitize_headers(headers)
        except HeaderRejected:
            raise EgressBlocked("header") from None
        current_method, current_body, current_url = method.upper(), body, url
        for _ in range(self._max_redirects + 1):
            target = await self._resolve_target(current_url)
            try:
                response = await self._send(target, current_method, current_headers, current_body,
                                            timeout_sec)
            finally:
                # `_send` is the actual guarantee (it never trusts the jar for what goes out on the
                # wire; see the module docstring). This clear is defence in depth so a Set-Cookie
                # extracted from this hop's response cannot outlive the call -- on the success path
                # below, on the next redirect hop, or on a later, unrelated request on this shared
                # client -- and it has to be in a `finally` because httpx extracts cookies inside
                # `send()` itself, before an exception on this line (a transport failure, a redirect to
                # a URL httpx's own parser refuses) ever reaches this method (`_follow_redirects`).
                self._client.cookies.clear()
            if response.status_code not in REDIRECTS or "location" not in response.headers:
                return await self._read(response)
            await response.aclose()
            # Every URL touched below -- location, and inside headers_for_redirect, both endpoints'
            # .port (a lazily-parsed property that raises ValueError just like .port did in
            # _resolve_target) -- is wrapped in the same try, so a malformed Location header fails as
            # EgressBlocked("url") instead of an unhandled ValueError.
            try:
                location = urljoin(current_url, response.headers["location"])
                is_downgrade = urlsplit(current_url).scheme == "https" and urlsplit(location).scheme == "http"
                next_headers = headers_for_redirect(current_headers, current_url, location)
            except ValueError:
                raise EgressBlocked("url") from None
            if is_downgrade:
                raise EgressBlocked("downgrade")
            if response.status_code in BODYLESS and current_method not in ("GET", "HEAD"):
                current_method, current_body = "GET", None
                next_headers = bodyless_headers(next_headers)
            current_headers = next_headers
            current_url = location
        raise EgressBlocked("too-many-redirects")

    async def _resolve_target(self, url: str) -> _Target:
        """Parse `url`, check its host against the allowlist, resolve it, validate every address DNS
        returned, and pin the first one that passed. Returns the `_Target` `_send` connects to.

        The order above is the whole design (see the module docstring) and is not incidental to this
        method's shape: allowlist before DNS means an unapproved name is never looked up at all, and
        every resolved address is checked -- not just the one that ends up pinned -- so a server that
        answers with a mix of public and private addresses cannot get the private one used just by
        listing it last.
        """
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            port = parts.port  # a lazily-parsed property: an out-of-range or non-numeric port raises here
        except ValueError:
            raise EgressBlocked("url") from None
        if parts.scheme not in DEFAULT_PORTS:
            raise EgressBlocked("scheme")
        if not host:
            # Unreachable: parse_allowlist cannot produce an empty-host entry (find_entry returns None
            # first), and a directly-constructed one is caught by has_hostname_syntax("") below. Kept
            # as an explicit statement of intent, not something this code depends on.
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
        # "invalid" is policy.py's ip_category sentinel for "not an address at all" -- i.e. host is a
        # name, not an IP literal, and needs DNS.
        is_ip_literal = ip_category(host) != "invalid"
        if is_ip_literal:
            addresses = [host]
        else:
            # A wildcard entry's match is a bare suffix test (see has_hostname_syntax's docstring): make
            # sure the request-side host is actually a well-formed name before it is used that way, or
            # before it is handed to DNS at all.
            if not has_hostname_syntax(host):
                raise EgressBlocked("allowlist")
            addresses = await self._resolve(host)
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
            # `headers` was sanitized once in `_follow_redirects`: no Host, no framing/hop-by-hop headers, no
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
        # This is the actual guarantee behind "cookies are never stored" (2b design §5.5), not the jar
        # clear in _follow_redirects -- see the module docstring for why the jar merges a Cookie header
        # at all despite `cookies=None`. Force the wire to carry exactly the Cookie value, if any, that
        # survived sanitize_headers/headers_for_redirect above, never one httpx assembled on its own
        # from a jar entry this client did not intend to send.
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
            # actually meant to bound. The cost of this choice is real and is taken deliberately -- see
            # the module docstring's last paragraph.
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
