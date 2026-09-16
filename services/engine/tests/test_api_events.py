import asyncio
import json

import pytest

from engine.events.publish import RedisPublisher, run_channel
from engine.events.stream import PING, TERMINAL, _put_dropping_oldest, event_stream
from engine.events.writer import append_event
from engine.llm.scripted import ScriptedLLM
from tests.factories import make_run
from tests.test_api_runs import _saved


async def _collect(api, url: str, *, headers: dict | None = None, limit: int = 50) -> list[dict]:
    """Read an SSE stream until it closes or `limit` events arrive."""
    events: list[dict] = []
    async with api.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        current: dict = {}
        async for line in response.aiter_lines():
            if line.startswith("id:"):
                current["id"] = int(line[3:].strip())
            elif line.startswith("data:"):
                current["data"] = json.loads(line[5:].strip())
            elif line == "":
                if current.get("data"):
                    events.append(current)
                    if len(events) >= limit or current["data"]["type"] in (
                            "run_succeeded", "run_failed", "run_cancelled"):
                        break
                current = {}
    return events


def _ids(events: list[dict]) -> list[int]:
    """The `id`s of the events that carry one. A run through the worker also emits `node_token` events
    (the LLM node streams its whole scripted response as one token), which carry no id by design (design
    7.3, `test_token_events_carry_no_id` below) -- they show up in `_collect`'s list like any other event
    but must be excluded before checking that ids are gapless."""
    return [event["id"] for event in events if "id" in event]


def _type_and_seq(chunk: str) -> tuple[str, int | None]:
    """Pull `type`/`seq` out of one `format_event()` chunk, for tests that drive `event_stream` directly."""
    data_line = next(line for line in chunk.splitlines() if line.startswith("data:"))
    payload = json.loads(data_line[len("data:"):].strip())
    return payload["type"], payload.get("seq")


async def test_a_finished_run_replays_from_the_start_and_closes(api, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    events = await asyncio.wait_for(_collect(api, f"/runs/{run['runId']}/events"), 30)

    types = [event["data"]["type"] for event in events]
    assert types[0] == "run_queued" and types[-1] == "run_succeeded"
    assert _ids(events) == list(range(1, len(_ids(events)) + 1))  # gapless ids, ignoring node_token


async def test_reconnecting_with_last_event_id_skips_what_was_seen(api, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    first = await asyncio.wait_for(_collect(api, f"/runs/{run['runId']}/events"), 30)

    again = await asyncio.wait_for(
        _collect(api, f"/runs/{run['runId']}/events", headers={"Last-Event-ID": "2"}), 30)

    assert _ids(again) == [event_id for event_id in _ids(first) if event_id > 2]


async def test_a_dropped_publish_is_filled_in_from_postgres(api, pool, redis):
    """The stream must not depend on Redis delivering every message."""
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn, conn.transaction():
        for index in range(1, 4):
            await append_event(conn.cursor(), run_id, "node_started", node_id=f"n{index}",
                               exec_index=1, attempt=1)
        last = await append_event(conn.cursor(), run_id, "run_succeeded")

    async def publish_last_only():
        await asyncio.sleep(0.3)
        await RedisPublisher(redis).publish(run_id, last)  # events 1-3 were never published

    task = asyncio.create_task(publish_last_only())
    events = await asyncio.wait_for(_collect(api, f"/runs/{run_id}/events?after=1"), 20)
    await task

    assert [event["id"] for event in events] == [2, 3, 4]


async def test_token_events_carry_no_id(api, pool, redis):
    run_id = await make_run(pool, status="running")

    async def publish():
        await asyncio.sleep(0.3)
        publisher = RedisPublisher(redis)
        await publisher.publish(run_id, {"runId": run_id, "type": "node_token", "nodeId": "llm_1",
                                         "payload": {"text": "안"}})
        async with pool.connection() as conn, conn.transaction():
            event = await append_event(conn.cursor(), run_id, "run_succeeded")
        await publisher.publish(run_id, event)

    task = asyncio.create_task(publish())
    events = await asyncio.wait_for(_collect(api, f"/runs/{run_id}/events"), 20)
    await task

    token = [event for event in events if event["data"]["type"] == "node_token"]
    assert token and "id" not in token[0]  # Last-Event-ID must not move past a transient event


async def test_a_non_uuid_or_unknown_run_id_is_a_404_not_a_500(api):
    import uuid

    assert (await api.get("/runs/not-a-uuid/events")).status_code == 404
    assert (await api.get(f"/runs/{uuid.uuid4()}/events")).status_code == 404


async def test_a_malformed_last_event_id_or_after_falls_back_to_a_full_replay(api, worker_factory):
    """`int()` on a client-controlled string that only passed `.isdigit()` is a CPU stall (an unbounded
    digit string) and, for some non-ASCII "digits", a 500 (the same class of bug Task 11's review found
    in `Content-Length`). None of these should be anything but a full replay from 0 (Task 14 item 3)."""
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    for after_value in ("not-a-number", "-1", "1.5", "²", "9" * 10_000):
        events = await asyncio.wait_for(
            _collect(api, f"/runs/{run['runId']}/events?after={after_value}"), 30)
        assert events[0]["data"]["type"] == "run_queued"  # never a 500, never partial -- a full replay

    # Same guard applies to the header, which real clients (not just hostile ones) send on reconnect.
    # httpx's own `Headers` refuses a non-ASCII `str` header value outright, so the raw byte is passed
    # directly -- matching how it actually reaches the server: h11 permits obs-text in header values and
    # Starlette decodes them as latin-1 (Task 11 review), the same path a superscript-two would take.
    events = await asyncio.wait_for(
        _collect(api, f"/runs/{run['runId']}/events", headers={"Last-Event-ID": "²".encode("latin-1")}),
        30)
    assert events[0]["data"]["type"] == "run_queued"


async def test_run_events_ready_signals_after_the_subscription_is_confirmed(redis):
    """`ready` must fire only once Redis has actually acknowledged the SUBSCRIBE, not on a bare scheduler
    tick -- otherwise a publish that races the caller is lost, which is exactly the bug `event_stream`'s
    "subscribe first" design exists to prevent (Task 14 item 1)."""
    from engine.events.publish import run_events

    ready = asyncio.Event()
    gen = run_events(redis, "race-run", ready=ready)
    getter = asyncio.ensure_future(gen.__anext__())
    await asyncio.wait_for(ready.wait(), 5)

    # No artificial delay: if `ready` fired early (e.g. after one `asyncio.sleep(0)`, which only confirms
    # the SUBSCRIBE command was *sent*), Redis might not have registered the subscription yet and this
    # publish would never be delivered -- `getter` would then hang until the timeout below.
    await RedisPublisher(redis).publish("race-run", {"type": "ping-event"})

    event = await asyncio.wait_for(getter, 5)
    assert event["type"] == "ping-event"
    await gen.aclose()


async def test_a_publish_racing_the_subscription_is_not_lost(api, pool, redis):
    """A single, terminal event published with no delay right as the stream opens must still arrive. With
    only one event ever published for this run, there is nothing later to trigger gap-fill -- a lost
    subscribe race here would hang the stream until the test's own timeout, not just skip an id (Task 14
    item 1, exercised through the real route and `event_stream`, not just the `run_events` primitive)."""
    run_id = await make_run(pool, status="running")

    async def publish_immediately():
        async with pool.connection() as conn, conn.transaction():
            event = await append_event(conn.cursor(), run_id, "run_succeeded")
        await RedisPublisher(redis).publish(run_id, event)

    task = asyncio.create_task(publish_immediately())
    events = await asyncio.wait_for(_collect(api, f"/runs/{run_id}/events"), 20)
    await task

    assert [event["data"]["type"] for event in events] == ["run_succeeded"]


def test_bounded_queue_drops_the_oldest_pending_event():
    """The live queue behind the pump must not grow without bound when nobody is draining it; the oldest
    entry is discarded first (Task 14 item 2). Safety for what this drops is proven end-to-end by
    `test_overflowing_the_queue_drops_the_oldest_but_gap_filling_recovers_it` below."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=3)
    for seq in range(1, 6):
        _put_dropping_oldest(queue, {"seq": seq})

    assert queue.qsize() == 3
    assert [queue.get_nowait()["seq"] for _ in range(3)] == [3, 4, 5]


async def test_overflowing_the_queue_drops_the_oldest_but_gap_filling_recovers_it(pool, redis):
    """Publish far more events than a one-slot queue can hold, with the stream never draining in between,
    then resume reading: every event must still show up, in order, with none skipped. The drop only ever
    creates a `seq` gap, and `event_stream` always re-reads a gap from Postgres regardless of how it
    opened -- so this holds no matter how much the tiny queue actually dropped (Task 14 item 2)."""
    run_id = await make_run(pool, status="running")
    agen = event_stream(pool, redis, run_id, after=0, ping_sec=0.05, queue_maxsize=1).__aiter__()

    first = await asyncio.wait_for(agen.__anext__(), 5)
    assert first == PING  # proves the subscription is already live, with nothing published yet

    total = 6  # well beyond queue_maxsize=1
    events = []
    async with pool.connection() as conn, conn.transaction():
        for _ in range(total - 1):
            events.append(await append_event(conn.cursor(), run_id, "node_started", node_id="n",
                                             exec_index=1, attempt=1))
        events.append(await append_event(conn.cursor(), run_id, "run_succeeded"))

    publisher = RedisPublisher(redis)
    for event in events:
        await publisher.publish(run_id, event)
    await asyncio.sleep(0.3)  # best-effort: let the pump receive (and mostly drop) the backlog

    seqs: list[int] = []
    while True:
        chunk = await asyncio.wait_for(agen.__anext__(), 10)
        if chunk == PING:
            continue
        event_type, seq = _type_and_seq(chunk)
        if seq is not None:
            seqs.append(seq)
        if event_type in TERMINAL:
            break

    assert seqs == list(range(1, total + 1))
    await agen.aclose()


async def test_client_disconnect_closes_the_subscription_and_leaves_nothing_pending(pool, redis):
    """Closing the stream mid-flight (the browser tab closing) must release the Redis subscription and
    leave no pump task behind -- checked directly against Redis's own subscriber count rather than
    inferred from behaviour (Task 14 item 4)."""
    run_id = await make_run(pool, status="running")
    channel = run_channel(run_id)
    assert (await redis.pubsub_numsub(channel))[0][1] == 0

    agen = event_stream(pool, redis, run_id, after=0, ping_sec=0.05).__aiter__()
    first = await asyncio.wait_for(agen.__anext__(), 5)
    assert first == PING  # the subscription is live: nothing stored, nothing published yet
    assert (await redis.pubsub_numsub(channel))[0][1] == 1

    await agen.aclose()  # what Starlette does to the body iterator on a client disconnect

    assert (await redis.pubsub_numsub(channel))[0][1] == 0


async def test_a_waiting_run_stays_open_and_pings_without_an_id(pool, redis):
    """A run parked on an approval (`waiting`) has no terminal event yet, so the stream must stay open and
    keep the connection alive with pings -- and a ping must carry no `id:` line, since it is not a stored
    event with a `seq` (Task 14 item 5)."""
    run_id = await make_run(pool, status="waiting")
    agen = event_stream(pool, redis, run_id, after=0, ping_sec=0.05).__aiter__()

    first = await asyncio.wait_for(agen.__anext__(), 5)
    second = await asyncio.wait_for(agen.__anext__(), 5)

    assert first == PING and second == PING
    assert "id:" not in first
    await agen.aclose()


async def test_the_stream_ends_exactly_once_at_the_terminal_event(pool, redis):
    """Once a terminal event has been yielded, the generator must end -- not loop back, not re-check, not
    yield anything else (Task 14 item 5)."""
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn, conn.transaction():
        await append_event(conn.cursor(), run_id, "run_succeeded")

    agen = event_stream(pool, redis, run_id, after=0, ping_sec=5).__aiter__()
    first = await asyncio.wait_for(agen.__anext__(), 5)
    assert "run_succeeded" in first

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), 5)
