"""Workflow CRUD, optimistic locking and validation (MVP design 8.1, 8.3)."""
import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from psycopg import errors as pg_errors
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

import engine.api.routers.workflows as workflows_router
import engine.db.workflows as workflow_db
from engine.validator import analyze
from tests.factories import WORKSPACE

CHAIN = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "{{ start.topic }}"}},
        {"id": "end", "type": "end", "config": {"outputs": {"result": "{{ llm_1.text }}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"},
              {"id": "e2", "source": "llm_1", "target": "end"}],
}


async def _create(api, name: str = "내 워크플로") -> dict:
    response = await api.post("/workflows", json={"name": name})
    assert response.status_code == 201
    return response.json()


async def test_create_read_and_list(api):
    created = await _create(api)

    fetched = (await api.get(f"/workflows/{created['id']}")).json()
    listed = (await api.get("/workflows")).json()

    assert (created["revision"], fetched["name"]) == (1, "내 워크플로")
    assert fetched["draftDsl"] == {"version": "1", "nodes": [], "edges": []}
    assert [item["id"] for item in listed["workflows"]] == [created["id"]]


async def test_saving_a_draft_bumps_the_revision(api):
    created = await _create(api)

    saved = await api.put(f"/workflows/{created['id']}",
                          json={"draftDsl": CHAIN, "revision": 1, "name": "이름 변경"})

    assert saved.status_code == 200 and saved.json()["revision"] == 2
    fetched = (await api.get(f"/workflows/{created['id']}")).json()
    assert fetched["draftDsl"] == CHAIN and fetched["name"] == "이름 변경"


async def test_a_stale_revision_conflicts_and_returns_the_current_draft(api):
    created = await _create(api)
    await api.put(f"/workflows/{created['id']}", json={"draftDsl": CHAIN, "revision": 1})

    conflict = await api.put(f"/workflows/{created['id']}", json={"draftDsl": {"nodes": [], "edges": []},
                                                                  "revision": 1})

    assert conflict.status_code == 409
    error = conflict.json()["error"]
    assert error["code"] == "REVISION_CONFLICT" and error["details"]["currentRevision"] == 2
    assert error["details"]["draftDsl"] == CHAIN


async def test_a_draft_may_be_invalid_but_not_oversized(api):
    created = await _create(api)
    broken = {"version": "1", "nodes": [{"id": "start", "type": "start"}], "edges": []}

    assert (await api.put(f"/workflows/{created['id']}", json={"draftDsl": broken, "revision": 1})).status_code == 200

    huge = {"version": "1", "nodes": [{"id": "start", "type": "start", "label": "가" * 300_000}], "edges": []}
    oversized = await api.put(f"/workflows/{created['id']}", json={"draftDsl": huge, "revision": 2})
    assert oversized.status_code == 422 and oversized.json()["error"]["code"] == "LIMIT_EXCEEDED"


async def test_validate_reports_issues_without_saving(api):
    created = await _create(api)
    broken = {"version": "1", "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}],
              "edges": []}

    response = await api.post(f"/workflows/{created['id']}/validate", json={"draftDsl": broken})

    codes = {issue["code"] for issue in response.json()["issues"]}
    assert response.status_code == 200 and codes
    assert (await api.get(f"/workflows/{created['id']}")).json()["revision"] == 1


async def test_a_body_that_is_not_standard_json_is_rejected(api):
    response = await api.post("/workflows", content='{"name": NaN}',
                              headers={"content-type": "application/json"})

    assert response.status_code == 400 and response.json()["error"]["code"] == "INVALID_JSON"


async def test_a_body_over_the_limit_is_rejected(api):
    big = json.dumps({"name": "x" * (api.config.max_body_bytes + 10)})

    response = await api.post("/workflows", content=big, headers={"content-type": "application/json"})

    assert response.status_code == 413 and response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_a_workflow_with_an_active_run_cannot_be_deleted(api, pool):
    created = await _create(api)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO workflow_versions (id, workflow_id, workspace_id, version_no, dsl, dsl_hash)"
            " VALUES (gen_random_uuid(), %s, %s, 1, %s, 'h') RETURNING id",
            (created["id"], WORKSPACE, Jsonb(CHAIN)))
        version = await (await conn.execute(
            "SELECT id FROM workflow_versions WHERE workflow_id=%s", (created["id"],))).fetchone()
        await conn.execute(
            "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status)"
            " VALUES (gen_random_uuid(), %s, %s, %s, 'waiting')",
            (WORKSPACE, created["id"], version["id"]))

    blocked = await api.delete(f"/workflows/{created['id']}")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "WORKFLOW_HAS_ACTIVE_RUNS"

    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET status='cancelled' WHERE workflow_id=%s", (created["id"],))
    assert (await api.delete(f"/workflows/{created['id']}")).status_code == 204
    assert (await api.get(f"/workflows/{created['id']}")).status_code == 404


# ---------------------------------------------------------------- extra coverage beyond the plan's own tests


async def test_a_name_that_is_only_whitespace_is_rejected(api):
    response = await api.post("/workflows", json={"name": "   "})

    assert response.status_code == 422 and response.json()["error"]["code"] == "REQUEST_ERROR"


async def test_renaming_to_only_whitespace_on_save_is_also_rejected(api):
    created = await _create(api)

    response = await api.put(f"/workflows/{created['id']}",
                             json={"draftDsl": {"version": "1", "nodes": [], "edges": []},
                                   "revision": 1, "name": "  "})

    assert response.status_code == 422 and response.json()["error"]["code"] == "REQUEST_ERROR"


async def test_saving_with_a_wrong_type_revision_is_rejected(api):
    created = await _create(api)

    response = await api.put(f"/workflows/{created['id']}",
                             json={"draftDsl": {"version": "1", "nodes": [], "edges": []}, "revision": "1"})

    assert response.status_code == 422 and response.json()["error"]["code"] == "REQUEST_ERROR"


async def test_ids_that_are_not_uuids_are_reported_as_not_found_not_a_server_error(api):
    bad = "not-a-uuid"

    get_response = await api.get(f"/workflows/{bad}")
    put_response = await api.put(f"/workflows/{bad}",
                                 json={"draftDsl": {"version": "1", "nodes": [], "edges": []}, "revision": 1})
    delete_response = await api.delete(f"/workflows/{bad}")
    validate_response = await api.post(f"/workflows/{bad}/validate",
                                       json={"draftDsl": {"version": "1", "nodes": [], "edges": []}})

    for response in (get_response, put_response, delete_response, validate_response):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_validate_does_not_create_a_version_row(api, pool):
    created = await _create(api)

    await api.post(f"/workflows/{created['id']}/validate", json={"draftDsl": CHAIN})

    async with pool.connection() as conn:
        count = await (await conn.execute(
            "SELECT count(*) AS n FROM workflow_versions WHERE workflow_id=%s", (created["id"],))).fetchone()
    assert count["n"] == 0


async def test_validate_reports_an_oversized_draft_as_an_issue_not_a_hard_error(api):
    """Unlike save_workflow (which never calls analyze() and so needs its own _check_size gate),
    validate_workflow leaves the size check entirely to analyze(): confirms the two never disagree by
    checking there is only one behaviour to disagree about."""
    created = await _create(api)
    huge = {"version": "1", "nodes": [{"id": "start", "type": "start", "label": "가" * 300_000}], "edges": []}

    response = await api.post(f"/workflows/{created['id']}/validate", json={"draftDsl": huge})

    assert response.status_code == 200
    codes = {issue["code"] for issue in response.json()["issues"]}
    assert "LIMIT_EXCEEDED" in codes


async def test_a_concurrent_save_reports_the_revision_that_actually_won(api, monkeypatch):
    """Two clients both attempt to save at revision 1; only one CAS can win. The loser's 409 must describe
    whatever the winner actually committed -- built from a fresh post-CAS read (see the `current = await
    _require(...)` line in save_workflow), never from a stale cached view.

    save_workflow has no pre-CAS read any more (A3: it was dead weight -- the 404 case it existed for is
    already produced by the post-CAS re-read when the CAS matches zero rows for a missing id, exactly as it
    does for a stale revision). So the only coordination point left is workflow_db.save_draft itself: this
    pins the first caller to reach it as the winner and holds the second back until the first has committed,
    guaranteeing a genuine CAS race every run instead of hoping asyncio.gather schedules one.

    Every wait is bounded by asyncio.timeout and fails the test with a timeout error rather than hanging --
    there is no pytest-timeout in this project, so an unbounded wait here would be a stuck CI job forever."""
    created = await _create(api)
    real_save_draft = workflow_db.save_draft
    first_done = asyncio.Event()
    order: list[str] = []

    async def patched_save_draft(conn, **kwargs):
        async with asyncio.timeout(5):
            is_first = not order
            order.append(kwargs["draft"]["which"])
            if is_first:
                result = await real_save_draft(conn, **kwargs)
                first_done.set()
                return result
            await first_done.wait()
            return await real_save_draft(conn, **kwargs)

    monkeypatch.setattr(workflow_db, "save_draft", patched_save_draft)

    async with asyncio.timeout(10):
        responses = await asyncio.gather(
            api.put(f"/workflows/{created['id']}", json={"draftDsl": {"which": "A"}, "revision": 1}),
            api.put(f"/workflows/{created['id']}", json={"draftDsl": {"which": "B"}, "revision": 1}),
        )

    winners = [r for r in responses if r.status_code == 200]
    losers = [r for r in responses if r.status_code == 409]
    assert len(winners) == 1 and len(losers) == 1

    fetched = (await api.get(f"/workflows/{created['id']}")).json()
    details = losers[0].json()["error"]["details"]
    assert details["currentRevision"] == fetched["revision"] == 2
    assert details["draftDsl"] == fetched["draftDsl"]


async def test_delete_workflow_locks_the_row_instead_of_a_plain_read(api, monkeypatch):
    """The has_active_runs check and the DELETE must run under workflow_db.lock (SELECT ... FOR UPDATE),
    not workflow_db.get: a plain read lets a run inserted in the gap between the check and the DELETE be
    silently swept away by ON DELETE CASCADE (see the mechanism test below for why FOR UPDATE closes that
    gap). This pins the implementation choice directly, since a real end-to-end race between an HTTP DELETE
    and a raw INSERT is not reliably reproducible in-process (asyncio consistently schedules the DELETE's
    own resumption ahead of a competing task's first query in this harness, regardless of locking, so such
    a race would pass even against the buggy plain-read version and prove nothing).

    A second, more direct gap this pins: the pool is autocommit (engine/db/pool.py), so `FOR UPDATE` only
    holds a lock for as long as it runs inside an explicit transaction. If delete_workflow's
    `conn.transaction()` were ever removed, the SELECT ... FOR UPDATE would commit -- and release its lock
    -- immediately after that one statement, before has_active_runs even runs, making the whole FOR UPDATE a
    no-op while every one of these assertions (including the 204 below) still passes. Recording the
    connection's transaction_status right after the lock call catches that: it must be INTRANS (inside an
    still-open transaction), not IDLE (autocommitted already)."""
    created = await _create(api)
    real_lock, real_get = workflow_db.lock, workflow_db.get
    calls: list[str] = []
    statuses: list[TransactionStatus] = []

    async def spy_lock(conn, workflow_id):
        calls.append("lock")
        row = await real_lock(conn, workflow_id)
        statuses.append(conn.info.transaction_status)
        return row

    async def spy_get(conn, workflow_id):
        calls.append("get")
        return await real_get(conn, workflow_id)

    monkeypatch.setattr(workflow_db, "lock", spy_lock)
    monkeypatch.setattr(workflow_db, "get", spy_get)

    assert (await api.delete(f"/workflows/{created['id']}")).status_code == 204

    assert calls == ["lock"]
    assert statuses == [TransactionStatus.INTRANS]


async def test_a_run_insert_blocks_on_the_workflow_lock_and_fails_once_it_is_gone(api, pool):
    """The mechanism workflow_db.lock and delete_workflow rely on (see its docstring and the spy test
    above): holding FOR UPDATE on the workflow row blocks a concurrent INSERT into runs, because Postgres
    takes a FOR KEY SHARE lock on the referenced row to enforce runs.workflow_id's FK. Proven directly and
    deterministically here -- not by racing two coroutines and hoping for adversarial timing, but by
    holding the lock open ourselves, confirming a concurrent insert is genuinely stuck behind it, and only
    then releasing it (via delete) to observe the insert fail instead of quietly succeeding."""
    created = await _create(api)

    async def insert_run() -> None:
        version_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO workflow_versions (id, workflow_id, workspace_id, version_no, dsl, dsl_hash)"
                " VALUES (%s, %s, %s, 1, %s, 'h')",
                (version_id, created["id"], WORKSPACE, Jsonb(CHAIN)))
            await conn.execute(
                "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status)"
                " VALUES (%s, %s, %s, %s, 'queued')",
                (run_id, WORKSPACE, created["id"], version_id))

    async with pool.connection() as holder:
        await holder.execute("BEGIN")
        await workflow_db.lock(holder, created["id"])

        insert_task = asyncio.create_task(insert_run())
        await asyncio.sleep(0.3)
        assert not insert_task.done()  # genuinely blocked on the lock, not just slow

        await holder.execute("DELETE FROM workflows WHERE id=%s", (created["id"],))
        await holder.execute("COMMIT")

    with pytest.raises(pg_errors.ForeignKeyViolation):
        await insert_task


async def test_a_bad_name_written_by_an_older_path_does_not_break_get(api, monkeypatch):
    """read_json refuses a lone surrogate on the way in, so nothing written through the API can carry one,
    but a row written some other way (a migration, a seed script, a future writer that skips read_json)
    could. Simulated here by faking the db layer, since neither psycopg nor Postgres will let a real INSERT
    carry one. Left unsanitized, this breaks response serialization outright (Task 11 review, same failure
    mode): Starlette's JSONResponse encodes with ensure_ascii=False and str.encode("utf-8") raises
    UnicodeEncodeError on an unpaired surrogate, which is not a clean 4xx/5xx but a raised exception."""
    created = await _create(api)
    bad_row = {"id": created["id"], "name": "이름\udcff", "revision": 1,
              "updated_at": datetime.now(timezone.utc), "draft_dsl": {"note": "\ud800탈출"}}

    async def fake_get(conn, workflow_id):
        return bad_row

    monkeypatch.setattr(workflow_db, "get", fake_get)

    response = await api.get(f"/workflows/{created['id']}")

    assert response.status_code == 200
    assert "�" in response.json()["name"] and "\udcff" not in response.json()["name"]
    assert "�" in response.json()["draftDsl"]["note"]


async def test_a_bad_draft_written_by_an_older_path_does_not_break_the_409_body(api, monkeypatch):
    created = await _create(api)
    bad_row = {"id": created["id"], "name": "ok", "revision": 1,
              "updated_at": datetime.now(timezone.utc), "draft_dsl": {"note": "\udcff"}}

    async def fake_get(conn, workflow_id):
        return bad_row

    monkeypatch.setattr(workflow_db, "get", fake_get)

    response = await api.put(f"/workflows/{created['id']}",
                             json={"draftDsl": {"a": 1}, "revision": 999})

    assert response.status_code == 409
    assert "�" in response.json()["error"]["details"]["draftDsl"]["note"]


# ---------------------------------------------------------------- adversarial-review follow-ups (B1-B6)


async def test_validate_runs_analysis_off_the_event_loop(api, monkeypatch):
    """Design 8.1: validation is CPU work on untrusted input and must stay off the event loop. Spy on
    asyncio.to_thread itself (not on `analyze`, which would pass whether or not it actually ran in a
    thread) so a regression that calls analyze() directly on the loop is caught."""
    created = await _create(api)
    real_to_thread = asyncio.to_thread
    calls: list = []

    async def spy_to_thread(func, *args, **kwargs):
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)

    response = await api.post(f"/workflows/{created['id']}/validate", json={"draftDsl": CHAIN})

    assert response.status_code == 200
    assert calls == [analyze]


async def test_a_name_over_200_characters_is_rejected(api):
    response = await api.post("/workflows", json={"name": "가" * 201})

    assert response.status_code == 422 and response.json()["error"]["code"] == "REQUEST_ERROR"


async def test_a_well_formed_but_unknown_id_is_404_on_every_route(api):
    """Unlike the not-a-uuid-at-all case (which _workflow_id rejects before any query), a syntactically
    valid UUID that matches no row must still reach _require's 404 on every route -- validate_workflow's own
    _require included (it is untouched by A3, which only removed save_workflow's redundant pre-CAS read)."""
    missing = str(uuid.uuid4())
    empty_dsl = {"version": "1", "nodes": [], "edges": []}

    get_response = await api.get(f"/workflows/{missing}")
    put_response = await api.put(f"/workflows/{missing}", json={"draftDsl": empty_dsl, "revision": 1})
    delete_response = await api.delete(f"/workflows/{missing}")
    validate_response = await api.post(f"/workflows/{missing}/validate", json={"draftDsl": empty_dsl})

    for response in (get_response, put_response, delete_response, validate_response):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_list_all_returns_every_workflow_for_the_workspace_most_recently_updated_first(api, pool):
    """A single-workflow list test can't catch a wrong workspace filter, a `LIMIT 1`, or an ASC instead of
    DESC ordering -- all three still pass it. This creates several, touches one after the rest so its
    updated_at is unambiguously newest, and plants a row in a different workspace directly (workspace_id has
    no FK, and the API only ever writes one workspace) to prove the WHERE clause is still there."""
    first = await _create(api, name="a")
    second = await _create(api, name="b")
    third = await _create(api, name="c")
    empty_dsl = {"version": "1", "nodes": [], "edges": []}
    await api.put(f"/workflows/{first['id']}", json={"draftDsl": empty_dsl, "revision": 1})

    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO workflows (id, workspace_id, name, draft_dsl) VALUES (%s, %s, %s, %s)",
            (str(uuid.uuid4()), str(uuid.uuid4()), "other workspace", Jsonb(empty_dsl)))

    listed = (await api.get("/workflows")).json()["workflows"]

    assert [item["id"] for item in listed] == [first["id"], third["id"], second["id"]]


async def test_a_draft_dsl_that_is_not_an_object_is_rejected(api):
    created = await _create(api)

    save_string = await api.put(f"/workflows/{created['id']}", json={"draftDsl": "not-an-object", "revision": 1})
    save_list = await api.put(f"/workflows/{created['id']}", json={"draftDsl": [], "revision": 1})
    validate_string = await api.post(f"/workflows/{created['id']}/validate", json={"draftDsl": "nope"})
    validate_list = await api.post(f"/workflows/{created['id']}/validate", json={"draftDsl": []})

    for response in (save_string, save_list, validate_string, validate_list):
        assert response.status_code == 422 and response.json()["error"]["code"] == "REQUEST_ERROR"


async def test_validate_rejects_an_oversized_draft_without_running_analyze(api, monkeypatch):
    """Covers A1: a draft over MAX_DSL_BYTES still gets a 200 with a LIMIT_EXCEEDED issue, but by the cheap
    pre-check, not by paying for a full analyze() call -- spying on the module's own `analyze` reference
    (what _analysis actually calls) proves the slow path never ran."""
    created = await _create(api)
    huge = {"version": "1", "nodes": [{"id": "start", "type": "start", "label": "x" * 600_000}], "edges": []}
    calls: list = []
    real_analyze = workflows_router.analyze

    def spy_analyze(*args, **kwargs):
        calls.append(args)
        return real_analyze(*args, **kwargs)

    monkeypatch.setattr(workflows_router, "analyze", spy_analyze)

    response = await api.post(f"/workflows/{created['id']}/validate", json={"draftDsl": huge})

    assert response.status_code == 200
    codes = {issue["code"] for issue in response.json()["issues"]}
    assert "LIMIT_EXCEEDED" in codes
    assert calls == []
