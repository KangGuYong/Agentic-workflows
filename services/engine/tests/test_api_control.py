"""Resume, retry and cancel (MVP design 5.9, 8.2)."""
import asyncio

import pytest

from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.test_api_runs import _saved

HITL = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"draft": {"type": "string"}},
                               "required": ["draft"]}}},
        {"id": "human_approval_1", "type": "human_approval",
         "config": {"message": "검토해 주세요", "review": "{{ start.draft }}", "allowEdit": True}},
        {"id": "end", "type": "end",
         "config": {"outputs": {"final": "{{ human_approval_1.editedValue }}"}}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "human_approval_1"},
        {"id": "e2", "source": "human_approval_1", "sourceHandle": "approve", "target": "end"},
        {"id": "e3", "source": "human_approval_1", "sourceHandle": "reject", "target": "end"},
    ],
}


async def _wait_status(api, run_id: str, status: str) -> dict:
    async def check():
        run = (await api.get(f"/runs/{run_id}")).json()
        return run if run["status"] == status else None

    return await until(check)


async def test_an_approval_can_be_answered_and_the_run_finishes(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    waiting = await _wait_status(api, run["runId"], "waiting")

    assert waiting["waitingFor"]["review"] == "원고" and waiting["waitingFor"]["allowEdit"] is True
    answer = {"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve",
              "editedValue": "수정된 원고"}
    accepted = await api.post(f"/runs/{run['runId']}/resume", json=answer)

    assert accepted.status_code == 202
    finished = await _wait_status(api, run["runId"], "succeeded")
    assert finished["outputs"] == {"final": "수정된 원고"}


async def test_a_wrong_target_or_a_bad_answer_is_refused(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "waiting")

    mismatch = await api.post(f"/runs/{run['runId']}/resume",
                              json={"nodeId": "human_approval_1", "execIndex": 7, "decision": "approve"})
    bad = await api.post(f"/runs/{run['runId']}/resume",
                         json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "maybe"})
    typed = await api.post(f"/runs/{run['runId']}/resume",
                           json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve",
                                 "editedValue": 42})

    assert (mismatch.status_code, mismatch.json()["error"]["code"]) == (409, "RESUME_TARGET_MISMATCH")
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "VALIDATION_FAILED")
    assert typed.status_code == 422  # editedValue must keep the review's type
    assert (await api.get(f"/runs/{run['runId']}")).json()["status"] == "waiting"  # still answerable


async def test_a_resume_with_an_unexpected_extra_key_is_refused(api, worker_factory):
    """The answer body is stored verbatim as `resume_payload` and fed straight back to the engine on
    resume, so an extra field the reviewer's client happened to send must never reach a node -- it is
    rejected before it is stored, not silently dropped."""
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "waiting")

    response = await api.post(f"/runs/{run['runId']}/resume",
                              json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve",
                                    "asUser": "admin"})

    assert (response.status_code, response.json()["error"]["code"]) == (422, "VALIDATION_FAILED")
    assert (await api.get(f"/runs/{run['runId']}")).json()["status"] == "waiting"


async def test_the_server_sets_reviewed_at(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "waiting")

    await api.post(f"/runs/{run['runId']}/resume",
                   json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve",
                         "reviewedAt": "1999-01-01T00:00:00+00:00"})

    await _wait_status(api, run["runId"], "succeeded")
    nodes = (await api.get(f"/runs/{run['runId']}/nodes")).json()["nodeRuns"]
    approval = [node for node in nodes if node["nodeId"] == "human_approval_1"][-1]
    assert not approval["output"]["reviewedAt"].startswith("1999")


async def test_resuming_a_run_that_is_not_waiting_is_a_conflict(api):
    workflow_id = await _saved(api)
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    response = await api.post(f"/runs/{run['runId']}/resume",
                              json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"})

    assert (response.status_code, response.json()["error"]["code"]) == (409, "INVALID_STATE_TRANSITION")


async def test_only_one_of_two_concurrent_resumes_wins(api, worker_factory):
    """`resume_run` takes the row `FOR UPDATE` before checking `status='waiting'`: whichever request locks
    the row first queues the run, and the other's own lock wait then finds it already `queued`."""
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "waiting")
    answer = {"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"}

    first, second = await asyncio.gather(
        api.post(f"/runs/{run['runId']}/resume", json=answer),
        api.post(f"/runs/{run['runId']}/resume", json=answer),
    )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [202, 409]


async def test_a_failed_run_can_be_retried_and_keeps_finished_nodes(api, pool, worker_factory):
    from engine.errors import ErrorCode, NodeError

    workflow_id = await _saved(api)
    worker = await worker_factory(ScriptedLLM([NodeError(ErrorCode.LLM_UNAVAILABLE, "모델 없음", retryable=False)]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "failed")
    worker._llm = ScriptedLLM(["요약본"])  # the model is back

    retried = await api.post(f"/runs/{run['runId']}/retry")

    assert retried.status_code == 202
    finished = await _wait_status(api, run["runId"], "succeeded")
    assert finished["retryCount"] == 1 and finished["outputs"] == {"result": "요약본"}
    nodes = (await api.get(f"/runs/{run['runId']}/nodes")).json()["nodeRuns"]
    assert len([node for node in nodes if node["nodeId"] == "start"]) == 1  # not re-run


async def test_retry_is_refused_without_a_checkpoint(api, pool):
    from tests.factories import make_run

    run_id = await make_run(pool, status="failed")

    response = await api.post(f"/runs/{run_id}/retry")

    assert (response.status_code, response.json()["error"]["code"]) == (409, "RUN_DATA_EXPIRED")


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "waiting", "cancelled"])
async def test_retry_is_refused_unless_the_run_failed(api, pool, status):
    from tests.factories import make_run

    run_id = await make_run(pool, status=status)

    response = await api.post(f"/runs/{run_id}/retry")

    assert (response.status_code, response.json()["error"]["code"]) == (409, "INVALID_STATE_TRANSITION")


async def test_retry_resets_the_recovery_budget(api, pool):
    """`recovery_count` caps how many times the reaper will auto-recover a crashed lease before giving up
    as ENGINE_RECOVERY_EXHAUSTED (worker/reaper.py, MAX_RECOVERIES). A manual retry is a deliberate new
    attempt, so it must not inherit an exhausted budget from the run it is replacing -- otherwise a single
    unrelated crash right after the retry would fail it again with no recovery at all."""
    from tests.factories import make_run

    run_id = await make_run(pool, status="failed")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)"
            " VALUES (%s, '', 'c1', '{}'::jsonb, '{}'::jsonb)", (run_id,))
        await conn.execute("UPDATE runs SET recovery_count=3 WHERE id=%s", (run_id,))

    response = await api.post(f"/runs/{run_id}/retry")

    assert response.status_code == 202
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT recovery_count FROM runs WHERE id=%s", (run_id,))).fetchone()
    assert row["recovery_count"] == 0


async def test_cancelling_a_queued_run_is_immediate(api):
    workflow_id = await _saved(api)
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    response = await api.post(f"/runs/{run['runId']}/cancel")

    assert response.json()["status"] == "cancelled"
    assert (await api.get(f"/runs/{run['runId']}")).json()["status"] == "cancelled"


async def test_cancelling_a_waiting_run_is_immediate_and_final(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "waiting")

    await api.post(f"/runs/{run['runId']}/cancel")

    assert (await api.get(f"/runs/{run['runId']}")).json()["status"] == "cancelled"
    late = await api.post(f"/runs/{run['runId']}/resume",
                          json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"})
    assert late.status_code == 409


async def test_cancelling_a_running_run_only_requests_it(api, pool, worker_factory):
    class _Slow:
        def __init__(self):
            self.started = asyncio.Event()

        async def chat(self, **kwargs):
            self.started.set()
            await asyncio.sleep(30)
            raise AssertionError("cancelled")

    workflow_id = await _saved(api)
    llm = _Slow()
    await worker_factory(llm)
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await asyncio.wait_for(llm.started.wait(), 20)

    response = await api.post(f"/runs/{run['runId']}/cancel")

    # Exactly "running", not merely "one of the two terminal-looking values": the LLM call is still
    # sleeping, so the run's status is unambiguously 'running' at the moment `cancel_run` decides, and
    # `cancel_now`'s WHERE clause (status IN ('queued','waiting')) must not match it -- a mutation that
    # widened that clause to also match 'running' would end the run here and now and report "cancelled"
    # instead of only requesting it, which the old `in (...)` assertion could never catch.
    assert response.json()["status"] == "running"
    assert (await _wait_status(api, run["runId"], "cancelled"))["cancelRequested"] is True


async def test_cancelling_closes_open_node_run_rows_with_an_event(api, pool):
    """A run can park on an approval while a parallel branch is still `running` (reaper.py's own
    `_expire_one` comment makes the same point about `waiting`): cancelling must close that row and emit
    the `node_failed` every other terminal path emits, so a run_events replay never shows a node running
    forever."""
    from tests.factories import make_run

    run_id = await make_run(pool, status="waiting")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE runs SET waiting_node_id=%s, waiting_exec_index=%s WHERE id=%s",
                           ("approval", 0, run_id))
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status, waited)"
            " VALUES (gen_random_uuid(), %s, 'approval', 0, 1, 'waiting', true)", (run_id,))
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status)"
            " VALUES (gen_random_uuid(), %s, 'branch_b', 0, 1, 'running')", (run_id,))

    response = await api.post(f"/runs/{run_id}/cancel")

    assert response.json()["status"] == "cancelled"
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT node_id, status FROM node_runs WHERE run_id=%s", (run_id,))).fetchall()
        events = await (await conn.execute(
            "SELECT type, node_id FROM run_events WHERE run_id=%s ORDER BY seq", (run_id,))).fetchall()
    assert {row["node_id"]: row["status"] for row in rows} == {"approval": "waiting", "branch_b": "cancelled"}
    assert ("node_failed", "branch_b") in [(event["type"], event["node_id"]) for event in events]
    assert events[-1]["type"] == "run_cancelled"


async def test_cancel_cannot_race_a_claim_into_a_run_that_is_both_cancelled_and_running(api, pool):
    """The API's cancel takes the run row `FOR UPDATE` before deciding; the worker's `claim_next` takes it
    `FOR UPDATE SKIP LOCKED`. Whichever transaction locks the row first decides the outcome and the other
    observes it afterwards -- neither can partially apply, so the run can never end up marked `cancelled`
    while a worker also holds its lease."""
    import engine.db.runs as run_db

    for _ in range(8):
        workflow_id = await _saved(api)
        run = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()
        run_id = run["runId"]

        async def claim():
            async with pool.connection() as conn:
                return await run_db.claim_next(conn, owner="race-worker", lease_sec=30)

        cancel_response, claimed = await asyncio.gather(
            api.post(f"/runs/{run_id}/cancel"), claim(),
        )

        final = (await api.get(f"/runs/{run_id}")).json()
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT lease_owner FROM runs WHERE id=%s", (run_id,))).fetchone()

        if claimed is not None:  # the worker's claim locked the row first
            assert final["status"] == "running" and final["cancelRequested"] is True
            assert row["lease_owner"] == "race-worker"
        else:  # the cancel locked the row first: nothing ever claimed it
            assert final["status"] == "cancelled"
            assert row["lease_owner"] is None


# ---------------------------------------------------------------- Task 15 review: B1-B4


async def test_queue_resume_only_updates_a_waiting_run(pool):
    """Mutation coverage for the AND status='waiting' guard in queue_resume: the route's own lock_run
    plus status check already refuses a resume of a non-waiting run over the API, so a suite that only
    exercises the route never notices that guard going missing from the query itself. Call the query
    directly on a run that is not waiting and check both the return value and that nothing changed."""
    import engine.db.runs as run_db
    from tests.factories import make_run

    run_id = await make_run(pool, status="queued")

    async with pool.connection() as conn:
        changed = await run_db.queue_resume(conn, run_id=run_id, answer={"decision": "approve"})
        row = await (await conn.execute(
            "SELECT status, resume_payload FROM runs WHERE id=%s", (run_id,))).fetchone()

    assert changed is False
    assert row["status"] == "queued" and row["resume_payload"] is None


async def test_queue_retry_only_updates_a_failed_run(pool):
    """Same shape as test_queue_resume_only_updates_a_waiting_run, for queue_retry's own
    AND status='failed' guard."""
    import engine.db.runs as run_db
    from tests.factories import make_run

    run_id = await make_run(pool, status="running")

    async with pool.connection() as conn:
        changed = await run_db.queue_retry(conn, run_id)
        row = await (await conn.execute(
            "SELECT status, retry_count, active_ms FROM runs WHERE id=%s", (run_id,))).fetchone()

    assert changed is False
    assert row["status"] == "running" and row["retry_count"] == 0 and row["active_ms"] == 0


async def test_cancelling_a_running_run_publishes_a_redis_control_message(api, redis, pool):
    """Design 8.2 Redis fast path: dropping the request_cancel publish used to survive the suite because
    the heartbeat's own database poll (heartbeat_sec=0.2 in worker_factory) finishes the run anyway, with
    nothing checking Redis itself. Subscribe to runs:control directly and require the control message to
    actually arrive, independent of any worker or heartbeat."""
    import json

    from engine.events.publish import CONTROL_CHANNEL
    from tests.factories import make_run

    run_id = await make_run(pool, status="running")
    pubsub = redis.pubsub()
    await pubsub.subscribe(CONTROL_CHANNEL)
    try:
        async with asyncio.timeout(5):
            async for message in pubsub.listen():
                if message["type"] == "subscribe":
                    break

        async def next_control_message():
            async with asyncio.timeout(5):
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        return json.loads(message["data"])
            return None

        received, response = await asyncio.gather(next_control_message(),
                                                   api.post(f"/runs/{run_id}/cancel"))
    finally:
        await pubsub.aclose()

    assert response.json()["status"] == "running"
    assert received == {"runId": run_id, "action": "cancel"}


@pytest.mark.parametrize("path", ["resume", "retry", "cancel"])
async def test_a_non_uuid_run_id_is_not_found_not_a_crash(api, path):
    """A1: resume_run, retry_run and cancel_run used to reach lock_run's WHERE id=%s against a uuid
    column with the raw path segment, so a non-UUID id raised InvalidTextRepresentation -- a bare 500 with
    a logged traceback -- instead of the 404 every other route in this file gives a bad id."""
    response = await api.post(f"/runs/not-a-uuid/{path}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("path", ["resume", "retry", "cancel"])
async def test_a_well_formed_but_unknown_run_id_is_not_found(api, path):
    import uuid

    # resume requires a JSON object body before it ever reaches the 404 check; retry and cancel ignore
    # the body entirely, so sending one for all three keeps this test uniform across the three routes.
    response = await api.post(f"/runs/{uuid.uuid4()}/{path}", json={})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_queue_retry_clears_a_stale_cancel_request(pool):
    """A2: a run can fail for its own reason inside the heartbeat window after a cancel was already
    requested, leaving cancel_requested_at set on the failed row. A retry must clear it, or the retried
    attempt reports cancelRequested: true for a cancel nobody asked for on this attempt, and the next
    heartbeat tick reads it as authoritative and cancels the run the caller just retried."""
    import engine.db.runs as run_db
    from tests.factories import make_run

    run_id = await make_run(pool, status="failed")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE runs SET cancel_requested_at=now() WHERE id=%s", (run_id,))

    async with pool.connection() as conn:
        changed = await run_db.queue_retry(conn, run_id)
        row = await (await conn.execute(
            "SELECT cancel_requested_at FROM runs WHERE id=%s", (run_id,))).fetchone()

    assert changed is True
    assert row["cancel_requested_at"] is None


async def test_queue_resume_clears_a_stale_cancel_request(pool):
    """Same hole as test_queue_retry_clears_a_stale_cancel_request, on /resume: the Redis publish for an
    earlier cancel can be lost and the node can park on a fresh approval before the next heartbeat tick
    ever reads cancel_requested_at, leaving it set on a waiting row with no worker watching it."""
    import engine.db.runs as run_db
    from tests.factories import make_run

    run_id = await make_run(pool, status="waiting")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE runs SET cancel_requested_at=now() WHERE id=%s", (run_id,))

    async with pool.connection() as conn:
        changed = await run_db.queue_resume(conn, run_id=run_id, answer={"decision": "approve"})
        row = await (await conn.execute(
            "SELECT cancel_requested_at FROM runs WHERE id=%s", (run_id,))).fetchone()

    assert changed is True
    assert row["cancel_requested_at"] is None


async def test_retrying_a_run_with_a_stale_cancel_request_still_succeeds(api, pool, worker_factory):
    """The end-to-end shape of A2: a run that failed for its own reason while a cancel request from an
    earlier, unrelated-to-the-failure /cancel call was still sitting on the row must retry clean -- right
    after the retry it must not report cancelRequested: true, and the retried attempt must be free to
    succeed rather than being cancelled by the next heartbeat tick reading the stale flag."""
    from engine.errors import ErrorCode, NodeError

    workflow_id = await _saved(api)
    worker = await worker_factory(
        ScriptedLLM([NodeError(ErrorCode.LLM_UNAVAILABLE, "모델 없음", retryable=False)]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "failed")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE runs SET cancel_requested_at=now() WHERE id=%s", (run["runId"],))
    worker._llm = ScriptedLLM(["요약본"])  # the model is back

    retried = await api.post(f"/runs/{run['runId']}/retry")

    assert retried.status_code == 202
    right_after = (await api.get(f"/runs/{run['runId']}")).json()
    assert right_after["cancelRequested"] is False
    finished = await _wait_status(api, run["runId"], "succeeded")
    assert finished["cancelRequested"] is False and finished["outputs"] == {"result": "요약본"}


async def test_retry_resets_active_ms(pool):
    """A3: active_ms is cumulative for a run's whole lifetime and _heartbeat fails a run once it exceeds
    run_max_active_ms; left unreset, a retried run that had already used up its active time budget would
    fail with RUN_TIMEOUT on the very first heartbeat tick after the retry, with no way for the API to
    ever make it succeed."""
    import engine.db.runs as run_db
    from tests.factories import make_run

    run_id = await make_run(pool, status="failed")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE runs SET active_ms=999999999 WHERE id=%s", (run_id,))

    async with pool.connection() as conn:
        changed = await run_db.queue_retry(conn, run_id)
        row = await (await conn.execute("SELECT active_ms FROM runs WHERE id=%s", (run_id,))).fetchone()

    assert changed is True
    assert row["active_ms"] == 0


async def test_retry_message_is_honest_about_a_run_that_never_checkpointed(api, pool):
    """A4: the missing-checkpoint 409 used to always blame the retention period, even for a run that
    never wrote a checkpoint at all -- a compile failure, a deleted stored version, or inputs rejected at
    start all fail before execute_run ever calls graph.ainvoke. make_run's freshly-inserted failed row has
    no node_runs either, so it is exactly this case: the message must not claim retention expired."""
    from tests.factories import make_run

    run_id = await make_run(pool, status="failed")

    response = await api.post(f"/runs/{run_id}/retry")

    assert (response.status_code, response.json()["error"]["code"]) == (409, "RUN_DATA_EXPIRED")
    assert "보존 기간" not in response.json()["error"]["message"]


async def test_retry_message_blames_retention_once_the_run_actually_executed(api, pool):
    """The other half of A4: once a run has at least one node_runs row, it did execute and would normally
    have a checkpoint, so a missing one there really is best explained by the retention period -- unlike
    the previous test, this message may say so."""
    from tests.factories import make_run

    run_id = await make_run(pool, status="failed")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status, finished_at)"
            " VALUES (gen_random_uuid(), %s, 'start', 1, 1, 'succeeded', now())", (run_id,))

    response = await api.post(f"/runs/{run_id}/retry")

    assert (response.status_code, response.json()["error"]["code"]) == (409, "RUN_DATA_EXPIRED")
    assert "보존 기간" in response.json()["error"]["message"]
