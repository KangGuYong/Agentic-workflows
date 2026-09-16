import asyncio

from engine.db import runs as run_db
from engine.events.publish import RedisPublisher
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
        self._seconds = seconds

    async def chat(self, **kwargs):
        self.started.set()
        await asyncio.sleep(self._seconds)
        raise AssertionError("should have been cancelled")


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


async def test_a_worker_that_lost_its_lease_writes_nothing(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(seconds=1.0)
    await worker_factory(llm, lease_sec=1)
    await asyncio.wait_for(llm.started.wait(), 10)

    async with pool.connection() as conn:  # another worker steals the run
        await conn.execute("UPDATE runs SET lease_owner='thief', lease_expires_at=now() + interval '1 hour'"
                           " WHERE id=%s", (run_id,))

    await asyncio.sleep(1.5)
    row = await _run_row(pool, run_id)
    assert (row["status"], row["lease_owner"]) == ("running", "thief")  # untouched by the old owner


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
