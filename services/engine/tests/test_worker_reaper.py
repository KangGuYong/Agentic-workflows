import asyncio

from psycopg.types.json import Jsonb

from engine.db import runs as run_db
from engine.llm.scripted import ScriptedLLM
from engine.worker.reaper import Reaper
from tests.conftest import until
from tests.factories import make_run
from tests.test_worker_run import CHAIN


async def _claimed(pool, *, recovery_count: int = 0, cancel: bool = False, store_run_data: bool = True,
                   inputs: dict | None = None) -> str:
    """A run that a dead worker left behind: running, lease already expired, one node still open."""
    run_id = await make_run(pool, status="running", store_run_data=store_run_data,
                            inputs=inputs if inputs is not None else {"topic": "AI"})
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET lease_owner='dead', lease_expires_at=now() - interval '1 minute',"
            " recovery_count=%s, cancel_requested_at = CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s",
            (recovery_count, cancel, run_id),
        )
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status)"
            " VALUES (gen_random_uuid(), %s, 'llm_1', 1, 1, 'running')", (run_id,))
    return run_id


async def _row(pool, run_id):
    async with pool.connection() as conn:
        return await run_db.get_run(conn, run_id)


async def _events(pool, run_id):
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT type, node_id, exec_index, attempt, payload FROM run_events WHERE run_id=%s ORDER BY seq",
            (run_id,))).fetchall()


async def _node_runs(pool, run_id):
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT node_id, status FROM node_runs WHERE run_id=%s", (run_id,))).fetchall()


async def test_an_expired_lease_is_requeued_and_counted(pool, redis):
    run_id = await _claimed(pool)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert (row["status"], row["recovery_count"], row["lease_owner"]) == ("queued", 1, None)

    # The dead worker left "llm_1" open: the reaper must close it with a matching node_failed event, or a
    # consumer replaying run_events sees that node running forever, even though the run itself is recovering.
    events = await _events(pool, run_id)
    assert [e["type"] for e in events] == ["node_failed", "run_recovered"]
    closing = events[0]
    assert (closing["node_id"], closing["exec_index"], closing["attempt"]) == ("llm_1", 1, 1)
    assert closing["payload"]["error"]["code"] == "NODE_FAILED"
    assert closing["payload"]["willRetry"] is False
    assert events[1]["payload"] == {"recoveryCount": 1}

    node_rows = await _node_runs(pool, run_id)
    assert node_rows[0]["status"] == "failed"  # closed, not left "running"


async def test_a_requeued_run_keeps_its_inputs_even_when_store_run_data_is_false(pool, redis):
    """The requeue path is not terminal: the run still needs its inputs to actually run again."""
    run_id = await _claimed(pool, store_run_data=False)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert row["status"] == "queued"
    assert row["inputs"] == {"topic": "AI"}


async def test_too_many_recoveries_fail_the_run(pool, redis):
    run_id = await _claimed(pool, recovery_count=3)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert (row["status"], row["error"]["code"]) == ("failed", "ENGINE_RECOVERY_EXHAUSTED")

    events = await _events(pool, run_id)
    assert [e["type"] for e in events] == ["node_failed", "run_failed"]
    # The closing node_failed event carries the run's own reason, not a generic one.
    assert events[0]["payload"]["error"]["code"] == "ENGINE_RECOVERY_EXHAUSTED"
    assert events[0]["payload"]["willRetry"] is False

    node_run = await _node_runs(pool, run_id)
    assert node_run[0]["status"] == "failed"  # no attempt is left open


async def test_recovery_exhausted_clears_inputs_when_store_run_data_is_false(pool, redis):
    run_id = await _claimed(pool, recovery_count=3, store_run_data=False)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert row["status"] == "failed"
    assert row["inputs"] is None


async def test_an_expired_lease_with_a_cancel_request_ends_cancelled(pool, redis):
    run_id = await _claimed(pool, cancel=True)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert row["status"] == "cancelled"

    events = await _events(pool, run_id)
    assert [e["type"] for e in events] == ["node_failed", "run_cancelled"]
    assert events[0]["payload"]["error"]["code"] == "NODE_FAILED"
    assert events[0]["payload"]["willRetry"] is False

    node_run = await _node_runs(pool, run_id)
    assert node_run[0]["status"] == "cancelled"


async def test_a_cancelled_recovery_clears_inputs_when_store_run_data_is_false(pool, redis):
    run_id = await _claimed(pool, cancel=True, store_run_data=False)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert row["status"] == "cancelled"
    assert row["inputs"] is None


async def test_an_approval_waiting_too_long_is_cancelled(pool, redis):
    run_id = await make_run(pool, status="waiting")
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET updated_at = now() - interval '31 days' WHERE id=%s", (run_id,))

    await Reaper(_config(), pool, redis).sweep()

    assert (await _row(pool, run_id))["status"] == "cancelled"


async def test_only_one_reaper_sweeps_at_a_time(pool, redis):
    await _claimed(pool)
    first, second = Reaper(_config(), pool, redis), Reaper(_config(), pool, redis)

    async def hold():
        async with first.exclusive() as acquired:
            assert acquired
            await asyncio.sleep(0.5)

    task = asyncio.create_task(hold())
    await asyncio.sleep(0.1)
    async with second.exclusive() as acquired:
        assert acquired is False
    await task


async def test_a_failing_sweep_does_not_end_the_reaper_loop(pool, redis, monkeypatch):
    """A single bad sweep (a transient DB error, say) must not silently stop the reaper forever."""
    calls = {"n": 0}
    real_sweep = Reaper.sweep

    async def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        await real_sweep(self)

    monkeypatch.setattr(Reaper, "sweep", flaky)
    reaper = Reaper(_config(), pool, redis)
    await reaper.start()
    try:
        async def swept_twice():
            return calls["n"] >= 2 or None

        from tests.conftest import until
        await until(swept_twice, timeout=5.0, interval=0.02)
    finally:
        await reaper.stop()


# B1: nothing covers `_take`'s `if not changed` branch. Dropping either `AND status='running'` or
# `AND lease_expires_at < now()` from its CAS UPDATE leaves the rest of the suite green, since every other
# test's row still matches both predicates. These two make the real row disagree with what `_recover_one`
# was handed, the way a genuine race between the reaper's SELECT and its UPDATE would.

async def test_take_leaves_a_row_alone_once_it_has_already_finished(pool, redis):
    """Covers `AND status='running'`: a row the original (not-actually-dead) worker finished between the
    reaper's SELECT and its UPDATE must not be resurrected into `queued`. `lease_expires_at` is left as
    `_claimed` set it (already expired) so only the status predicate is what excludes this row — dropping
    just the `lease_expires_at` predicate must not be what makes this test pass."""
    run_id = await _claimed(pool)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET status='succeeded', finished_at=now() WHERE id=%s", (run_id,))
    stale_row = {"recovery_count": 0, "cancel_requested_at": None, "store_run_data": True}

    await Reaper(_config(), pool, redis)._recover_one(run_id, stale_row)

    row = await _row(pool, run_id)
    assert row["status"] == "succeeded"  # untouched
    assert await _events(pool, run_id) == []


async def test_take_leaves_a_row_alone_once_its_lease_was_renewed(pool, redis):
    """Covers `AND lease_expires_at < now()`: a lease a live worker just renewed must not be stolen."""
    run_id = await _claimed(pool)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET lease_owner='worker-1', lease_expires_at=now() + interval '1 hour'"
            " WHERE id=%s", (run_id,))
    stale_row = {"recovery_count": 0, "cancel_requested_at": None, "store_run_data": True}

    await Reaper(_config(), pool, redis)._recover_one(run_id, stale_row)

    row = await _row(pool, run_id)
    assert (row["status"], row["lease_owner"]) == ("running", "worker-1")  # untouched
    assert await _events(pool, run_id) == []


# B2: nothing exercises `notify_queued`. A worker asleep on LISTEN must be woken the moment the reaper
# requeues a run, or a recovered run would sit `queued` until the next poll interval instead.
async def test_a_requeue_wakes_a_listener(pool, redis, listen_conn):
    await listen_conn.execute(f"LISTEN {run_db.QUEUE_CHANNEL}")
    run_id = await _claimed(pool)

    await Reaper(_config(), pool, redis).sweep()

    notified = []
    async with asyncio.timeout(5):
        async for notify in listen_conn.notifies(stop_after=1):
            notified.append(notify)
    assert notified and notified[0].payload == run_id


# B3: making `_expire_waiting` ignore the age entirely (dropping the `updated_at` interval check) leaves
# the rest of the suite green — every other waiting-run test in this file is already 31+ days stale. A
# fresh approval must survive a sweep untouched, or every pending approval in production would be
# cancelled the moment the reaper next ran.
async def test_a_fresh_waiting_run_survives_a_sweep(pool, redis):
    run_id = await make_run(pool, status="waiting")

    await Reaper(_config(), pool, redis).sweep()

    assert (await _row(pool, run_id))["status"] == "waiting"


# B4: the wiring. Deleting `await self._reaper.start()` from `Worker.start()` leaves all the worker tests
# green, since nothing else in the suite drives a real abandoned run through a real Worker's own Reaper.
async def test_a_workers_own_reaper_requeues_and_reruns_an_abandoned_run(pool, redis, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="running", inputs={"topic": "AI"})
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET lease_owner='dead', lease_expires_at=now() - interval '1 minute' WHERE id=%s",
            (run_id,))

    await worker_factory(ScriptedLLM(["요약본"]), reaper_interval_sec=0.1)

    row = await until(lambda: _terminal(pool, run_id))
    assert (row["status"], row["outputs"]) == ("succeeded", {"result": "요약본"})

    types = [e["type"] for e in await _events(pool, run_id)]
    assert types[0] == "run_recovered"  # the reaper's own event for requeuing it
    assert "run_started" in types  # the worker's attempt to actually run it, afterwards
    assert types[-1] == "run_succeeded"


async def _terminal(pool, run_id):
    row = await _row(pool, run_id)
    return row if row["status"] in ("succeeded", "failed", "cancelled") else None


# B5: `sweep()`'s `if not acquired: return` is uncovered — the existing lock test (below) drives
# `exclusive()` directly and never calls `sweep()` itself.
async def test_a_sweep_held_off_by_another_reapers_lock_changes_nothing(pool, redis):
    run_id = await _claimed(pool)
    first, second = Reaper(_config(), pool, redis), Reaper(_config(), pool, redis)

    async def hold():
        async with first.exclusive() as acquired:
            assert acquired
            await asyncio.sleep(0.5)

    task = asyncio.create_task(hold())
    await asyncio.sleep(0.1)
    await second.sweep()  # locked out: must be a no-op
    await task

    row = await _row(pool, run_id)
    assert (row["status"], row["lease_owner"]) == ("running", "dead")  # untouched
    assert await _events(pool, run_id) == []


# B6 (A2): a terminal transition must honour store_run_data the same way every other one does.
async def test_a_stale_waiting_run_with_store_run_data_false_clears_inputs(pool, redis):
    run_id = await make_run(pool, status="waiting", store_run_data=False, inputs={"secret": "value"})
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET updated_at = now() - interval '31 days' WHERE id=%s", (run_id,))

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert row["status"] == "cancelled"
    assert row["inputs"] is None


# B6 (A4): `resume_payload` must not survive a terminal transition — `run_db.finish` already nulls it for
# every other terminal path, and design 6.4 says it is cleared however the resumed invocation ends. The
# requeue path is the one exception: it is not terminal, and `execute_run` tolerates a stale resume payload
# by continuing from the checkpoint instead.
async def test_resume_payload_is_cleared_on_terminal_paths_but_kept_on_requeue(pool, redis):
    resume = {"decision": "approve", "edited": "민감정보"}
    requeue_id = await _claimed(pool)
    cancelled_id = await _claimed(pool, cancel=True)
    exhausted_id = await _claimed(pool, recovery_count=3)
    async with pool.connection() as conn:
        for run_id in (requeue_id, cancelled_id, exhausted_id):
            await conn.execute("UPDATE runs SET resume_payload=%s WHERE id=%s", (Jsonb(resume), run_id))

    await Reaper(_config(), pool, redis).sweep()

    assert (await _row(pool, requeue_id))["resume_payload"] == resume  # kept: not terminal
    assert (await _row(pool, cancelled_id))["resume_payload"] is None
    assert (await _row(pool, exhausted_id))["resume_payload"] is None


def _config():
    import dataclasses

    from engine.config import load_config

    return dataclasses.replace(load_config(), reaper_interval_sec=0.05)
