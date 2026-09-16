"""Off-loop template rendering (Task 10, design 4). No containers needed.

`RenderPool` is what `RunDeps.render` is set to in the worker process: it must satisfy `RenderFn` exactly
(engine/runtime/deps.py) and its failure modes must match what that docstring promises the node wrapper -
a deadline is `TimeoutError`, a template problem comes back as the same `TemplateRenderError` the inline
path raises.
"""
from __future__ import annotations

import asyncio
import contextlib
import pickle
import time

import pytest

from engine.nodes.base import TemplateField
from engine.templates.render import TemplateRenderError
from engine.worker.render import RenderPool

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
    one render at a time): this proves size=2 actually runs two renders at once, by timing one render alone
    against two scheduled together."""
    solo_pool = RenderPool(size=1, timeout=30)
    data = {"start": {"rows": list(range(1_200))}}
    try:
        started = time.monotonic()
        await solo_pool(SLOW, data)
        solo_elapsed = time.monotonic() - started
    finally:
        solo_pool.close()

    pool = RenderPool(size=2, timeout=30)
    try:
        started = time.monotonic()
        await asyncio.gather(pool(SLOW, data), pool(SLOW, data))
        concurrent_elapsed = time.monotonic() - started
    finally:
        pool.close()

    # Two renders serialised through one worker would take roughly 2x the solo time; run concurrently on
    # two workers they should take closer to 1x. Generous margin to avoid flakiness on a loaded machine.
    assert concurrent_elapsed < solo_elapsed * 1.6


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
