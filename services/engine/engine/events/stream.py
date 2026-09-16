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
import logging
from collections.abc import AsyncIterator
from typing import Any

from psycopg_pool import AsyncConnectionPool

from engine.events.publish import run_events
from engine.jsondata import safe_text

log = logging.getLogger(__name__)

TERMINAL = {"run_succeeded", "run_failed", "run_cancelled"}
PING = ": ping\n\n"
QUEUE_MAXSIZE = 1000  # bound a stalled client's backlog; see the module docstring for why dropping is safe
STORED_PAGE_SIZE = 500  # bound each `_stored` round trip; see `_stored_all` for why this must be paged


def _sanitize(value: Any) -> Any:
    """Scrub NUL/lone-surrogate text before it reaches `json.dumps(..., ensure_ascii=False)` (mirrors
    `routers.runs._sanitize`). Every other event body passes through `check_text` on its way into
    `run_events` (the recorder's `_safe`), but `node_token` is published straight from the LLM's raw
    streamed text -- fed from `ollama._content`'s `json.loads` -- with no such check in between."""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {safe_text(key): _sanitize(item) for key, item in value.items()}
    return value


def format_event(event: dict[str, Any]) -> str:
    event = _sanitize(event)
    lines = []
    if event.get("seq") is not None:  # transient events (e.g. node_token) get no id (design 7.3)
        lines.append(f"id: {event['seq']}")
    lines.append(f"event: {event['type']}")
    lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


async def _stored(pool: AsyncConnectionPool, run_id: str, after: int, before: int | None = None,
                  *, limit: int = STORED_PAGE_SIZE) -> list[dict[str, Any]]:
    """One page of stored events, oldest first. Bounded by `limit`: see `_stored_all`, which is what every
    caller in this module actually uses -- this is not called directly outside this module."""
    query = ("SELECT seq, type, node_id, exec_index, attempt, payload, created_at FROM run_events"
             " WHERE run_id=%s AND seq > %s")
    args: list[Any] = [run_id, after]
    if before is not None:
        query += " AND seq < %s"
        args.append(before)
    query += " ORDER BY seq LIMIT %s"
    args.append(limit)
    async with pool.connection() as conn:
        rows = await (await conn.execute(query, args)).fetchall()
    return [{"seq": row["seq"], "runId": run_id, "type": row["type"], "nodeId": row["node_id"],
             "execIndex": row["exec_index"], "attempt": row["attempt"],
             "ts": row["created_at"].isoformat(), "payload": row["payload"] or {}} for row in rows]


async def _stored_all(pool: AsyncConnectionPool, run_id: str, after: int, before: int | None = None,
                      *, page_size: int = STORED_PAGE_SIZE) -> AsyncIterator[dict[str, Any]]:
    """Every stored event after `after` (and, if given, before `before`), oldest first, paged.

    A single unbounded `SELECT ... WHERE run_id=%s AND seq > %s` (the pre-fix `_stored`) read every
    matching row into memory in one `fetchall()` before the caller could yield a single byte -- the same
    class of bug Task 13's review fixed on the sibling `GET /runs/{id}/nodes` route. The engine's
    recursion limit permits on the order of 20,000 node executions per run, each writing at least two
    events with previews up to `MAX_EVENT_PREVIEW_BYTES`, so a full replay (`GET /runs/{id}/events` with no
    `Last-Event-ID`) or a large gap-fill (the `before=` path below) could pull roughly 10**5 rows and
    hundreds of MB before ever yielding. Looping a bounded `_stored` here keeps each round trip and each
    batch of rows bounded, while still handing every caller a single ordered stream (A3 of the whole-branch
    review).
    """
    last = after
    while True:
        page = await _stored(pool, run_id, last, before, limit=page_size)
        for event in page:
            yield event
            last = event["seq"]
        if len(page) < page_size:  # a short (or empty) page means there is nothing more to fetch
            return


def _put_dropping_oldest(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    """Enqueue `event`, discarding the oldest pending one first if the queue is already full. Never
    blocks: the pump below is the queue's only producer, so nothing else can run between the `full()`
    check and the `put_nowait()` -- both are synchronous, with no `await` in between."""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait(event)


async def event_stream(pool: AsyncConnectionPool, redis: Any, run_id: str, *, after: int = 0,
                       ping_sec: float = 15.0, queue_maxsize: int = QUEUE_MAXSIZE,
                       page_size: int = STORED_PAGE_SIZE) -> AsyncIterator[str]:
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
        async for event in _stored_all(pool, run_id, after, page_size=page_size):
            yield format_event(event)
            last = event["seq"]
            if event["type"] in TERMINAL:
                return
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), ping_sec)
            except TimeoutError:
                if subscriber.done():
                    return  # the subscription is gone; end the stream so the client reconnects
                async for missed in _stored_all(pool, run_id, last, page_size=page_size):
                    yield format_event(missed)
                    last = missed["seq"]
                    if missed["type"] in TERMINAL:
                        return
                yield PING
                continue
            seq = event.get("seq")
            if seq is None:
                yield format_event(event)
                continue
            if seq <= last:
                continue  # already sent -- a duplicate delivery, or a reconnect replaying old ground
            if seq > last + 1:  # a publish never arrived or the queue dropped it: read the missing range
                async for missed in _stored_all(pool, run_id, last, seq, page_size=page_size):
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
            try:
                await subscriber
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                # The pump failed (e.g. the Redis connection died mid-stream). The loop above already
                # ended the response for the client; re-raising here would only turn a clean close into
                # an unhandled ASGI exception out of `aclose()` -- log it instead (Task 14 review A2).
                log.warning("event pump failed for run %s", run_id, exc_info=True)
