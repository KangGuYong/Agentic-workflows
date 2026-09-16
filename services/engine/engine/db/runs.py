"""Run rows: claiming, the lease, and the transitions a worker owns (MVP design 5.1, 5.2).

Every worker write carries `AND lease_owner = %(owner)s` so a worker that lost its lease writes nothing.
Callers manage transactions; these helpers never commit on their own.
"""
from __future__ import annotations

from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

QUEUE_CHANNEL = "runs_queued"


async def notify_queued(conn: AsyncConnection, run_id: str | None = None) -> None:
    """Wake a worker. Sent inside the transaction that queued the run, so it cannot fire for a rolled-back run."""
    await conn.execute("SELECT pg_notify(%s, %s)", (QUEUE_CHANNEL, run_id or ""))


async def claim_next(conn: AsyncConnection, *, owner: str, lease_sec: int) -> dict[str, Any] | None:
    """Take the oldest queued run, or None. This single statement is both the queue and the CAS claim."""
    row = await (await conn.execute(
        "UPDATE runs SET status='running', lease_owner=%(owner)s,"
        "   lease_expires_at=now() + make_interval(secs => %(lease)s),"
        "   started_at=coalesce(started_at, now()), updated_at=now()"
        " WHERE id = (SELECT id FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1"
        "             FOR UPDATE SKIP LOCKED)"
        " RETURNING *",
        {"owner": owner, "lease": lease_sec},
    )).fetchone()
    return row


async def heartbeat(conn: AsyncConnection, *, run_id: str, owner: str, lease_sec: int,
                    delta_ms: int) -> dict[str, Any] | None:
    """Extend the lease and add to the active time. None means the lease is gone: stop writing."""
    return await (await conn.execute(
        "UPDATE runs SET lease_expires_at=now() + make_interval(secs => %(lease)s),"
        "   active_ms = active_ms + %(delta)s, updated_at=now()"
        " WHERE id=%(id)s AND lease_owner=%(owner)s AND status='running'"
        " RETURNING cancel_requested_at, active_ms",
        {"id": run_id, "owner": owner, "lease": lease_sec, "delta": delta_ms},
    )).fetchone()


async def finish(conn: AsyncConnection, *, run_id: str, owner: str,
                 status: Literal["succeeded", "failed", "cancelled"],
                 outputs: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
                 clear_inputs: bool = False) -> bool:
    """Terminal transition. False means this worker no longer owns the run and wrote nothing."""
    row = await (await conn.execute(
        "UPDATE runs SET status=%(status)s, outputs=%(outputs)s, error=%(error)s,"
        "   inputs = CASE WHEN %(clear)s THEN NULL ELSE inputs END,"
        "   lease_owner=NULL, lease_expires_at=NULL, resume_payload=NULL,"
        "   finished_at=now(), updated_at=now()"
        " WHERE id=%(id)s AND lease_owner=%(owner)s AND status='running' RETURNING id",
        {"id": run_id, "owner": owner, "status": status, "clear": clear_inputs,
         "outputs": Jsonb(outputs) if outputs is not None else None,
         "error": Jsonb(error) if error is not None else None},
    )).fetchone()
    return row is not None


async def set_waiting(conn: AsyncConnection, *, run_id: str, owner: str, node_id: str, exec_index: int) -> bool:
    """Park the run on an approval and release the lease; waiting costs no worker resources."""
    row = await (await conn.execute(
        "UPDATE runs SET status='waiting', waiting_node_id=%(node)s, waiting_exec_index=%(index)s,"
        "   resume_payload=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=now()"
        " WHERE id=%(id)s AND lease_owner=%(owner)s AND status='running' RETURNING id",
        {"id": run_id, "owner": owner, "node": node_id, "index": exec_index},
    )).fetchone()
    return row is not None


async def expire_lease(conn: AsyncConnection, *, run_id: str, owner: str) -> bool:
    """Hand the run back for recovery after an engine fault: the reaper picks it up on its next pass.
    False means the lease was already gone, so there was nothing to hand back."""
    row = await (await conn.execute(
        "UPDATE runs SET lease_expires_at=now(), updated_at=now()"
        " WHERE id=%s AND lease_owner=%s RETURNING id", (run_id, owner),
    )).fetchone()
    return row is not None


async def clear_resume_payload(conn: AsyncConnection, *, run_id: str, owner: str) -> bool:
    """A stored answer must not survive the invocation that used it, whatever its outcome.

    Fenced like every other worker write: a worker that lost the run must not clear an answer the new
    owner is about to use, or the reviewer would be asked to approve the same step twice.
    """
    row = await (await conn.execute(
        "UPDATE runs SET resume_payload=NULL WHERE id=%s AND lease_owner=%s RETURNING id", (run_id, owner),
    )).fetchone()
    return row is not None


async def close_open_node_runs(conn: AsyncConnection, run_id: str, status: str) -> list[dict[str, Any]]:
    """Close attempts left `running` by a crash, a cancel or a timeout, so the trace has no open rows.

    Returns the rows it closed (node_id, exec_index, attempt): the caller records a matching `node_failed`
    event for each one, since closing the status column alone would leave a `run_events` replay showing
    that node running forever.
    """
    cursor = await conn.execute(
        "UPDATE node_runs SET status=%s, finished_at=now() WHERE run_id=%s AND status='running'"
        " RETURNING node_id, exec_index, attempt",
        (status, run_id),
    )
    return await cursor.fetchall()


async def get_run(conn: AsyncConnection, run_id: str) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM runs WHERE id=%s", (run_id,))).fetchone()


async def get_version_dsl(conn: AsyncConnection, version_id: str) -> dict[str, Any] | None:
    row = await (await conn.execute(
        "SELECT dsl, dsl_hash FROM workflow_versions WHERE id=%s", (version_id,)
    )).fetchone()
    return row


# ---------------------------------------------------------------- API: create and read (Task 13)

MAX_INPUT_BYTES = 256_000  # design 8.4; well under MAX_BODY_BYTES so the message names the right limit
MAX_IDEMPOTENCY_KEY_BYTES = 200  # half of runs_idempotency_idx's btree key; see create_run (Task 13 review A2)


async def insert_queued(conn: AsyncConnection, *, workflow_id: str, version_id: str, workspace_id: str,
                        inputs: dict[str, Any], idempotency_key: str | None,
                        store_run_data: bool) -> dict[str, Any]:
    return await (await conn.execute(
        "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status, inputs,"
        " idempotency_key, store_run_data) VALUES (gen_random_uuid(), %s, %s, %s, 'queued', %s, %s, %s)"
        " RETURNING *",
        (workspace_id, workflow_id, version_id, Jsonb(inputs), idempotency_key, store_run_data),
    )).fetchone()


async def find_by_idempotency_key(conn: AsyncConnection, workflow_id: str, key: str) -> dict[str, Any] | None:
    return await (await conn.execute(
        "SELECT * FROM runs WHERE workflow_id=%s AND idempotency_key=%s", (workflow_id, key)
    )).fetchone()


async def lock_run(conn: AsyncConnection, run_id: str) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,))).fetchone()


async def list_for_workflow(conn: AsyncConnection, workflow_id: str, *, limit: int = 50,
                            cursor: tuple[Any, str] | None = None) -> list[dict[str, Any]]:
    """Newest first, keyset-paged (design 8.1: `?cursor&limit`). `cursor` is the `(created_at, id)` of the
    last row the caller has already seen; the id tiebreaker keeps paging stable when two runs share a
    `created_at`, which two runs queued in the same transaction-commit instant can. The caller decides
    page size and, by passing `limit + 1`, whether there is a next page (see `runs.py`'s router)."""
    if cursor is None:
        return await (await conn.execute(
            "SELECT id, status, created_at, started_at, finished_at, retry_count, workflow_version_id"
            " FROM runs WHERE workflow_id=%s ORDER BY created_at DESC, id DESC LIMIT %s",
            (workflow_id, limit),
        )).fetchall()
    return await (await conn.execute(
        "SELECT id, status, created_at, started_at, finished_at, retry_count, workflow_version_id"
        " FROM runs WHERE workflow_id=%s AND (created_at, id) < (%s, %s)"
        " ORDER BY created_at DESC, id DESC LIMIT %s",
        (workflow_id, cursor[0], cursor[1], limit),
    )).fetchall()


async def list_node_runs(conn: AsyncConnection, run_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """`input`/`output` can each carry up to 256KB (design 7.2), so an unbounded read of a large trace is
    both a huge response and real `_sanitize` CPU on the event loop while a pooled connection sits pinned.
    The caller bounds this with `limit` (validated to at most 200 by the router) and, by passing
    `limit + 1`, learns whether more rows exist without a second query. Worst case remains large even
    paged: the engine's recursion limit allows roughly 20,000 node executions per run, each retried some
    number of times, so a full trace can still take on the order of a hundred 200-row pages -- bounded per
    request, not bounded in total."""
    return await (await conn.execute(
        "SELECT node_id, exec_index, attempt, status, input, output, error, meta, tokens_in, tokens_out,"
        " truncated, started_at, finished_at FROM node_runs WHERE run_id=%s"
        " ORDER BY started_at, exec_index, attempt LIMIT %s", (run_id, limit)
    )).fetchall()


async def waiting_payload(conn: AsyncConnection, run_id: str, node_id: str, exec_index: int) -> dict[str, Any] | None:
    """The interrupt payload of the approval this run is parked on (stored by the recorder)."""
    row = await (await conn.execute(
        "SELECT meta->'waiting' AS waiting FROM node_runs"
        " WHERE run_id=%s AND node_id=%s AND exec_index=%s AND waited ORDER BY attempt DESC LIMIT 1",
        (run_id, node_id, exec_index),
    )).fetchone()
    return row["waiting"] if row else None


async def has_checkpoint(conn: AsyncConnection, run_id: str) -> bool:
    row = await (await conn.execute(
        "SELECT 1 FROM checkpoints WHERE thread_id=%s LIMIT 1", (str(run_id),)
    )).fetchone()
    return row is not None
