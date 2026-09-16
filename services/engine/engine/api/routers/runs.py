"""Creating and reading runs (MVP design 8.1, 8.3, 8.4)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from psycopg.errors import UniqueViolation

from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.db import runs as run_db
from engine.db import workflows as workflow_db
from engine.dsl.models import dsl_hash
from engine.events.publish import RedisPublisher
from engine.events.stream import event_stream
from engine.events.writer import append_event
from engine.jsondata import safe_text
from engine.validator import analyze, has_errors

log = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_LIMIT = 50  # design 8.1's `?cursor&limit`, shared by both listing endpoints
MAX_LIMIT = 200


def _workflow_id(raw: str) -> str:
    """A path segment that isn't a UUID at all cannot match any row. Reject it before it ever reaches a
    query: the `id` column is uuid, so psycopg/Postgres would otherwise refuse to bind the parameter and
    raise a DataError that the generic handler turns into a 500, instead of the 404 a bad id deserves
    (same guard as `workflows._workflow_id`, Task 12 review)."""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "워크플로를 찾을 수 없습니다") from None


def _run_id(raw: str) -> str:
    """Same guard as `_workflow_id`, for the run id path segment."""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "실행을 찾을 수 없습니다") from None


def _parse_limit(request: Request) -> int:
    """`?limit=`, defaulting to DEFAULT_LIMIT and rejected outright past MAX_LIMIT rather than silently
    clamped -- a client asking for 100,000 rows almost certainly misunderstands the API, and clamping
    would hide that (Task 13 review A3/A4)."""
    raw = request.query_params.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(422, "REQUEST_ERROR", "limit의 형식이 올바르지 않습니다") from None
    if not (1 <= value <= MAX_LIMIT):
        raise ApiError(422, "REQUEST_ERROR", f"limit은 1~{MAX_LIMIT} 사이여야 합니다")
    return value


MAX_AFTER_DIGITS = 20  # comfortably covers any real `seq` (bigint, max 19 digits) with room to spare


def _parse_after(request: Request) -> int:
    """`Last-Event-ID` (a reconnect) or `?after=` (a first connect), defaulting to 0 -- a full replay.

    Both are client-controlled strings that only need to pass `.isdigit()` before reaching `int()` in the
    plan's own sketch, and that is not enough: `str.isdigit()` is also true for non-ASCII decimal-ish
    characters (e.g. superscript digits) that `int()` cannot parse and raises `ValueError` on, exactly the
    `Content-Length: "²"` bug the Task 11 review found in a different header. A digit string with no
    length bound is also free CPU: `int()` on an enormous digit string is quadratic in its length. Neither
    case is an error worth surfacing to the caller -- an SSE reconnect id that fails to parse should just
    fall back to a full replay, not a 4xx -- so anything that is not a short run of ASCII digits is
    treated the same as "absent" rather than raising (same reasoning as the plan's `.isdigit()` fallback,
    just closing the two gaps in it)."""
    raw = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    if raw.isascii() and raw.isdigit() and len(raw) <= MAX_AFTER_DIGITS:
        return int(raw)
    return 0


def _encode_cursor(created_at: datetime, run_id: str) -> str:
    """Opaque keyset cursor: the `(created_at, id)` of the last row on a page (Task 13 review A4)."""
    raw = f"{created_at.isoformat()}|{run_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(raw: str) -> tuple[datetime, str]:
    """The inverse of `_encode_cursor`. Anything that doesn't round-trip -- bad base64, a reordered or
    hand-edited payload, an id that isn't a UUID -- is a malformed cursor: a 422, never a 500 from a
    DataError deep in the query (same reasoning as `_workflow_id`/`_run_id`)."""
    try:
        created_at_raw, run_id_raw = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8").split("|", 1)
        return datetime.fromisoformat(created_at_raw), str(uuid.UUID(run_id_raw))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ApiError(422, "REQUEST_ERROR", "cursor의 형식이 올바르지 않습니다") from None


def _sanitize(value: Any) -> Any:
    """Scrub NUL/lone-surrogate text out of a JSON value read back from the database (mirrors
    `workflows._sanitize`). `inputs` arriving through `create_run` are already checked by `read_json`
    (Task 11), but `outputs`/`error`/the waiting payload are written by the worker, a separate process,
    and a row written by any other path -- a migration, a test fixture, a future writer that forgets to
    sanitize -- could still hold text that breaks response serialization (a lone surrogate raises
    UnicodeEncodeError from inside Starlette's JSONResponse, which becomes a bare 500)."""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {safe_text(key): _sanitize(item) for key, item in value.items()}
    return value


def _run_view(row: dict[str, Any], waiting: dict[str, Any] | None = None) -> dict[str, Any]:
    view = {
        "id": str(row["id"]),
        "status": row["status"],
        "versionId": str(row["workflow_version_id"]),
        "inputs": _sanitize(row["inputs"]),
        "outputs": _sanitize(row["outputs"]),
        "error": _sanitize(row["error"]),
        "cancelRequested": row["cancel_requested_at"] is not None,
        "retryCount": row["retry_count"],
        "recoveryCount": row["recovery_count"],
        "createdAt": row["created_at"].isoformat(),
        "startedAt": row["started_at"].isoformat() if row["started_at"] else None,
        "finishedAt": row["finished_at"].isoformat() if row["finished_at"] else None,
    }
    if waiting is not None:
        view["waitingFor"] = _sanitize(waiting)
    return view


def _node_run_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodeId": row["node_id"], "execIndex": row["exec_index"], "attempt": row["attempt"],
        "status": row["status"], "input": _sanitize(row["input"]), "output": _sanitize(row["output"]),
        "error": _sanitize(row["error"]), "meta": _sanitize(row["meta"]), "truncated": row["truncated"],
        "tokensIn": row["tokens_in"], "tokensOut": row["tokens_out"],
        "startedAt": row["started_at"].isoformat(),
        "finishedAt": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


async def _run_or_404(conn, run_id: str) -> dict[str, Any]:
    row = await run_db.get_run(conn, run_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "실행을 찾을 수 없습니다")
    return row


@router.post("/workflows/{workflow_id}/runs", status_code=202)
async def create_run(workflow_id: str, request: Request) -> dict[str, Any]:
    workflow_id = _workflow_id(workflow_id)
    body = require_object(await read_json(request))
    inputs = field(body, "inputs", dict, required=False, default={}) or {}
    revision = field(body, "revision", int)
    # A present-but-blank header ("" or whitespace) is not a key. h11 hands it to us as an empty string,
    # which is falsy -- `if key:` below correctly skips the idempotency lookup for it -- but it is not
    # NULL, so unless it is normalised away here it still gets written by insert_queued and *does*
    # participate in runs_idempotency_idx (a partial unique index with `WHERE idempotency_key IS NOT
    # NULL`): every later create from a client that always sends the header blank then collides with the
    # first one forever and the UniqueViolation recovery below answers with that first run's id (Task 13
    # review A1). Normalising to None keeps a blank header out of the index exactly like an absent one.
    key = (request.headers.get("idempotency-key") or "").strip() or None
    if key is not None and len(key.encode("utf-8")) > run_db.MAX_IDEMPOTENCY_KEY_BYTES:
        # Half of runs_idempotency_idx's btree key. h11 allows header values up to ~16KB, so a client can
        # reach Postgres's own hard limit here: `ProgramLimitExceeded: index row size ... exceeds btree
        # version 4 maximum`, which is not a UniqueViolation and so was falling through the except clause
        # below as a bare 500 (Task 13 review A2). Reject it before it ever reaches the index.
        raise ApiError(422, "REQUEST_ERROR",
                       f"Idempotency-Key가 너무 깁니다 (최대 {run_db.MAX_IDEMPOTENCY_KEY_BYTES}B)")
    if len(json.dumps(inputs, ensure_ascii=False).encode("utf-8")) > run_db.MAX_INPUT_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE",
                       f"실행 입력이 너무 큽니다 (최대 {run_db.MAX_INPUT_BYTES // 1024}KB)")

    pool = request.app.state.pool

    # Read the draft (and check the idempotency fast path) *before* opening a transaction, and analyse off
    # the lock entirely (Task 13 review A5). analyze() used to run inside the same transaction as
    # workflow_db.lock's FOR UPDATE -- a CPU-bound asyncio.to_thread hop while holding both a pooled
    # connection and the workflow's row lock -- so a concurrent autosave PUT (which needs that same lock)
    # queued up behind it, and every other request sharing the pool (including /healthz) queued up behind
    # the borrowed connection. `revision` bumps on every successful save (workflow_db.save_draft), so
    # re-checking it after taking the lock below is exactly as strong as holding the lock across the
    # analysis: an unchanged revision means an unchanged draft (nothing to re-analyse), and a changed one
    # is caught as the same 409 before anything is pinned or inserted.
    async with pool.connection() as conn:
        if key:
            existing = await run_db.find_by_idempotency_key(conn, workflow_id, key)
            if existing is not None:  # a double click or a retried request (design 8.4)
                return {"runId": str(existing["id"]), "versionId": str(existing["workflow_version_id"])}
        workflow = await workflow_db.get(conn, workflow_id)
        if workflow is None:
            raise ApiError(404, "NOT_FOUND", "워크플로를 찾을 수 없습니다")
        if workflow["revision"] != revision:
            raise ApiError(409, "REVISION_CONFLICT", "다른 곳에서 먼저 저장했습니다",
                           {"currentRevision": workflow["revision"]})
        draft = workflow["draft_dsl"]

    analysis = await asyncio.to_thread(analyze, draft, request.app.state.registry)
    if has_errors(analysis.issues) or analysis.dsl is None:
        raise ApiError(422, "VALIDATION_FAILED", "워크플로에 오류가 있습니다",
                       {"issues": [issue.to_dict() for issue in analysis.issues]})

    try:
        async with pool.connection() as conn, conn.transaction():
            workflow = await workflow_db.lock(conn, workflow_id)
            if workflow is None:
                raise ApiError(404, "NOT_FOUND", "워크플로를 찾을 수 없습니다")
            if workflow["revision"] != revision:
                # Something saved between the pre-lock read above and here: the draft this run would pin
                # is already stale, so re-analysing it would be wasted work either way. Task 12's delete
                # race stays closed regardless of where the lock starts -- the INSERT into runs below still
                # takes FOR KEY SHARE on this row through its workflow_id FK, which is what actually
                # serialises against a concurrent delete_workflow's FOR UPDATE, not the analysis step.
                raise ApiError(409, "REVISION_CONFLICT", "다른 곳에서 먼저 저장했습니다",
                               {"currentRevision": workflow["revision"]})
            version = await workflow_db.pin_version(conn, workflow_id=workflow_id, dsl=draft,
                                                    dsl_hash=dsl_hash(analysis.dsl))
            run = await run_db.insert_queued(
                conn, workflow_id=workflow_id, version_id=str(version["id"]),
                workspace_id=str(workflow["workspace_id"]), inputs=inputs, idempotency_key=key,
                store_run_data=analysis.dsl.settings.storeRunData,
            )
            event = await append_event(conn.cursor(), str(run["id"]), "run_queued",
                                       payload={"versionNo": version["version_no"]})
            await run_db.notify_queued(conn, str(run["id"]))  # inside the transaction: never fires early
    except UniqueViolation:  # two requests with the same key raced; the winner's row is the answer
        async with pool.connection() as conn:
            existing = await run_db.find_by_idempotency_key(conn, workflow_id, key or "")
        if existing is None:
            raise
        return {"runId": str(existing["id"]), "versionId": str(existing["workflow_version_id"])}

    await _publish(request, str(run["id"]), event)
    return {"runId": str(run["id"]), "versionId": str(version["id"])}


@router.get("/workflows/{workflow_id}/runs")
async def list_runs(workflow_id: str, request: Request) -> dict[str, Any]:
    workflow_id = _workflow_id(workflow_id)
    limit = _parse_limit(request)
    raw_cursor = request.query_params.get("cursor")
    cursor = _decode_cursor(raw_cursor) if raw_cursor else None
    async with request.app.state.pool.connection() as conn:
        # Fetch one extra row: its presence, not a second COUNT query, is how we know there is a next page.
        rows = await run_db.list_for_workflow(conn, workflow_id, limit=limit + 1, cursor=cursor)
    page, has_more = rows[:limit], len(rows) > limit
    next_cursor = _encode_cursor(page[-1]["created_at"], str(page[-1]["id"])) if has_more else None
    return {"runs": [{"id": str(row["id"]), "status": row["status"],
                      "versionId": str(row["workflow_version_id"]),
                      "createdAt": row["created_at"].isoformat(),
                      "finishedAt": row["finished_at"].isoformat() if row["finished_at"] else None,
                      "retryCount": row["retry_count"]} for row in page],
            "nextCursor": next_cursor}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    run_id = _run_id(run_id)
    async with request.app.state.pool.connection() as conn:
        row = await _run_or_404(conn, run_id)
        waiting = None
        if row["status"] == "waiting" and row["waiting_node_id"]:
            waiting = await run_db.waiting_payload(conn, run_id, row["waiting_node_id"],
                                                   row["waiting_exec_index"])
    return _run_view(row, waiting)


@router.get("/runs/{run_id}/nodes")
async def get_node_runs(run_id: str, request: Request) -> dict[str, Any]:
    run_id = _run_id(run_id)
    limit = _parse_limit(request)
    async with request.app.state.pool.connection() as conn:
        await _run_or_404(conn, run_id)
        # Fetch one extra row: its presence, not a second COUNT query, is how we know there is more.
        rows = await run_db.list_node_runs(conn, run_id, limit=limit + 1)
    page, has_more = rows[:limit], len(rows) > limit
    return {"nodeRuns": [_node_run_view(row) for row in page], "hasMore": has_more}


@router.get("/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    """Live events for one run over SSE (design 7.3): subscribes on Redis, then replays what is already
    in Postgres, then plays the live channel, filling any gap from Postgres and dropping the oldest queued
    event if the client falls behind -- see `engine.events.stream` for why both are safe.

    Same UUID guard and `_run_or_404` as every other route in this file, checked on a connection borrowed
    just for that lookup and released before streaming starts -- unlike the ordinary JSON routes, this one
    then holds a *separate* Redis pubsub connection open for as long as the client keeps reading, so the
    number of concurrent streams this process can serve is bounded by the Redis client's own connection
    pool, not by anything here. `request.app.state.pool` (Postgres) is only borrowed briefly, to page in
    stored events, exactly like `get_node_runs` above.
    """
    run_id = _run_id(run_id)
    async with request.app.state.pool.connection() as conn:
        await _run_or_404(conn, run_id)
    after = _parse_after(request)
    stream = event_stream(request.app.state.pool, request.app.state.redis, run_id, after=after)
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _publish(request: Request, run_id: str, event: dict[str, Any]) -> None:
    try:  # the event is already committed; losing the live copy only delays the editor (SSE gap fill)
        await RedisPublisher(request.app.state.redis).publish(run_id, event)
    except Exception:
        log.warning("publishing event failed for run %s", run_id, exc_info=True)
