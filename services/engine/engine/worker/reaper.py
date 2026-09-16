"""Recovers runs whose worker died, and expires approvals nobody answered (MVP design 5.2).

Runs inside every worker, but a Postgres advisory lock lets only one of them sweep at a time.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from engine.config import EngineConfig
from engine.db import runs as run_db
from engine.errors import ErrorCode
from engine.events.publish import RedisPublisher
from engine.events.writer import append_event

log = logging.getLogger(__name__)

LOCK_KEY = 8_273_441_002  # arbitrary, stable: identifies "the reaper" among advisory locks
MAX_RECOVERIES = 3
WAITING_MAX_DAYS = 30
BATCH = 50

# Short, fixed reasons for the node_failed event closing an attempt the dead worker left open. The run's
# own error (when it has one) is used instead of these on the recovery-exhausted path (see _recover_one).
_CANCELLED_NODE_ERROR = {"code": str(ErrorCode.NODE_FAILED), "message": "실행이 취소되었습니다"}
_RECOVERED_NODE_ERROR = {"code": str(ErrorCode.NODE_FAILED), "message": "작업자가 응답하지 않아 실행을 복구합니다"}


class Reaper:
    def __init__(self, config: EngineConfig, pool: AsyncConnectionPool, redis: Any) -> None:
        self._config = config
        self._pool = pool
        self._publisher = RedisPublisher(redis)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.reaper_interval_sec)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                # a failed sweep must not stop the worker: the next tick tries again
                log.warning("reaper sweep failed", exc_info=True)

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[bool]:
        """Session-level advisory lock so several workers do not recover the same runs."""
        async with self._pool.connection() as conn:
            row = await (await conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,))).fetchone()
            acquired = bool(row["ok"])
            try:
                yield acquired
            finally:
                if acquired:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))

    async def sweep(self) -> None:
        async with self.exclusive() as acquired:
            if not acquired:
                return
            await self._recover_expired()
            await self._expire_waiting()

    async def _recover_expired(self) -> None:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id, recovery_count, cancel_requested_at, store_run_data FROM runs"
                " WHERE status='running' AND lease_expires_at < now() ORDER BY lease_expires_at LIMIT %s",
                (BATCH,),
            )).fetchall()
        for row in rows:
            await self._recover_one(str(row["id"]), row)

    async def _recover_one(self, run_id: str, row: dict[str, Any]) -> None:
        # store_run_data governs the two terminal paths below (cancelled, recovery-exhausted) the same way
        # Worker._terminal honours it: the run is done, so its inputs are dropped unless the caller asked
        # to keep them. The requeue path is not terminal — the run still needs its inputs to run again.
        clear_inputs = not row["store_run_data"]
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            if row["cancel_requested_at"] is not None:
                changed = await self._take(cursor, run_id, "cancelled", finished=True, clear_inputs=clear_inputs)
                event_type, payload = "run_cancelled", {}
                node_error = _CANCELLED_NODE_ERROR
            elif row["recovery_count"] >= MAX_RECOVERIES:
                error = {"code": str(ErrorCode.ENGINE_RECOVERY_EXHAUSTED),
                         "message": "실행을 여러 번 복구했지만 끝내지 못했습니다"}
                changed = await self._take(cursor, run_id, "failed", finished=True, error=error,
                                           clear_inputs=clear_inputs)
                event_type, payload = "run_failed", {"error": error}
                node_error = error  # the run's own reason, not a generic one
            else:
                changed = await self._take(cursor, run_id, "queued", finished=False)
                event_type = "run_recovered"
                payload = {"recoveryCount": row["recovery_count"] + 1}
                node_error = _RECOVERED_NODE_ERROR
            if not changed:  # a worker renewed the lease between our select and update
                return
            closed = await run_db.close_open_node_runs(
                conn, run_id, "cancelled" if event_type == "run_cancelled" else "failed"
            )
            # A node the dead worker left "running" is closed here, not through the recorder, so it gets
            # no node_failed event of its own — without one, a consumer replaying run_events sees that node
            # running forever, whether the run itself ended up recovering or actually terminating.
            for closed_row in closed:
                await append_event(cursor, run_id, "node_failed", node_id=closed_row["node_id"],
                                   exec_index=closed_row["exec_index"], attempt=closed_row["attempt"],
                                   payload={"error": node_error, "willRetry": False})
            if event_type == "run_recovered":
                await run_db.notify_queued(conn, run_id)
            event = await append_event(cursor, run_id, event_type, payload=payload)
        with contextlib.suppress(Exception):
            await self._publisher.publish(run_id, event)

    async def _take(self, cursor, run_id: str, status: str, *, finished: bool, clear_inputs: bool = False,
                    error: dict[str, Any] | None = None) -> bool:
        await cursor.execute(
            "UPDATE runs SET status=%(status)s, lease_owner=NULL, lease_expires_at=NULL,"
            "   recovery_count = recovery_count + CASE WHEN %(status)s='queued' THEN 1 ELSE 0 END,"
            "   error = COALESCE(%(error)s, error),"
            "   inputs = CASE WHEN %(clear)s THEN NULL ELSE inputs END,"
            "   finished_at = CASE WHEN %(finished)s THEN now() ELSE finished_at END, updated_at=now()"
            " WHERE id=%(id)s AND status='running' AND lease_expires_at < now() RETURNING id",
            {"id": run_id, "status": status, "finished": finished, "clear": clear_inputs,
             "error": Jsonb(error) if error else None},
        )
        return await cursor.fetchone() is not None

    async def _expire_waiting(self) -> None:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id FROM runs WHERE status='waiting'"
                " AND updated_at < now() - make_interval(days => %s) LIMIT %s",
                (WAITING_MAX_DAYS, BATCH),
            )).fetchall()
        for row in rows:
            run_id = str(row["id"])
            async with self._pool.connection() as conn, conn.transaction():
                cursor = conn.cursor()
                await cursor.execute(
                    "UPDATE runs SET status='cancelled', finished_at=now(), updated_at=now()"
                    " WHERE id=%s AND status='waiting' RETURNING id", (run_id,))
                if await cursor.fetchone() is None:
                    continue
                # A waiting run has no node_runs row left "running" (the paused node's own row is recorded
                # as "waiting", not "running" — runtime/recorder.py), so this normally closes nothing; kept
                # for the same reason as every other terminal path: no attempt may be left open silently.
                await run_db.close_open_node_runs(conn, run_id, "cancelled")
                event = await append_event(cursor, run_id, "run_cancelled",
                                           payload={"reason": "waiting_expired"})
            with contextlib.suppress(Exception):
                await self._publisher.publish(run_id, event)
