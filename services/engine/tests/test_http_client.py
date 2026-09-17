"""The egress policy end to end, with a fake resolver so no test ever touches real DNS."""
import asyncio
import gzip
import logging
import ssl
import tracemalloc

import httpx
import pytest
import trustme

from engine.http.client import (
    EgressBlocked,
    GuardedClient,
    ResponseTooLarge,
    TransportFailed,
    UnsupportedMedia,
)
from engine.http.policy import AllowEntry, parse_allowlist

NAME = "api.internal.test"


class FakeResolver:
    """Answers from a table. `calls` proves the allowlist runs *before* DNS."""

    def __init__(self, table: dict[str, list[str]]) -> None:
        self.table = table
        self.calls: list[str] = []

    async def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        try:
            return self.table[host]
        except KeyError:
            raise OSError(f"no such host: {host}") from None


async def _serve(handler, context: ssl.SSLContext | None = None) -> tuple[int, asyncio.AbstractServer]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=context)
    return server.sockets[0].getsockname()[1], server


def _responder(body: bytes = b"ok", status: str = "200 OK", headers: str = "") -> object:
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            head = f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n{headers}Connection: close\r\n\r\n"
            writer.write(head.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    return handle


def _client(allowlist: str, resolver, **kwargs) -> GuardedClient:
    return GuardedClient(parse_allowlist(allowlist), resolver=resolver, **kwargs)


async def test_a_host_outside_the_allowlist_is_never_resolved():
    resolver = FakeResolver({"evil.test": ["93.184.216.34"]})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://evil.test/x", headers={}, body=None, timeout_sec=5)

    assert exc.value.category == "allowlist"
    assert resolver.calls == []  # the host name itself must not become a DNS exfiltration channel
    await client.aclose()


async def test_one_forbidden_address_blocks_the_whole_request():
    resolver = FakeResolver({"api.example.com": ["93.184.216.34", "10.0.0.5"]})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://api.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "private"
    await client.aclose()


async def test_a_bare_ip_url_is_blocked_by_the_allowlist():
    resolver = FakeResolver({})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="http://169.254.169.254/latest/meta-data/", headers={},
                             body=None, timeout_sec=5)

    assert exc.value.category == "allowlist"
    assert resolver.calls == []
    await client.aclose()


async def test_an_allowlisted_ip_literal_is_still_classified():
    """An IP literal in the allowlist is not a way around the address rules."""
    resolver = FakeResolver({})
    client = _client("http://169.254.169.254", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="http://169.254.169.254/", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "link-local"
    await client.aclose()


async def test_allow_private_does_not_open_the_metadata_endpoint():
    """allowPrivate means "a machine on my network", not "any address the policy would block"."""
    resolver = FakeResolver({})
    client = _client("http://169.254.169.254;allowPrivate", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="http://169.254.169.254/latest/meta-data/", headers={},
                             body=None, timeout_sec=5)

    assert exc.value.category == "link-local"
    await client.aclose()


async def test_allow_private_permits_only_its_own_entry():
    resolver = FakeResolver({"internal.test": ["10.0.0.7"], "other.test": ["10.0.0.8"]})
    client = _client("http://internal.test:8080;allowPrivate, http://other.test:8080", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="http://other.test:8080/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "private"
    await client.aclose()


async def test_a_request_reaches_the_pinned_address_over_tls():
    authority = trustme.CA()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert(NAME).configure_cert(context)
    port, server = await _serve(_responder(b'{"ok": true}', headers="Content-Type: application/json\r\n"),
                                context)
    verify = ssl.create_default_context()
    authority.configure_trust(verify)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"https://{NAME}:{port};allowPrivate", resolver, verify=verify)

    async with server:
        response = await client.request(method="GET", url=f"https://{NAME}:{port}/x", headers={},
                                        body=None, timeout_sec=5)

    assert (response.status, response.body) == (200, {"ok": True})
    assert resolver.calls == [NAME]
    await client.aclose()


async def test_a_redirect_to_a_private_address_is_blocked():
    port, server = await _serve(_responder(b"", status="302 Found",
                                           headers="Location: http://internal.test:9000/x\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"], "internal.test": ["10.0.0.5"]})
    client = _client(f"http://{NAME}:{port};allowPrivate, http://internal.test:9000", resolver)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "private"
    await client.aclose()


async def test_a_redirect_to_a_host_outside_the_allowlist_is_blocked():
    port, server = await _serve(_responder(b"", status="302 Found",
                                           headers="Location: http://evil.test/x\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"], "evil.test": ["93.184.216.34"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "allowlist"
    assert "evil.test" not in resolver.calls
    await client.aclose()


async def test_authorization_does_not_reach_a_cross_origin_redirect_target():
    """Pins that _request actually calls redirect_headers -- not merely that redirect_headers itself is
    correct in isolation (see test_http_headers.py). Replacing that call with a no-op dict(headers)
    still passes every other test in this file; only checking the SECOND server's own captured wire
    catches it, since the cookie-jar tests above cover the jar mechanism, not the caller's own
    Authorization header."""
    captured: list[str] = []
    holder: dict = {}

    async def handle_a(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            location = f"http://other.internal.test:{holder['port_b']}/landed"
            resp = (f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n"
                    f"Connection: close\r\n\r\n")
            writer.write(resp.encode())
            await writer.drain()
        finally:
            writer.close()

    async def handle_b(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured.append(head.decode())
            body = b"ok"
            resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port_a, server_a = await _serve(handle_a)
    port_b, server_b = await _serve(handle_b)
    holder["port_b"] = port_b
    resolver = FakeResolver({"origin.internal.test": ["127.0.0.1"], "other.internal.test": ["127.0.0.1"]})
    client = _client(
        f"http://origin.internal.test:{port_a};allowPrivate, "
        f"http://other.internal.test:{port_b};allowPrivate",
        resolver,
    )

    async with server_a, server_b:
        await client.request(method="GET", url=f"http://origin.internal.test:{port_a}/start",
                             headers={"Authorization": "Bearer SECRET"}, body=None, timeout_sec=5)

    assert len(captured) == 1
    assert "authorization" not in captured[0].lower()
    await client.aclose()


async def test_too_many_redirects_is_blocked():
    port = 0
    holder: dict = {}

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        location = f"http://{NAME}:{holder['port']}/next"
        head = f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        writer.write(head.encode())
        await writer.drain()
        writer.close()

    port, server = await _serve(handle)
    holder["port"] = port
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_redirects=3)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "too-many-redirects"
    await client.aclose()


async def test_an_https_to_http_downgrade_is_blocked():
    authority = trustme.CA()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert(NAME).configure_cert(context)
    port, server = await _serve(_responder(b"", status="302 Found",
                                           headers=f"Location: http://{NAME}:9000/x\r\n"), context)
    verify = ssl.create_default_context()
    authority.configure_trust(verify)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"https://{NAME}:{port};allowPrivate, http://{NAME}:9000;allowPrivate", resolver,
                     verify=verify)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"https://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "downgrade"
    await client.aclose()


async def test_a_response_over_the_cap_stops_being_read():
    """Not just that ResponseTooLarge is eventually raised -- a "buffer the whole body, then check the
    cap" implementation would raise that too, once the whole (here: deliberately slow, deliberately much
    larger than the cap) body finally arrived. The server below paces its writes and never stops on its
    own, so only a genuinely streaming read -- one that aborts as soon as the running total crosses the
    cap -- can finish inside the tight overall timeout this test allows."""
    sent = {"bytes": 0}

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            head = "HTTP/1.1 200 OK\r\nContent-Length: 100000000\r\nConnection: close\r\n\r\n"
            writer.write(head.encode())
            await writer.drain()
            chunk = b"x" * 4096
            for _ in range(2_000):  # ~8 MB if ever fully sent -- paced below to take ~20s to get there
                writer.write(chunk)
                sent["bytes"] += len(chunk)
                try:
                    await asyncio.wait_for(writer.drain(), timeout=0.5)
                except Exception:
                    break  # the client closed its side -- exactly what a correct implementation does
                await asyncio.sleep(0.01)
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_response_bytes=1_000)

    async with server:
        with pytest.raises(ResponseTooLarge):
            await asyncio.wait_for(
                client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                               timeout_sec=5),
                timeout=3,
            )

    # A buffer-then-check implementation cannot finish inside the 3s budget above (the paced body takes
    # ~20s to fully arrive) and would fail this test with a bare TimeoutError instead of ResponseTooLarge.
    # A streaming implementation aborts within the first chunk or two, well under the cap's worth of data.
    assert sent["bytes"] < 50_000, f"server was allowed to send {sent['bytes']} bytes past the cap"
    await client.aclose()


async def test_an_oversized_request_body_never_leaves_the_process():
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:9000;allowPrivate", resolver, max_request_bytes=100)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="POST", url=f"http://{NAME}:9000/x", headers={}, body="y" * 200,
                             timeout_sec=5)

    assert exc.value.category == "request-too-large"
    assert resolver.calls == []
    await client.aclose()


async def test_a_connection_failure_is_a_transport_error():
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:9;allowPrivate", resolver)  # port 9 (discard) refuses

    with pytest.raises(TransportFailed):
        await client.request(method="GET", url=f"http://{NAME}:9/x", headers={}, body=None, timeout_sec=2)

    await client.aclose()


async def test_a_name_that_does_not_resolve_is_blocked():
    resolver = FakeResolver({})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://api.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "dns"
    await client.aclose()


async def test_a_text_response_comes_back_as_a_string():
    port, server = await _serve(_responder(b"hello", headers="Content-Type: text/plain\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        response = await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={},
                                        body=None, timeout_sec=5)

    assert response.body == "hello" and response.headers["content-type"] == "text/plain"
    await client.aclose()


async def test_an_unsupported_content_type_is_reported():
    port, server = await _serve(_responder(b"\x00\x01", headers="Content-Type: image/png\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(UnsupportedMedia):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    await client.aclose()


# ---------------------------------------------------------------------------------------------------
# Review round 2: connection reuse, cookies, header smuggling, decompression, escaping exceptions,
# credential headers on redirect, and the mutation gaps the first round of tests left open.
# ---------------------------------------------------------------------------------------------------


async def test_a_connection_is_not_reused_across_hostnames_on_the_same_address():
    """httpcore's connection-pool key is (scheme, address, port); the SNI hostname pinning depends on is
    not part of it. Without disabling keep-alive, a second hostname resolving to the same address could
    reuse the first hostname's already-verified connection with no new handshake -- and no verification
    against the second name -- at all.

    The server below never closes and never says "Connection: close" -- it loops, answering request
    after request on the same socket, exactly like a real keep-alive-friendly server would. That
    matters: a handler that serves one response and closes in its own `finally` ends the connection
    itself, which would make this test pass whether or not the client's own keep-alive setting does
    anything (confirmed by deleting `limits=` entirely and watching this test still pass). With a
    looping server, only the client's own limit can stop the reuse.

    A failed TLS handshake on the second connection never reaches this handler at all (verification
    fails during the handshake itself, before asyncio hands the stream to the callback), so there is no
    reliable "accepted N connections" count to assert here -- the outcome of the *second request* is
    the whole test: TransportFailed means a fresh, correctly-refused handshake happened; a 200 would
    mean the first connection's already-completed verification was reused for a name it never verified.
    """
    authority = trustme.CA()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert("a.internal.test").configure_cert(context)  # cert covers ONLY this name

    async def handle(reader, writer):
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                body = b"ok"
                resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\n\r\n"
                writer.write(resp.encode() + body)
                await writer.drain()
        except Exception:
            pass

    port, server = await _serve(handle, context)
    verify = ssl.create_default_context()
    authority.configure_trust(verify)
    resolver = FakeResolver({"a.internal.test": ["127.0.0.1"], "b.internal.test": ["127.0.0.1"]})
    client = _client(
        f"https://a.internal.test:{port};allowPrivate, https://b.internal.test:{port};allowPrivate",
        resolver, verify=verify,
    )

    # Not `async with server:` -- Server.wait_closed() waits for every accepted connection to finish,
    # and the second (deliberately failed) handshake below never sends the close the server-side
    # handler above is waiting to read, so wait_closed() would hang forever. server.close() alone just
    # stops the listener; the dangling handler task is torn down with the test's own event loop.
    try:
        first = await asyncio.wait_for(
            client.request(method="GET", url=f"https://a.internal.test:{port}/one", headers={},
                           body=None, timeout_sec=5), timeout=8)
        assert first.status == 200

        # If the connection above were reused, this would also return 200 with no new handshake at all
        # -- a certificate issued only for a.internal.test silently accepted for b.internal.test.
        with pytest.raises(TransportFailed):
            await asyncio.wait_for(
                client.request(method="GET", url=f"https://b.internal.test:{port}/two", headers={},
                               body=None, timeout_sec=5), timeout=8)
    finally:
        await client.aclose()
        server.close()


async def test_the_connection_pool_still_caps_total_connections():
    """httpx.Limits() resets max_connections to unlimited if constructed to change anything else --
    passing limits=httpx.Limits(max_keepalive_connections=0) alone would silently drop the default
    100-connection cap along with disabling keep-alive. Read the pool's own configuration back rather
    than trusting a second constant here to stay in sync with whatever client.py passes."""
    client = _client("https://x.test", FakeResolver({}))
    pool = client._client._transport._pool
    assert pool._max_connections == 100
    assert pool._max_keepalive_connections == 0
    await client.aclose()


async def test_a_set_cookie_does_not_survive_a_cross_origin_redirect():
    captured = {}
    holder: dict = {}

    async def handle_a(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            location = f"http://other.internal.test:{holder['port_b']}/landed"
            resp = (f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n"
                    f"Set-Cookie: sess=LEAKED\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            writer.write(resp.encode())
            await writer.drain()
        finally:
            writer.close()

    async def handle_b(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured["head"] = head.decode()
            body = b"ok"
            resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port_a, server_a = await _serve(handle_a)
    port_b, server_b = await _serve(handle_b)
    holder["port_b"] = port_b
    resolver = FakeResolver({"origin.internal.test": ["127.0.0.1"], "other.internal.test": ["127.0.0.1"]})
    client = _client(
        f"http://origin.internal.test:{port_a};allowPrivate, "
        f"http://other.internal.test:{port_b};allowPrivate",
        resolver,
    )

    async with server_a, server_b:
        await client.request(method="GET", url=f"http://origin.internal.test:{port_a}/start", headers={},
                             body=None, timeout_sec=5)

    assert "cookie" not in captured["head"].lower()
    await client.aclose()


async def test_a_set_cookie_does_not_survive_to_a_later_separate_request():
    captured: list[str] = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured.append(head.decode())
            body = b"ok"
            resp = (f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\n"
                    f"Set-Cookie: sess=FIRST\r\nConnection: close\r\n\r\n")
            writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        await client.request(method="GET", url=f"http://{NAME}:{port}/one", headers={}, body=None,
                             timeout_sec=5)
        await client.request(method="GET", url=f"http://{NAME}:{port}/two", headers={}, body=None,
                             timeout_sec=5)

    assert len(captured) == 2
    assert "cookie" not in captured[1].lower()
    await client.aclose()


async def test_the_cookie_jar_itself_is_empty_after_a_response_with_set_cookie():
    """Isolates the jar clear in _request from the Cookie-header override in _send: even though _send
    never trusts the jar for what goes out on the wire, the jar object itself must not keep growing --
    removing self._client.cookies.clear() would leave this assertion failing even though no cookie
    would (yet) have reached any wire, because nothing has asked the jar to attach itself again."""
    port, server = await _serve(_responder(b"ok", headers="Content-Type: text/plain\r\n"
                                           "Set-Cookie: sess=FROM_SERVER\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                             timeout_sec=5)

    assert dict(client._client.cookies) == {}
    await client.aclose()


async def test_a_preexisting_jar_cookie_never_reaches_the_wire():
    """Isolates the Cookie-header pop in _send from the jar clear in _request: seed the jar directly
    (as if a previous response's Set-Cookie had not been cleared in time, or anything else put
    something there), before this request's own build_request() call ever runs. Only the pop in _send
    -- not the clear, which has not had anything to act on yet -- can be what keeps this off the wire."""
    captured: list[str] = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured.append(head.decode())
            body = b"ok"
            resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)
    client._client.cookies.set("sess", "PRESEEDED", domain="127.0.0.1")

    async with server:
        await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                             timeout_sec=5)

    assert "cookie" not in captured[0].lower()
    await client.aclose()


async def test_the_wire_cookie_is_the_callers_not_the_jars():
    """Isolates the intended-cookie logic in _send from a "pass-through" that merely fails to clear
    what httpx's jar already put on the built request: seed the jar with one value and pass a
    *different* one explicitly, and check that the value that actually reaches the wire is the one
    this call asked for, not the one already sitting in the jar."""
    captured: list[str] = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured.append(head.decode())
            body = b"ok"
            resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)
    client._client.cookies.set("sess", "FROM_JAR", domain="127.0.0.1")

    async with server:
        await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={"Cookie": "mine=1"},
                             body=None, timeout_sec=5)

    cookie_lines = [ln for ln in captured[0].split("\r\n") if ln.lower().startswith("cookie:")]
    assert cookie_lines == ["Cookie: mine=1"]
    await client.aclose()


async def test_the_jar_is_cleared_even_when_the_response_itself_is_refused():
    """httpx extracts Set-Cookie inside send() before this client ever regains control, so a response
    that this client goes on to refuse for an unrelated reason (here: a Location header httpx's own
    parser rejects, escaping through request()'s safety net) still populates the jar first. The clear
    has to run on that path too, or a failing request leaves the jar growing forever and a later,
    unrelated request could pick up whatever it left behind."""
    async def handle(reader, writer):
        try:
            while True:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: mailto:x@y.z\r\n"
                             b"Set-Cookie: sess=STUCK\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        except Exception:
            pass

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    with pytest.raises(EgressBlocked):
        await asyncio.wait_for(
            client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                           timeout_sec=5), timeout=8)

    assert dict(client._client.cookies) == {}
    await client.aclose()
    server.close()


async def test_tenant_supplied_framing_headers_never_reach_the_wire():
    captured = {}

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured["head"] = head.decode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        await client.request(
            method="POST", url=f"http://{NAME}:{port}/x",
            headers={"Transfer-Encoding": "chunked", "Content-Length": "999", "Connection": "keep-alive",
                    "Expect": "100-continue", "Proxy-Authorization": "Basic xyz"},
            body="hello", timeout_sec=5,
        )

    head = captured["head"].lower()
    assert "transfer-encoding" not in head
    assert "content-length: 999" not in head
    assert "content-length: 5" in head  # httpx's own, correct for "hello"
    assert "100-continue" not in head
    assert "proxy-auth" not in head
    # httpx always sends its own default "Connection: keep-alive" regardless of max_keepalive_connections
    # (that setting governs what the *pool* does afterward, not this header) -- the tenant's own attempt
    # to set Connection must not add a second, conflicting line, only ever this one default.
    connection_lines = [line for line in head.split("\r\n") if line.startswith("connection:")]
    assert connection_lines == ["connection: keep-alive"]
    await client.aclose()


@pytest.mark.parametrize("headers", [
    {"X-Foo": "bar\r\nX-Injected: evil"},
    {"X-Foo": "line1\nline2"},
    {"X-Foo": "\x00null"},
    {"X-Foo": "가나다"},  # non-ASCII value (Korean)
    {"bad header": "value"},  # a space is not a valid RFC 7230 token character
    {"X-Foo": 123},  # non-str value
])
async def test_a_malformed_header_is_rejected(headers):
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:9000;allowPrivate", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url=f"http://{NAME}:9000/x", headers=headers, body=None,
                             timeout_sec=5)

    assert exc.value.category == "header"
    assert resolver.calls == []  # rejected before DNS, like every other guard
    await client.aclose()


async def test_the_transport_failure_log_omits_the_query_string(caplog):
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:9;allowPrivate", resolver)  # port 9 (discard) refuses

    with (
        caplog.at_level(logging.WARNING, logger="engine.http.client"),
        pytest.raises(TransportFailed),
    ):
        await client.request(method="GET", url=f"http://{NAME}:9/report?api_key=SECRET123",
                             headers={}, body=None, timeout_sec=2)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "SECRET123" not in messages
    assert "api_key" not in messages
    assert "127.0.0.1" not in messages  # the pinned literal address must not appear either -- only host
    await client.aclose()


async def test_accept_encoding_is_always_identity_and_overrides_the_caller():
    captured = {}

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured["head"] = head.decode()
            body = b"ok"
            resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        await client.request(method="GET", url=f"http://{NAME}:{port}/x",
                             headers={"Accept-Encoding": "gzip, br"}, body=None, timeout_sec=5)

    head = captured["head"].lower()
    assert "accept-encoding: identity" in head
    assert "gzip" not in head
    await client.aclose()


async def test_a_compressed_response_is_capped_against_the_wire_bytes_not_the_decoded_size():
    """A malicious or merely non-compliant allowlisted server can send Content-Encoding: gzip whatever
    Accept-Encoding says. The cap must bound the bytes actually transferred, not whatever a decompressor
    would expand them to -- otherwise a tiny compressed body is a decompression bomb."""
    payload = gzip.compress(b"x" * 20_000_000)
    cap = max(len(payload) // 2, 16)
    port, server = await _serve(_responder(payload, headers="Content-Encoding: gzip\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_response_bytes=cap)

    tracemalloc.start()
    try:
        async with server:
            with pytest.raises(ResponseTooLarge):
                await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                     timeout_sec=5)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 5_000_000, f"peak traced memory was {peak} bytes -- decompression ran despite the cap"
    await client.aclose()


async def test_a_server_that_compresses_anyway_is_reported_as_unsupported_media():
    """The other side of the previous test's trade, on the record rather than a surprise: a
    *legitimate* server that ignores Accept-Encoding: identity and gzips its JSON anyway (some do) now
    fails this call outright, because _read hands aiter_raw()'s undecompressed bytes straight to
    _decode -- there is no decompression step left to run. Before switching to aiter_raw(), httpx would
    have decompressed this transparently and the JSON would have parsed. Keeping the safer behaviour
    (see the module docstring's last paragraph) means this functional cost is real and is accepted
    deliberately, not accidentally."""
    payload = gzip.compress(b'{"hello": "world"}')
    port, server = await _serve(_responder(payload, headers="Content-Encoding: gzip\r\n"
                                           "Content-Type: application/json\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(UnsupportedMedia):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    await client.aclose()


async def test_a_confusable_unicode_host_is_blocked_not_crashed():
    """U+2100 NFKC-normalizes to "a/c"; Python's own urlsplit refuses a netloc like this with a raw
    ValueError rather than parsing it -- that must become EgressBlocked, never an unhandled crash."""
    resolver = FakeResolver({})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked):
        await client.request(method="GET", url="https://℀.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    await client.aclose()


@pytest.mark.parametrize("location", [
    "mailto:test@example.com", "javascript:alert(1)", "data:text/html,hi", "tel:+15551234567",
])
async def test_a_redirect_to_a_non_http_scheme_is_blocked(location):
    """httpx's own AsyncClient.send() always builds a redirect request internally to populate
    response.next_request, even with follow_redirects=False -- and its stricter URL parser rejects an
    opaque, non-hierarchical URI like these (no "/"-rooted path) before this client ever gets to look at
    the Location header itself. That surfaces as httpx.InvalidURL, caught by request()'s safety net and
    reported as EgressBlocked("internal") rather than EgressBlocked("scheme") -- a different category,
    but the same guarantee: the request is never followed to a non-http(s) destination either way."""
    port, server = await _serve(_responder(b"", status="302 Found", headers=f"Location: {location}\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "internal"
    await client.aclose()


async def test_a_host_whose_idna_encoding_fails_is_blocked_not_crashed():
    """A label over 63 octets fails Python's idna codec with UnicodeError even though the raw Unicode
    string is perfectly well-formed text -- that must become EgressBlocked, not an unhandled crash."""
    resolver = FakeResolver({})
    client = _client("https://api.example.com", resolver)
    host = ("가" * 60) + ".example.com"  # 60 Korean syllables in one label -- too long once punycoded

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url=f"https://{host}/x", headers={}, body=None, timeout_sec=5)

    assert exc.value.category == "allowlist"
    assert resolver.calls == []
    await client.aclose()


async def test_an_invalid_json_body_is_reported_as_unsupported_media():
    port, server = await _serve(_responder(b"{not valid json", headers="Content-Type: application/json\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(UnsupportedMedia):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    await client.aclose()


async def test_an_unrecognized_but_valid_utf8_content_type_is_reported():
    port, server = await _serve(_responder(b"hello", headers="Content-Type: application/octet-stream\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(UnsupportedMedia):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    await client.aclose()


async def test_a_redirect_to_a_malformed_port_is_blocked():
    """.port is a lazily-parsed property: unlike the non-http-scheme cases above, httpx's own internal
    redirect-request construction does not choke on an out-of-range port, so control genuinely returns
    to this client's own redirect handling -- this is what actually exercises the ValueError guard
    around the Location-URL handling in _request(), not the generic safety net."""
    port, server = await _serve(_responder(b"", status="302 Found",
                                           headers="Location: http://other.internal.test:999999/x\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "url"
    await client.aclose()


async def test_an_unexpected_exception_is_reported_as_a_non_retryable_internal_error(caplog):
    """The client's contract is exactly four exception types (see request()'s docstring). Whatever the
    exact mechanism -- an httpx.InvalidURL that is not an HTTPError, an h11 protocol error, a resolver
    that raises something other than OSError -- nothing else may escape request(). It must come back
    as EgressBlocked("internal"), not TransportFailed: per 2b design §5.6, EgressBlocked is
    non-retryable and TransportFailed is retryable, and a bug in this module's own logic (a broken
    policy classifier, here simulated with a resolver that raises the wrong exception type) must fail
    the node once, loudly -- not be retried forever as if the network had merely blinked."""
    class ExplodingResolver:
        async def resolve(self, host):
            raise RuntimeError("boom")

    client = _client("https://api.example.com", ExplodingResolver())

    with (
        caplog.at_level(logging.ERROR, logger="engine.http.client"),
        pytest.raises(EgressBlocked) as exc,
    ):
        await client.request(method="GET", url="https://api.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "internal"
    assert not isinstance(exc.value, TransportFailed)
    assert any("unexpected" in record.getMessage() for record in caplog.records)
    await client.aclose()


async def test_a_broken_policy_classifier_fails_closed_without_reaching_the_socket(monkeypatch):
    """A bug in the policy layer itself must not turn into traffic. Simulate a typo in the address
    classifier (ip_category raising instead of answering) against a private address that must be
    blocked with no allowPrivate, and confirm the request never reaches the socket -- fail-closed, not
    fail-open, even when the code that is supposed to say why is itself broken."""
    import engine.http.client as mod

    hits: list[int] = []

    async def handle(reader, writer):
        hits.append(1)
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 6\r\n"
                         b"Connection: close\r\n\r\nSECRET")
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["10.0.0.5"]})  # private, and no allowPrivate -- must be blocked
    client = _client(f"http://{NAME}:{port}", resolver)

    def broken(_address):
        raise KeyError("typo in the category table")

    monkeypatch.setattr(mod, "ip_category", broken)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "internal"
    assert hits == []  # fail-closed: the broken classifier never let the request reach the socket
    await client.aclose()


async def test_content_type_does_not_survive_the_post_to_get_conversion():
    captured: list[str] = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured.append(head.decode())
            if head.decode().startswith("POST"):
                resp = ("HTTP/1.1 302 Found\r\nLocation: /landed\r\n"
                        "Content-Length: 0\r\nConnection: close\r\n\r\n")
                writer.write(resp.encode())
            else:
                body = b"ok"
                resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
                writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        await client.request(method="POST", url=f"http://{NAME}:{port}/x",
                             headers={"Content-Type": "application/json"}, body="{\"a\":1}", timeout_sec=5)

    assert len(captured) == 2
    assert "content-type" not in captured[1].lower()
    assert captured[1].lower().startswith("get ")
    await client.aclose()


async def test_a_302_redirect_converts_post_to_a_bodyless_get():
    captured: list[str] = []

    async def handle(reader, writer):
        try:
            data = await reader.readuntil(b"\r\n\r\n")
            head = data.decode()
            captured.append(head)
            if head.startswith("POST"):
                content_length = 0
                for line in head.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                if content_length:
                    await reader.readexactly(content_length)
                resp = ("HTTP/1.1 302 Found\r\nLocation: /landed\r\nContent-Length: 0\r\n"
                        "Connection: close\r\n\r\n")
                writer.write(resp.encode())
            else:
                body = b"ok"
                resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
                writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        response = await client.request(method="POST", url=f"http://{NAME}:{port}/x", headers={},
                                        body="{\"a\":1}", timeout_sec=5)

    assert response.status == 200
    assert len(captured) == 2
    assert captured[1].startswith("GET ")
    await client.aclose()


async def test_every_resolved_address_is_validated_not_only_the_last_one():
    resolver = FakeResolver({"api.example.com": ["10.0.0.5", "93.184.216.34"]})  # bad first, good last
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://api.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "private"
    await client.aclose()


async def test_the_first_validated_address_is_the_one_connected_to():
    port, server = await _serve(_responder(b"ok", headers="Content-Type: text/plain\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1", "127.0.0.2"]})  # only .1 has a listener
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        response = await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={},
                                        body=None, timeout_sec=5)

    assert response.status == 200
    await client.aclose()


async def test_an_unsupported_scheme_is_blocked():
    resolver = FakeResolver({})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="ftp://api.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "scheme"
    assert resolver.calls == []
    await client.aclose()


async def test_an_empty_dns_answer_is_blocked():
    resolver = FakeResolver({"api.example.com": []})
    client = _client("https://api.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://api.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "dns"
    await client.aclose()


async def test_an_allow_entry_with_an_empty_host_is_still_rejected_before_dns():
    """parse_allowlist refuses to ever construct an AllowEntry with an empty host, so with it as the
    only entry source, _check's own "if not host" guard is unobservable -- that argument is correct
    (see this task's round-2 review) but AllowEntry is a public export, and nothing stops some future
    entry source from building one directly with host="". White-box construct one to prove the guard
    is what stands between that and find_entry's exact-match rule happily matching "" against "":
    without it, this would resolve "" and classify whatever came back instead of refusing outright."""
    resolver = FakeResolver({"": ["10.0.0.5"]})
    client = GuardedClient((AllowEntry(scheme="https", host="", port=443, allow_private=False),),
                           resolver=resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client._check("https:///x")

    assert exc.value.category == "allowlist"
    assert resolver.calls == []
    await client.aclose()


@pytest.mark.parametrize("host", ["..example.com", "\x00.example.com", "%.example.com", ".example.com"])
async def test_a_syntactically_invalid_host_does_not_pass_a_wildcard_suffix_match(host):
    """AllowEntry.matches()'s wildcard branch is a bare suffix test (host.endswith(".example.com")); it
    relies on the allowlist side always being a validated hostname (parse_allowlist guarantees that) but
    has no opinion on the *request*-side host. None of these can actually resolve, so this is not a
    live bypass, but the suffix test alone would happily match all four against "*.example.com" -- the
    request-side syntax check in _check exists so that only a well-formed name reaches that match."""
    resolver = FakeResolver({})
    client = _client("https://*.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url=f"https://{host}/x", headers={}, body=None,
                             timeout_sec=5)

    assert exc.value.category == "allowlist"
    assert resolver.calls == []
    await client.aclose()


async def test_a_syntactically_valid_wildcard_match_still_reaches_dns():
    """The companion to the test above: is_hostname_syntax must not be so strict that it blocks a
    perfectly ordinary subdomain a wildcard entry is supposed to allow."""
    resolver = FakeResolver({"sub.example.com": ["10.0.0.5"]})
    client = _client("https://*.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://sub.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    assert resolver.calls == ["sub.example.com"]  # it matched and reached DNS, not blocked at the gate
    assert exc.value.category == "private"
    await client.aclose()


async def test_a_unicode_host_is_idna_encoded_before_the_allowlist_check():
    resolver = FakeResolver({"xn--fiq.example.com": ["10.0.0.5"]})
    client = _client("https://xn--fiq.example.com", resolver)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="GET", url="https://中.example.com/x", headers={}, body=None,
                             timeout_sec=5)

    # It reached DNS at all -- meaning it matched the allowlist entry -- only because the host was
    # IDNA-encoded to "xn--fiq.example.com" before find_entry ran.
    assert resolver.calls == ["xn--fiq.example.com"]
    assert exc.value.category == "private"
    await client.aclose()


async def test_the_query_string_reaches_the_server():
    captured = {}

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured["head"] = head.decode()
            body = b"ok"
            resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        await client.request(method="GET", url=f"http://{NAME}:{port}/x?a=1&b=2", headers={}, body=None,
                             timeout_sec=5)

    request_line = captured["head"].splitlines()[0]
    assert request_line == "GET /x?a=1&b=2 HTTP/1.1"
    await client.aclose()


async def test_a_request_body_exactly_at_the_cap_is_allowed():
    resolver = FakeResolver({NAME: []})  # empty answer -> "dns", but only once the size check passes
    client = _client(f"http://{NAME}:9000;allowPrivate", resolver, max_request_bytes=100)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="POST", url=f"http://{NAME}:9000/x", headers={}, body="y" * 100,
                             timeout_sec=5)

    assert exc.value.category == "dns"  # not "request-too-large": exactly at the cap must be allowed
    await client.aclose()


async def test_a_request_body_one_byte_over_the_cap_is_blocked():
    resolver = FakeResolver({NAME: []})
    client = _client(f"http://{NAME}:9000;allowPrivate", resolver, max_request_bytes=100)

    with pytest.raises(EgressBlocked) as exc:
        await client.request(method="POST", url=f"http://{NAME}:9000/x", headers={}, body="y" * 101,
                             timeout_sec=5)

    assert exc.value.category == "request-too-large"
    await client.aclose()


async def test_a_response_body_exactly_at_the_cap_is_allowed():
    port, server = await _serve(_responder(b"x" * 500, headers="Content-Type: text/plain\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_response_bytes=500)

    async with server:
        response = await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={},
                                        body=None, timeout_sec=5)

    assert response.body == "x" * 500
    await client.aclose()


async def test_a_response_body_one_byte_over_the_cap_is_blocked():
    port, server = await _serve(_responder(b"x" * 501, headers="Content-Type: text/plain\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_response_bytes=500)

    async with server:
        with pytest.raises(ResponseTooLarge):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    await client.aclose()


async def test_too_many_redirects_counts_the_actual_hops():
    hits = {"count": 0}
    holder: dict = {}

    async def handle(reader, writer):
        hits["count"] += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
            location = f"http://{NAME}:{holder['port']}/next"
            head = (f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n"
                    f"Connection: close\r\n\r\n")
            writer.write(head.encode())
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    holder["port"] = port
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_redirects=3)

    async with server:
        with pytest.raises(EgressBlocked) as exc:
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert exc.value.category == "too-many-redirects"
    assert hits["count"] == 4  # the initial request plus exactly max_redirects (3) more, no fewer or more
    await client.aclose()


async def test_a_caller_supplied_host_header_cannot_override_the_pinned_host():
    captured = {}

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            captured["head"] = head.decode()
            body = b"ok"
            resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            writer.write(resp.encode() + body)
            await writer.drain()
        finally:
            writer.close()

    port, server = await _serve(handle)
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        await client.request(method="GET", url=f"http://{NAME}:{port}/x",
                             headers={"host": "evil.example", "HOST": "also-evil.example"}, body=None,
                             timeout_sec=5)

    host_lines = [line for line in captured["head"].split("\r\n") if line.lower().startswith("host:")]
    assert host_lines == [f"Host: {NAME}:{port}"]
    await client.aclose()


async def test_trust_env_is_disabled(monkeypatch):
    port, server = await _serve(_responder(b"ok", headers="Content-Type: text/plain\r\n"))
    # Point every proxy env var at a port nothing listens on. If trust_env were True, httpx would try to
    # CONNECT through it and this request would fail or hang; with trust_env=False it is ignored, and the
    # request reaches the pinned address directly.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9/")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9/")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9/")
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        response = await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={},
                                        body=None, timeout_sec=5)

    assert response.status == 200
    await client.aclose()


async def test_the_response_is_closed_even_when_the_cap_is_exceeded(monkeypatch):
    calls = []
    original_aclose = httpx.Response.aclose

    async def tracking_aclose(self):
        calls.append(1)
        await original_aclose(self)

    monkeypatch.setattr(httpx.Response, "aclose", tracking_aclose)

    port, server = await _serve(_responder(b"x" * 20_000))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_response_bytes=1_000)

    async with server:
        with pytest.raises(ResponseTooLarge):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    assert calls, "response.aclose() was never called after the cap was exceeded"
    await client.aclose()
