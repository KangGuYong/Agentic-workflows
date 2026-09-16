import asyncio

from engine.db import runs as run_db
from engine.worker.reaper import Reaper
from tests.factories import make_run


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


def _config():
    import dataclasses

    from engine.config import load_config

    return dataclasses.replace(load_config(), reaper_interval_sec=0.05)
