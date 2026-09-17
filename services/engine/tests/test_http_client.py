"""The egress policy end to end, with a fake resolver so no test ever touches real DNS."""
import asyncio
import ssl

import pytest
import trustme

from engine.http.client import EgressBlocked, GuardedClient, ResponseTooLarge, TransportFailed
from engine.http.policy import parse_allowlist

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
    port, server = await _serve(_responder(b"x" * 20_000))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver, max_response_bytes=1_000)

    async with server:
        with pytest.raises(ResponseTooLarge):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

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
    from engine.http.client import UnsupportedMedia

    port, server = await _serve(_responder(b"\x00\x01", headers="Content-Type: image/png\r\n"))
    resolver = FakeResolver({NAME: ["127.0.0.1"]})
    client = _client(f"http://{NAME}:{port};allowPrivate", resolver)

    async with server:
        with pytest.raises(UnsupportedMedia):
            await client.request(method="GET", url=f"http://{NAME}:{port}/x", headers={}, body=None,
                                 timeout_sec=5)

    await client.aclose()
