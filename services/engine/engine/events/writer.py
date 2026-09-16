"""Run-scoped event numbering (MVP design 7.2).

`seq` is allocated by incrementing `runs.event_seq` in the same transaction as the row that is being
written, so the API and the worker can both write events without colliding, and no number is skipped.
"""
from __future__ import annotations

from typing import Any

from psycopg import AsyncCursor
from psycopg.types.json import Jsonb


class RunGone(Exception):
    """The run row disappeared under us (deleted workflow). Nothing more can be recorded for it."""


async def append_event(
    cursor: AsyncCursor,
    run_id: str,
    event_type: str,
    *,
    node_id: str | None = None,
    exec_index: int | None = None,
    attempt: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert one event and return it in wire form (spec 7.1) for publishing after the commit.

    `ts` is Postgres's own `created_at` for the row (via `RETURNING`), not `datetime.now(UTC)` taken in
    this process (A5 of the whole-branch review): the published (live) copy and `events.stream._stored`'s
    replayed copy of the *same* event must carry the same timestamp. Stamping it here from the caller's
    clock instead disagreed with `created_at` by however long the transaction took to commit plus any
    clock skew, so a client that saw an event live and then again after an SSE reconnect saw two different
    `ts` values for one event.
    """
    await cursor.execute("UPDATE runs SET event_seq = event_seq + 1 WHERE id = %s RETURNING event_seq", (run_id,))
    row = await cursor.fetchone()
    if row is None:
        raise RunGone(run_id)
    seq = row["event_seq"]
    await cursor.execute(
        "INSERT INTO run_events (run_id, seq, type, node_id, exec_index, attempt, payload)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING created_at",
        (run_id, seq, event_type, node_id, exec_index, attempt, Jsonb(payload) if payload is not None else None),
    )
    inserted = await cursor.fetchone()
    return {
        "seq": seq,
        "runId": run_id,
        "type": event_type,
        "nodeId": node_id,
        "execIndex": exec_index,
        "attempt": attempt,
        "ts": inserted["created_at"].isoformat(),
        "payload": payload or {},
    }
