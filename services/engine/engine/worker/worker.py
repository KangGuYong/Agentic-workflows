"""The worker: claim a run, drive the engine, write the terminal state (MVP design 5.2, 6.3)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from engine.compiler.build import CompiledWorkflow, WorkflowInvalid, compile_workflow
from engine.config import EngineConfig
from engine.db import runs as run_db
from engine.db.checkpointer import make_checkpointer
from engine.errors import ErrorCode, LeaseLost
from engine.events.publish import RedisPublisher, control_messages
from engine.events.recorder import PostgresRecorder
from engine.events.writer import append_event
from engine.llm.base import LLMClient
from engine.nodes.registry import NodeRegistry, default_registry
from engine.runtime.deps import RunDeps
from engine.runtime.guard import FlagGuard
from engine.runtime.runner import ResumeRejected, RunOutcome, execute_run
from engine.worker.reaper import Reaper

log = logging.getLogger(__name__)

MAX_COMPILED_CACHE = 32  # lru_cache-level cap (design 8.4); a plain FIFO eviction is enough here


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
        self._reaper = Reaper(config, pool, redis)  # every worker has one; an advisory lock picks the sweeper
        self._checkpointer = make_checkpointer(pool, config)  # encrypted unless dev-insecure (design 5.3)
        self._compiled: dict[str, CompiledWorkflow] = {}
        self._guards: dict[str, FlagGuard] = {}
        self._timed_out: set[str] = set()  # run ids the heartbeat cancelled for exceeding run_max_active_ms
        self._cancelled: set[str] = set()  # run ids the heartbeat cancelled for an authoritative cancel
        self._tasks: set[asyncio.Task] = set()
        self._listen: AsyncConnection | None = None
        self._running = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._running = True
        await self._reconnect_listen()
        self._spawn(self._claim_loop())
        self._spawn(self._control_loop())
        await self._reaper.start()

    async def stop(self) -> None:
        self._running = False
        await self._reaper.stop()  # before our own tasks: its task is not tracked in self._tasks
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
            try:
                while self._running and len(in_flight) < self._config.worker_max_runs:
                    async with self._pool.connection() as conn:
                        row = await run_db.claim_next(conn, owner=self.owner, lease_sec=self._config.lease_sec)
                    if row is None:
                        break
                    task = self._spawn(self._execute(row))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                await self._wait_for_work()
            except asyncio.CancelledError:
                raise
            except Exception:
                # a failure here (claim query, listen reconnect, ...) must retry, not end the worker
                log.exception("claim loop iteration failed; retrying after the poll interval")
                await asyncio.sleep(self._config.claim_poll_sec)

    async def _reconnect_listen(self) -> None:
        self._listen = None  # cleared first: a failed connect must not leave a dead connection in place
        listen = await AsyncConnection.connect(self._config.database_url, autocommit=True)
        await listen.execute(f"LISTEN {run_db.QUEUE_CHANNEL}")
        self._listen = listen

    async def _wait_for_work(self) -> None:
        """Sleep until something is queued or the poll interval elapses.

        The notification is an optimisation: it is sent inside the transaction that queued the run, and the
        poll covers the window where no worker was listening.
        """
        if self._listen is None:
            await self._reconnect_listen()
            return
        try:
            async for _ in self._listen.notifies(timeout=self._config.claim_poll_sec, stop_after=1):
                return
        except Exception:
            # a dropped listener must not stop the worker: close what's left and reconnect from scratch
            log.warning("listen connection failed; reconnecting", exc_info=True)
            with contextlib.suppress(Exception):
                await self._listen.close()
            await self._reconnect_listen()

    async def _control_loop(self) -> None:
        """One subscription per worker; messages are routed to the guard of a run we hold."""
        while self._running:
            try:
                async for message in control_messages(self._redis):
                    guard = self._guards.get(str(message.get("runId")))
                    if guard is not None and message.get("action") == "cancel":
                        guard.cancel()
                # control_messages only returns by exhausting pubsub.listen(), which in practice never ends
                # without raising; this sleep is just a guard against busy-spinning if it ever does.
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Redis is a convenience path; a lost cancel is still noticed by the next heartbeat poll
                log.warning("control channel dropped; resubscribing", exc_info=True)
                await asyncio.sleep(1)

    # ------------------------------------------------------------------ one run

    async def _execute(self, row: dict[str, Any]) -> None:
        run_id = str(row["id"])
        guard = FlagGuard()
        self._guards[run_id] = guard
        try:
            await self._run(run_id, row, guard)
        except asyncio.CancelledError:
            # stop() cancelled us mid-run: hand the lease back for recovery before the cancellation lands
            with contextlib.suppress(Exception):
                await asyncio.shield(self._expire_lease(run_id))
            raise
        except Exception:
            log.exception("run %s: unhandled failure escaped _execute", run_id)
            with contextlib.suppress(Exception):
                await self._expire_lease(run_id)
        finally:
            self._guards.pop(run_id, None)
            # No path through _run may leave these dangling: a stale entry would misclassify a later run
            # that reuses this run_id (A3b) or leak memory for one that doesn't.
            self._timed_out.discard(run_id)
            self._cancelled.discard(run_id)

    async def _expire_lease(self, run_id: str) -> None:
        async with self._pool.connection() as conn:
            await run_db.expire_lease(conn, run_id=run_id, owner=self.owner)

    async def _heartbeat(self, run_id: str, guard: FlagGuard, task: asyncio.Task) -> None:
        """Extend the lease, notice cancels, and stop a run that used up its active time.

        A cancel, a timeout, or a lost lease all force the run's own task to stop: `guard.check()` alone
        only fires at a node boundary (compiler/wrapper.py's comment on this), so a run blocked inside a
        single long node call (an LLM request, in particular) would otherwise ignore all three until that
        call finally returns — for a lost lease that means writing `node_runs`/`run_events`/checkpoint rows
        the new owner is concurrently writing too (design 6.2 forbids this worker writing anything at all
        once the lease is gone).
        """
        interval = self._config.heartbeat_sec
        last = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            try:
                async with self._pool.connection() as conn:
                    beat = await run_db.heartbeat(conn, run_id=run_id, owner=self.owner,
                                                  lease_sec=self._config.lease_sec,
                                                  delta_ms=int((now - last) * 1000))
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient DB failure must not end the heartbeat silently: the run's own task keeps
                # executing regardless, and needs its lease extended on the next tick or a healthy run
                # gets reclaimed by the reaper out from under it.
                log.warning("heartbeat failed for run %s; retrying next interval", run_id, exc_info=True)
                continue
            last = now
            if beat is None:  # the lease is gone: stop the run and write nothing (design 6.2)
                guard.lose_lease()
                task.cancel()  # don't wait for the next node boundary; the new owner may already be running
                return
            if beat["cancel_requested_at"] is not None:  # the authoritative fact, not the Redis fast path
                self._cancelled.add(run_id)
                guard.cancel()
                task.cancel()  # interrupt whatever the run is doing now, not just its next node boundary
                return
            if beat["active_ms"] > self._config.run_max_active_ms:
                self._timed_out.add(run_id)
                task.cancel()
                return

    async def _run(self, run_id: str, row: dict[str, Any], guard: FlagGuard) -> None:
        # The lease is held from the moment we're claimed (design 6.4 step 1), so the heartbeat starts
        # here too: a slow compile must be covered by it, not just the execute_run call (N4).
        task = asyncio.current_task()
        beat = self._spawn(self._heartbeat(run_id, guard, task))
        try:
            try:
                # The reaper owns `run_recovered`: it is the event for the act of recovering an abandoned
                # run, written once when it requeues one. Every attempt to run still starts with
                # `run_started`, carrying how many times this run has been recovered so far.
                await self._event(run_id, "run_started", payload={"recoveryCount": row["recovery_count"]})

                try:
                    compiled = await self._compile(row)
                except WorkflowInvalid as exc:  # a stored version must already be valid: a run failure
                    await self._terminal(run_id, "failed", error={
                        "code": str(ErrorCode.NODE_FAILED),
                        "message": "저장된 워크플로를 실행할 수 없습니다",
                        "details": [issue.to_dict() for issue in exc.issues[:5]],
                    })
                    return
                if compiled is None:  # the version was deleted between queueing and claiming
                    await self._terminal(run_id, "failed", error={
                        "code": str(ErrorCode.NODE_FAILED),
                        "message": "워크플로 버전을 찾을 수 없습니다",
                    })
                    return

                recorder = PostgresRecorder(self._pool, run_id, publisher=self._publisher,
                                            store_run_data=row["store_run_data"])
                deps = RunDeps(run_id=run_id, llm=self._llm, recorder=recorder, guard=guard, render=self._render)
                outcome = await execute_run(compiled, deps=deps, inputs=row["inputs"] or {},
                                            resume=row["resume_payload"])
            finally:
                # Cancelled before any handler below runs, not after (A3): a beat firing while an except
                # body is still writing the terminal state would re-extend a lease that write is trying to
                # give up (A3a), or race the classification those handlers read from `_timed_out`/
                # `_cancelled` (A3b).
                beat.cancel()
        except LeaseLost:
            log.info("lease lost for run %s; leaving it to the new owner", run_id)
            return
        except ResumeRejected as exc:  # the API checks first, so this is a stale payload
            log.warning("resume rejected for run %s: %s", run_id, exc)
            async with self._pool.connection() as conn:
                await run_db.set_waiting(conn, run_id=run_id, owner=self.owner,
                                         node_id=row["waiting_node_id"] or "",
                                         exec_index=row["waiting_exec_index"] or 0)
            return
        except asyncio.CancelledError:
            # The heartbeat forced this task to stop mid-node: figure out which of its reasons applies, in
            # order of authority. `stop()` shutting the worker down sets none of these, so it falls through
            # to the re-raise (_execute's handler expires the lease for recovery).
            if guard.lease_lost:  # design 6.2: the new owner continues; write nothing at all
                return
            clear_inputs = not row["store_run_data"]
            # asyncio.shield does not stop a second cancel from reaching this task — it would still
            # propagate out of `_run` while the shielded write below runs to completion in the background.
            # Kept anyway: the ordering is harmless and `finish`'s lease_owner fencing protects the outcome
            # either way.
            if run_id in self._timed_out:
                await asyncio.shield(self._terminal(run_id, "failed", error={
                    "code": str(ErrorCode.RUN_TIMEOUT), "message": "실행 시간이 한도를 넘었습니다"},
                    clear_inputs=clear_inputs))
                return
            if run_id in self._cancelled:
                await asyncio.shield(self._terminal(run_id, "cancelled", clear_inputs=clear_inputs))
                return
            raise
        except Exception as exc:  # infrastructure problem: let recovery retry
            log.error("run %s aborted: %s", run_id, type(exc).__name__, exc_info=True)
            await self._expire_lease(run_id)
            return

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
            closed = await run_db.close_open_node_runs(conn, run_id, "cancelled" if status == "cancelled" else "failed")
            # A node abandoned mid-call (a cancel or a timeout forcing the task to stop inside it) is
            # closed here rather than through the recorder, so it gets no node_failed event of its own —
            # without one, a consumer replaying run_events sees that node running forever (A5). Use the
            # run's own reason for the error payload; design 8.2 lists no cancel-specific code, so a cancel
            # falls back to NODE_FAILED.
            node_error = error or {"code": str(ErrorCode.NODE_FAILED), "message": "실행이 취소되었습니다"}
            for closed_row in closed:
                await append_event(conn.cursor(), run_id, "node_failed", node_id=closed_row["node_id"],
                                   exec_index=closed_row["exec_index"], attempt=closed_row["attempt"],
                                   payload={"error": node_error, "willRetry": False})
            event = await append_event(conn.cursor(), run_id, f"run_{status}",
                                       payload={"error": error} if error else {})
        await self._publish(run_id, event)

    async def _event(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            event = await append_event(conn.cursor(), run_id, event_type, payload=payload)
        await self._publish(run_id, event)

    async def _publish(self, run_id: str, event: dict[str, Any]) -> None:
        try:  # the event is already committed; losing the live copy only delays the editor (SSE gap fill)
            await self._publisher.publish(run_id, event)
        except Exception:
            log.warning("publishing event failed for run %s", run_id, exc_info=True)

    async def _compile(self, row: dict[str, Any]) -> CompiledWorkflow | None:
        async with self._pool.connection() as conn:
            version = await run_db.get_version_dsl(conn, str(row["workflow_version_id"]))
        if version is None:  # the version was deleted between queueing and claiming
            return None
        cached = self._compiled.get(version["dsl_hash"])
        if cached is None:
            cached = compile_workflow(version["dsl"], checkpointer=self._checkpointer, registry=self._registry)
            self._compiled[version["dsl_hash"]] = cached
            if len(self._compiled) > MAX_COMPILED_CACHE:
                self._compiled.pop(next(iter(self._compiled)))  # FIFO eviction of the oldest entry
        return cached
