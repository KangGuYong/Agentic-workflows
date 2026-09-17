"""API skeleton: token auth, strict/bounded bodies, and the shared error shape (design 8.1, 8.2)."""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from engine.api import errors
from engine.api.app import create_app
from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.api.security import TokenAuthMiddleware
from engine.jsondata import MAX_JSON_DEPTH
from tests.helpers import make_config

# ---------------------------------------------------------------- against the real app (needs db/redis)


async def test_healthz_reports_both_backends(api):
    response = await api.get("/healthz")

    assert response.status_code == 200 and response.json() == {"status": "ok", "db": True, "redis": True}


async def test_healthz_also_requires_the_token(api):
    """TokenAuthMiddleware runs in front of routing for every path unconditionally, so /healthz stays
    behind the shared token like every other route -- see the app-level-dependency tests below for why
    that isn't handled with FastAPI's app-level `dependencies=[...]` instead."""
    response = await api.get("/healthz", headers={"Authorization": ""})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_a_request_without_the_token_is_rejected(api):
    response = await api.get("/node-types", headers={"Authorization": ""})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_node_types_describe_the_registry(api):
    response = await api.get("/node-types")

    types = {item["type"]: item for item in response.json()["nodeTypes"]}
    assert set(types) == {"start", "end", "template", "llm", "classifier", "condition", "merge",
                          "human_approval"}
    assert types["llm"]["configSchema"]["properties"]["prompt"]["x-template"] is True
    assert types["llm"]["defaultPolicy"]["retry"]["maxAttempts"] == 3
    assert types["condition"]["isBranch"] is True and types["start"]["defaultPolicy"] is None


async def test_an_unknown_path_uses_the_error_format(api):
    response = await api.get("/nope")

    assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- app-level dependency vs. route override


async def test_a_route_level_empty_dependencies_list_cannot_skip_an_app_level_dependency():
    """General FastAPI fact, not specific to this app (which doesn't use an app-level dependency -- see
    TokenAuthMiddleware): `dependencies=[]` on a route is a no-op against an app-level dependency, not an
    override. That gap is real but a different one from A3's: even a correctly-applied app-level
    dependency only covers routes added via `add_api_route`, and FastAPI's own docs routes are not."""
    from fastapi import Depends

    def _always_reject() -> None:
        raise ApiError(401, "UNAUTHORIZED", "nope")

    probe = FastAPI(dependencies=[Depends(_always_reject)])
    errors.install(probe)

    @probe.get("/open", dependencies=[])
    async def _open() -> dict:
        return {"ok": True}

    transport = ASGITransport(app=probe)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/open")

    assert response.status_code == 401


# ---------------------------------------------------------------- the unhandled-exception handler (unit)


async def test_unhandled_errors_return_the_internal_error_shape_without_leaking_details():
    """Starlette's ServerErrorMiddleware re-raises after sending the response, so httpx's default
    ASGITransport(raise_app_exceptions=True) would surface the exception to the test instead of a 500
    body; raise_app_exceptions=False is required to observe what a real client actually receives."""
    probe = FastAPI()
    errors.install(probe)

    @probe.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("super secret connection string")

    transport = ASGITransport(app=probe, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "INTERNAL", "message": "서버 오류가 발생했습니다"}}
    assert "secret" not in response.text and "RuntimeError" not in response.text


# ---------------------------------------------------------------- body helpers (unit; Task 12 will route through them)


def _body_app(max_body_bytes: int = 64) -> FastAPI:
    probe = FastAPI()
    probe.state.config = SimpleNamespace(max_body_bytes=max_body_bytes)
    errors.install(probe)

    @probe.post("/echo")
    async def _echo(request: Request) -> dict:
        body = require_object(await read_json(request))
        count = field(body, "count", int, required=False, default=None)
        return {"body": body, "count": count}

    return probe


async def _post(app: FastAPI, content: bytes, *, headers: dict | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/echo", content=content, headers=headers or {})


async def test_read_json_rejects_a_body_over_the_limit_by_declared_content_length():
    response = await _post(_body_app(max_body_bytes=64), b'{"a": 1}')

    assert response.status_code == 200  # sanity: a small, well-formed body is accepted first

    big = b'{"a": "' + b"x" * 200 + b'"}'
    response = await _post(_body_app(max_body_bytes=64), big)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_a_lying_content_length_does_not_let_an_oversized_body_through():
    """The Content-Length header is client-supplied and untrusted: a client that under-reports it must
    still be rejected once the actual bytes are read, not waved through on the declared value."""
    big = b'{"a": "' + b"x" * 200 + b'"}'
    response = await _post(_body_app(max_body_bytes=64), big, headers={"content-length": "5"})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_an_oversized_declared_content_length_with_a_small_actual_body_is_rejected_up_front():
    """Isolates the pre-check from the streaming check below it: the declared Content-Length is over the
    limit but the actual body is tiny, so a 413 here can only be the declared-length check firing --
    unlike test_read_json_rejects_a_body_over_the_limit_by_declared_content_length above, whose body is
    oversized too and would still fail with the pre-check deleted entirely."""
    response = await _post(_body_app(max_body_bytes=64), b'{"a": 1}', headers={"content-length": "999999"})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def _raw_asgi_call(app: FastAPI, method: str, path: str, *,
                          raw_headers: list[tuple[bytes, bytes]] | None = None, receive=None):
    """Drives `app` as a bare ASGI callable, bypassing httpx entirely. Needed for cases httpx won't let
    us construct on its own: a header value with a raw non-ASCII byte, or a request with no
    Content-Length at all whose `receive` we control chunk by chunk (a real chunked-transfer-encoding
    request also has no Content-Length; httpx's own chunked support doesn't expose call counts)."""
    messages: list[dict] = []

    async def default_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers or [],
        "client": ("test", 12345),
        "server": ("test", 80),
        "scheme": "http",
    }

    async def send(message: dict) -> None:
        messages.append(message)

    await app(scope, receive or default_receive, send)

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


async def test_a_chunked_body_over_the_limit_is_rejected_without_buffering_it_all():
    """No Content-Length at all, as with a real `Transfer-Encoding: chunked` request, so only the
    streaming size check inside read_json's loop can be why this 413s. The counting `receive` below also
    proves the fix stops pulling chunks instead of accumulating the whole body first: the bug this covers
    let a chunked POST pull 67MB into the process before ever returning 413."""
    chunk = b"x" * 10
    total_chunks = 500  # 5000 bytes total if fully drained, far past the 64 byte limit below
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls <= total_chunks:
            return {"type": "http.request", "body": chunk, "more_body": calls < total_chunks}
        return {"type": "http.disconnect"}

    status, body = await _raw_asgi_call(_body_app(max_body_bytes=64), "POST", "/echo", receive=receive)

    assert status == 413
    assert json.loads(body)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    # 64 // 10 = 7 chunks is enough to cross the limit; stopping anywhere near there (not at 500) is the
    # proof the server never buffered the rest of the body.
    assert calls < total_chunks // 5


async def test_read_json_rejects_a_body_nested_past_the_json_depth_bound():
    depth = MAX_JSON_DEPTH + 20
    nested = ('{"a":' * depth) + "1" + ("}" * depth)

    response = await _post(_body_app(max_body_bytes=10_000), nested.encode())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


async def test_read_json_rejects_nan_even_though_json_loads_would_accept_it():
    response = await _post(_body_app(), b'{"a": NaN}')

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


async def test_read_json_rejects_duplicate_keys():
    response = await _post(_body_app(), b'{"a": 1, "a": 2}')

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


async def test_require_object_rejects_a_non_object_body():
    response = await _post(_body_app(), b"[1, 2, 3]")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


async def test_field_rejects_true_where_an_int_is_required():
    response = await _post(_body_app(), b'{"count": true}')

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_ERROR"


async def test_field_accepts_a_real_int():
    response = await _post(_body_app(), b'{"count": 3}')

    assert response.status_code == 200 and response.json()["count"] == 3


def test_field_accepts_an_int_where_a_float_is_asked_for():
    """A JSON number written without a fraction, like `3`, decodes to a Python int; that is still a
    perfectly good float value and must not be rejected just because it has no decimal point."""
    assert field({"weight": 3}, "weight", float) == 3.0


def test_field_still_rejects_a_bool_where_a_float_is_asked_for():
    with pytest.raises(ApiError):
        field({"weight": True}, "weight", float)


def test_field_treats_an_explicit_null_as_absent_on_an_optional_field():
    assert field({"count": None}, "count", int, required=False, default=7) == 7


def test_field_still_rejects_an_explicit_null_on_a_required_field():
    with pytest.raises(ApiError):
        field({"count": None}, "count", int)


# ---------------------------------------------------------------- TokenAuthMiddleware (unit; covers every path)


def _secured_app(token: str | None = "test-token") -> FastAPI:
    probe = FastAPI()
    probe.state.config = SimpleNamespace(api_token=token)
    probe.add_middleware(TokenAuthMiddleware)
    errors.install(probe)

    @probe.get("/ping")
    async def _ping() -> dict:
        return {"ok": True}

    return probe


async def _get(app: FastAPI, path: str, *, headers: dict | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


async def test_a_non_ascii_authorization_header_is_rejected_not_a_500():
    """Reproduces the bug directly: h11 allows obs-text in a header value and Starlette decodes it as
    latin-1, so a client can send a genuinely non-ASCII Authorization header; that used to blow up
    hmac.compare_digest(str, str) with an unhandled TypeError (a 500, plus a logged traceback, from a
    request that never even had a valid token). Built as raw ASGI because httpx's own header encoding
    would reject or mangle a literal non-ASCII byte before it reached the app."""
    status, body = await _raw_asgi_call(_secured_app(), "GET", "/ping",
                                        raw_headers=[(b"authorization", b"Bearer \xfc")])

    assert status == 401
    assert json.loads(body)["error"]["code"] == "UNAUTHORIZED"


async def test_docs_routes_require_the_token_too():
    """/openapi.json, /docs, /redoc and /docs/oauth2-redirect are registered with Starlette's add_route,
    which app-level `dependencies=[...]` never sees; TokenAuthMiddleware covers them anyway because it
    sits in front of routing entirely, not inside FastAPI's dependency system."""
    app = _secured_app()

    for path in ("/openapi.json", "/docs", "/redoc"):
        response = await _get(app, path)
        assert response.status_code == 401, path

    for path in ("/openapi.json", "/docs", "/redoc"):
        response = await _get(app, path, headers={"authorization": "Bearer test-token"})
        assert response.status_code == 200, path


async def test_a_wrong_auth_scheme_is_rejected():
    for header in ("Token test-token", "Basic test-token", "bearertest-token", ""):
        response = await _get(_secured_app(), "/ping", headers={"authorization": header})
        assert response.status_code == 401, header


async def test_create_app_warns_when_no_token_is_configured(caplog):
    """Design 8.1 deliberately allows an unset token to pass unauthenticated (for local development) --
    that contract is not changed here -- but create_app must say so loudly at startup rather than relying
    on main.py, since load_config() maps ENGINE_API_TOKEN="" to None and create_app is reachable without
    going through an entrypoint (e.g. from tests, as here)."""
    config = make_config()

    with caplog.at_level(logging.WARNING):
        create_app(config, pool=SimpleNamespace(), redis=SimpleNamespace())

    assert any("ENGINE_API_TOKEN" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------- /healthz (unit, fakes; no containers)


class _BrokenConnection:
    async def __aenter__(self):
        raise RuntimeError("db down")

    async def __aexit__(self, *exc_info):
        return False


class _BrokenPool:
    def connection(self):
        return _BrokenConnection()


class _BrokenRedis:
    async def ping(self):
        raise RuntimeError("redis down")


class _HangingConnection:
    async def __aenter__(self):
        await asyncio.sleep(999)

    async def __aexit__(self, *exc_info):
        return False


class _HangingPool:
    def connection(self):
        return _HangingConnection()


class _OkRedis:
    async def ping(self):
        return True


def _healthz_app(pool, redis) -> FastAPI:
    return create_app(make_config(), pool, redis)


async def test_healthz_reports_degraded_when_both_backends_are_broken():
    app = _healthz_app(_BrokenPool(), _BrokenRedis())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": False, "redis": False}


async def test_healthz_times_out_on_a_wedged_pool_instead_of_hanging_forever():
    """The pool's connection() never yields. Before the fix, /healthz had no timeout around it, so the
    request (and the pool slot it holds) would hang forever instead of reporting degraded; this must come
    back within a few seconds, well under the 15s the fixture's own `until` helper would tolerate."""
    app = _healthz_app(_HangingPool(), _OkRedis())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with asyncio.timeout(5):
            response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": False, "redis": True}
