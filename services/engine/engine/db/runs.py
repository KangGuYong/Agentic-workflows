"""Run rows: claiming, the lease, and the transitions a worker owns (MVP design 5.1, 5.2).

Every worker write carries `AND lease_owner = %(owner)s` so a worker that lost its lease writes nothing.
Callers manage transactions; these helpers never commit on their own.
"""
from __future__ import annotations

from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from engine.db.crypto import open_payload, seal_payload
from engine.events.redact import redact

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
                 key: bytes | None,
                 outputs: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
                 clear_inputs: bool = False) -> bool:
    """Terminal transition. False means this worker no longer owns the run and wrote nothing.

    `outputs` is redacted before it is stored (design 10.1/10.3, A1 of the whole-branch review). This is
    safe -- unlike `inputs` (see `insert_queued`'s docstring for the write-time redaction this column
    *can't* have) -- because nothing reads `runs.outputs` back into execution: a retry replays from the
    checkpoint, never from this column, and it is otherwise only ever read back out for display
    (`routers.runs._run_view`, which redacts again on its own -- belt and suspenders, and the only thing
    that can catch a row written before this fix existed).
    """
    row = await (await conn.execute(
        "UPDATE runs SET status=%(status)s, outputs=%(outputs)s, error=%(error)s,"
        "   inputs = CASE WHEN %(clear)s THEN NULL ELSE inputs END,"
        "   lease_owner=NULL, lease_expires_at=NULL, resume_payload=NULL,"
        "   finished_at=now(), updated_at=now()"
        " WHERE id=%(id)s AND lease_owner=%(owner)s AND status='running' RETURNING id",
        {"id": run_id, "owner": owner, "status": status, "clear": clear_inputs,
         "outputs": seal_payload(key, "outputs", redact(outputs)) if outputs is not None else None,
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
                        store_run_data: bool, key: bytes | None) -> dict[str, Any]:
    """`inputs` is stored **unredacted**, unlike every other observation column (design 10.1 lists it
    among the redacted-at-rest columns; A1 of the whole-branch review records the conflict this runs
    into). The worker feeds this exact column to `execute_run` as the run's initial state
    (`worker.py::_run`: `inputs=row["inputs"] or {}`) -- redacting a value here would hand the workflow
    `"[REDACTED]"` instead of whatever a field that happens to look like a secret key actually held,
    silently corrupting the run. Redaction is applied on the read path instead
    (`routers.runs._run_view`), which is the transmission path design 10.3 actually governs, and which
    `outputs` (see `finish`) goes through the same way in addition to being redacted at write.

    It is encrypted at rest instead (2b design §9), which is the protection this column *can* have: a
    database dump no longer carries every run's input. The returned row carries the plaintext back,
    because the caller just supplied it and has no use for the ciphertext."""
    row = await (await conn.execute(
        "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status, inputs,"
        " idempotency_key, store_run_data) VALUES (gen_random_uuid(), %s, %s, %s, 'queued', %s, %s, %s)"
        " RETURNING *",
        (workspace_id, workflow_id, version_id, seal_payload(key, "inputs", inputs), idempotency_key,
         store_run_data),
    )).fetchone()
    return {**row, "inputs": inputs}


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


async def has_node_runs(conn: AsyncConnection, run_id: str) -> bool:
    """Whether this run ever got far enough to execute a node. `execute_run` never calls
    `graph.ainvoke` -- so no checkpoint is ever written -- on three paths: a compile failure, a stored
    version that no longer exists, and inputs rejected by `check_storable` at `start`. A caller deciding
    why `has_checkpoint` came back False (retry_run's 409) uses this to tell "never had one" apart from
    "had one, now gone" (see `retry_run`)."""
    row = await (await conn.execute(
        "SELECT 1 FROM node_runs WHERE run_id=%s LIMIT 1", (run_id,)
    )).fetchone()
    return row is not None


# ---------------------------------------------------------------- API: resume, retry, cancel (Task 15)


async def queue_resume(conn: AsyncConnection, *, run_id: str, answer: dict[str, Any]) -> bool:
    """Hand a checked approval answer to the worker. `resume_run` takes this row `FOR UPDATE` before
    calling this, so of two concurrent resumes only one ever sees `status='waiting'` here -- the other's
    own lock wait ends after this commits, by which point status is already 'queued' and it is refused.

    `cancel_requested_at` is cleared: a run parks on an approval with the flag still set when a cancel's
    Redis publish is lost and the node happens to park before the next heartbeat tick ever reads it (the
    worker already released the lease via `set_waiting`, so nothing else clears it). Left in place, the
    requeued run would come back reporting `cancelRequested: true` on an answer the reviewer just
    submitted, and the *next* run this row's lease owner heartbeats would read it as its own cancel."""
    row = await (await conn.execute(
        "UPDATE runs SET status='queued', resume_payload=%s, cancel_requested_at=NULL, updated_at=now()"
        " WHERE id=%s AND status='waiting' RETURNING id", (Jsonb(answer), run_id),
    )).fetchone()
    return row is not None


async def queue_retry(conn: AsyncConnection, run_id: str) -> bool:
    """Requeue a failed run from its last checkpoint.

    `recovery_count` is reset to 0: it caps how many times the reaper will auto-recover a crashed lease
    before giving up as ENGINE_RECOVERY_EXHAUSTED (worker/reaper.py), and a manual retry is a deliberate
    new attempt, not a continuation of the attempt that exhausted it -- leaving the old count in place
    would let one unrelated crash right after the retry fail it again with no recovery budget left.

    `active_ms` is reset to 0 for the same reason: it is cumulative across a run's whole lifetime
    (`heartbeat` only ever adds to it) and `_heartbeat` fails the run once it exceeds `run_max_active_ms`,
    so a run retried without resetting it would fail with RUN_TIMEOUT on its very first heartbeat tick --
    forever, since nothing else ever lowers it. A deliberate human retry is exactly the "start the clock
    over" case that column exists to distinguish from a crash recovery (which does not reset it).

    `cancel_requested_at` is cleared for the same reason as `queue_resume`: a running node can fail for
    its own reason inside the heartbeat window after a cancel was requested but before the worker acted on
    it, leaving the flag set on the now-`failed` row. Left in place, the retried run would report
    `cancelRequested: true` immediately after a retry nobody asked to cancel, and the next heartbeat tick
    would read it as authoritative and cancel the attempt the caller just asked for.

    `resume_payload`/`lease_owner`/`lease_expires_at` are already NULL on every path that reaches
    'failed' (`finish` and the reaper's `_take` both clear them on their terminal write), but this clears
    them again too rather than leaning on that invariant holding forever.
    """
    row = await (await conn.execute(
        "UPDATE runs SET status='queued', retry_count=retry_count + 1, recovery_count=0, active_ms=0,"
        "   error=NULL, cancel_requested_at=NULL, resume_payload=NULL, lease_owner=NULL,"
        "   lease_expires_at=NULL, finished_at=NULL, updated_at=now()"
        " WHERE id=%s AND status='failed' RETURNING id", (run_id,),
    )).fetchone()
    return row is not None


async def cancel_now(conn: AsyncConnection, run_id: str) -> bool:
    """Queued and waiting runs have no worker holding them, so the API ends them itself (design 5.9).

    `inputs` is cleared under the same `store_run_data` rule as every other terminal writer (`finish`,
    and the reaper's `_take`/`_expire_one`) -- A2 of the whole-branch review found this was the one
    terminal path that forgot it, so cancelling a `queued` or `waiting` run kept `inputs` even with
    `storeRunData=false`. The CASE reads `store_run_data` straight off the row being updated instead of
    taking a separate SELECT first, since the row is already locked and written here. `lease_owner`/
    `lease_expires_at` are cleared too for consistency with those same siblings, though both are already
    NULL on every path that reaches `queued`/`waiting` in the first place.
    """
    row = await (await conn.execute(
        "UPDATE runs SET status='cancelled', cancel_requested_at=coalesce(cancel_requested_at, now()),"
        "   finished_at=now(), resume_payload=NULL,"
        "   inputs = CASE WHEN store_run_data THEN inputs ELSE NULL END,"
        "   lease_owner=NULL, lease_expires_at=NULL, updated_at=now()"
        " WHERE id=%s AND status IN ('queued','waiting') RETURNING id", (run_id,),
    )).fetchone()
    return row is not None


async def request_cancel(conn: AsyncConnection, run_id: str) -> bool:
    """A running run has a worker: ask it to stop instead of ending the row here. The worker's own
    `finish` (lease-fenced) decides the eventual terminal status, same as any other stop reason."""
    row = await (await conn.execute(
        "UPDATE runs SET cancel_requested_at=coalesce(cancel_requested_at, now()), updated_at=now()"
        " WHERE id=%s AND status='running' RETURNING id", (run_id,),
    )).fetchone()
    return row is not None


def decode_run(row: dict[str, Any] | None, key: bytes | None) -> dict[str, Any] | None:
    """Return the row with its two encrypted columns decoded in place.

    Every reader of `runs.inputs`/`runs.outputs` goes through this: the columns are `bytea`, so a caller
    that forgets gets ciphertext where it expected a dict -- the worker would start a run from nonsense
    and the API would try to serialise bytes.
    """
    if row is None:
        return None
    decoded = dict(row)
    decoded["inputs"] = open_payload(key, "inputs", row.get("inputs"))
    decoded["outputs"] = open_payload(key, "outputs", row.get("outputs"))
    return decoded
