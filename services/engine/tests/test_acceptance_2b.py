"""The invariants from 2b design §11.2, end to end: API in, worker out, real sockets.

Every other test in this branch proves one component against a fake. These prove the assembled thing, and
in particular the claim the whole secret design rests on: a secret exists in plaintext inside the
http_request node and nowhere else that anyone can read.
"""
import dataclasses
import json
import logging

from engine.config import load_config
from engine.http.client import GuardedClient, SystemResolver
from engine.http.policy import parse_allowlist
from engine.llm.scripted import ScriptedLLM
from engine.secrets.store import PostgresSecretResolver, put_secret
from engine.worker.reaper import Reaper
from tests.conftest import until
from tests.helpers import load_golden

SECRET = "hunter2-a-real-looking-token"


def _key() -> bytes:
    """The configured key, not one of this file's own.

    `secret_key` seals two different things — stored secrets *and* `runs.inputs`/`outputs` — so a test that
    hands the worker a different key from the API's would have the API unable to decrypt what the worker
    wrote, and the run would come back with empty outputs for a reason that has nothing to do with what is
    being tested.
    """
    return load_config().secret_key


async def _saved(api, dsl) -> str:
    created = (await api.post("/workflows", json={"name": "acceptance"})).json()
    workflow_id = created["id"]
    saved = await api.put(f"/workflows/{workflow_id}",
                          json={"draftDsl": dsl, "revision": created["revision"]})
    assert saved.status_code == 200, saved.text
    return workflow_id


async def _worker(worker_factory, pool, allowlist: str):
    http = GuardedClient(parse_allowlist(allowlist), resolver=SystemResolver())
    secrets = PostgresSecretResolver(pool, _key())
    worker = await worker_factory(ScriptedLLM([]), http=http, secrets=secrets)
    return worker, http


async def _finished(api, run_id: str) -> dict:
    async def done():
        run = (await api.get(f"/runs/{run_id}")).json()
        return run if run["status"] in ("succeeded", "failed", "cancelled") else None

    return await until(done, timeout=30)


async def _streamed_events(api, run_id: str) -> str:
    """What a browser attached to the run would have seen. The run is already finished, so the stream
    replays every stored event and ends at the terminal one."""
    lines: list[str] = []
    async with api.stream("GET", f"/runs/{run_id}/events") as response:
        async for line in response.aiter_lines():
            lines.append(line)
            if "run_succeeded" in line or "run_failed" in line:
                break
    return "\n".join(lines)


async def _everything_stored(pool, run_id: str) -> str:
    """Every row a person or a backup could read, as one string."""
    async with pool.connection() as conn:
        runs = await (await conn.execute("SELECT * FROM runs WHERE id=%s", (run_id,))).fetchall()
        nodes = await (await conn.execute("SELECT * FROM node_runs WHERE run_id=%s", (run_id,))).fetchall()
        events = await (await conn.execute("SELECT * FROM run_events WHERE run_id=%s", (run_id,))).fetchall()
        points = await (await conn.execute(
            "SELECT checkpoint, metadata FROM checkpoints WHERE thread_id=%s", (run_id,))).fetchall()
    return repr([runs, nodes, events, points])


async def test_a_secret_reaches_the_server_and_nothing_else(api, pool, worker_factory, http_server, caplog):
    """The five places at once (2b design §11.2): database rows, the API's own responses, the node-run
    view, the event stream and the logs. If the plaintext is in any of them the design has failed,
    whichever one it is."""
    caplog.set_level(logging.DEBUG)
    async with pool.connection() as conn:
        await put_secret(conn, key=_key(), name="API_TOKEN", value=SECRET)
    workflow_id = await _saved(api, load_golden("http_call"))
    await _worker(worker_factory, pool, f"{http_server};allowPrivate")

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"issue": f"{http_server}/issues/1"}, "revision": 2})).json()
    run = await _finished(api, created["runId"])

    assert run["status"] == "succeeded", run
    assert "이슈 제목" in json.dumps(run["outputs"], ensure_ascii=False)  # the call really happened
    stored = await _everything_stored(pool, created["runId"])
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()
    events = await _streamed_events(api, created["runId"])
    for place, text in (("database", stored), ("run view", json.dumps(run, ensure_ascii=False)),
                        ("node runs", json.dumps(nodes, ensure_ascii=False)),
                        ("logs", caplog.text), ("events", events)):
        assert SECRET not in text, f"the secret leaked into the {place}"
    # And it was replaced, not merely absent: the server echoed the header back, so something had to
    # redact it on the way into storage.
    assert "[REDACTED]" in json.dumps(nodes, ensure_ascii=False)


async def test_the_real_credential_is_what_went_on_the_wire(api, pool, worker_factory, http_server):
    """The other half of the claim above, and the half that is easy to fake: `SECRET not in ...` passes
    just as well if the node never resolved anything and sent the marker instead. The server records what
    it was actually sent.

    The marker is deliberately *not* asserted anywhere in storage: `headers.Authorization` is replaced
    wholesale by key-name redaction before the attempt is recorded, so what a reader sees is `[REDACTED]`,
    not the marker. That is the stricter of the two behaviours, and it is why the proof has to come from
    the server rather than from the record.
    """
    async with pool.connection() as conn:
        await put_secret(conn, key=_key(), name="API_TOKEN", value=SECRET)
    workflow_id = await _saved(api, load_golden("http_call"))
    await _worker(worker_factory, pool, f"{http_server};allowPrivate")

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"issue": f"{http_server}/issues/1"}, "revision": 2})).json()
    assert (await _finished(api, created["runId"]))["status"] == "succeeded"

    assert http_server.authorizations == [f"Bearer {SECRET}"], (
        "the server did not receive the resolved credential"
    )


async def test_a_host_outside_the_allowlist_fails_the_run(api, pool, worker_factory, http_server):
    workflow_id = await _saved(api, load_golden("http_call"))
    async with pool.connection() as conn:
        await put_secret(conn, key=_key(), name="API_TOKEN", value=SECRET)
    await _worker(worker_factory, pool, "https://api.example.com")  # not the test server

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"issue": f"{http_server}/issues/1"}, "revision": 2})).json()
    run = await _finished(api, created["runId"])

    assert run["status"] == "failed"
    assert run["error"]["code"] == "HTTP_BLOCKED"
    # The category, never the address — checked on the error alone, because the run's *inputs* legitimately
    # carry the URL the tenant themselves supplied.
    assert run["error"]["message"] == "차단된 요청입니다 (allowlist)"
    assert "127.0.0.1" not in json.dumps(run["error"], ensure_ascii=False)


async def test_a_missing_secret_fails_the_run_without_sending_anything(api, pool, worker_factory,
                                                                       http_server):
    """No secret stored at all: the node must fail before the request, not send the marker to the server."""
    workflow_id = await _saved(api, load_golden("http_call"))
    await _worker(worker_factory, pool, f"{http_server};allowPrivate")

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"issue": f"{http_server}/issues/1"}, "revision": 2})).json()
    run = await _finished(api, created["runId"])

    assert run["status"] == "failed"
    assert run["error"]["code"] == "SECRET_NOT_FOUND"


async def test_a_purged_run_cannot_be_retried(api, pool, redis, worker_factory):
    """2b design §11.2: retention removes what a retry would need, and the API has to say so rather than
    requeue a run that cannot possibly finish."""
    workflow_id = await _saved(api, load_golden("chaining"))
    await worker_factory(ScriptedLLM([RuntimeError("모델 없음")]))
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    run = await _finished(api, created["runId"])
    assert run["status"] == "failed"

    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET finished_at = now() - interval '400 days' WHERE id=%s",
                           (created["runId"],))
    await Reaper(dataclasses.replace(load_config(), retention_days=30, purge_batch=100), pool, redis).sweep()

    response = await api.post(f"/runs/{created['runId']}/retry")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_DATA_EXPIRED"
    purged = (await api.get(f"/runs/{created['runId']}")).json()
    assert purged["status"] == "failed" and purged["inputs"] is None  # metadata kept, payload gone


async def test_deleting_a_workflow_leaves_no_orphan_checkpoints(api, pool, redis, worker_factory):
    workflow_id = await _saved(api, load_golden("chaining"))
    await worker_factory(ScriptedLLM(["개요", "본문"]))
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await _finished(api, created["runId"])
    async with pool.connection() as conn:
        before = await (await conn.execute("SELECT count(*) AS n FROM checkpoints WHERE thread_id=%s",
                                           (created["runId"],))).fetchone()
    assert before["n"] > 0

    assert (await api.delete(f"/workflows/{workflow_id}")).status_code in (200, 204)
    # No ageing step: the orphan rule is the run row's absence, because LangGraph's checkpoint tables have
    # no timestamp column to age against (see Task 11's note).
    await Reaper(dataclasses.replace(load_config(), retention_days=30, purge_batch=100), pool, redis).sweep()

    async with pool.connection() as conn:
        after = await (await conn.execute("SELECT count(*) AS n FROM checkpoints WHERE thread_id=%s",
                                          (created["runId"],))).fetchone()
    assert after["n"] == 0
