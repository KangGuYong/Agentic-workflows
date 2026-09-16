"""Workflow CRUD, optimistic locking and validation (MVP design 8.1, 8.3)."""
import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

import engine.db.workflows as workflow_db
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
    """Two clients both read revision 1 and race to save; the loser's 409 must describe whichever save
    actually committed (the current state), not whatever the loser itself saw before its own CAS was
    attempted -- that read can be stale by the time the CAS fails (see save_workflow's comment).

    Plain asyncio.gather on the two requests is not a reliable way to force this: on a fast local Postgres
    one request can run start-to-finish (read, CAS, commit) before the other's own pre-CAS read even fires,
    which never exercises the stale-read path at all. Instead this pins the exact interleaving that does:
    both requests' pre-CAS reads complete (both see revision 1, nothing committed yet) before either is
    allowed to attempt its CAS, and the second CAS is held back until the first has completed its own."""
    created = await _create(api)
    real_get, real_save_draft = workflow_db.get, workflow_db.save_draft
    read_count = 0
    both_read = asyncio.Event()
    save_order: list[str] = []
    winner_committed = asyncio.Event()

    async def patched_get(conn, workflow_id):
        nonlocal read_count
        result = await real_get(conn, workflow_id)
        read_count += 1
        if read_count >= 2:
            both_read.set()
        return result

    async def patched_save_draft(conn, **kwargs):
        await both_read.wait()
        save_order.append(kwargs["draft"]["which"])
        if len(save_order) == 1:
            result = await real_save_draft(conn, **kwargs)
            winner_committed.set()
            return result
        await winner_committed.wait()
        return await real_save_draft(conn, **kwargs)

    monkeypatch.setattr(workflow_db, "get", patched_get)
    monkeypatch.setattr(workflow_db, "save_draft", patched_save_draft)

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
    a race would pass even against the buggy plain-read version and prove nothing)."""
    created = await _create(api)
    real_lock, real_get = workflow_db.lock, workflow_db.get
    calls: list[str] = []

    async def spy_lock(conn, workflow_id):
        calls.append("lock")
        return await real_lock(conn, workflow_id)

    async def spy_get(conn, workflow_id):
        calls.append("get")
        return await real_get(conn, workflow_id)

    monkeypatch.setattr(workflow_db, "lock", spy_lock)
    monkeypatch.setattr(workflow_db, "get", spy_get)

    assert (await api.delete(f"/workflows/{created['id']}")).status_code == 204

    assert calls == ["lock"]


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
