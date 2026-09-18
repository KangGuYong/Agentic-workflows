"""Off-loop template rendering (Task 10, design 4). No containers needed.

`RenderPool` is what `RunDeps.render` is set to in the worker process: it must satisfy `RenderFn` exactly
(engine/runtime/deps.py) and its failure modes must match what that docstring promises the node wrapper -
a deadline is `TimeoutError`, a template problem comes back as the same `TemplateRenderError` the inline
path raises.
"""
from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import os
import pickle
import time

import pytest

from engine.errors import EngineFault
from engine.nodes.base import TemplateField
from engine.templates.render import TemplateRenderError
from engine.worker.render import RenderPool


async def until_sync(check, *, timeout: float = 30.0, interval: float = 0.01) -> None:
    """Wait for a synchronous predicate, bounded. Plan convention 5: no test may wait forever."""
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(interval)


def _get_pid() -> int:
    """A tiny job scheduled directly through pebble (bypassing `_job`/`render_fields`, which has no way to
    report one): identifies which worker process actually ran it."""
    return os.getpid()


FIELDS = [TemplateField("prompt", "{{ start.topic }} 요약", "string")]
# O(n^2)-ish: tojson re-serialises the whole list on every loop iteration, so a modest row count already
# takes seconds - the same shape of template the design calls out as the CPU risk rendering must be bounded
# against.
SLOW = [TemplateField("prompt", "{% for row in start.rows %}{{ start.rows | tojson | length }}{% endfor %}",
                      "string")]


async def test_rendering_happens_in_the_pool():
    pool = RenderPool(size=1, timeout=10)
    try:
        assert await pool(FIELDS, {"start": {"topic": "AI"}}) == {"prompt": "AI 요약"}
    finally:
        pool.close()


async def test_a_slow_template_hits_the_deadline_and_the_pool_survives():
    pool = RenderPool(size=1, timeout=0.5)
    data = {"start": {"rows": list(range(20_000))}}
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await pool(SLOW, data)
        assert time.monotonic() - started < 5  # killed, not waited out

        assert await pool(FIELDS, {"start": {"topic": "AI"}}) == {"prompt": "AI 요약"}
    finally:
        pool.close()


async def test_the_event_loop_keeps_running_during_a_render():
    pool = RenderPool(size=1, timeout=5)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        with pytest.raises(TimeoutError):
            # ~11s of work against a 5s deadline (see the SLOW docstring): long enough that a blocking
            # (thread- or inline-) implementation would starve the ticker below the assertion's floor.
            await pool(SLOW, {"start": {"rows": list(range(4_000))}})
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
        pool.close()

    assert ticks > 5  # the loop was not blocked by the render


async def test_template_errors_come_back_as_errors():
    pool = RenderPool(size=1, timeout=5)
    try:
        with pytest.raises(TemplateRenderError):
            await pool([TemplateField("prompt", "{{ start.missing }}", "string")], {"start": {}})
    finally:
        pool.close()


async def test_template_error_keeps_its_type_and_message_across_the_process_boundary():
    """A pickling failure in pebble would surface as a generic PicklingError/RemoteTraceback, not this."""
    pool = RenderPool(size=1, timeout=5)
    try:
        with pytest.raises(TemplateRenderError) as exc_info:
            await pool([TemplateField("prompt", "{{ start.missing }}", "string")], {"start": {}})
        assert type(exc_info.value) is TemplateRenderError
        assert "start.missing" in str(exc_info.value)
    finally:
        pool.close()


def test_template_field_outputs_and_render_error_survive_pickling():
    """These three cross the process boundary on every call: pebble pickles the arguments out and the
    result (or the raised exception) back."""
    field = TemplateField("prompt", "{{ start.topic }}", "string")
    assert pickle.loads(pickle.dumps(field)) == field

    outputs = {"start": {"topic": "AI", "n": 1, "nested": {"x": [1, 2, 3]}}}
    assert pickle.loads(pickle.dumps(outputs)) == outputs

    error = TemplateRenderError("참조한 값이 없습니다: start.missing")
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is TemplateRenderError
    assert str(restored) == str(error)


async def test_two_renders_overlap_in_a_pool_of_two():
    """A pool that is really one worker underneath would still pass every test above (they each schedule
    one render at a time): this proves size=2 actually runs two renders at once, and in two processes.

    Neither half is timed any more. The original version compared two wall clocks -- two renders on a
    two-worker pool against one render on a one-worker pool, asserting the pair finished in under 1.6x --
    which failed about one run in five under full-suite load, because on a loaded machine that ratio is a
    property of the machine, not of the pool. Both facts are now read directly instead: `in_flight` counts
    renders that are scheduled and unfinished, and `multiprocessing.active_children()` names the worker
    processes the OS actually has.
    """
    pool = RenderPool(size=2, timeout=30)
    data = {"start": {"rows": list(range(1_200))}}
    try:
        await asyncio.gather(pool(SLOW, data), pool(SLOW, data))  # warm both workers

        # Both in flight at the same moment, observed rather than inferred from elapsed time. The old
        # version of this assertion compared two wall clocks (`concurrent < solo * 1.6`) and failed about
        # one run in five under full-suite load — it was measuring the machine, not the pool. `in_flight`
        # counts scheduled-and-unfinished renders, so seeing 2 *is* the overlap.
        both = [asyncio.create_task(pool(SLOW, data)) for _ in range(2)]
        await until_sync(lambda: pool.in_flight == 2)
        await asyncio.wait_for(asyncio.gather(*both), 60)

        # Two worker *processes*, read from the OS rather than inferred. The previous version scheduled
        # two `os.getpid()` jobs and asserted the pids differed, which is a race: both jobs are
        # instantaneous, so pebble can serve the second from the same worker that just finished the first
        # (observed: one run in twelve came back with one pid twice).
        workers = [child for child in multiprocessing.active_children()
                   if child.name == "pebble_pool_worker"]
        pids = {child.pid for child in workers}

        solo_pid = await asyncio.wrap_future(pool._pool.schedule(_get_pid, timeout=10))
    finally:
        pool.close()

    assert len(pids) == 2, f"size=2 produced {len(pids)} worker process(es)"
    assert solo_pid in pids and solo_pid != os.getpid()  # rendering really happens in one of them



async def test_close_stops_the_worker_process():
    """A `close()` that only flips a flag (or never reaches pebble's `stop()`) would leave the whole suite
    green: every test closes its pool, but nothing else checks the worker actually died. Get its pid
    directly, close the pool, and check both that the pid is no longer among this process's live children
    and that pebble itself now refuses new work."""
    pool = RenderPool(size=1, timeout=10)
    pid = await asyncio.wrap_future(pool._pool.schedule(_get_pid, timeout=10))
    assert pid in {p.pid for p in multiprocessing.active_children()}

    pool.close()

    assert pid not in {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(RuntimeError):
        pool._pool.schedule(_get_pid, timeout=10)


async def test_close_resolves_a_pending_render_instead_of_hanging_it_forever():
    """A1 regression: pebble's own stop()/join() never touch pending futures, so a close() that only calls
    them would leave every `await pool(...)` call that was in flight at close time pending forever - masked
    today only by main.py always cancelling every run task (which cancels this await from outside) before
    calling render.close(). Nothing here cancels the awaiting task from outside: only close() itself may
    unblock it, and it must do so within a couple of seconds, not the render's own long timeout."""
    pool = RenderPool(size=1, timeout=30)
    data = {"start": {"rows": list(range(20_000))}}
    task = asyncio.create_task(pool(SLOW, data))
    await asyncio.sleep(0.2)  # let the job actually start in the child before closing under it

    pool.close()

    done, pending = await asyncio.wait({task}, timeout=5)
    assert not pending, "close() left the outstanding render pending"
    with pytest.raises(BaseException):  # CancelledError from future.cancel(), or another clear error
        task.result()


async def test_a_dead_pool_raises_engine_fault_not_a_node_error():
    """A2 regression: once pebble's pool status goes ERROR/STOPPED it is terminal; undetected, every render
    after that raises a bare RuntimeError or a BrokenProcessPool that (absent this fix) would surface as a
    generic, non-retryable node error - a worker with a dead render pool would then claim runs and
    permanently fail them as fast as it could claim them. It must raise EngineFault instead, so the worker's
    generic handler expires the lease and lets recovery retry on a healthy worker."""
    pool = RenderPool(size=1, timeout=10)
    try:
        await pool(FIELDS, {"start": {"topic": "AI"}})  # get it running first, as a live pool would be

        pool._pool.stop()  # simulate the underlying pool dying behind RenderPool's back

        with pytest.raises(EngineFault):
            await pool(FIELDS, {"start": {"topic": "AI"}})
    finally:
        pool.close()  # must still be safe even though the underlying pool is already stopped


async def test_close_is_safe_to_call_twice():
    pool = RenderPool(size=1, timeout=5)
    await pool(FIELDS, {"start": {"topic": "AI"}})
    pool.close()
    pool.close()  # must not raise


async def test_close_does_not_hang_while_a_job_is_running():
    pool = RenderPool(size=1, timeout=30)
    data = {"start": {"rows": list(range(20_000))}}
    task = asyncio.create_task(pool(SLOW, data))
    await asyncio.sleep(0.2)  # let the job actually start in the child before closing under it

    started = time.monotonic()
    pool.close()
    assert time.monotonic() - started < 10  # close() must not wait for the in-flight job to finish

    task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task


async def test_a_caller_waits_for_a_slot_instead_of_piling_up_behind_the_pool(tmp_path):
    """The queue in front of the pool is bounded by `size`: while both slots are held, a third caller is
    still waiting *here*, inside its own node's deadline, rather than sitting in pebble's queue."""
    pool = RenderPool(size=2, timeout=30, memory_limit_mb=None)
    try:
        # Through the public call, so both slots are genuinely taken: `_render` would bypass the very
        # semaphore this test is about.
        held = [asyncio.create_task(pool(SLOW, {"start": {"rows": list(range(3_000))}}))
                for _ in range(2)]
        await asyncio.sleep(0.2)
        assert pool.in_flight == 2

        third = asyncio.create_task(pool(FIELDS, {"start": {"topic": "AI"}}))
        await asyncio.sleep(0.2)
        # `not third.done()` would prove nothing: a call queued inside pebble is also unfinished. The
        # difference is whether it was *scheduled* at all — `in_flight` counts scheduled-and-unfinished
        # renders, so a third one showing up here means the wait moved back into pebble, where no node
        # deadline can see it.
        assert pool.in_flight == 2, "the third caller was handed to pebble instead of waiting for a slot"
        assert not third.done()

        await asyncio.wait_for(asyncio.gather(*held), 60)
        assert await asyncio.wait_for(third, 60) == {"prompt": "AI 요약"}
        assert pool.in_flight == 0
    finally:
        await asyncio.to_thread(pool.close)
