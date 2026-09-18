"""Secrets: write-only (2b design §4.2).

There is no read endpoint. `GET /secrets` returns names and times so the editor can offer
`{{secret.NAME}}` completions; the value exists in exactly two places — the encrypted row, and the inside
of one http_request call.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request, Response

from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.secrets import store as secret_db

router = APIRouter()

# The 64-character bound matches the CHECK on secrets.name: the two have to agree, or the API hands the
# database a row it refuses and the tenant sees a 500 instead of a refusal.
NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MIN_VALUE_CHARS = 8  # below this, value-based redaction (2b design §6) would scrub unrelated text
MAX_VALUE_CHARS = 4096


def _name(raw: str) -> str:
    # A malformed name is a 404, not a 422: the path segment is the identity of the resource, and a name
    # that cannot exist is indistinguishable from one that does not.
    if not NAME.match(raw):
        raise ApiError(404, "NOT_FOUND", "시크릿을 찾을 수 없습니다")
    return raw


def _key(request: Request) -> bytes:
    key = request.app.state.config.secret_key
    if key is None:  # only reachable with ENGINE_DEV_INSECURE=1
        raise ApiError(503, "INTERNAL", "시크릿 저장소가 설정되지 않았습니다")
    return key


@router.put("/secrets/{name}", status_code=204)
async def put_secret(name: str, request: Request) -> Response:
    name = _name(name)
    key = _key(request)
    body = require_object(await read_json(request))
    value = field(body, "value", str)
    if not MIN_VALUE_CHARS <= len(value) <= MAX_VALUE_CHARS:
        # The bounds, never the value: an error that echoed what was sent would put the secret in the
        # response body, and from there into whatever logged the exchange.
        raise ApiError(422, "REQUEST_ERROR",
                       f"시크릿 값은 {MIN_VALUE_CHARS}~{MAX_VALUE_CHARS}자여야 합니다")
    async with request.app.state.pool.connection() as conn:
        await secret_db.put_secret(conn, key=key, name=name, value=value)
    return Response(status_code=204)


@router.delete("/secrets/{name}", status_code=204)
async def delete_secret(name: str, request: Request) -> Response:
    name = _name(name)
    async with request.app.state.pool.connection() as conn:
        if not await secret_db.delete_secret(conn, name):
            raise ApiError(404, "NOT_FOUND", "시크릿을 찾을 수 없습니다")
    return Response(status_code=204)


@router.get("/secrets")
async def list_secrets(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        rows = await secret_db.list_secrets(conn)
    return {"secrets": [{"name": row["name"], "createdAt": row["created_at"].isoformat(),
                         "updatedAt": row["updated_at"].isoformat()} for row in rows]}
