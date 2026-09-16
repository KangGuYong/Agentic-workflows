"""SSE stream for one run (MVP design 7.3).

Order matters: subscribe on Redis, wait for the subscription to be genuinely confirmed, *then* read what
is already stored in Postgres, then play the live messages -- de-duplicating and filling any gap from
Postgres. That ordering is the whole correctness argument: an event is always published to Redis only
after its `run_events` row has committed (see `engine.api.routers.runs._publish` and the worker's
recorder), so as long as our subscription is confirmed active before we take the initial Postgres read,
every event either shows up in that initial read (already committed by then) or arrives on the live
channel (we were already subscribed before it was published) -- there is no window in which it can fall
through both.

A plain `await asyncio.sleep(0)` after calling `pubsub.subscribe()` does not establish that: redis-py's
`subscribe()` only *sends* the SUBSCRIBE command and returns, it does not wait for Redis's reply (see
`engine.events.publish.run_events`). One scheduler tick is nowhere near a guaranteed network round trip.
`run_events` accepts a `ready` event for exactly this reason, and `event_stream` below waits on it before
doing anything else.

The live queue is bounded (`QUEUE_MAXSIZE`): without a bound, a client that cannot keep up would make it
grow without limit while the worker keeps publishing. Dropping the oldest queued event on overflow is safe
for exactly the reason above -- it only ever produces a `seq` gap, and the loop below already re-reads any
gap from Postgres, regardless of why it opened. The one event type with no `seq` at all (`node_token`,
transient) cannot be gap-filled; losing one to backpressure is an accepted degradation for a client that
has fallen behind, not silent loss from the durable record.

Each open stream holds one Redis pubsub connection for its lifetime and, briefly, one pooled Postgres
connection while paging in stored events. Nothing in this module caps how many streams can be open at
once -- that is bounded by the Redis client's own connection pool and by the size of
`request.app.state.pool` (an `AsyncConnectionPool`), the same as every other route.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from psycopg_pool import AsyncConnectionPool

from engine.events.publish import run_events

TERMINAL = {"run_succeeded", "run_failed", "run_cancelled"}
PING = ": ping\n\n"
QUEUE_MAXSIZE = 1000  # bound a stalled client's backlog; see the module docstring for why dropping is safe


def format_event(event: dict[str, Any]) -> str:
    lines = []
    if event.get("seq") is not None:  # transient events (e.g. node_token) get no id (design 7.3)
        lines.append(f"id: {event['seq']}")
    lines.append(f"event: {event['type']}")
    lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


async def _stored(pool: AsyncConnectionPool, run_id: str, after: int,
                  before: int | None = None) -> list[dict[str, Any]]:
    query = ("SELECT seq, type, node_id, exec_index, attempt, payload, created_at FROM run_events"
             " WHERE run_id=%s AND seq > %s")
    args: list[Any] = [run_id, after]
    if before is not None:
        query += " AND seq < %s"
        args.append(before)
    async with pool.connection() as conn:
        rows = await (await conn.execute(query + " ORDER BY seq", args)).fetchall()
    return [{"seq": row["seq"], "runId": run_id, "type": row["type"], "nodeId": row["node_id"],
             "execIndex": row["exec_index"], "attempt": row["attempt"],
             "ts": row["created_at"].isoformat(), "payload": row["payload"] or {}} for row in rows]


def _put_dropping_oldest(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    """Enqueue `event`, discarding the oldest pending one first if the queue is already full. Never
    blocks: the pump below is the queue's only producer, so nothing else can run between the `full()`
    check and the `put_nowait()` -- both are synchronous, with no `await` in between."""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait(event)


async def event_stream(pool: AsyncConnectionPool, redis: Any, run_id: str, *, after: int = 0,
                       ping_sec: float = 15.0, queue_maxsize: int = QUEUE_MAXSIZE) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_maxsize)
    ready = asyncio.Event()

    async def pump() -> None:
        try:
            async for event in run_events(redis, run_id, ready=ready):
                _put_dropping_oldest(queue, event)
        finally:
            # Unblock the waiter below even if the subscription never came up (a Redis error, or this
            # task being cancelled before it got that far) -- otherwise a failed subscribe would hang the
            # stream forever instead of surfacing the error.
            ready.set()

    subscriber = asyncio.create_task(pump())
    consumed_subscriber_result = False
    try:
        await ready.wait()
        if subscriber.done():
            consumed_subscriber_result = True
            await subscriber  # the pump ended before subscribing; surface why instead of streaming blind

        last = after
        for event in await _stored(pool, run_id, after):
            yield format_event(event)
            last = event["seq"]
            if event["type"] in TERMINAL:
                return
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), ping_sec)
            except TimeoutError:
                yield PING
                continue
            seq = event.get("seq")
            if seq is None:
                yield format_event(event)
                continue
            if seq <= last:
                continue  # already sent -- a duplicate delivery, or a reconnect replaying old ground
            if seq > last + 1:  # a publish never arrived or the queue dropped it: read the missing range
                for missed in await _stored(pool, run_id, last, before=seq):
                    yield format_event(missed)
                    last = missed["seq"]
            yield format_event(event)
            last = seq
            if event["type"] in TERMINAL:
                return
    finally:
        if not subscriber.done():
            subscriber.cancel()
        if not consumed_subscriber_result:
            with contextlib.suppress(asyncio.CancelledError):
                await subscriber
