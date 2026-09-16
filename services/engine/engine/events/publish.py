"""Redis transport for live events and run control (MVP design 3, 5.9).

Redis is never the source of truth: a lost event is filled in from `run_events` by the SSE endpoint, and a
lost cancel is noticed by the worker's next heartbeat.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

CONTROL_CHANNEL = "runs:control"


def run_channel(run_id: str) -> str:
    return f"run:{run_id}"


class RedisPublisher:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        await self._redis.publish(run_channel(run_id), json.dumps(event, ensure_ascii=False))

    async def request_cancel(self, run_id: str) -> None:
        await self._redis.publish(CONTROL_CHANNEL, json.dumps({"runId": run_id, "action": "cancel"}))


def _decode(data: Any) -> dict[str, Any] | None:
    """One message, or None when it is not ours: anyone can publish on a Redis channel, and a consumer
    that treats a stray string or list as an event would die on the first attribute access."""
    try:
        parsed = json.loads(data)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def control_messages(redis: Any) -> AsyncIterator[dict[str, Any]]:
    """Every worker subscribes once and routes messages to the runs it holds."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(CONTROL_CHANNEL)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            parsed = _decode(message["data"])
            if parsed is not None:
                yield parsed
    finally:
        await pubsub.aclose()


async def run_events(redis: Any, run_id: str) -> AsyncIterator[dict[str, Any]]:
    """Live events for one run, for the SSE endpoint."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(run_channel(run_id))
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            parsed = _decode(message["data"])
            if parsed is not None:
                yield parsed
    finally:
        await pubsub.aclose()
