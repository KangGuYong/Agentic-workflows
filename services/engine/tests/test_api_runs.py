"""Creating and reading runs (MVP design 8.1, 8.3, 8.4)."""
import asyncio
import logging
import threading
import uuid

import pytest
from psycopg.types.json import Jsonb

import engine.api.routers.runs as runs_router
import engine.db.runs as run_db
from engine.config import load_config
from engine.db.crypto import open_payload
from engine.llm.scripted import ScriptedLLM
from engine.validator import analyze as real_analyze
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


SECRET_OUTPUT = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start", "config": {"inputs": INPUT_SCHEMA}},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "{{ start.topic }}"}},
        {"id": "end", "type": "end", "config": {"outputs": {"apiKey": "{{ llm_1.text }}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"},
              {"id": "e2", "source": "llm_1", "target": "end"}],
}


async def test_run_inputs_and_outputs_are_redacted_on_get_run(api, pool, worker_factory):
    """design 10.1/10.3, A1 of the whole-branch review: `GET /runs/{id}` used to hand a secret-looking
    `inputs`/`outputs` value straight back, even though the sibling `GET /runs/{id}/nodes` route already
    redacted the same kind of data through the recorder's own `_safe`. `outputs` is redacted before it is
    even stored (`db.runs.finish`, checked here straight from Postgres); `inputs` cannot be (the worker
    feeds it to `execute_run` as the run's initial state), so it is redacted only on this read path -- and
    both are redacted again here regardless, since this is the transmission path design 10.3 governs."""
    workflow_id = await _saved(api, dsl=SECRET_OUTPUT)
    await worker_factory(ScriptedLLM(["sk-live-secret"]))

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI", "password": "hunter2-SECRET"},
                                    "revision": 2})).json()

    async def done():
        run = (await api.get(f"/runs/{created['runId']}")).json()
        return run if run["status"] == "succeeded" else None

    run = await until(done)
    assert run["inputs"] == {"topic": "AI", "password": "[REDACTED]"}
    assert run["outputs"] == {"apiKey": "[REDACTED]"}

    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT outputs FROM runs WHERE id=%s", (created["runId"],))).fetchone()
    # Two properties in one row: the column is ciphertext (2b design §9), and what it decodes to was
    # already redacted before it ever reached storage.
    assert b"sk-live-secret" not in bytes(row["outputs"])
    assert open_payload(load_config().secret_key, "outputs", row["outputs"]) == {"apiKey": "[REDACTED]"}


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


# ---------------------------------------------------------------- B1: listing orders newest-first, not LIMIT 1


async def test_listing_runs_returns_both_newest_first(api):
    workflow_id = await _saved(api)

    first = (await api.post(f"/workflows/{workflow_id}/runs",
                            json={"inputs": {"topic": "A"}, "revision": 2})).json()
    second = (await api.post(f"/workflows/{workflow_id}/runs",
                             json={"inputs": {"topic": "B"}, "revision": 2})).json()

    listed = (await api.get(f"/workflows/{workflow_id}/runs")).json()["runs"]

    assert [item["id"] for item in listed] == [second["runId"], first["runId"]]


# ---------------------------------------------------------------- B2: node trace order survives insertion order


async def test_node_runs_are_ordered_by_started_at_not_insertion_order(api, pool):
    """A retry or a loop can insert node_run rows in an order that does not match
    (started_at, exec_index, attempt) -- e.g. node "a" is recorded after node "c" here, exactly as a retried
    node would be recorded after a node that started later but failed faster. Passes today only because a
    three-row table happens to come back in heap (insertion) order without an ORDER BY; this pins the actual
    contract instead of that accident."""
    from datetime import datetime, timedelta, timezone

    from tests.factories import make_run

    run_id = await make_run(pool, status="succeeded")
    base = datetime.now(timezone.utc)
    # inserted as c, a, b -- started as a, b, c
    rows = [("c", base + timedelta(seconds=3)), ("a", base + timedelta(seconds=1)),
            ("b", base + timedelta(seconds=2))]
    async with pool.connection() as conn, conn.transaction():
        for node_id, started_at in rows:
            await conn.execute(
                "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status, started_at)"
                " VALUES (gen_random_uuid(), %s, %s, 0, 1, 'succeeded', %s)",
                (run_id, node_id, started_at))

    response = await api.get(f"/runs/{run_id}/nodes")

    assert [node["nodeId"] for node in response.json()["nodeRuns"]] == ["a", "b", "c"]


# ---------------------------------------------------------------- B3: the idempotency fast path must actually run


async def test_the_idempotency_fast_path_answers_without_reaching_insert_queued(api, monkeypatch):
    """Deleting the pre-lock idempotency lookup is invisible against the happy path (insert_queued would
    just succeed twice under two different requests and the UniqueViolation recovery would paper over it),
    so pin the actual short-circuit directly: once the first request has created a run for this key, a
    second request with the same key must return that run's id *without* ever calling insert_queued again."""
    workflow_id = await _saved(api)
    headers = {"Idempotency-Key": "fast-path-1"}

    first = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "A"}, "revision": 2},
                           headers=headers)
    assert first.status_code == 202

    async def broken_insert_queued(conn, **kwargs):
        raise AssertionError("insert_queued must not run: the idempotency lookup should have answered first")

    monkeypatch.setattr(run_db, "insert_queued", broken_insert_queued)

    second = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "B"}, "revision": 2},
                            headers=headers)

    assert second.status_code == 202
    assert second.json()["runId"] == first.json()["runId"]


# ---------------------------------------------------------------- B4: GET /runs/{id} on a well-formed unknown id


async def test_get_run_on_a_well_formed_unknown_id_is_404(api):
    response = await api.get(f"/runs/{uuid.uuid4()}")

    assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- B5: the run_queued event carries versionNo


async def test_the_run_queued_event_carries_version_no(api, pool):
    workflow_id = await _saved(api)

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    async with pool.connection() as conn:
        event = await (await conn.execute(
            "SELECT payload FROM run_events WHERE run_id=%s AND type='run_queued'",
            (created["runId"],))).fetchone()
        version = await (await conn.execute(
            "SELECT version_no FROM workflow_versions WHERE id=%s", (created["versionId"],))).fetchone()

    assert event["payload"] == {"versionNo": version["version_no"]}


# ---------------------------------------------------------------- B6: A1 (blank key), A2 (overlong key),
# A3 (nodes limit/hasMore) and A4 (runs cursor/limit) each covered directly


async def test_a_blank_idempotency_key_header_does_not_pin_runs_together(api):
    """A1: `Idempotency-Key: ` (present but blank) used to be falsy for the lookup but truthy enough to be
    stored as `""` -- not NULL -- so it silently joined runs_idempotency_idx and pinned every later create
    from a client that always sends the header blank to the first run forever."""
    workflow_id = await _saved(api)
    headers = {"Idempotency-Key": ""}

    responses = [await api.post(f"/workflows/{workflow_id}/runs",
                                json={"inputs": {"topic": topic}, "revision": 2}, headers=headers)
                for topic in ("A", "B", "C")]

    assert [response.status_code for response in responses] == [202, 202, 202]
    assert len({response.json()["runId"] for response in responses}) == 3


async def test_an_overlong_idempotency_key_is_rejected_with_422_not_500(api, pool):
    """A2: an unbounded key is half of runs_idempotency_idx's btree key; a 3KB one used to raise psycopg's
    ProgramLimitExceeded, which `except UniqueViolation` does not catch, surfacing as a bare 500."""
    workflow_id = await _saved(api)
    headers = {"Idempotency-Key": "k" * 3000}

    response = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "A"}, "revision": 2},
                              headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_ERROR"
    async with pool.connection() as conn:
        count = await (await conn.execute(
            "SELECT count(*) AS n FROM runs WHERE workflow_id=%s", (workflow_id,))).fetchone()
    assert count["n"] == 0


async def test_get_node_runs_honors_limit_and_reports_has_more(api, pool):
    from tests.factories import make_run

    run_id = await make_run(pool, status="succeeded")
    async with pool.connection() as conn, conn.transaction():
        for i in range(5):
            await conn.execute(
                "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status)"
                " VALUES (gen_random_uuid(), %s, %s, 0, 1, 'succeeded')", (run_id, f"n{i}"))

    limited = await api.get(f"/runs/{run_id}/nodes", params={"limit": 2})
    full = await api.get(f"/runs/{run_id}/nodes", params={"limit": 10})

    assert limited.status_code == 200
    assert len(limited.json()["nodeRuns"]) == 2 and limited.json()["hasMore"] is True
    assert len(full.json()["nodeRuns"]) == 5 and full.json()["hasMore"] is False


async def test_an_out_of_range_node_runs_limit_is_rejected(api):
    too_big = await api.get(f"/runs/{uuid.uuid4()}/nodes", params={"limit": 500})
    zero = await api.get(f"/runs/{uuid.uuid4()}/nodes", params={"limit": 0})
    not_a_number = await api.get(f"/runs/{uuid.uuid4()}/nodes", params={"limit": "abc"})

    for response in (too_big, zero, not_a_number):
        assert response.status_code == 422 and response.json()["error"]["code"] == "REQUEST_ERROR"


async def test_paging_workflow_runs_with_a_cursor_returns_every_run_exactly_once(api):
    workflow_id = await _saved(api)
    created_ids = []
    for i in range(5):
        response = await api.post(f"/workflows/{workflow_id}/runs",
                                  json={"inputs": {"topic": f"t{i}"}, "revision": 2})
        created_ids.append(response.json()["runId"])

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # guard against an infinite-loop regression
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        response = await api.get(f"/workflows/{workflow_id}/runs", params=params)
        assert response.status_code == 200
        body = response.json()
        seen.extend(item["id"] for item in body["runs"])
        cursor = body["nextCursor"]
        if cursor is None:
            break
    else:
        pytest.fail("paging never terminated")

    assert seen == list(reversed(created_ids))  # newest first, no duplicate or skipped run


async def test_a_malformed_runs_cursor_is_rejected_with_422_not_500(api):
    workflow_id = await _saved(api)

    response = await api.get(f"/workflows/{workflow_id}/runs", params={"cursor": "not-a-valid-cursor!!"})

    assert response.status_code == 422 and response.json()["error"]["code"] == "REQUEST_ERROR"


# ---------------------------------------------------------------- B7: A5 -- analysis must not hold the lock


async def test_analysis_does_not_hold_the_workflow_lock(api, monkeypatch):
    """A concurrent autosave PUT must be able to complete while a create_run is parked inside analyze().
    analyze() is pushed to a worker thread (asyncio.to_thread), so it is paused here with a real
    threading.Event -- an asyncio.Event could not be waited on from that thread. Once the PUT has bumped
    the revision, releasing analyze must make create_run notice the draft it analysed is now stale and
    answer 409, not silently pin a version for a draft nobody can see anymore.

    If analyze() still ran inside the same transaction as the workflow's FOR UPDATE lock (the pre-fix
    behaviour), the PUT's own UPDATE against that row would block behind it and this test would time out
    instead of completing -- bounded by asyncio.timeout so that shows up as a failure, not a hang."""
    workflow_id = await _saved(api)  # revision 2 after create + save
    started = threading.Event()
    release = threading.Event()

    def patched_analyze(dsl, registry):
        started.set()
        if not release.wait(timeout=15):
            raise TimeoutError("release was never set")
        return real_analyze(dsl, registry)

    monkeypatch.setattr(runs_router, "analyze", patched_analyze)

    async with asyncio.timeout(20):
        create_task = asyncio.create_task(
            api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "AI"}, "revision": 2}))
        try:
            assert await asyncio.to_thread(started.wait, 10), "analyze() never started"

            put_response = await api.put(f"/workflows/{workflow_id}",
                                         json={"draftDsl": TYPED, "revision": 2, "name": "다른 이름"})

            assert put_response.status_code == 200 and put_response.json()["revision"] == 3
        finally:
            release.set()

        create_response = await create_task

    assert create_response.status_code == 409
    error = create_response.json()["error"]
    assert error["code"] == "REVISION_CONFLICT" and error["details"]["currentRevision"] == 3


async def test_inputs_and_outputs_come_back_decrypted_and_redacted(api, pool, worker_factory):
    from engine.llm.scripted import ScriptedLLM
    from tests.conftest import until

    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI", "password": "hunter2-secret"},
                                    "revision": 2})).json()

    async def done():
        run = (await api.get(f"/runs/{created['runId']}")).json()
        return run if run["status"] == "succeeded" else None

    run = await until(done)

    assert run["inputs"]["topic"] == "AI"
    assert run["inputs"]["password"] == "[REDACTED]"  # key-name redaction on the read path
    assert run["outputs"] == {"result": "요약본"}


async def test_the_stored_input_is_ciphertext_while_the_api_still_reads_it(api, pool):
    """The two halves of the property in one test: unreadable in the table, readable through the API."""
    workflow_id = await _saved(api)
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "훔쳐갈-값"}, "revision": 2})).json()

    async with pool.connection() as conn:
        raw = await (await conn.execute("SELECT inputs FROM runs WHERE id=%s",
                                        (created["runId"],))).fetchone()

    assert "훔쳐갈-값".encode() not in bytes(raw["inputs"])
    assert (await api.get(f"/runs/{created['runId']}")).json()["inputs"]["topic"] == "훔쳐갈-값"
