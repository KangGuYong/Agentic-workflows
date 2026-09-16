"""Workflow CRUD and validation (MVP design 8.1)."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response

from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.db import workflows as workflow_db
from engine.jsondata import safe_text
from engine.validator import MAX_DSL_BYTES, analyze

router = APIRouter()


def _sanitize(value: Any) -> Any:
    """Scrub NUL/lone-surrogate text out of a JSON value read back from the database. `read_json` refuses
    this text on the way in (Task 11), so nothing written through the API can contain it, but a row written
    by another path -- a migration, a seed script, a future writer that forgets to go through read_json --
    could. Left alone, a lone surrogate breaks response serialization: Starlette's JSONResponse encodes
    with ensure_ascii=False, and `str.encode("utf-8")` raises UnicodeEncodeError on an unpaired surrogate,
    which happens *inside* the error-response handler for a 409 and turns into a bare 500 (Task 11 review)."""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {safe_text(key): _sanitize(item) for key, item in value.items()}
    return value


def _workflow_id(raw: str) -> str:
    """A path segment that isn't a UUID at all cannot match any row. Reject it before it ever reaches a
    query: the `id` column is uuid, so psycopg/Postgres would otherwise refuse to bind the parameter and
    raise a DataError that the generic handler turns into a 500, instead of the 404 a bad id deserves."""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "워크플로를 찾을 수 없습니다") from None


def _check_name(name: str | None) -> None:
    if name is not None and (not name.strip() or len(name) > 200):
        raise ApiError(422, "REQUEST_ERROR", "이름은 1~200자여야 합니다")


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row["id"]), "name": _sanitize(row["name"]), "revision": row["revision"],
            "updatedAt": row["updated_at"].isoformat()}


async def _analysis(request: Request, dsl: dict[str, Any]):
    """Validation is CPU work on untrusted input: keep it off the event loop (design 8.1). NodeRegistry is
    built once at startup and only ever read (registry.get/registry.all), and `analyze` touches no shared
    mutable state of its own, so calling it from a worker thread concurrently with other requests is safe."""
    return await asyncio.to_thread(analyze, dsl, request.app.state.registry)


def _check_size(dsl: dict[str, Any]) -> None:
    if len(json.dumps(dsl, ensure_ascii=False).encode("utf-8")) > MAX_DSL_BYTES:
        raise ApiError(422, "LIMIT_EXCEEDED", f"워크플로가 너무 큽니다 (최대 {MAX_DSL_BYTES // 1024}KB)")


async def _require(conn, workflow_id: str) -> dict[str, Any]:
    row = await workflow_db.get(conn, workflow_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "워크플로를 찾을 수 없습니다")
    return row


@router.post("/workflows", status_code=201)
async def create_workflow(request: Request) -> dict[str, Any]:
    body = require_object(await read_json(request))
    name = field(body, "name", str)
    _check_name(name)
    async with request.app.state.pool.connection() as conn:
        return _view(await workflow_db.create(conn, name=name))


@router.get("/workflows")
async def list_workflows(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        rows = await workflow_db.list_all(conn)
    return {"workflows": [_view(row) for row in rows]}


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    workflow_id = _workflow_id(workflow_id)
    async with request.app.state.pool.connection() as conn:
        row = await _require(conn, workflow_id)
    return {**_view(row), "draftDsl": _sanitize(row["draft_dsl"])}


@router.put("/workflows/{workflow_id}")
async def save_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    workflow_id = _workflow_id(workflow_id)
    body = require_object(await read_json(request))
    draft = field(body, "draftDsl", dict)
    revision = field(body, "revision", int)
    name = field(body, "name", str, required=False)
    _check_name(name)
    _check_size(draft)  # a draft may be invalid (the editor saves work in progress) but not oversized
    async with request.app.state.pool.connection() as conn, conn.transaction():
        await _require(conn, workflow_id)
        saved = await workflow_db.save_draft(conn, workflow_id=workflow_id, draft=draft,
                                             revision=revision, name=name)
        if saved is None:
            # Stale CAS: re-read now, inside the same transaction, rather than reusing the row fetched
            # above by _require. Under concurrent saves that first read can already be stale by the time
            # the CAS fails -- e.g. two clients both send revision=1: the loser's pre-CAS read may still
            # show revision 1 even though the winner committed revision 2 (and a new draft) in between,
            # which would report a "current" state that was never actually current (Task 11 review-style
            # bug: the details must describe the revision that won, not what we happened to see first).
            current = await _require(conn, workflow_id)
            raise ApiError(409, "REVISION_CONFLICT", "다른 곳에서 먼저 저장했습니다",
                           {"currentRevision": current["revision"], "draftDsl": _sanitize(current["draft_dsl"])})
    return {"revision": saved["revision"]}


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, request: Request) -> Response:
    workflow_id = _workflow_id(workflow_id)
    async with request.app.state.pool.connection() as conn, conn.transaction():
        # FOR UPDATE, not a plain read: holding this lock across the has_active_runs check and the DELETE
        # blocks a concurrent run insert (Postgres takes FOR KEY SHARE on this row for the runs.workflow_id
        # FK) until we commit or roll back -- see workflow_db.lock for why that closes the race instead of
        # letting ON DELETE CASCADE silently drop a run created in the gap between the check and the delete.
        row = await workflow_db.lock(conn, workflow_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", "워크플로를 찾을 수 없습니다")
        if await workflow_db.has_active_runs(conn, workflow_id):
            raise ApiError(409, "WORKFLOW_HAS_ACTIVE_RUNS", "실행 중인 워크플로는 삭제할 수 없습니다")
        await workflow_db.delete(conn, workflow_id)
    return Response(status_code=204)


@router.post("/workflows/{workflow_id}/validate")
async def validate_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    workflow_id = _workflow_id(workflow_id)
    body = require_object(await read_json(request))
    draft = field(body, "draftDsl", dict)
    async with request.app.state.pool.connection() as conn:
        await _require(conn, workflow_id)
    # No _check_size pre-check here (unlike save_workflow): analyze() already measures and reports an
    # oversized DSL as an ordinary LIMIT_EXCEEDED issue in the 200 response below, using its own normalized
    # representation of the draft (post model_validate, defaults excluded) rather than the raw dict
    # _check_size would measure. Calling _check_size here too would be a second, independent size check on
    # a different representation of the same MAX_DSL_BYTES threshold: whenever the two happened to
    # disagree, this endpoint's answer would depend on which check ran, contradicting /validate's contract
    # to always report problems as issues in a 200, never a save. Letting analyze() be the only authority
    # for /validate removes that possibility -- "too big" is reported the same way regardless of exactly
    # where analyze() notices it.
    analysis = await _analysis(request, draft)
    return {"issues": [issue.to_dict() for issue in analysis.issues]}
