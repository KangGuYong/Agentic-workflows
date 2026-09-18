import asyncio
import time

import pytest

from engine.llm.base import ChatMessage, ChatResult
from engine.llm.semaphore import ModelSemaphore, SemaphoreLLM


async def test_only_the_limit_may_hold_a_model_at_once(redis):
    semaphore = ModelSemaphore(redis, limit=2, ttl_sec=60)
    held = []

    async def hold(index: int):
        async with semaphore.slot("m"):
            held.append(index)
            await asyncio.sleep(0.3)

    task = asyncio.gather(hold(1), hold(2), hold(3))
    await asyncio.sleep(0.1)
    assert len(held) == 2  # the third waits
    await task
    assert len(held) == 3
    assert await redis.zcard("llm:sem:m") == 0  # every slot was returned


async def test_a_slot_held_by_a_dead_worker_is_reclaimed_after_the_ttl(redis):
    semaphore = ModelSemaphore(redis, limit=1, ttl_sec=1)
    await redis.zadd("llm:sem:m", {"dead-worker": time.time() - 10})

    async with asyncio.timeout(5):
        async with semaphore.slot("m"):
            assert await redis.zcard("llm:sem:m") == 1  # the stale entry was dropped


async def test_the_slot_is_returned_when_the_call_fails(redis):
    semaphore = ModelSemaphore(redis, limit=1, ttl_sec=60)

    class _Boom:
        async def chat(self, **kwargs):
            raise RuntimeError("model down")

    with pytest.raises(RuntimeError):
        await SemaphoreLLM(_Boom(), semaphore).chat(model="m", messages=[ChatMessage("user", "안녕")])

    assert await redis.zcard("llm:sem:m") == 0


async def test_the_wrapper_passes_the_call_through(redis):
    class _Echo:
        async def chat(self, *, model, messages, schema=None, temperature=0.7, on_token=None):
            return ChatResult(text=f"{model}:{messages[-1].content}")

    result = await SemaphoreLLM(_Echo(), ModelSemaphore(redis, limit=1)).chat(
        model="m", messages=[ChatMessage("user", "안녕")]
    )

    assert result.text == "m:안녕"


async def test_holding_a_slot_leaves_no_background_task_behind(redis):
    semaphore = ModelSemaphore(redis, limit=1, ttl_sec=60)
    before = len(asyncio.all_tasks())

    async with semaphore.slot("m"):
        assert len(asyncio.all_tasks()) == before + 1  # the score refresher

    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) == before  # and it is gone again
