import asyncio
import json

from engine.events.publish import RedisPublisher, control_messages


async def test_published_events_reach_a_subscriber(redis):
    publisher = RedisPublisher(redis)
    pubsub = redis.pubsub()
    await pubsub.subscribe("run:r1")

    await publisher.publish("r1", {"seq": 1, "type": "run_started"})

    message = await _next(pubsub)
    assert json.loads(message["data"]) == {"seq": 1, "type": "run_started"}
    await pubsub.aclose()


async def test_cancel_requests_go_to_one_control_channel(redis):
    publisher = RedisPublisher(redis)
    received: list[dict] = []

    async def listen():
        async for message in control_messages(redis):
            received.append(message)
            return

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.2)
    await publisher.request_cancel("r2")
    await asyncio.wait_for(task, 5)

    assert received == [{"runId": "r2", "action": "cancel"}]


async def _next(pubsub, timeout: float = 5.0) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message is not None:
                return message


async def test_a_stray_message_never_reaches_the_consumer(redis):
    """Anyone can publish on a channel; a consumer must not see anything that is not an event."""
    received: list[dict] = []

    async def listen():
        async for message in control_messages(redis):
            received.append(message)
            if message.get("action") == "cancel":
                return

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.2)
    await redis.publish("runs:control", "not json at all")
    await redis.publish("runs:control", "42")
    await redis.publish("runs:control", '["cancel"]')
    await RedisPublisher(redis).request_cancel("r3")
    await asyncio.wait_for(task, 5)

    assert received == [{"runId": "r3", "action": "cancel"}]
