import asyncio
import contextlib
import time

from engine.db import runs as run_db
from engine.events.publish import RedisPublisher
from engine.llm.base import ChatResult
from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.factories import make_run
from tests.test_worker_run import CHAIN


async def _run_row(pool, run_id):
    async with pool.connection() as conn:
        return await run_db.get_run(conn, run_id)


class _SlowLLM:
    """Blocks inside the node so the test can interfere while the run is in flight."""

    def __init__(self, seconds: float = 30.0) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._seconds = seconds

    async def chat(self, **kwargs):
        self.started.set()
        try:
            await asyncio.sleep(self._seconds)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("should have been cancelled")


class _GatedLLM:
    """Blocks inside the node until the test releases it, so a boundary check can land in between."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()

    async def chat(self, **kwargs):
        self.started.set()
        await self.proceed.wait()
        return ChatResult(text="요약본")


async def test_a_cancel_request_stops_a_running_run(pool, redis, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM()
    await worker_factory(llm)
    await asyncio.wait_for(llm.started.wait(), 10)

    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET cancel_requested_at=now() WHERE id=%s", (run_id,))
    await RedisPublisher(redis).request_cancel(run_id)

    row = await until(lambda: _finished(pool, run_id))
    assert row["status"] == "cancelled"
    async with pool.connection() as conn:
        open_rows = await (await conn.execute(
            "SELECT count(*) AS n FROM node_runs WHERE run_id=%s AND status='running'", (run_id,))).fetchone()
    assert open_rows["n"] == 0


# B1: a worker that loses its lease mid-node must stop promptly (via task.cancel(), not just the next node
# boundary) and write nothing at all for this run: no node_runs row, no run_events row, no checkpoint row.
# The previous version of this test used a node fast enough to finish on its own, so it never actually
# exercised guard.lose_lease()/task.cancel() — it passed even with that code deleted.
async def test_a_worker_that_lost_its_lease_writes_nothing(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(seconds=30.0)  # long enough that only a forced cancel could end the run promptly
    worker = await worker_factory(llm)
    await asyncio.wait_for(llm.started.wait(), 10)

    # `llm.started` only means the "llm_1" node's own call began; the "start" node's own (legitimate,
    # pre-theft) checkpoint write can still be landing in the background at that instant. Wait for the
    # counts to go quiet before treating them as the pre-theft baseline, or that trailing write gets
    # miscounted as something the old owner wrote *after* losing the lease.
    before = await _settled_counts(pool, run_id)

    theft_started = time.monotonic()
    async with pool.connection() as conn:  # another worker steals the run mid-node
        await conn.execute("UPDATE runs SET lease_owner='thief', lease_expires_at=now() + interval '1 hour'"
                           " WHERE id=%s", (run_id,))

    await until(lambda: _guard_gone(worker, run_id))
    elapsed = time.monotonic() - theft_started
    assert elapsed < 5.0  # promptly stopped, nowhere near the 30s the node would have taken to return

    row = await _run_row(pool, run_id)
    assert (row["status"], row["lease_owner"]) == ("running", "thief")  # untouched by the old owner

    after = await _trace_counts(pool, run_id)
    assert after == before  # no node_runs, run_events or checkpoint* row written after the theft


# B2: a cancel that only reaches Redis (never written to `cancel_requested_at`) must still end the run, via
# the guard's boundary check rather than a forced task.cancel() — nothing before this covered _control_loop.
async def test_a_redis_only_cancel_ends_the_run_at_the_next_node_boundary(pool, redis, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _GatedLLM()
    worker = await worker_factory(llm)
    await asyncio.wait_for(llm.started.wait(), 10)

    await RedisPublisher(redis).request_cancel(run_id)
    await until(lambda: _guard_cancelled(worker, run_id))  # control_loop has set the flag

    llm.proceed.set()  # let llm_1 return; the "end" node's guard.check() sees the flag next

    row = await until(lambda: _finished(pool, run_id))
    assert row["status"] == "cancelled"
    assert row["cancel_requested_at"] is None  # this was a Redis-only cancel, never written to the row


# B3: a worker shutdown that follows a Redis-only cancel must not turn into an irreversible `cancelled`
# write — the run has to be left `running` with an expired lease for the reaper to pick up.
async def test_a_shutdown_after_a_redis_only_cancel_leaves_the_run_for_recovery(pool, redis, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(seconds=30.0)  # blocked in the node: only the guard flag is set, no forced task.cancel()
    worker = await worker_factory(llm)
    await asyncio.wait_for(llm.started.wait(), 10)

    await RedisPublisher(redis).request_cancel(run_id)
    await until(lambda: _guard_cancelled(worker, run_id))

    await worker.stop()

    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT status, lease_expires_at <= now() AS expired, cancel_requested_at FROM runs WHERE id=%s",
            (run_id,))).fetchone()
    assert row["status"] == "running"
    assert row["expired"] is True
    assert row["cancel_requested_at"] is None


# B4: one failing `run_db.heartbeat` call must not silently end the heartbeat task — later ticks have to
# keep extending the lease so a run that is still executing normally isn't reclaimed out from under itself.
async def test_the_heartbeat_survives_a_transient_database_error(pool, worker_factory, monkeypatch):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})

    original = run_db.heartbeat
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db hiccup")
        return await original(*args, **kwargs)

    monkeypatch.setattr(run_db, "heartbeat", flaky)
    # heartbeat_sec defaults to 0.2s in worker_factory; a 0.7s node call spans several ticks
    await worker_factory(ScriptedLLM(["요약본"], delay=0.7))

    row = await until(lambda: _finished(pool, run_id))
    assert row["status"] == "succeeded"
    assert calls["n"] >= 2  # the failed beat did not end the loop: later ticks were retried


# B5: after execute_run raises (an infrastructure fault), the lease must be expired once and left alone —
# not pushed back out by a heartbeat that is fenced on lease_owner but not on the lease having expired.
async def test_the_lease_is_expired_not_re_extended_after_an_infrastructure_failure(pool, worker_factory,
                                                                                     monkeypatch):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})

    async def broken(*args, **kwargs):
        raise RuntimeError("infra boom")

    monkeypatch.setattr("engine.worker.worker.execute_run", broken)

    # execute_run raises essentially instantly, so the real expire_lease write and the heartbeat being
    # cancelled both land within microseconds of each other — too tight a window for a real race to show
    # up reliably either way. Hold the write's caller open for a bit after it lands, long enough to span a
    # heartbeat_sec (0.2s, fixed by worker_factory): a heartbeat still alive at that point (the pre-A3
    # ordering) gets a real chance to fire during the hold and re-extend what was just expired.
    original_expire = run_db.expire_lease

    async def expire_then_hold(*args, **kwargs):
        result = await original_expire(*args, **kwargs)
        await asyncio.sleep(0.5)
        return result

    monkeypatch.setattr(run_db, "expire_lease", expire_then_hold)
    await worker_factory(ScriptedLLM(["요약본"]))  # worker_factory fixes heartbeat_sec at 0.2s

    row = await _stays_expired(pool, run_id, span_sec=0.6)
    assert row["status"] == "running"


# B6: `_timed_out` and `_cancelled` must never keep an entry once the run that put it there is over —
# otherwise a later claim of the same run id can be misclassified (A3b).
async def test_timed_out_and_cancelled_sets_are_empty_after_every_run(pool, worker_factory, monkeypatch):
    run_a = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})

    async def broken(*args, **kwargs):
        raise RuntimeError("infra boom")

    monkeypatch.setattr("engine.worker.worker.execute_run", broken)
    worker_a = await worker_factory(ScriptedLLM(["요약본"]), owner="worker-infra")

    async def infra_expired():
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT lease_expires_at <= now() AS expired FROM runs WHERE id=%s", (run_a,))).fetchone()
        return row if row["expired"] else None

    await until(infra_expired)
    # The DB write above and `_execute`'s own `finally` cleanup (which pops `_guards` and clears
    # `_timed_out`/`_cancelled` together, with no `await` between them) are two different things:
    # `expire_lease`'s commit is visible to this polling task before the worker's task has necessarily
    # resumed past its own `await` and reached that `finally` block. Waiting for the DB row alone (A6: this
    # used to flake here) races that gap; `_guard_gone` waits for the observable state the assertions
    # actually depend on instead of assuming it settles within one `until()` interval.
    await until(lambda: _guard_gone(worker_a, run_a))
    assert worker_a._timed_out == set()
    assert worker_a._cancelled == set()
    monkeypatch.undo()  # restore the real execute_run for the timeout scenario below

    run_b = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm_b = _SlowLLM()
    worker_b = await worker_factory(llm_b, run_max_active_ms=300, owner="worker-timeout")
    await asyncio.wait_for(llm_b.started.wait(), 10)

    row_b = await until(lambda: _finished(pool, run_b))
    await until(lambda: _guard_gone(worker_b, run_b))  # same race, same fix: wait for the cleanup itself
    assert (row_b["status"], row_b["error"]["code"]) == ("failed", "RUN_TIMEOUT")
    assert worker_b._timed_out == set()
    assert worker_b._cancelled == set()


# B7: a run cancelled while blocked inside a node must close that node's open node_runs row *and* leave a
# closing node_failed event, or a consumer replaying run_events would see that node running forever.
async def test_a_cancelled_mid_node_run_closes_its_open_node_in_the_trace(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(seconds=30.0)
    await worker_factory(llm)
    await asyncio.wait_for(llm.started.wait(), 10)

    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET cancel_requested_at=now() WHERE id=%s", (run_id,))

    row = await until(lambda: _finished(pool, run_id))
    assert row["status"] == "cancelled"

    async with pool.connection() as conn:
        node_rows = await (await conn.execute(
            "SELECT node_id, exec_index, attempt, status FROM node_runs WHERE run_id=%s", (run_id,))).fetchall()
        events = await (await conn.execute(
            "SELECT type, node_id, exec_index, attempt, payload FROM run_events WHERE run_id=%s ORDER BY seq",
            (run_id,))).fetchall()

    assert node_rows and all(r["status"] != "running" for r in node_rows)  # nothing left open

    # "start" already succeeded before the cancel; "llm_1" is the one that was open and force-closed.
    open_node = next(r for r in node_rows if r["node_id"] == "llm_1")
    closing = [e for e in events if e["type"] == "node_failed"
               and (e["node_id"], e["exec_index"], e["attempt"])
               == (open_node["node_id"], open_node["exec_index"], open_node["attempt"])]
    assert len(closing) == 1
    assert closing[0]["payload"]["willRetry"] is False


async def test_a_run_over_its_active_time_limit_fails(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM()
    await worker_factory(llm, run_max_active_ms=300)
    await asyncio.wait_for(llm.started.wait(), 10)

    row = await until(lambda: _finished(pool, run_id))

    assert (row["status"], row["error"]["code"]) == ("failed", "RUN_TIMEOUT")


async def _finished(pool, run_id):
    row = await _run_row(pool, run_id)
    return row if row["status"] in ("succeeded", "failed", "cancelled") else None


async def _guard_gone(worker, run_id):
    return run_id not in worker._guards or None


async def _guard_cancelled(worker, run_id):
    guard = worker._guards.get(run_id)
    return guard is not None and guard.cancelled


async def _trace_counts(pool, run_id):
    async with pool.connection() as conn:
        node_runs = (await (await conn.execute(
            "SELECT count(*) AS n FROM node_runs WHERE run_id=%s", (run_id,))).fetchone())["n"]
        events = (await (await conn.execute(
            "SELECT count(*) AS n FROM run_events WHERE run_id=%s", (run_id,))).fetchone())["n"]
        checkpoints = (await (await conn.execute(
            "SELECT count(*) AS n FROM checkpoints WHERE thread_id=%s", (run_id,))).fetchone())["n"]
        checkpoint_writes = (await (await conn.execute(
            "SELECT count(*) AS n FROM checkpoint_writes WHERE thread_id=%s", (run_id,))).fetchone())["n"]
    return (node_runs, events, checkpoints, checkpoint_writes)


async def _settled_counts(pool, run_id, *, stable_checks: int = 3, interval: float = 0.05):
    """`_trace_counts`, but only once it stops changing across `stable_checks` consecutive polls.

    LangGraph's own checkpoint writes for a node that already finished can still be landing a beat after
    the next node's call has visibly started, so a single read right after that isn't a safe baseline.
    """
    last = None
    stable = 0
    while stable < stable_checks:
        current = await _trace_counts(pool, run_id)
        stable = stable + 1 if current == last else 0
        last = current
        await asyncio.sleep(interval)
    return last


async def _expired_row(pool, run_id):
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT status, lease_expires_at <= now() AS expired FROM runs WHERE id=%s", (run_id,))).fetchone()
    return row if row["expired"] else None


async def _stays_expired(pool, run_id, *, span_sec: float, interval: float = 0.05):
    """Wait for the lease to expire, then keep polling for `span_sec` more asserting it stays expired.

    The second half doesn't wait for a state change (there isn't one to wait for — the point is that
    nothing changes); it fails immediately the moment the invariant breaks instead.
    """
    row = await until(lambda: _expired_row(pool, run_id))
    deadline = time.monotonic() + span_sec
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        row = await _expired_row(pool, run_id)
        assert row is not None, "lease_expires_at was pushed back out after the infra failure"
    return row


async def test_stop_gives_up_on_a_task_that_will_not_cancel(pool, redis, worker_factory, caplog):
    """A shutdown step that can wait forever turns a deploy into a SIGKILL — and, in the suite, into a
    hang with no output at all. The run is not lost: it keeps its lease, the lease expires, and the
    reaper recovers it, which is what happens when a worker dies outright anyway (design 6.2)."""
    import asyncio
    import logging

    from engine.worker import worker as worker_module

    worker = await worker_factory(ScriptedLLM([]))
    ignored_one_cancel = asyncio.Event()

    async def slow_to_cancel():
        """Swallows the first cancel, the way a task stuck in an uninterruptible cleanup does, then
        exits on the second — a task that never dies would hang the event loop's own shutdown, which is
        the failure this test exists to prevent, not to cause."""
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            ignored_one_cancel.set()
            await asyncio.sleep(3600)

    stuck = worker._spawn(slow_to_cancel())
    original = worker_module.STOP_TIMEOUT_SEC
    worker_module.STOP_TIMEOUT_SEC = 0.2
    try:
        with caplog.at_level(logging.WARNING):
            async with asyncio.timeout(10):  # the point of the test: stop() returns at all
                await worker.stop()
    finally:
        worker_module.STOP_TIMEOUT_SEC = original
        stuck.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stuck

    assert ignored_one_cancel.is_set()
    assert "did not stop" in caplog.text


async def test_the_heartbeat_does_not_use_the_shared_pool(pool, redis, worker_factory):
    """Pool starvation must not be able to cost a healthy run its lease."""
    worker = await worker_factory(ScriptedLLM(["요약본"]))

    assert worker._beat_conn is not None
    assert worker._beat_conn is not pool


async def test_the_lease_survives_a_pool_with_no_free_connections(pool, redis, worker_factory, db_url):
    """§11.2's first hardening invariant, and the measurement that motivated this task: 10 runs x a 10-way
    fan-out used 13 of 14 connections with 7 waiting. A heartbeat queued behind that can wait out the very
    lease it is trying to extend, and the reaper then hands a perfectly healthy run to a second worker."""
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    from tests.factories import make_run

    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(6.0)
    await worker_factory(llm, lease_sec=2, heartbeat_sec=0.2)
    await asyncio.wait_for(llm.started.wait(), 10)

    async with contextlib.AsyncExitStack() as stack:
        for _ in range(pool.max_size):  # nothing is left for anyone else
            await stack.enter_async_context(pool.connection())
        await asyncio.sleep(3)  # longer than lease_sec: an unextended lease is gone by now
        # A connection of its own, because the pool has nothing left to give -- the same reason the
        # heartbeat now has one.
        probe = await AsyncConnection.connect(db_url, autocommit=True, row_factory=dict_row)
        try:
            row = await (await probe.execute(
                "SELECT status, lease_expires_at > now() AS alive FROM runs WHERE id=%s",
                (run_id,))).fetchone()
        finally:
            await probe.close()

    assert row["status"] == "running"
    assert row["alive"], "the lease expired while the pool was full"


async def test_a_heartbeat_that_cannot_reach_the_database_gives_up_the_run(pool, redis, worker_factory,
                                                                          monkeypatch):
    """After lease_sec of failures the worker must stop, or two workers end up on one checkpoint."""
    from engine.db import runs as run_db
    from tests.factories import make_run

    async def always_fails(*args, **kwargs):
        raise RuntimeError("database is unreachable")

    monkeypatch.setattr(run_db, "heartbeat", always_fails)
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(30.0)
    await worker_factory(llm, lease_sec=1, heartbeat_sec=0.2)

    async with asyncio.timeout(20):
        await llm.started.wait()

        async def stopped():
            return llm.cancelled.is_set()

        await until(stopped)

    async with pool.connection() as conn:
        row = await run_db.get_run(conn, run_id)
    assert row["status"] == "running"  # left for recovery, not written by a worker that lost its lease


async def test_a_broken_heartbeat_connection_is_replaced_rather_than_fatal(pool, redis, worker_factory):
    """The dedicated connection is a single point of failure by construction: if it drops and is not
    reconnected, every run this worker holds silently loses its lease."""
    from tests.factories import make_run

    await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(6.0)
    worker = await worker_factory(llm, lease_sec=5, heartbeat_sec=0.2)
    await asyncio.wait_for(llm.started.wait(), 10)

    await worker._beat_conn.close()  # as if the database had dropped it

    async def reconnected():
        conn = worker._beat_conn
        return conn is not None and not conn.closed

    await until(reconnected, timeout=10.0)


async def test_a_transient_heartbeat_failure_does_not_give_up_a_healthy_run(pool, redis, worker_factory,
                                                                            monkeypatch):
    """The give-up rule is measured from the last *successful* beat, not from when the run started.

    Without that, a long-running run that has been beating happily for longer than its lease would be
    abandoned by the very first transient blip — which is the opposite of what the rule is for. The
    failures here are surrounded by successes on both sides, so only the reset of the success clock
    distinguishes the two behaviours.
    """
    from engine.db import runs as run_db
    from tests.factories import make_run

    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    real_heartbeat = run_db.heartbeat
    state = {"calls": 0, "failed": 0}

    async def flaky(*args, **kwargs):
        state["calls"] += 1
        if 4 <= state["calls"] <= 6:  # a blip well after the run has been beating successfully
            state["failed"] += 1
            raise RuntimeError("database blip")
        return await real_heartbeat(*args, **kwargs)

    monkeypatch.setattr(run_db, "heartbeat", flaky)
    llm = _GatedLLM()
    # lease_sec=1 with heartbeat_sec=0.2: the run is alive far longer than one lease before the blip,
    # so a rule keyed on the run's age rather than the last good beat would abandon it here.
    await worker_factory(llm, lease_sec=1, heartbeat_sec=0.2)
    await asyncio.wait_for(llm.started.wait(), 10)

    async def blip_is_over():
        return state["failed"] >= 3 and state["calls"] >= 8

    await until(blip_is_over, timeout=15.0)
    llm.proceed.set()

    async def finished():
        async with pool.connection() as conn:
            row = await run_db.get_run(conn, run_id)
        return row if row["status"] in ("succeeded", "failed", "cancelled") else None

    row = await until(finished, timeout=15.0)
    assert row["status"] == "succeeded"
