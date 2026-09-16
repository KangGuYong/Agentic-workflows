"""The worker: claim a run, drive the engine, write the terminal state (MVP design 5.2, 6.3)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from engine.compiler.build import CompiledWorkflow, WorkflowInvalid, compile_workflow
from engine.config import EngineConfig
from engine.db import runs as run_db
from engine.db.checkpointer import make_checkpointer
from engine.errors import ErrorCode, LeaseLost
from engine.events.publish import RedisPublisher
from engine.events.recorder import PostgresRecorder
from engine.events.writer import append_event
from engine.llm.base import LLMClient
from engine.nodes.registry import NodeRegistry, default_registry
from engine.runtime.deps import RunDeps
from engine.runtime.guard import FlagGuard
from engine.runtime.runner import ResumeRejected, RunOutcome, execute_run

log = logging.getLogger(__name__)


class Worker:
    """One process worth of execution. `owner` identifies this worker in `runs.lease_owner`."""

    def __init__(self, config: EngineConfig, pool: AsyncConnectionPool, redis: Any, *, llm: LLMClient,
                 owner: str | None = None, registry: NodeRegistry | None = None,
                 render: Any = None) -> None:
        self._config = config
        self._pool = pool
        self._redis = redis
        self._llm = llm
        self.owner = owner or f"worker-{uuid.uuid4()}"
        # One registry and one checkpointer per process: CompiledWorkflow is cached by dsl_hash alone.
        self._registry = registry or default_registry()
        self._render = render
        self._publisher = RedisPublisher(redis)
        self._checkpointer = make_checkpointer(pool, config)  # encrypted unless dev-insecure (design 5.3)
        self._compiled: dict[str, CompiledWorkflow] = {}
        self._guards: dict[str, FlagGuard] = {}
        self._tasks: set[asyncio.Task] = set()
        self._listen: AsyncConnection | None = None
        self._running = False
        self._idle = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._running = True
        self._listen = await AsyncConnection.connect(self._config.database_url, autocommit=True)
        await self._listen.execute(f"LISTEN {run_db.QUEUE_CHANNEL}")
        self._spawn(self._claim_loop())

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._listen is not None:
            await self._listen.close()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ claiming

    async def _claim_loop(self) -> None:
        in_flight: set[asyncio.Task] = set()
        while self._running:
            while self._running and len(in_flight) < self._config.worker_max_runs:
                async with self._pool.connection() as conn:
                    row = await run_db.claim_next(conn, owner=self.owner, lease_sec=self._config.lease_sec)
                if row is None:
                    break
                task = self._spawn(self._execute(row))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            await self._wait_for_work()

    async def _wait_for_work(self) -> None:
        """Sleep until something is queued or the poll interval elapses.

        The notification is an optimisation: it is sent inside the transaction that queued the run, and the
        poll covers the window where no worker was listening.
        """
        assert self._listen is not None
        try:
            async with asyncio.timeout(self._config.claim_poll_sec):
                async for _ in self._listen.notifies(stop_after=1):
                    return
        except TimeoutError:
            return
        except Exception:  # noqa: BLE001 - a dropped listener must not stop the worker
            log.warning("listen connection failed; reconnecting", exc_info=True)
            with contextlib.suppress(Exception):
                await self._listen.close()
            self._listen = await AsyncConnection.connect(self._config.database_url, autocommit=True)
            await self._listen.execute(f"LISTEN {run_db.QUEUE_CHANNEL}")

    # ------------------------------------------------------------------ one run

    async def _execute(self, row: dict[str, Any]) -> None:
        run_id = str(row["id"])
        guard = FlagGuard()
        self._guards[run_id] = guard
        try:
            compiled = await self._compile(row)
        except WorkflowInvalid as exc:  # a stored version must already be valid; treat it as a run failure
            await self._terminal(run_id, "failed", error={
                "code": str(ErrorCode.NODE_FAILED),
                "message": "저장된 워크플로를 실행할 수 없습니다",
                "details": [issue.to_dict() for issue in exc.issues[:5]],
            })
            return

        await self._event(run_id, "run_recovered" if row["recovery_count"] else "run_started")
        recorder = PostgresRecorder(self._pool, run_id, publisher=self._publisher,
                                    store_run_data=row["store_run_data"])
        deps = RunDeps(run_id=run_id, llm=self._llm, recorder=recorder, guard=guard, render=self._render)
        try:
            outcome = await execute_run(compiled, deps=deps, inputs=row["inputs"] or {},
                                        resume=row["resume_payload"])
        except LeaseLost:
            log.info("lease lost for run %s; leaving it to the new owner", run_id)
            return
        except ResumeRejected as exc:  # the API checks first, so this is a stale payload
            log.warning("resume rejected for run %s: %s", run_id, exc)
            async with self._pool.connection() as conn:
                await run_db.clear_resume_payload(conn, run_id=run_id, owner=self.owner)
            return
        except Exception as exc:  # noqa: BLE001 - infrastructure problem: let recovery retry
            log.error("run %s aborted: %s", run_id, type(exc).__name__, exc_info=True)
            async with self._pool.connection() as conn:
                await run_db.expire_lease(conn, run_id=run_id, owner=self.owner)
            return
        finally:
            self._guards.pop(run_id, None)

        await self._write_outcome(run_id, row, outcome)

    async def _write_outcome(self, run_id: str, row: dict[str, Any], outcome: RunOutcome) -> None:
        store = row["store_run_data"]
        if outcome.status == "waiting":
            waiting = outcome.waiting or {}
            async with self._pool.connection() as conn, conn.transaction():
                if await run_db.set_waiting(conn, run_id=run_id, owner=self.owner,
                                            node_id=waiting.get("nodeId", ""),
                                            exec_index=int(waiting.get("execIndex", 0))):
                    event = await append_event(conn.cursor(), run_id, "run_waiting", payload=waiting)
                else:
                    event = None
            if event:
                await self._publish(run_id, event)
            return
        await self._terminal(run_id, outcome.status, outputs=outcome.outputs, error=outcome.error,
                             clear_inputs=not store)

    async def _terminal(self, run_id: str, status: str, *, outputs: dict[str, Any] | None = None,
                        error: dict[str, Any] | None = None, clear_inputs: bool = False) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            owned = await run_db.finish(conn, run_id=run_id, owner=self.owner, status=status,
                                        outputs=outputs, error=error, clear_inputs=clear_inputs)
            if not owned:  # someone else owns the run now: write nothing at all
                return
            await run_db.close_open_node_runs(conn, run_id, "cancelled" if status == "cancelled" else "failed")
            event = await append_event(conn.cursor(), run_id, f"run_{status}",
                                       payload={"error": error} if error else {})
        await self._publish(run_id, event)

    async def _event(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            event = await append_event(conn.cursor(), run_id, event_type, payload=payload)
        await self._publish(run_id, event)

    async def _publish(self, run_id: str, event: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self._publisher.publish(run_id, event)

    async def _compile(self, row: dict[str, Any]) -> CompiledWorkflow:
        async with self._pool.connection() as conn:
            version = await run_db.get_version_dsl(conn, str(row["workflow_version_id"]))
        cached = self._compiled.get(version["dsl_hash"])
        if cached is None:
            cached = compile_workflow(version["dsl"], checkpointer=self._checkpointer, registry=self._registry)
            self._compiled[version["dsl_hash"]] = cached
        return cached
