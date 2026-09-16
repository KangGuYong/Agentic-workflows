"""API skeleton: token auth, strict/bounded bodies, and the shared error shape (design 8.1, 8.2)."""
from types import SimpleNamespace

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from engine.api import errors
from engine.api.body import field, read_json, require_object

# ---------------------------------------------------------------- against the real app (needs db/redis)


async def test_healthz_reports_both_backends(api):
    response = await api.get("/healthz")

    assert response.status_code == 200 and response.json() == {"status": "ok", "db": True, "redis": True}


async def test_healthz_also_requires_the_token(api):
    """FastAPI's app-level `dependencies=[...]` always runs; a route can't opt out with `dependencies=[]`
    (verified separately below), so /healthz stays behind the shared token like every other route."""
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
    """Documents why /healthz keeps the token: `dependencies=[]` on a route is a no-op, not an override."""
    from fastapi import Depends

    from engine.api.errors import ApiError

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
