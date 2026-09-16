"""Creating and reading runs (MVP design 8.1, 8.3, 8.4)."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request
from psycopg.errors import UniqueViolation

from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.db import runs as run_db
from engine.db import workflows as workflow_db
from engine.dsl.models import dsl_hash
from engine.events.publish import RedisPublisher
from engine.events.writer import append_event
from engine.jsondata import safe_text
from engine.validator import analyze, has_errors

log = logging.getLogger(__name__)

router = APIRouter()


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
    key = request.headers.get("idempotency-key")
    if len(json.dumps(inputs, ensure_ascii=False).encode("utf-8")) > run_db.MAX_INPUT_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE",
                       f"실행 입력이 너무 큽니다 (최대 {run_db.MAX_INPUT_BYTES // 1024}KB)")

    pool = request.app.state.pool
    try:
        async with pool.connection() as conn, conn.transaction():
            if key:
                existing = await run_db.find_by_idempotency_key(conn, workflow_id, key)
                if existing is not None:  # a double click or a retried request (design 8.4)
                    return {"runId": str(existing["id"]),
                            "versionId": str(existing["workflow_version_id"])}
            workflow = await workflow_db.lock(conn, workflow_id)
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
    async with request.app.state.pool.connection() as conn:
        rows = await run_db.list_for_workflow(conn, workflow_id)
    return {"runs": [{"id": str(row["id"]), "status": row["status"],
                      "versionId": str(row["workflow_version_id"]),
                      "createdAt": row["created_at"].isoformat(),
                      "finishedAt": row["finished_at"].isoformat() if row["finished_at"] else None,
                      "retryCount": row["retry_count"]} for row in rows]}


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
    async with request.app.state.pool.connection() as conn:
        await _run_or_404(conn, run_id)
        rows = await run_db.list_node_runs(conn, run_id)
    return {"nodeRuns": [_node_run_view(row) for row in rows]}


async def _publish(request: Request, run_id: str, event: dict[str, Any]) -> None:
    try:  # the event is already committed; losing the live copy only delays the editor (SSE gap fill)
        await RedisPublisher(request.app.state.redis).publish(run_id, event)
    except Exception:
        log.warning("publishing event failed for run %s", run_id, exc_info=True)
