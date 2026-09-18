"""Task 0 spike (2b design §11.1): does connecting to a pinned IP keep the original name for Host, SNI
and certificate verification? Everything in the egress policy depends on it, so it is proven here with a
real TLS server before any of it is built. These tests stay in the suite as a regression guard on httpx."""
import asyncio
import ssl

import httpx
import pytest
import trustme

LOCALHOST = "127.0.0.1"
NAME = "api.internal.test"


async def _serve(context: ssl.SSLContext) -> tuple[int, asyncio.AbstractServer, list[str]]:
    """A TLS server that answers any request with a 200 and closes, recording the decoded request head
    of every connection it receives (so a test can assert on the Host header actually sent)."""
    received: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            received.append(head.decode())
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception:  # a client that hangs up mid-handshake is the point of one of the tests
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, LOCALHOST, 0, ssl=context)
    return server.sockets[0].getsockname()[1], server, received


def _server_context(authority: trustme.CA, name: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert(name).configure_cert(context)
    return context


def _client_context(authority: trustme.CA) -> ssl.SSLContext:
    context = ssl.create_default_context()
    authority.configure_trust(context)
    return context


async def _get(client: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
    """Bounded await for client.get(). A real hang would otherwise surface as a bare TimeoutError whose
    str() is empty, which is confusing to read in a failure — fail loudly with context instead."""
    try:
        return await asyncio.wait_for(client.get(url, **kwargs), timeout=5)
    except TimeoutError:
        pytest.fail(f"GET {url} did not complete within 5s (handshake or connect hung)")


async def test_a_pinned_ip_still_sends_the_original_host_and_verifies_the_certificate():
    authority = trustme.CA()
    port, server, received = await _serve(_server_context(authority, NAME))
    async with server, httpx.AsyncClient(verify=_client_context(authority)) as client:
        response = await _get(
            client,
            f"https://{LOCALHOST}:{port}/",
            headers={"Host": NAME},
            extensions={"sni_hostname": NAME},
        )

    assert response.status_code == 200
    # The server saw the original hostname, not the IP it was actually dialed on — the Host header
    # is the third leg (besides SNI and cert verification) that pinning must leave untouched.
    assert f"Host: {NAME}\r\n" in received[0]
    assert LOCALHOST not in received[0]  # the IP we connected to appears nowhere in the request


async def test_a_certificate_for_another_name_is_rejected():
    """The proof that verification is still on: same connection shape, wrong certificate."""
    authority = trustme.CA()
    port, server, _ = await _serve(_server_context(authority, "someone-else.test"))
    async with server, httpx.AsyncClient(verify=_client_context(authority)) as client:
        with pytest.raises(httpx.ConnectError) as exc:
            await _get(
                client,
                f"https://{LOCALHOST}:{port}/",
                headers={"Host": NAME},
                extensions={"sni_hostname": NAME},
            )

    assert "certificate" in str(exc.value).lower() or "hostname" in str(exc.value).lower()


async def test_without_the_sni_extension_the_ip_is_what_gets_verified():
    """Documents why the extension is mandatory: without it the certificate is checked against the IP."""
    authority = trustme.CA()
    port, server, _ = await _serve(_server_context(authority, NAME))
    async with server, httpx.AsyncClient(verify=_client_context(authority)) as client:
        with pytest.raises(httpx.ConnectError):
            await _get(client, f"https://{LOCALHOST}:{port}/", headers={"Host": NAME})
