from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.factories import make_run

CHAIN = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}},
                               "required": ["topic"]}}},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "{{ start.topic }} 요약"}},
        {"id": "end", "type": "end", "config": {"outputs": {"result": "{{ llm_1.text }}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"},
              {"id": "e2", "source": "llm_1", "target": "end"}],
}

BROKEN = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "안녕"},
         "policy": {"retry": {"maxAttempts": 1}}},
        {"id": "end", "type": "end"},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"},
              {"id": "e2", "source": "llm_1", "target": "end"}],
}


async def _status(pool, run_id: str):
    from engine.db import runs as run_db

    async def check():
        async with pool.connection() as conn:
            row = await run_db.get_run(conn, run_id)
        return row if row["status"] in ("succeeded", "failed", "cancelled", "waiting") else None

    return await until(check)


async def test_a_queued_run_is_picked_up_and_finished(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    await worker_factory(ScriptedLLM(["요약본"]))

    row = await _status(pool, run_id)

    assert (row["status"], row["outputs"]) == ("succeeded", {"result": "요약본"})
    assert row["lease_owner"] is None and row["finished_at"] is not None
    async with pool.connection() as conn:
        events = await (await conn.execute(
            "SELECT type FROM run_events WHERE run_id=%s ORDER BY seq", (run_id,))).fetchall()
    types = [event["type"] for event in events]
    assert types[0] == "run_started" and types[-1] == "run_succeeded"
    assert "node_finished" in types


async def test_a_node_failure_fails_the_run_with_its_error(pool, worker_factory):
    from engine.errors import ErrorCode, NodeError

    run_id = await make_run(pool, dsl=BROKEN, status="queued")
    await worker_factory(ScriptedLLM([NodeError(ErrorCode.LLM_UNAVAILABLE, "모델 없음", retryable=False)]))

    row = await _status(pool, run_id)

    assert row["status"] == "failed"
    assert (row["error"]["code"], row["error"]["nodeId"]) == ("LLM_UNAVAILABLE", "llm_1")


async def test_a_run_waiting_for_approval_releases_the_lease(pool, worker_factory):
    hitl = {
        "version": "1",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "human_approval_1", "type": "human_approval", "config": {"message": "승인할까요?"}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "human_approval_1"},
            {"id": "e2", "source": "human_approval_1", "sourceHandle": "approve", "target": "end"},
            {"id": "e3", "source": "human_approval_1", "sourceHandle": "reject", "target": "end"},
        ],
    }
    run_id = await make_run(pool, dsl=hitl, status="queued")
    await worker_factory(ScriptedLLM([]))

    row = await _status(pool, run_id)

    assert (row["status"], row["waiting_node_id"], row["waiting_exec_index"]) == ("waiting", "human_approval_1", 1)
    assert row["lease_owner"] is None


async def test_only_one_of_two_workers_runs_a_given_run(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    await worker_factory(ScriptedLLM(["요약본"]), owner="worker-1")
    await worker_factory(ScriptedLLM(["요약본"]), owner="worker-2")

    await _status(pool, run_id)

    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT node_id, attempt FROM node_runs WHERE run_id=%s AND node_id='llm_1'", (run_id,))).fetchall()
    assert len(rows) == 1  # the loser never opened an attempt
