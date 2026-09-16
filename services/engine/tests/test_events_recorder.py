import asyncio

import pytest

from engine.events.recorder import PostgresRecorder
from engine.nodes.base import Usage
from engine.runtime.recorder import DuplicateAttempt
from tests.factories import make_run  # added in this task


async def _records(pool, run_id: str) -> list[dict]:
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT node_id, exec_index, attempt, status, input, output, error, meta, waited, truncated,"
            " tokens_out FROM node_runs WHERE run_id=%s ORDER BY started_at, attempt", (run_id,)
        )).fetchall()


async def _events(pool, run_id: str) -> list[dict]:
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT seq, type, node_id, exec_index, attempt, payload FROM run_events"
            " WHERE run_id=%s ORDER BY seq", (run_id,)
        )).fetchall()


async def test_an_attempt_is_opened_closed_and_evented(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)

    await recorder.node_started("llm_1", 1, 1, {"prompt": "안녕"})
    await recorder.node_succeeded("llm_1", 1, 1, {"text": "답"}, Usage(3, 4), defaulted=False, meta={})

    [record] = await _records(pool, run_id)
    assert (record["status"], record["input"], record["output"]) == ("succeeded", {"prompt": "안녕"}, {"text": "답"})
    assert record["tokens_out"] == 4
    assert [(event["seq"], event["type"]) for event in await _events(pool, run_id)] == [
        (1, "node_started"), (2, "node_finished")
    ]


async def test_a_second_worker_opening_the_same_attempt_is_told_to_stop(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)
    await recorder.node_started("llm_1", 1, 1, None)

    with pytest.raises(DuplicateAttempt):
        await PostgresRecorder(pool, run_id).node_started("llm_1", 1, 1, None)


async def test_waiting_is_remembered_after_the_attempt_finishes(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)
    await recorder.node_started("human_approval_1", 1, 1, None)
    await recorder.node_waiting("human_approval_1", 1, 1, {"message": "검토"})

    assert await recorder.find_waiting("human_approval_1", 1) == 1
    await recorder.node_succeeded("human_approval_1", 1, 1, {"decision": "approve"}, Usage(),
                                  defaulted=False, meta={})
    assert await recorder.find_waiting("human_approval_1", 1) == 1  # still "ever waited"
    assert await recorder.attempts_so_far("human_approval_1", 1) == 1


async def test_closing_an_already_closed_attempt_is_allowed(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)
    await recorder.node_started("llm_1", 1, 1, None)
    await recorder.node_succeeded("llm_1", 1, 1, {"text": "답"}, Usage(), defaulted=False, meta={})

    await recorder.node_succeeded("llm_1", 1, 1, {"text": "답"}, Usage(), defaulted=False, meta={})

    assert len(await _records(pool, run_id)) == 1


async def test_event_numbers_are_gapless_when_two_recorders_write_at_once(pool):
    run_id = await make_run(pool)
    one, two = PostgresRecorder(pool, run_id), PostgresRecorder(pool, run_id)

    async def write(recorder, node_id):
        for index in range(1, 11):
            await recorder.node_started(node_id, index, 1, None)

    await asyncio.gather(write(one, "a"), write(two, "b"))

    assert [event["seq"] for event in await _events(pool, run_id)] == list(range(1, 21))


async def test_secrets_are_redacted_and_large_values_truncated(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)

    await recorder.node_started("llm_1", 1, 1, {"headers": {"Authorization": "Bearer x"}})
    await recorder.node_succeeded("llm_1", 1, 1, {"text": "가" * 300_000}, Usage(), defaulted=False, meta={})

    [record] = await _records(pool, run_id)
    assert record["input"] == {"headers": {"Authorization": "[REDACTED]"}}
    assert record["truncated"] and set(record["output"]) == {"_truncated"}
    finished = (await _events(pool, run_id))[-1]
    assert len(str(finished["payload"]["outputPreview"])) < 2_000


async def test_store_run_data_false_keeps_metadata_only(pool):
    run_id = await make_run(pool, store_run_data=False)
    recorder = PostgresRecorder(pool, run_id, store_run_data=False)

    await recorder.node_started("llm_1", 1, 1, {"prompt": "비밀"})
    await recorder.node_succeeded("llm_1", 1, 1, {"text": "답"}, Usage(1, 2), defaulted=False, meta={})

    [record] = await _records(pool, run_id)
    assert (record["input"], record["output"], record["status"]) == (None, None, "succeeded")
    assert record["tokens_out"] == 2
    assert "outputPreview" not in ((await _events(pool, run_id))[-1]["payload"] or {})
