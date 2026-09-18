"""Retention cleanup (2b design §7).

Metadata is kept forever; payloads and checkpoints are dropped once a run has been finished for longer
than the retention window. `runs.purged_at` marks a run as done so the sweep never rescans it — without
it the same rows would come back on every tick for the life of the deployment.
"""
from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection


async def expired_runs(conn: AsyncConnection, *, retention_days: int, limit: int) -> list[dict[str, Any]]:
    return await (await conn.execute(
        "SELECT id FROM runs"
        " WHERE purged_at IS NULL AND finished_at IS NOT NULL"
        "   AND finished_at < now() - make_interval(days => %s)"
        " ORDER BY finished_at LIMIT %s",
        (retention_days, limit),
    )).fetchall()


async def purge_run(conn: AsyncConnection, run_id: str) -> None:
    """Drop everything a tenant put in, keep everything an operator needs."""
    await conn.execute("UPDATE node_runs SET input=NULL, output=NULL WHERE run_id=%s", (run_id,))
    await conn.execute("UPDATE run_events SET payload='{}'::jsonb WHERE run_id=%s", (run_id,))
    # One statement each: psycopg refuses several commands in a single parameterised execute
    # ("cannot insert multiple commands into a prepared statement"). They share the caller's transaction.
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        await conn.execute(f"DELETE FROM {table} WHERE thread_id=%s", (run_id,))
    await conn.execute(
        "UPDATE runs SET inputs=NULL, outputs=NULL, resume_payload=NULL, purged_at=now(), updated_at=now()"
        " WHERE id=%s", (run_id,))


async def collect_orphan_checkpoints(conn: AsyncConnection, *, limit: int) -> int:
    """Checkpoints whose run row is gone — deleting a workflow cascades its runs away but not these.

    The rule is the run row's existence, with no age bound, and that is deliberate rather than a
    simplification: LangGraph owns these tables and they carry no timestamp column at all (checked against
    its own MIGRATIONS list), and our Alembic migrations run *before* `AsyncPostgresSaver.setup()` creates
    them, so there is nowhere to add one either. What makes the rule safe is the write order: a checkpoint
    is only ever written by a worker executing a run it has already claimed, so the run row is committed
    before any checkpoint for it exists, and run rows disappear only when a workflow is deleted — exactly
    when their checkpoints should follow. A future path that wrote a checkpoint *before* committing its run
    row would break that, and would need its own protection here.

    `thread_id` is text while `runs.id` is a uuid, so the join casts the uuid rather than the text: casting
    text to uuid would raise on any thread id that is not a uuid and stop the whole sweep.
    """
    cursor = await conn.execute(
        "WITH orphan AS ("
        "  SELECT c.thread_id FROM checkpoints c"
        "   LEFT JOIN runs r ON r.id::text = c.thread_id"
        "   WHERE r.id IS NULL"
        "   GROUP BY c.thread_id LIMIT %s)"
        " DELETE FROM checkpoints WHERE thread_id IN (SELECT thread_id FROM orphan)",
        (limit,),
    )
    deleted = cursor.rowcount
    await conn.execute(
        "DELETE FROM checkpoint_writes w WHERE NOT EXISTS ("
        "  SELECT 1 FROM runs r WHERE r.id::text = w.thread_id)")
    await conn.execute(
        "DELETE FROM checkpoint_blobs b WHERE NOT EXISTS ("
        "  SELECT 1 FROM runs r WHERE r.id::text = b.thread_id)")
    return deleted
