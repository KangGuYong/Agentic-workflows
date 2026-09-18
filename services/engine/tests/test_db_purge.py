"""Retention cleanup (2b design §7): metadata is kept, payloads and checkpoints are dropped."""
import dataclasses

from engine.config import load_config
from engine.db import purge as purge_db
from engine.db import runs as run_db
from engine.worker.reaper import Reaper
from tests.factories import make_run

CHECKPOINT_COLUMNS = ("thread_id", "checkpoint_ns", "checkpoint_id", "type", "checkpoint", "metadata")


async def _age(pool, run_id: str, days: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET status='succeeded', finished_at = now() - make_interval(days => %s)"
            " WHERE id=%s", (days, run_id))


async def _row(pool, run_id: str):
    async with pool.connection() as conn:
        row = await run_db.get_run(conn, run_id)
    return run_db.decode_run(row, load_config().secret_key)


async def _checkpoint(pool, thread_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)"
            " VALUES (%s, '', '1', 't', '{}', '{}')", (thread_id,))
        await conn.execute(
            "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx,"
            " channel, type, blob) VALUES (%s, '', '1', 't1', 0, 'c', 't', '\\x00')", (thread_id,))
        await conn.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob)"
            " VALUES (%s, '', 'c', '1', 't', '\\x00')", (thread_id,))


async def _checkpoints(pool, thread_id: str) -> int:
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT count(*) AS n FROM checkpoints WHERE thread_id=%s", (thread_id,))).fetchone()
    return row["n"]


async def _checkpoint_rows(pool) -> tuple[int, int, int]:
    async with pool.connection() as conn:
        counts = []
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            row = await (await conn.execute(f"SELECT count(*) AS n FROM {table}")).fetchone()
            counts.append(row["n"])
    return tuple(counts)


def _config(**overrides):
    return dataclasses.replace(load_config(), retention_days=30, purge_batch=100, **overrides)


async def test_a_fresh_run_keeps_its_payloads(pool, redis):
    run_id = await make_run(pool, status="succeeded", inputs={"topic": "AI"})

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert row["purged_at"] is None


async def test_an_expired_run_keeps_metadata_and_loses_payloads(pool, redis):
    run_id = await make_run(pool, status="running", inputs={"topic": "AI"})
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status, input, output)"
            " VALUES (gen_random_uuid(), %s, 'llm_1', 1, 1, 'succeeded', '{\"a\": 1}', '{\"b\": 2}')",
            (run_id,))
        await conn.execute(
            "INSERT INTO run_events (run_id, seq, type, payload) VALUES (%s, 1, 'run_started', '{\"x\": 1}')",
            (run_id,))
    await _checkpoint(pool, run_id)
    await _age(pool, run_id, 31)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert (row["status"], row["inputs"], row["outputs"]) == ("succeeded", None, None)
    assert row["purged_at"] is not None and row["finished_at"] is not None
    async with pool.connection() as conn:
        node = await (await conn.execute(
            "SELECT status, input, output FROM node_runs WHERE run_id=%s", (run_id,))).fetchone()
        event = await (await conn.execute(
            "SELECT type, payload FROM run_events WHERE run_id=%s", (run_id,))).fetchone()
    assert (node["status"], node["input"], node["output"]) == ("succeeded", None, None)
    assert (event["type"], event["payload"]) == ("run_started", {})
    # All three checkpoint tables, not just `checkpoints`: the blobs are where the state actually is.
    assert await _checkpoint_rows(pool) == (0, 0, 0)


async def test_a_purged_run_is_not_scanned_again(pool, redis):
    run_id = await make_run(pool, status="running")
    await _age(pool, run_id, 31)
    await Reaper(_config(), pool, redis).sweep()

    async with pool.connection() as conn:
        rows = await purge_db.expired_runs(conn, retention_days=30, limit=100)

    assert [str(row["id"]) for row in rows] == []


async def test_a_run_inside_the_window_is_not_expired_yet(pool):
    """The boundary the retention setting actually names: 29 days old is not 31."""
    run_id = await make_run(pool, status="running")
    await _age(pool, run_id, 29)

    async with pool.connection() as conn:
        rows = await purge_db.expired_runs(conn, retention_days=30, limit=100)

    assert [str(row["id"]) for row in rows] == []
    await _age(pool, run_id, 31)
    async with pool.connection() as conn:
        rows = await purge_db.expired_runs(conn, retention_days=30, limit=100)
    assert [str(row["id"]) for row in rows] == [run_id]


async def test_the_batch_limit_is_honoured(pool):
    """One small batch per sweep: an unbounded delete on a large backlog would hold locks for minutes."""
    for _ in range(3):
        await _age(pool, await make_run(pool, status="running"), 31)

    async with pool.connection() as conn:
        rows = await purge_db.expired_runs(conn, retention_days=30, limit=2)

    assert len(rows) == 2


async def test_an_unfinished_run_is_never_purged(pool, redis):
    run_id = await make_run(pool, status="running", inputs={"topic": "AI"})
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET created_at = now() - interval '90 days' WHERE id=%s", (run_id,))

    await Reaper(_config(), pool, redis).sweep()

    assert (await _row(pool, run_id))["purged_at"] is None


async def test_an_orphan_checkpoint_is_collected(pool, redis):
    """Deleting a workflow cascades its runs away but not their checkpoints."""
    await _checkpoint(pool, "11111111-1111-1111-1111-111111111111")

    await Reaper(_config(), pool, redis).sweep()

    assert await _checkpoint_rows(pool) == (0, 0, 0)


async def test_a_live_runs_checkpoint_is_never_collected(pool, redis):
    run_id = await make_run(pool, status="running")
    await _checkpoint(pool, run_id)

    await Reaper(_config(), pool, redis).sweep()

    assert await _checkpoints(pool, run_id) == 1
    assert await _checkpoint_rows(pool) == (1, 1, 1)


async def test_a_queued_runs_checkpoint_is_never_collected(pool, redis):
    """The closest thing to a race there is: a run whose row exists but which has not started. Its row is
    what makes its checkpoints non-orphans, so this is the case that would break if the orphan rule ever
    keyed on anything other than the run row's existence."""
    run_id = await make_run(pool, status="queued")
    await _checkpoint(pool, run_id)

    await Reaper(_config(), pool, redis).sweep()

    assert await _checkpoints(pool, run_id) == 1


async def test_a_thread_id_that_is_not_a_uuid_is_still_collected(pool, redis):
    """`runs.id` is a uuid and `thread_id` is text: the comparison must not blow up on a thread id that
    could never be a run id, or one bad row would stop every sweep from collecting anything."""
    await _checkpoint(pool, "not-a-uuid")

    await Reaper(_config(), pool, redis).sweep()

    assert await _checkpoints(pool, "not-a-uuid") == 0
