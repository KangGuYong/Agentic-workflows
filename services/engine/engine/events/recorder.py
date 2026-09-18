"""Postgres implementation of the Plan 1 Recorder protocol (node_runs + run_events)."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from engine.events.redact import MAX_EVENT_PREVIEW_BYTES, MAX_STORED_BYTES, clip_json, redact
from engine.events.writer import append_event
from engine.jsondata import check_text
from engine.nodes.base import Usage
from engine.runtime.recorder import DuplicateAttempt

log = logging.getLogger(__name__)


class RecorderInconsistent(RuntimeError):
    """A close arrived for an attempt that was never opened. The wrapper turns this into an EngineFault."""


class PostgresRecorder:
    """One instance per run. Writes are single transactions; publishing happens after the commit."""

    def __init__(self, pool: AsyncConnectionPool, run_id: str, *, publisher: Any = None,
                 store_run_data: bool = True) -> None:
        self._pool = pool
        self._run_id = run_id
        self._publisher = publisher
        self._store = store_run_data

    # ------------------------------------------------------------------ reads

    async def attempts_so_far(self, node_id: str, exec_index: int) -> int:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT count(*) AS n FROM node_runs WHERE run_id=%s AND node_id=%s AND exec_index=%s",
                (self._run_id, node_id, exec_index),
            )).fetchone()
        return int(row["n"])

    async def find_waiting(self, node_id: str, exec_index: int) -> int | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT max(attempt) AS attempt FROM node_runs"
                " WHERE run_id=%s AND node_id=%s AND exec_index=%s AND waited",
                (self._run_id, node_id, exec_index),
            )).fetchone()
        return row["attempt"]

    # ------------------------------------------------------------------ writes

    async def node_started(self, node_id: str, exec_index: int, attempt: int, input: dict[str, Any] | None) -> None:
        stored, truncated = self._value(input)
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            await cursor.execute(
                "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status, input, truncated)"
                " VALUES (%s, %s, %s, %s, %s, 'running', %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (str(uuid.uuid4()), self._run_id, node_id, exec_index, attempt,
                 Jsonb(stored) if stored is not None else None, truncated),
            )
            if await cursor.fetchone() is None:
                # The unique key already holds this attempt: another worker is running the same run.
                raise DuplicateAttempt((node_id, exec_index, attempt))
            event = await append_event(cursor, self._run_id, "node_started", node_id=node_id,
                                       exec_index=exec_index, attempt=attempt)
        await self._publish(event)

    async def node_succeeded(self, node_id: str, exec_index: int, attempt: int, output: dict[str, Any],
                             usage: Usage, *, defaulted: bool, meta: dict[str, Any]) -> None:
        value, truncated = self._value(output)
        preview = self._preview(output)
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            await self._close(cursor, node_id, exec_index, attempt,
                              "defaulted" if defaulted else "succeeded",
                              output=value, truncated=truncated, usage=usage, meta=meta)
            payload = {"defaulted": defaulted, "tokensOut": usage.tokens_out, **meta}
            if preview is not None:
                payload["outputPreview"] = preview
            event = await append_event(cursor, self._run_id, "node_finished", node_id=node_id,
                                       exec_index=exec_index, attempt=attempt, payload=payload)
        await self._publish(event)

    async def node_failed(self, node_id: str, exec_index: int, attempt: int, error: dict[str, Any],
                          *, will_retry: bool) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            safe_error = self._safe(error, MAX_EVENT_PREVIEW_BYTES)
            await self._close(cursor, node_id, exec_index, attempt, "failed", error=error)
            event = await append_event(cursor, self._run_id, "node_failed", node_id=node_id,
                                       exec_index=exec_index, attempt=attempt,
                                       # the reason a node failed is metadata: kept even without run data,
                                       # but redacted and clipped like any other payload
                                       payload={"error": safe_error, "willRetry": will_retry})
        await self._publish(event)

    async def node_waiting(self, node_id: str, exec_index: int, attempt: int, payload: dict[str, Any]) -> None:
        # Stored even when run data is not kept: without it the approval cannot be answered or shown.
        stored, _ = clip_json(self._safe(payload), MAX_STORED_BYTES)
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            await cursor.execute(
                "UPDATE node_runs SET status='waiting', waited=true, meta=%s"
                " WHERE run_id=%s AND node_id=%s AND exec_index=%s AND attempt=%s",
                (Jsonb({"waiting": stored}), self._run_id, node_id, exec_index, attempt),
            )
            if cursor.rowcount == 0:
                raise RecorderInconsistent((node_id, exec_index, attempt))
            # the event body is a preview like any other: omitted entirely when run data is not kept
            event = await append_event(cursor, self._run_id, "node_waiting", node_id=node_id,
                                       exec_index=exec_index, attempt=attempt, payload=self._preview(payload))
        await self._publish(event)

    async def node_token(self, node_id: str, exec_index: int, text: str) -> None:
        """Transient (spec 7.1): published for the live preview, never stored and never numbered."""
        await self._publish({"runId": self._run_id, "type": "node_token", "nodeId": node_id,
                             "execIndex": exec_index, "payload": {"text": text}})

    # ------------------------------------------------------------------ helpers

    async def _close(self, cursor, node_id: str, exec_index: int, attempt: int, status: str, *,
                     output: Any = None, truncated: bool = False, usage: Usage | None = None,
                     meta: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
        """Close one attempt.

        Fenced to `status IN ('running','waiting')` (A4 of the whole-branch review): matching purely on
        `(run_id, node_id, exec_index, attempt)`, as this used to, let a worker whose heartbeat stalled
        through a Postgres blip resurrect a row the reaper had already closed. Scenario: the blip outlives
        `lease_sec` (the heartbeat cannot learn the lease is gone while the database is unreachable), the
        reaper requeues the run and closes this attempt `failed` via `close_open_node_runs`, a second
        worker starts a new attempt on the same node -- and then the first worker's call finally returns
        and this same `_close` runs, flipping the row back to `succeeded` *after* a newer attempt is
        already running. 'running' is the ordinary case; 'waiting' is a resumed `human_approval` attempt
        being closed for the first time (its row was left `waiting` by `node_waiting` and has not reached
        `_close` before). A row already closed -- by the reaper/a cancel, or by this same attempt's own
        earlier close -- now makes this UPDATE match no row: `RecorderInconsistent` below, which
        `_recorded` (compiler/wrapper.py) turns into an `EngineFault`, exactly the "give up, let recovery
        retry" outcome a losing write should get.

        A close that writes the status the row already holds is still allowed, so the one case the old
        leniency existed for -- a resumed `human_approval` attempt replayed after a crash landed between
        its own commit and the graph's checkpoint commit -- stays idempotent. That replay is deterministic:
        rejecting it would fail the same way on every recovery until `MAX_RECOVERIES` gave up, turning a
        survivable crash into a permanently failed run. Only a *conflicting* close is refused, which is
        precisely the resurrection above (the reaper wrote `failed`, the stale worker writes `succeeded`).
        """
        usage = usage or Usage()
        await cursor.execute(
            "UPDATE node_runs SET status=%s, output=%s, error=%s, meta=%s, tokens_in=%s, tokens_out=%s,"
            " truncated=node_runs.truncated OR %s, finished_at=now()"
            " WHERE run_id=%s AND node_id=%s AND exec_index=%s AND attempt=%s"
            "   AND (status IN ('running','waiting') OR status=%s)",
            (status, Jsonb(output) if output is not None else None,
             Jsonb(redact(error)) if error is not None else None,
             Jsonb(meta) if meta else None, usage.tokens_in, usage.tokens_out, truncated,
             self._run_id, node_id, exec_index, attempt, status),
        )
        if cursor.rowcount == 0:
            raise RecorderInconsistent((node_id, exec_index, attempt))

    def _value(self, value: Any) -> tuple[Any, bool]:
        if value is None or not self._store:
            return None, False
        return clip_json(self._safe(value), MAX_STORED_BYTES)

    def _preview(self, value: Any) -> Any:
        """Event bodies are previews of stored data, so they follow the same rules."""
        if not self._store or value is None:
            return None
        return self._safe(value, MAX_EVENT_PREVIEW_BYTES)

    @staticmethod
    def _safe(value: Any, limit: int | None = None) -> Any:
        """Redacted, and checked for text jsonb refuses. The engine already rejects NUL and lone surrogates
        in node outputs; this is the last barrier before the column, as the in-memory recorder does."""
        safe = redact(value)
        check_text(safe)
        return safe if limit is None else clip_json(safe, limit)[0]

    async def _publish(self, event: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        try:  # the event is already committed; losing the live copy only delays the editor (SSE gap fill)
            await self._publisher.publish(self._run_id, event)
        except Exception:  # noqa: BLE001
            log.warning("publishing event failed for run %s", self._run_id, exc_info=True)
