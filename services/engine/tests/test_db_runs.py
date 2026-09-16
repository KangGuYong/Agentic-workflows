import asyncio

from engine.db import runs as run_db
from tests.factories import make_run


async def _row(pool, run_id: str) -> dict:
    async with pool.connection() as conn:
        return await (await conn.execute("SELECT * FROM runs WHERE id=%s", (run_id,))).fetchone()


async def test_claiming_moves_one_queued_run_to_running(pool):
    run_id = await make_run(pool, status="queued")

    async with pool.connection() as conn:
        claimed = await run_db.claim_next(conn, owner="worker-1", lease_sec=30)

    assert str(claimed["id"]) == run_id and claimed["status"] == "running"
    row = await _row(pool, run_id)
    assert row["lease_owner"] == "worker-1" and row["started_at"] is not None


async def test_two_workers_never_claim_the_same_run(pool):
    await make_run(pool, status="queued")

    async def claim(owner: str):
        async with pool.connection() as conn:
            return await run_db.claim_next(conn, owner=owner, lease_sec=30)

    first, second = await asyncio.gather(claim("worker-1"), claim("worker-2"))

    assert len([row for row in (first, second) if row is not None]) == 1


async def test_claiming_an_empty_queue_returns_nothing(pool):
    await make_run(pool, status="succeeded")
    async with pool.connection() as conn:
        assert await run_db.claim_next(conn, owner="worker-1", lease_sec=30) is None


async def test_the_heartbeat_extends_the_lease_and_reports_a_cancel(pool):
    run_id = await make_run(pool, status="queued")
    async with pool.connection() as conn:
        await run_db.claim_next(conn, owner="worker-1", lease_sec=1)
        await conn.execute("UPDATE runs SET cancel_requested_at=now() WHERE id=%s", (run_id,))

        beat = await run_db.heartbeat(conn, run_id=run_id, owner="worker-1", lease_sec=30, delta_ms=1000)
        stolen = await run_db.heartbeat(conn, run_id=run_id, owner="worker-2", lease_sec=30, delta_ms=1000)

    assert beat["cancel_requested_at"] is not None and beat["active_ms"] == 1000
    assert stolen is None  # a worker that lost the lease must find out


async def test_only_the_lease_owner_can_finish_a_run(pool):
    run_id = await make_run(pool, status="queued")
    async with pool.connection() as conn:
        await run_db.claim_next(conn, owner="worker-1", lease_sec=30)

        assert await run_db.finish(conn, run_id=run_id, owner="worker-2", status="succeeded") is False
        assert await run_db.finish(conn, run_id=run_id, owner="worker-1", status="succeeded",
                                   outputs={"final": "x"}) is True

    row = await _row(pool, run_id)
    assert (row["status"], row["outputs"], row["lease_owner"]) == ("succeeded", {"final": "x"}, None)
    assert row["finished_at"] is not None


async def test_finishing_can_drop_the_inputs_when_run_data_is_not_stored(pool):
    run_id = await make_run(pool, status="queued", store_run_data=False, inputs={"topic": "비밀"})
    async with pool.connection() as conn:
        await run_db.claim_next(conn, owner="worker-1", lease_sec=30)
        await run_db.finish(conn, run_id=run_id, owner="worker-1", status="succeeded", clear_inputs=True)

    assert (await _row(pool, run_id))["inputs"] is None


async def test_waiting_records_the_target_and_releases_the_lease(pool):
    run_id = await make_run(pool, status="queued")
    async with pool.connection() as conn:
        await run_db.claim_next(conn, owner="worker-1", lease_sec=30)
        assert await run_db.set_waiting(conn, run_id=run_id, owner="worker-1",
                                        node_id="human_approval_1", exec_index=1) is True

    row = await _row(pool, run_id)
    assert (row["status"], row["waiting_node_id"], row["waiting_exec_index"]) == ("waiting", "human_approval_1", 1)
    assert row["lease_owner"] is None and row["resume_payload"] is None


async def test_open_node_runs_are_closed_when_a_run_stops(pool):
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status)"
            " VALUES (gen_random_uuid(), %s, 'llm_1', 1, 1, 'running')", (run_id,))

        closed = await run_db.close_open_node_runs(conn, run_id, "cancelled")

        rows = await (await conn.execute("SELECT status, finished_at FROM node_runs WHERE run_id=%s",
                                         (run_id,))).fetchall()
    assert closed == 1 and rows[0]["status"] == "cancelled" and rows[0]["finished_at"] is not None


async def test_notify_wakes_a_listener(pool, listen_conn):
    await listen_conn.execute("LISTEN runs_queued")

    async with pool.connection() as conn:
        await run_db.notify_queued(conn)

    async with asyncio.timeout(5):
        async for _ in listen_conn.notifies(stop_after=1):
            pass


async def test_many_workers_racing_claim_each_run_exactly_once(db_url, pool):
    """Real contention on independent connections: this is what FOR UPDATE SKIP LOCKED is for."""
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    runs = {await make_run(pool, status="queued") for _ in range(20)}

    async def claimer(owner: str) -> list[str]:
        taken: list[str] = []
        conn = await AsyncConnection.connect(db_url, autocommit=True, row_factory=dict_row)
        try:
            while True:
                row = await run_db.claim_next(conn, owner=owner, lease_sec=30)
                if row is None:
                    return taken
                taken.append(str(row["id"]))
        finally:
            await conn.close()

    results = await asyncio.gather(*(claimer(f"worker-{index}") for index in range(8)))

    claimed = [run_id for taken in results for run_id in taken]
    assert sorted(claimed) == sorted(runs)  # every run once, none skipped, none twice


async def test_closing_open_attempts_leaves_finished_ones_alone(pool):
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status, finished_at)"
            " VALUES (gen_random_uuid(), %s, 'llm_1', 1, 1, 'succeeded', now())", (run_id,))
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status)"
            " VALUES (gen_random_uuid(), %s, 'llm_2', 1, 1, 'running')", (run_id,))

        closed = await run_db.close_open_node_runs(conn, run_id, "cancelled")

        rows = await (await conn.execute(
            "SELECT node_id, status FROM node_runs WHERE run_id=%s ORDER BY node_id", (run_id,))).fetchall()
    assert closed == 1
    assert [(row["node_id"], row["status"]) for row in rows] == [("llm_1", "succeeded"), ("llm_2", "cancelled")]


async def test_only_the_lease_owner_can_clear_a_stored_answer(pool):
    run_id = await make_run(pool, status="queued")
    async with pool.connection() as conn:
        await run_db.claim_next(conn, owner="worker-1", lease_sec=30)
        await conn.execute("""UPDATE runs SET resume_payload='{"decision": "approve"}' WHERE id=%s""", (run_id,))

        assert await run_db.clear_resume_payload(conn, run_id=run_id, owner="worker-2") is False
        assert (await _row(pool, run_id))["resume_payload"] == {"decision": "approve"}
        assert await run_db.clear_resume_payload(conn, run_id=run_id, owner="worker-1") is True

    assert (await _row(pool, run_id))["resume_payload"] is None
