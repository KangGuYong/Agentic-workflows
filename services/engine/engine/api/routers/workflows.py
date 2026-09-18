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
from engine.validator import MAX_DSL_BYTES, Analysis, analyze, compute_before, compute_schemas
from engine.validator.issues import Issue, error

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


def _oversized_issue(dsl: dict[str, Any]) -> Issue | None:
    """The same check `analyze()` eventually performs, run on the raw draft before paying for it (A1). A
    hostile draft up to max_body_bytes (1,000,000B) clears read_json's own gate but is already more than
    MAX_DSL_BYTES (524,288B) ever needs to be: analyze() only rejects it after WorkflowDSL.model_validate of
    up to ~15k nodes, two model_dumps and a json.dumps -- on a 1MB body that's ~236ms of CPU inside
    asyncio.to_thread's executor (min(32, cpu+4) threads, unbounded queue) versus ~9ms for this raw byte
    count, and that executor can hold the GIL long enough meanwhile to stall /healthz and Task 14's SSE
    streams. The message text depends only on the MAX_DSL_BYTES constant, not on this draft's actual size,
    so it is byte-for-byte identical to the issue analyze() would have produced -- a client cannot tell
    which path answered."""
    if len(json.dumps(dsl, ensure_ascii=False).encode("utf-8")) > MAX_DSL_BYTES:
        return error("LIMIT_EXCEEDED", f"워크플로가 너무 큽니다 (최대 {MAX_DSL_BYTES // 1024}KB)")
    return None


def _check_size(dsl: dict[str, Any]) -> None:
    issue = _oversized_issue(dsl)
    if issue is not None:
        raise ApiError(422, issue.code, issue.message)


def _node_analysis(analysis: Analysis) -> dict[str, Any]:
    """Per-node facts the editor cannot work out for itself (3 설계 §4.1).

    `variables` is the guaranteed set: the nodes that have certainly run by the time this one does, which
    is what autocomplete may offer without a `default()`. `outputSchema` is what a reference into that
    node can address. `handles` are a branch node's outputs, which depend on its *config* -- a
    classifier's handles are whatever categories the tenant typed -- so `/node-types` cannot carry them.

    No explicit size cap: `structure.MAX_NODES` (100) already rejects a larger workflow in phase 1, as a
    LIMIT_EXCEEDED *error*, which leaves `graph` unset and this mapping empty. A second bound here would
    be unreachable code with an untestable branch.

    Empty when phases 1-2 did not pass: `analyze` stops at the first phase with errors and leaves
    `graph` unset, so there is nothing to walk. The editor keeps its previous result in that case rather
    than losing autocomplete the moment a draft is momentarily broken.
    """
    graph = analysis.graph
    if graph is None:
        return {}
    before = compute_before(graph)
    schemas = compute_schemas(graph)
    return {
        node_id: {
            # sorted, because `compute_before` returns frozensets and their iteration order is not
            # stable between processes: an unsorted list would reshuffle the editor's suggestions on
            # every revalidation and make two responses impossible to compare.
            "variables": sorted(before[node_id]),
            "outputSchema": schemas[node_id],
            "handles": list(graph.nodes[node_id].spec.handles(graph.nodes[node_id].config)),
        }
        for node_id in graph.order
    }


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
        # No pre-CAS read (A3): it was dead weight. A missing workflow_id makes the CAS UPDATE below match
        # zero rows exactly like a stale revision does, and the 404 that a pre-read would have produced here
        # is already produced by the post-CAS re-read's own _require call a few lines down -- so dropping it
        # changes nothing observable while saving one SELECT on every autosave (the editor calls this every
        # ~1s).
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
    # Cheap gate before the expensive path (A1): analyze() would reach the same LIMIT_EXCEEDED issue only
    # after WorkflowDSL.model_validate of the whole draft, two model_dumps and a json.dumps -- a ~26x CPU
    # amplifier on a hostile body measured at ~236ms versus ~9ms for this raw byte count, run inside
    # asyncio.to_thread's executor which can hold the GIL long enough meanwhile to stall /healthz and Task
    # 14's SSE streams. _oversized_issue reuses the identical MAX_DSL_BYTES threshold and produces the exact
    # Issue analyze() would have (the message depends only on the constant, not the measurement), so
    # /validate's contract -- always a 200 with issues, never a save -- and its answer for "too big" are both
    # unchanged; only which code path notices it differs, and a client cannot tell which one did.
    oversized = _oversized_issue(draft)
    if oversized is not None:
        return {"issues": [oversized.to_dict()]}
    analysis = await _analysis(request, draft)
    # Deliberately not a second `analyze`: these are cheap re-walks of the graph it already built, and
    # /validate is the editor's hottest endpoint (one call per debounced keystroke burst).
    return {"issues": [issue.to_dict() for issue in analysis.issues], "nodes": _node_analysis(analysis)}
