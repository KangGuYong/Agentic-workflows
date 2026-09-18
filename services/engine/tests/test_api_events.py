import asyncio
import contextlib
import json

import pytest

from engine.events.publish import RedisPublisher, run_channel, run_events
from engine.events.stream import PING, TERMINAL, _put_dropping_oldest, event_stream, format_event
from engine.events.writer import append_event
from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.factories import make_run
from tests.test_api_runs import _saved


async def _finish(conn, run_id: str, type_: str = "run_succeeded", **kwargs):
    """Append a terminal event *and* move the run to the matching status, in one transaction — exactly
    what `Worker._terminal` does. The stream re-reads `runs.status` when it meets a terminal event
    (2b design §8.3), so a terminal event sitting on a row still marked `running` is a shape the engine
    cannot produce, and a test that builds one is not testing the engine."""
    await conn.execute("UPDATE runs SET status=%s WHERE id=%s", (type_.removeprefix("run_"), run_id))
    return await append_event(conn.cursor(), run_id, type_, **kwargs)


class _FakePubSub:
    """A `redis.asyncio` pubsub double whose `listen()` yields nothing until the test explicitly feeds
    it a message -- unlike the real Redis container, which confirms a subscription in milliseconds and
    so can never deterministically prove code waits for that confirmation (see `test_a_dropped_publish...`
    and friends, which need real timing; these tests need the *absence* of a signal to stay pending)."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:  # only *sends*, like the real client -- see publish.py
        return None

    async def confirm_subscribed(self) -> None:
        await self._queue.put({"type": "subscribe"})

    async def deliver(self, data: str) -> None:
        await self._queue.put({"type": "message", "data": data})

    async def die(self, exc: BaseException) -> None:
        await self._queue.put(exc)

    async def listen(self):
        while True:
            item = await self._queue.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self) -> None:
        return None


class _FakeRedis:
    def __init__(self, pubsub: _FakePubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> _FakePubSub:
        return self._pubsub


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
    assert _ids(events)  # non-empty: a filter that (bug) drops every id would make the next line vacuous
    assert _ids(events) == list(range(1, len(_ids(events)) + 1))  # gapless ids, ignoring node_token


async def test_reconnecting_with_last_event_id_skips_what_was_seen(api, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    first = await asyncio.wait_for(_collect(api, f"/runs/{run['runId']}/events"), 30)

    again = await asyncio.wait_for(
        _collect(api, f"/runs/{run['runId']}/events", headers={"Last-Event-ID": "2"}), 30)

    assert _ids(first) and _ids(again)  # non-empty: guards against a filter that vacuously drops everything
    assert _ids(again) == [event_id for event_id in _ids(first) if event_id > 2]


async def test_a_dropped_publish_is_filled_in_from_postgres(api, pool, redis):
    """The stream must not depend on Redis delivering every message."""
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn, conn.transaction():
        for index in range(1, 4):
            await append_event(conn.cursor(), run_id, "node_started", node_id=f"n{index}",
                               exec_index=1, attempt=1)
        last = await _finish(conn, run_id, "run_succeeded")

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
            event = await _finish(conn, run_id, "run_succeeded")
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


async def test_run_events_ready_does_not_fire_before_the_subscribe_confirmation():
    """The deterministic counterpart to the test above: reverting `ready`'s wait to fire right after
    `pubsub.subscribe()` returns (instead of on the first `{"type": "subscribe"}` reply) must fail this,
    since our fake `subscribe()` returns instantly but never queues a confirmation on its own (Task 14
    item 1 / B1) -- unlike the real Redis container, where `subscribe()` returning and the confirmation
    arriving are both fast enough that a bug here can hide behind real network timing."""
    pubsub = _FakePubSub()
    ready = asyncio.Event()
    gen = run_events(_FakeRedis(pubsub), "race-run", ready=ready)
    getter = asyncio.ensure_future(gen.__anext__())
    try:
        await asyncio.sleep(0.2)
        assert not ready.is_set()  # subscribe() returned long ago; only the confirmation may set `ready`

        await pubsub.confirm_subscribed()
        await asyncio.wait_for(ready.wait(), 5)
    finally:
        getter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await getter
        await gen.aclose()


async def test_event_stream_does_not_read_postgres_before_the_subscription_is_confirmed(pool):
    """The `event_stream`-level counterpart to the two tests above: with a fake pubsub that withholds its
    subscribe confirmation, the generator must still be blocked on `ready` -- and so must not have taken
    its initial Postgres read yet -- even though a terminal event has been sitting in `run_events` the
    whole time (Task 14 item 1 / B1)."""
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn, conn.transaction():
        await _finish(conn, run_id, "run_succeeded")

    pubsub = _FakePubSub()
    agen = event_stream(pool, _FakeRedis(pubsub), run_id, after=0, ping_sec=5).__aiter__()
    getter = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.2)
    assert not getter.done()  # still waiting on the subscribe confirmation; the stored row was not read

    await pubsub.confirm_subscribed()
    first = await asyncio.wait_for(getter, 5)
    assert "run_succeeded" in first  # released the instant the subscription was actually confirmed

    await agen.aclose()


async def test_a_publish_racing_the_subscription_is_not_lost(api, pool, redis):
    """A single, terminal event published with no delay right as the stream opens must still arrive. With
    only one event ever published for this run, there is nothing later to trigger gap-fill -- a lost
    subscribe race here would hang the stream until the test's own timeout, not just skip an id (Task 14
    item 1, exercised through the real route and `event_stream`, not just the `run_events` primitive)."""
    run_id = await make_run(pool, status="running")

    async def publish_immediately():
        async with pool.connection() as conn, conn.transaction():
            event = await _finish(conn, run_id, "run_succeeded")
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
        events.append(await _finish(conn, run_id, "run_succeeded"))

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
        await _finish(conn, run_id, "run_succeeded")

    agen = event_stream(pool, redis, run_id, after=0, ping_sec=5).__aiter__()
    first = await asyncio.wait_for(agen.__anext__(), 5)
    assert "run_succeeded" in first

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), 5)


async def test_gap_fill_range_excludes_the_event_that_triggered_it(pool, redis):
    """A live event that opens a gap (`seq > last + 1`) must trigger a gap-fill read of exactly the
    events strictly between what was last sent and it -- not including itself, since it is yielded
    separately right after. Making the range inclusive (`before=seq+1`) would re-read and re-yield the
    triggering event a second time (Task 14 item 2 / B2). The triggering event here is deliberately
    non-terminal, and a further terminal event follows it: the existing suite's only gap fill is triggered
    by a terminal event, and every collector in it stops at the first terminal it sees -- which, under the
    inclusive-range mutant, is the duplicate's *first* copy, so a regression that only shows up in a second,
    extra copy right after it slips past unnoticed there. Collecting up to (and including) a *later*
    terminal event, instead of stopping as soon as four ids have arrived, is what actually surfaces it: a
    naive `while len(seqs) < 4` would itself stop one item too early and miss the duplicate the same way."""
    run_id = await make_run(pool, status="running")
    agen = event_stream(pool, redis, run_id, after=0, ping_sec=0.05, queue_maxsize=10).__aiter__()
    first = await asyncio.wait_for(agen.__anext__(), 5)
    assert first == PING  # subscribed, nothing stored or published yet

    async with pool.connection() as conn, conn.transaction():
        skipped = [await append_event(conn.cursor(), run_id, "node_started", node_id=f"n{i}",
                                      exec_index=1, attempt=1) for i in range(1, 4)]
        fourth = await append_event(conn.cursor(), run_id, "node_started", node_id="n4",
                                    exec_index=1, attempt=1)
        fifth = await _finish(conn, run_id, "run_succeeded")
    assert [event["seq"] for event in skipped] == [1, 2, 3]

    publisher = RedisPublisher(redis)
    await publisher.publish(run_id, fourth)  # only events 4 and 5 are published live -- 1-3 are gap-filled
    await publisher.publish(run_id, fifth)

    seqs: list[int] = []
    while True:
        chunk = await asyncio.wait_for(agen.__anext__(), 5)
        if chunk == PING:
            continue
        event_type, seq = _type_and_seq(chunk)
        seqs.append(seq)
        if event_type == "run_succeeded":
            break

    assert seqs == [1, 2, 3, 4, 5]  # an inclusive `before` would produce [1, 2, 3, 4, 4, 5]
    await agen.aclose()


async def test_a_live_duplicate_of_an_already_sent_event_is_not_re_emitted(pool, redis):
    """A live delivery that repeats an event the initial stored replay already sent must be swallowed, not
    re-yielded -- and, worse than a visible duplicate, must not move `last` backwards: without the `seq <=
    last: continue` guard, `last` regresses to the duplicate's (already-sent) seq and the *next* real event
    then looks like it opened a gap, re-reading and re-emitting the whole range that was already sent
    (Task 14 item 3 / B3). The existing suite's reconnect test can't catch this because it filters
    duplicates in SQL (`Last-Event-ID`), never exercising the live dedupe path at all."""
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn, conn.transaction():
        await append_event(conn.cursor(), run_id, "node_started", node_id="n1",
                           exec_index=1, attempt=1)
        second_event = await append_event(conn.cursor(), run_id, "node_started", node_id="n2",
                                          exec_index=1, attempt=1)

    agen = event_stream(pool, redis, run_id, after=0, ping_sec=5, queue_maxsize=10).__aiter__()
    stored = [await asyncio.wait_for(agen.__anext__(), 5) for _ in range(2)]
    assert [_type_and_seq(chunk)[1] for chunk in stored] == [1, 2]  # both already sent by the initial read

    async with pool.connection() as conn, conn.transaction():
        third_event = await append_event(conn.cursor(), run_id, "node_started", node_id="n3",
                                         exec_index=1, attempt=1)

    publisher = RedisPublisher(redis)
    await publisher.publish(run_id, second_event)  # a duplicate delivery of the already-sent event 2
    await publisher.publish(run_id, third_event)

    chunk = await asyncio.wait_for(agen.__anext__(), 5)
    event_type, seq = _type_and_seq(chunk)
    # The duplicate must not surface as its own chunk, and `last` must not have regressed: the very next
    # thing out of the generator is event 3, delivered plainly, with no re-read of 1-2 in front of it.
    assert (event_type, seq) == ("node_started", 3)

    await agen.aclose()


async def test_the_live_queue_stays_bounded(pool, redis, monkeypatch):
    """The overflow test elsewhere in this file (`test_overflowing_the_queue_drops_the_oldest...`) only
    asserts every seq eventually arrives in order, which is equally true of an unbounded queue that never
    drops anything -- so restoring the plan's original unbounded queue would still pass it. This spies on
    the actual `asyncio.Queue` instance `event_stream` builds and checks it never grows past its bound,
    even with a large backlog of undrained publishes sitting in front of it (Task 14 item 2 / B4)."""
    import engine.events.stream as stream_module

    captured: list[asyncio.Queue] = []
    real_queue_cls = asyncio.Queue

    class _SpyQueue(real_queue_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self)

    class _AsyncioProxy:
        """Delegates everything to the real `asyncio` module except `Queue`, so only this module's own
        `asyncio.Queue(...)` call is intercepted -- the rest of the interpreter's asyncio machinery
        (event loop internals included) keeps using the real class untouched."""

        Queue = _SpyQueue

        def __getattr__(self, name):
            return getattr(asyncio, name)

    monkeypatch.setattr(stream_module, "asyncio", _AsyncioProxy())

    run_id = await make_run(pool, status="running")
    agen = event_stream(pool, redis, run_id, after=0, ping_sec=0.2, queue_maxsize=4).__aiter__()
    first = await asyncio.wait_for(agen.__anext__(), 5)
    assert first == PING
    assert len(captured) == 1
    queue = captured[0]

    publisher = RedisPublisher(redis)
    async with pool.connection() as conn, conn.transaction():
        for i in range(20):  # well beyond queue_maxsize=4, and never drained from the generator side
            event = await append_event(conn.cursor(), run_id, "node_started", node_id="n",
                                       exec_index=1, attempt=1)
            await publisher.publish(run_id, event)
    await asyncio.sleep(0.3)  # let the pump actually receive and enqueue the backlog

    assert queue.qsize() == 4  # bounded at queue_maxsize, not grown to 20

    await agen.aclose()


async def test_replay_and_gap_fill_both_page_through_more_events_than_one_page(pool, redis):
    """A3 of the whole-branch review: `_stored` used to be one unbounded `SELECT ... ORDER BY seq` with no
    `LIMIT`, `fetchall()`'d into memory -- the same bug Task 13's review fixed on the sibling
    `GET /runs/{id}/nodes` route. `page_size=3` stands in for the production `STORED_PAGE_SIZE` so this
    does not need to generate hundreds of events to prove the caller actually loops: the initial replay
    (7 events) and the live gap-fill path (9 more, never published, found only when a later published
    event opens a gap past them) must each still deliver every event, in order, with none skipped or
    duplicated, even though neither fits in one page."""
    run_id = await make_run(pool, status="running")
    agen = event_stream(pool, redis, run_id, after=0, ping_sec=5, queue_maxsize=20, page_size=3).__aiter__()

    async with pool.connection() as conn, conn.transaction():
        first_batch = [await append_event(conn.cursor(), run_id, "node_started", node_id=f"n{i}",
                                          exec_index=1, attempt=1) for i in range(1, 8)]  # seq 1-7

    seqs: list[int] = []
    while len(seqs) < len(first_batch):
        chunk = await asyncio.wait_for(agen.__anext__(), 5)
        if chunk == PING:
            continue
        _, seq = _type_and_seq(chunk)
        seqs.append(seq)
    assert seqs == list(range(1, 8))  # the initial replay paged through more than two pages, in order

    # Nine more stored events, never published live, then one live terminal event far ahead of them: the
    # gap it opens (seq 8-16) must be filled by paging, not by a single unbounded read.
    async with pool.connection() as conn, conn.transaction():
        for i in range(1, 10):
            await append_event(conn.cursor(), run_id, "node_started", node_id=f"g{i}", exec_index=1, attempt=1)
        terminal = await _finish(conn, run_id, "run_succeeded")  # seq 17

    await RedisPublisher(redis).publish(run_id, terminal)

    while True:
        chunk = await asyncio.wait_for(agen.__anext__(), 5)
        if chunk == PING:
            continue
        event_type, seq = _type_and_seq(chunk)
        seqs.append(seq)
        if event_type == "run_succeeded":
            break

    assert seqs == list(range(1, 18))  # gap-filled 8-16 and the terminal event, gapless and in order
    await agen.aclose()


async def test_the_live_and_replayed_copies_of_one_event_share_the_same_ts(pool):
    """A5 of the whole-branch review: `append_event`'s returned dict (published live over Redis) used to
    stamp `ts` with `datetime.now(UTC)` taken in this process before its transaction committed, while
    `_stored`'s replay (an SSE reconnect) reads the row's actual `created_at`. A client that saw `seq=1`
    live and then again after a reconnect would see two different timestamps for the *same* event, off by
    the commit latency plus any clock skew. `append_event` now returns the column Postgres itself
    assigned via `RETURNING created_at`, so both copies agree exactly."""
    run_id = await make_run(pool, status="running")

    async with pool.connection() as conn, conn.transaction():
        published = await append_event(conn.cursor(), run_id, "run_queued")

    stored = (await _stored_events(pool, run_id))[0]
    assert published["ts"] == stored["ts"]


async def _stored_events(pool, run_id: str) -> list[dict]:
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT seq, created_at FROM run_events WHERE run_id=%s ORDER BY seq", (run_id,))).fetchall()
    return [{"seq": row["seq"], "ts": row["created_at"].isoformat()} for row in rows]


async def test_a_terminal_event_only_in_postgres_ends_the_stream(pool, redis):
    """A publish that never reached Redis (the worker's publish is best-effort everywhere) must not leave
    the stream open forever: the next idle tick re-reads Postgres, finds the terminal event sitting there,
    and ends the stream -- instead of pinging indefinitely (Task 14 item 1 / A1 / B5)."""
    run_id = await make_run(pool, status="running")
    agen = event_stream(pool, redis, run_id, after=0, ping_sec=0.05).__aiter__()
    first = await asyncio.wait_for(agen.__anext__(), 5)
    assert first == PING  # subscribed; nothing stored or published yet

    async with pool.connection() as conn, conn.transaction():
        await _finish(conn, run_id, "run_succeeded")  # committed, but never published

    chunk = await asyncio.wait_for(agen.__anext__(), 2)  # within a ping interval or two, not "forever"
    assert "run_succeeded" in chunk

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), 2)


async def test_a_dead_pump_keeps_serving_from_postgres_instead_of_ending(pool):
    """Task 13 reverses this test's original contract, on purpose.

    It used to assert that a pump whose Redis connection died mid-stream ended the stream, so the client
    would reconnect. Every event is durably in `run_events`, so the stream can simply read from there
    instead: a Redis outage should make the editor slower, not disconnect it (2b design §8.2). What still
    must not happen is the old bug this test was written for — pinging forever while silently delivering
    nothing.
    """
    pubsub = _FakePubSub()
    run_id = await make_run(pool, status="running")
    agen = event_stream(pool, _FakeRedis(pubsub), run_id, after=0, ping_sec=0.05).__aiter__()
    getter = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)
    await pubsub.confirm_subscribed()
    assert await asyncio.wait_for(getter, 5) == PING

    await pubsub.die(ConnectionError("boom"))

    try:
        # Written while there is no subscription at all: only the Postgres path can deliver it.
        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "node_started", node_id="n1", exec_index=1,
                               attempt=1)
        delivered = None
        for _ in range(20):
            chunk = await asyncio.wait_for(agen.__anext__(), 2)
            if "node_started" in chunk:
                delivered = chunk
                break
        assert delivered is not None, "the stream pinged on without delivering what Postgres had"

        async with pool.connection() as conn, conn.transaction():
            await _finish(conn, run_id, "run_succeeded")
        for _ in range(20):
            try:
                chunk = await asyncio.wait_for(agen.__anext__(), 2)
            except StopAsyncIteration:
                break
            if "run_succeeded" in chunk:
                continue
        else:
            pytest.fail("the degraded stream never ended once the run finished")
    finally:
        with contextlib.suppress(Exception):
            await agen.aclose()


async def test_a_client_disconnect_after_the_pump_died_does_not_raise(pool):
    """`aclose()` is what Starlette calls on a client disconnect. If the pump already failed by then, the
    `finally` block used to surface that failure by re-raising it out of `await subscriber` -- turning an
    ordinary disconnect into an unhandled ASGI exception instead of a clean close (Task 14 item 1 / A2 /
    B5)."""
    pubsub = _FakePubSub()
    run_id = await make_run(pool, status="running")
    agen = event_stream(pool, _FakeRedis(pubsub), run_id, after=0, ping_sec=0.2).__aiter__()
    getter = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)
    await pubsub.confirm_subscribed()
    first = await asyncio.wait_for(getter, 5)
    assert first == PING

    await pubsub.die(ConnectionError("boom"))
    await asyncio.sleep(0.1)  # give the pump task a chance to actually finish with that exception

    await agen.aclose()  # must not raise, even though the pump ended abnormally


async def test_a_malformed_message_on_the_channel_does_not_kill_the_stream(pool, redis):
    """Anyone can PUBLISH on `run:{id}` (spec 7.3's own design leans on that). A message that decodes as
    JSON but isn't shaped like one of our events -- missing `type`, or `seq` of the wrong type -- must be
    dropped rather than crash the stream with a `KeyError`/`TypeError` that uvicorn turns into a silently
    dropped connection, and the stream must still deliver the next real event afterwards (Task 14 item 3 /
    A3 / B5). Nothing is stored for this run before the junk arrives, deliberately: with something already
    in Postgres, the gap-fill a bad `seq` triggers can incidentally re-read and yield a *real* stored event
    before ever formatting the malformed one, masking exactly the crash this test exists to catch."""
    run_id = await make_run(pool, status="running")
    agen = event_stream(pool, redis, run_id, after=0, ping_sec=0.2).__aiter__()
    first = await asyncio.wait_for(agen.__anext__(), 5)
    assert first == PING  # subscribed; nothing stored yet

    channel = run_channel(run_id)
    await redis.publish(channel, '{"seq":5}')  # no `type` at all -- would crash `event["type"]` unguarded
    second = await asyncio.wait_for(agen.__anext__(), 5)
    assert second == PING  # survived: the ping loop kept going instead of raising KeyError

    await redis.publish(channel, '{"seq":"5","type":"node_started"}')  # `seq` is a str, not int/None
    third = await asyncio.wait_for(agen.__anext__(), 5)
    assert third == PING  # survived: no TypeError from comparing a str `seq` to an int `last`

    await redis.publish(channel, "not even json")
    await redis.publish(channel, '"just a string"')  # valid JSON, but not an object
    fourth = await asyncio.wait_for(agen.__anext__(), 5)
    assert fourth == PING

    async with pool.connection() as conn, conn.transaction():
        event = await _finish(conn, run_id, "run_succeeded")
    await RedisPublisher(redis).publish(run_id, event)

    chunk = await asyncio.wait_for(agen.__anext__(), 5)
    assert "run_succeeded" in chunk  # the stream survived every piece of junk and still delivers real events

    await agen.aclose()


def test_format_event_sanitizes_a_lone_surrogate_instead_of_crashing_the_encoder():
    """`StreamingResponse` encodes each chunk as UTF-8; a lone surrogate in a payload (unreachable via
    Redis or Postgres today only because their own encoders raise first -- see the module docstring
    reasoning in `engine.events.stream`) would otherwise fail that encoding mid-body. `node_token` is the
    one event type fed straight from the LLM's raw streamed text with no `check_text`/`redact`/clip in
    front of it (Task 14 item 3 / A4 / B5)."""
    event = {"seq": None, "runId": "r1", "type": "node_token", "nodeId": "llm_1", "execIndex": 1,
             "payload": {"text": "안\ud800녕"}}

    chunk = format_event(event)

    chunk.encode("utf-8")  # must not raise UnicodeEncodeError
    assert "\ud800" not in chunk
    assert "�" in chunk


async def test_the_stream_keeps_delivering_from_postgres_when_redis_is_gone(pool):
    """Every event is durably in Postgres; a Redis outage must degrade the stream, not end it."""

    class DeadRedis:
        def pubsub(self):
            raise ConnectionError("redis is down")

    run_id = await make_run(pool, status="running")
    stream = event_stream(pool, DeadRedis(), run_id, ping_sec=0.1)
    collected: list[str] = []

    async def pump():
        async for chunk in stream:
            collected.append(chunk)

    task = asyncio.create_task(pump())
    try:
        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "node_started", node_id="n1", exec_index=1, attempt=1)

        async def seen():
            return any("node_started" in chunk for chunk in collected)

        await until(seen, timeout=10)

        async with pool.connection() as conn:
            await conn.execute("UPDATE runs SET status='succeeded' WHERE id=%s", (run_id,))
        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "run_succeeded")

        async with asyncio.timeout(10):
            await task
    finally:
        task.cancel()

    assert any("run_succeeded" in chunk for chunk in collected)


async def test_a_redis_outage_mid_stream_falls_back_to_polling(pool, redis):
    """The subscription can die *after* the stream is up, which is the common shape of an outage: the
    pump raises, and everything after that has to come from Postgres."""
    run_id = await make_run(pool, status="running")
    collected: list[str] = []

    async def pump():
        async for chunk in event_stream(pool, redis, run_id, ping_sec=0.1):
            collected.append(chunk)

    task = asyncio.create_task(pump())
    try:
        async def subscribed():
            return any(chunk == PING for chunk in collected)

        await until(subscribed, timeout=10)
        await redis.aclose()  # the subscription is gone from here on

        async with pool.connection() as conn:
            await conn.execute("UPDATE runs SET status='succeeded' WHERE id=%s", (run_id,))
        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "run_succeeded")

        async with asyncio.timeout(15):
            await task
    finally:
        task.cancel()

    assert any("run_succeeded" in chunk for chunk in collected)


async def test_a_retried_run_keeps_one_stream_open(pool, redis):
    """Task 15 appends run_queued after run_failed; the stream must not stop at the stale terminal event."""
    run_id = await make_run(pool, status="failed")
    async with pool.connection() as conn, conn.transaction():
        await append_event(conn.cursor(), run_id, "run_failed")
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET status='queued' WHERE id=%s", (run_id,))

    collected: list[str] = []

    async def pump():
        async for chunk in event_stream(pool, redis, run_id, ping_sec=0.1):
            collected.append(chunk)

    task = asyncio.create_task(pump())
    try:
        async def replayed():
            return any("run_failed" in chunk for chunk in collected)

        await until(replayed, timeout=10)

        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "run_started")
        async with pool.connection() as conn:
            await conn.execute("UPDATE runs SET status='succeeded' WHERE id=%s", (run_id,))
        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "run_succeeded")

        async with asyncio.timeout(10):
            await task
    finally:
        task.cancel()

    text = "".join(collected)
    assert "run_started" in text and text.rindex("run_succeeded") > text.index("run_failed")


async def test_a_terminal_event_on_a_genuinely_finished_run_still_ends_the_stream(pool, redis):
    """The other half of the re-read: it must not turn every stream into one that never closes."""
    run_id = await make_run(pool, status="running")
    collected: list[str] = []

    async def pump():
        async for chunk in event_stream(pool, redis, run_id, ping_sec=0.1):
            collected.append(chunk)

    task = asyncio.create_task(pump())
    try:
        async with pool.connection() as conn:
            await conn.execute("UPDATE runs SET status='failed' WHERE id=%s", (run_id,))
        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "run_failed")

        async with asyncio.timeout(10):
            await task
    finally:
        task.cancel()

    assert any("run_failed" in chunk for chunk in collected)


async def test_a_stale_terminal_event_found_by_gap_fill_does_not_end_a_requeued_run(pool, redis):
    """The retry case again, but through the idle path rather than the initial replay: the terminal event
    is committed and never published, so it is the ping tick's gap-fill that meets it. Both paths have to
    re-read the status, and only a test that reaches this one proves the second does."""
    run_id = await make_run(pool, status="running")
    collected: list[str] = []

    async def pump():
        async for chunk in event_stream(pool, redis, run_id, ping_sec=0.05):
            collected.append(chunk)

    task = asyncio.create_task(pump())
    try:
        async def subscribed():
            return any(chunk == PING for chunk in collected)

        await until(subscribed, timeout=10)

        # Committed, never published: only the gap-fill on an idle tick can find it. The run is already
        # back to `queued`, the way /retry leaves it.
        async with pool.connection() as conn, conn.transaction():
            await append_event(conn.cursor(), run_id, "run_failed")
        async with pool.connection() as conn:
            await conn.execute("UPDATE runs SET status='queued' WHERE id=%s", (run_id,))

        async def saw_failure():
            return any("run_failed" in chunk for chunk in collected)

        await until(saw_failure, timeout=10)
        await asyncio.sleep(0.3)  # several ping intervals: a stream that ended here would stop growing
        assert not task.done(), "the stream ended on a terminal event of a run that was requeued"

        async with pool.connection() as conn, conn.transaction():
            await _finish(conn, run_id, "run_succeeded")
        async with asyncio.timeout(10):
            await task
    finally:
        task.cancel()

    assert any("run_succeeded" in chunk for chunk in collected)


async def test_a_lost_subscription_is_retried_rather_than_abandoned_for_good(pool):
    """Polling Postgres keeps the client served, but it is a fallback, not a destination: a blip must not
    cost live delivery for the rest of the run. Counted through `pubsub()` calls, since a resubscribe is
    not otherwise observable — polling delivers the same bytes."""

    class _CountingRedis:
        def __init__(self, pubsub):
            self._pubsub = pubsub
            self.subscriptions = 0

        def pubsub(self):
            self.subscriptions += 1
            return self._pubsub

    pubsub = _FakePubSub()
    fake = _CountingRedis(pubsub)
    run_id = await make_run(pool, status="running")
    agen = event_stream(pool, fake, run_id, after=0, ping_sec=0.05).__aiter__()
    getter = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)
    await pubsub.confirm_subscribed()
    assert await asyncio.wait_for(getter, 5) == PING
    assert fake.subscriptions == 1

    await pubsub.die(ConnectionError("boom"))
    try:
        for _ in range(6):
            await asyncio.wait_for(agen.__anext__(), 2)
            if fake.subscriptions > 1:
                break
        assert fake.subscriptions > 1, "the stream never tried to get its subscription back"
    finally:
        with contextlib.suppress(Exception):
            await agen.aclose()
