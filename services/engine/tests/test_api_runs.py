"""Creating and reading runs (MVP design 8.1, 8.3, 8.4)."""
import asyncio
import logging
import uuid

import pytest
from psycopg.types.json import Jsonb

import engine.db.runs as run_db
from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.test_api_workflows import CHAIN, _create

INPUT_SCHEMA = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
TYPED = {**CHAIN, "nodes": [{**CHAIN["nodes"][0], "config": {"inputs": INPUT_SCHEMA}}, *CHAIN["nodes"][1:]]}


async def _saved(api, dsl=TYPED) -> str:
    workflow = await _create(api)
    await api.put(f"/workflows/{workflow['id']}", json={"draftDsl": dsl, "revision": 1})
    return workflow["id"]


async def test_creating_a_run_pins_a_version_and_queues_it(api, pool):
    workflow_id = await _saved(api)

    response = await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})

    assert response.status_code == 202
    body = response.json()
    run = (await api.get(f"/runs/{body['runId']}")).json()
    assert (run["status"], run["inputs"]) == ("queued", {"topic": "AI"})
    assert run["versionId"] == body["versionId"]
    async with pool.connection() as conn:
        events = await (await conn.execute("SELECT type, seq FROM run_events WHERE run_id=%s",
                                           (body["runId"],))).fetchall()
    assert [(event["type"], event["seq"]) for event in events] == [("run_queued", 1)]


async def test_the_same_idempotency_key_returns_the_same_run(api):
    workflow_id = await _saved(api)
    headers = {"Idempotency-Key": "click-1"}

    first = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "A"}, "revision": 2},
                           headers=headers)
    second = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "B"}, "revision": 2},
                            headers=headers)

    assert first.json()["runId"] == second.json()["runId"]


async def test_the_same_dsl_reuses_its_version(api):
    workflow_id = await _saved(api)

    first = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "A"}, "revision": 2})
    second = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "B"}, "revision": 2})

    assert first.json()["versionId"] == second.json()["versionId"]


async def test_running_a_stale_revision_conflicts(api):
    workflow_id = await _saved(api)

    response = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {}, "revision": 1})

    assert response.status_code == 409 and response.json()["error"]["code"] == "REVISION_CONFLICT"


async def test_an_invalid_workflow_is_not_run(api):
    workflow_id = await _saved(api, dsl={"version": "1", "nodes": [{"id": "start", "type": "start"}],
                                         "edges": []})

    response = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {}, "revision": 2})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["details"]["issues"]


async def test_oversized_inputs_are_rejected_by_the_input_specific_check(api):
    """The whole request body (~900KB) stays under max_body_bytes (1,000,000B default), so read_json's own
    size gate never trips -- only run_db.MAX_INPUT_BYTES (256,000B) does. The message names the run-input
    limit, not the request-body limit, which is how we know which check actually answered (see the sibling
    test below for the request-body check itself)."""
    workflow_id = await _saved(api)

    response = await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "가" * 300_000}, "revision": 2})

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert "실행 입력" in error["message"]  # the MAX_INPUT_BYTES message, not read_json's request-body one


async def test_an_oversized_request_body_is_rejected_by_read_json_first(api):
    """Well over max_body_bytes (1,000,000B): read_json's own bound trips before the route ever extracts
    `inputs`, so this is the other 413 path -- covering it here so both size gates in the plan actually
    get exercised somewhere in this file."""
    workflow_id = await _saved(api)

    response = await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "가" * 400_000}, "revision": 2})

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert "실행 입력" not in error["message"]  # read_json's generic request-too-large message, not ours


async def test_a_run_is_picked_up_and_its_nodes_readable(api, pool, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    async def done():
        run = (await api.get(f"/runs/{created['runId']}")).json()
        return run if run["status"] == "succeeded" else None

    run = await until(done)
    assert run["outputs"] == {"result": "요약본"}
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()["nodeRuns"]
    assert [(node["nodeId"], node["status"], node["attempt"]) for node in nodes] == [
        ("start", "succeeded", 1), ("llm_1", "succeeded", 1), ("end", "succeeded", 1)
    ]
    listed = (await api.get(f"/workflows/{workflow_id}/runs")).json()["runs"]
    assert [item["id"] for item in listed] == [created["runId"]]


# ---------------------------------------------------------------- path ids (Task 12 review: no DataError 500s)


async def test_a_non_uuid_workflow_id_is_404_not_500(api):
    created = await api.post("/workflows/not-a-uuid/runs", json={"inputs": {}, "revision": 1})
    listed = await api.get("/workflows/not-a-uuid/runs")

    assert created.status_code == 404 and created.json()["error"]["code"] == "NOT_FOUND"
    assert listed.status_code == 404 and listed.json()["error"]["code"] == "NOT_FOUND"


async def test_a_non_uuid_run_id_is_404_not_500(api):
    fetched = await api.get("/runs/not-a-uuid")
    nodes = await api.get("/runs/not-a-uuid/nodes")

    assert fetched.status_code == 404 and fetched.json()["error"]["code"] == "NOT_FOUND"
    assert nodes.status_code == 404 and nodes.json()["error"]["code"] == "NOT_FOUND"


async def test_get_node_runs_on_an_unknown_run_is_404(api):
    response = await api.get(f"/runs/{uuid.uuid4()}/nodes")

    assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- idempotency recovery under a real race


async def test_two_concurrent_creates_with_the_same_key_agree_on_one_run(api, pool, monkeypatch):
    """Both requests carry the same Idempotency-Key and race past the existence check with nothing yet in
    the table, so both reach insert_queued and only one INSERT can satisfy the partial unique index on
    (workflow_id, idempotency_key) -- the loser must land in the `except UniqueViolation` branch, re-query
    by (workflow_id, key) and answer with the winner's row instead of a 500. That only works if the
    migration's index actually matches that lookup's columns and predicate.

    insert_queued is patched to pin whichever caller reaches it first through to a real commit and hold the
    second back until then, guaranteeing a genuine UniqueViolation every run rather than hoping
    asyncio.gather happens to interleave two coroutines onto colliding INSERTs. Every wait is bounded."""
    workflow_id = await _saved(api)
    real_insert_queued = run_db.insert_queued
    first_done = asyncio.Event()
    order: list[str] = []

    async def patched_insert_queued(conn, **kwargs):
        async with asyncio.timeout(5):
            is_first = not order
            order.append(kwargs["inputs"]["topic"])
            if is_first:
                result = await real_insert_queued(conn, **kwargs)
                first_done.set()
                return result
            await first_done.wait()
            return await real_insert_queued(conn, **kwargs)

    monkeypatch.setattr(run_db, "insert_queued", patched_insert_queued)
    headers = {"Idempotency-Key": "race-1"}

    async with asyncio.timeout(10):
        responses = await asyncio.gather(
            api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "A"}, "revision": 2},
                    headers=headers),
            api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "B"}, "revision": 2},
                    headers=headers),
        )

    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json()["runId"] == responses[1].json()["runId"]
    async with pool.connection() as conn:
        count = await (await conn.execute(
            "SELECT count(*) AS n FROM runs WHERE workflow_id=%s AND idempotency_key=%s",
            (workflow_id, "race-1"))).fetchone()
    assert count["n"] == 1


# ---------------------------------------------------------------- notify_queued must stay inside the transaction


async def test_a_rejected_run_leaves_no_row_and_no_notification(api, pool, listen_conn):
    workflow_id = await _saved(api, dsl={"version": "1", "nodes": [{"id": "start", "type": "start"}],
                                         "edges": []})
    await listen_conn.execute(f"LISTEN {run_db.QUEUE_CHANNEL}")

    response = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {}, "revision": 2})

    assert response.status_code == 422
    async with pool.connection() as conn:
        count = await (await conn.execute(
            "SELECT count(*) AS n FROM runs WHERE workflow_id=%s", (workflow_id,))).fetchone()
    assert count["n"] == 0
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            async for _ in listen_conn.notifies(stop_after=1):
                pass


# ---------------------------------------------------------------- Redis publish failures never fail the request


async def test_a_redis_publish_failure_does_not_fail_run_creation(api, monkeypatch, caplog):
    async def broken(self, run_id, event):
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr("engine.events.publish.RedisPublisher.publish", broken)
    workflow_id = await _saved(api)

    with caplog.at_level(logging.WARNING, logger="engine.api.routers.runs"):
        response = await api.post(f"/workflows/{workflow_id}/runs",
                                  json={"inputs": {"topic": "AI"}, "revision": 2})

    assert response.status_code == 202  # the run is already committed; a lost live event only delays the editor
    assert any("publish" in record.message.lower() for record in caplog.records)


# ---------------------------------------------------------------- reading a run parked on an approval


async def test_get_run_on_an_approval_returns_the_stored_waiting_payload(api, pool):
    from tests.factories import make_run

    run_id = await make_run(pool, status="waiting")
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE runs SET waiting_node_id=%s, waiting_exec_index=%s WHERE id=%s",
                           ("approve_1", 0, run_id))
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status, waited, meta)"
            " VALUES (gen_random_uuid(), %s, %s, %s, 1, 'waiting', true, %s)",
            (run_id, "approve_1", 0, Jsonb({"waiting": {"prompt": "승인하시겠습니까?"}})))

    response = await api.get(f"/runs/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "waiting"
    assert body["waitingFor"] == {"prompt": "승인하시겠습니까?"}


# ---------------------------------------------------------------- store_run_data is recorded from the pinned DSL


async def test_a_run_records_store_run_data_false_from_its_workflow(api, pool):
    dsl = {**TYPED, "settings": {"storeRunData": False}}
    workflow_id = await _saved(api, dsl=dsl)

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT store_run_data FROM runs WHERE id=%s",
                                        (created["runId"],))).fetchone()
    assert row["store_run_data"] is False
