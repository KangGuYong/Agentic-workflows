"""Per-model concurrency limit shared by every worker (MVP design 6.4).

A sorted set per model holds one member per in-flight call, scored with the time it was taken. Entries older
than the TTL are dropped on every acquire, so a worker that dies never leaks a slot.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from engine.llm.base import ChatMessage, ChatResult, LLMClient, TokenSink

ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
  redis.call('PEXPIRE', KEYS[1], ARGV[5])
  return 1
end
return 0
"""


class ModelSemaphore:
    def __init__(self, redis: Any, *, limit: int = 1, ttl_sec: float = 120.0, poll_sec: float = 0.05) -> None:
        self._redis = redis
        self._limit = max(1, limit)
        self._ttl = ttl_sec
        self._poll = poll_sec
        self._acquire = redis.register_script(ACQUIRE)

    def key(self, model: str) -> str:
        return f"llm:sem:{model}"

    @contextlib.asynccontextmanager
    async def slot(self, model: str) -> AsyncIterator[None]:
        key, token = self.key(model), str(uuid.uuid4())
        delay = self._poll
        while not await self._try(key, token):
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1.0)  # back off, but keep waiting: the node's timeout bounds this
        refresher = asyncio.create_task(self._refresh(key, token))
        try:
            yield
        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
            with contextlib.suppress(Exception):
                await self._redis.zrem(key, token)

    async def _try(self, key: str, token: str) -> bool:
        now = time.time()
        result = await self._acquire(
            keys=[key], args=[now - self._ttl, self._limit, now, token, int(self._ttl * 2 * 1000)]
        )
        return bool(result)

    async def _refresh(self, key: str, token: str) -> None:
        """Keep a long call's slot alive; a dead worker stops refreshing and its slot expires."""
        while True:
            await asyncio.sleep(self._ttl / 4)
            with contextlib.suppress(Exception):
                await self._redis.zadd(key, {token: time.time()}, xx=True)


class SemaphoreLLM:
    """Wraps an LLMClient so every call holds a slot for its model."""

    def __init__(self, inner: LLMClient, semaphore: ModelSemaphore) -> None:
        self._inner = inner
        self._semaphore = semaphore

    async def chat(self, *, model: str, messages: list[ChatMessage], schema: dict[str, Any] | None = None,
                   temperature: float = 0.7, on_token: TokenSink | None = None) -> ChatResult:
        async with self._semaphore.slot(model):
            return await self._inner.chat(model=model, messages=messages, schema=schema,
                                          temperature=temperature, on_token=on_token)

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        async with self._semaphore.slot(model):
            return await self._inner.embed(model=model, texts=texts)
