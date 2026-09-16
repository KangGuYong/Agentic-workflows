"""Run-scoped event numbering (MVP design 7.2).

`seq` is allocated by incrementing `runs.event_seq` in the same transaction as the row that is being
written, so the API and the worker can both write events without colliding, and no number is skipped.
"""
from __future__ import annotations

from datetime import UTC, datetime
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
    """Insert one event and return it in wire form (spec 7.1) for publishing after the commit."""
    await cursor.execute("UPDATE runs SET event_seq = event_seq + 1 WHERE id = %s RETURNING event_seq", (run_id,))
    row = await cursor.fetchone()
    if row is None:
        raise RunGone(run_id)
    seq = row["event_seq"]
    await cursor.execute(
        "INSERT INTO run_events (run_id, seq, type, node_id, exec_index, attempt, payload)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (run_id, seq, event_type, node_id, exec_index, attempt, Jsonb(payload) if payload is not None else None),
    )
    return {
        "seq": seq,
        "runId": run_id,
        "type": event_type,
        "nodeId": node_id,
        "execIndex": exec_index,
        "attempt": attempt,
        "ts": datetime.now(UTC).isoformat(),
        "payload": payload or {},
    }
