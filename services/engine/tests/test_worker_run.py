import asyncio
import dataclasses
import json
import logging
import uuid

from engine.config import load_config
from engine.db import runs as run_db
from engine.events.publish import run_channel
from engine.llm.scripted import ScriptedLLM
from engine.worker.worker import Worker
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

# A second, differently-shaped workflow: exercises the compile cache with a different dsl_hash and a
# different end-node output key, so a wrong cache hit is visible in the outputs, not just internally.
CHAIN_B = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}},
                               "required": ["topic"]}}},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "{{ start.topic }} 번역"}},
        {"id": "end", "type": "end", "config": {"outputs": {"translated": "{{ llm_1.text }}"}}},
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


def _make_worker(pool, redis, llm, *, owner: str = "worker-1", **overrides) -> Worker:
    """A Worker not wired to the fixture, for tests that call `_execute` directly or need to inspect
    private state. Callers that `start()` one of these are responsible for `stop()`ing it too."""
    config = dataclasses.replace(load_config(), claim_poll_sec=0.2, heartbeat_sec=0.2, **overrides)
    return Worker(config, pool, redis, owner=owner, llm=llm)


async def _status(pool, run_id: str):
    async def check():
        async with pool.connection() as conn:
            row = await run_db.get_run(conn, run_id)
        # inputs/outputs are bytea (2b design §9); decode as every production reader does, so these
        # tests keep asserting on values rather than on ciphertext.
        row = run_db.decode_run(row, load_config().secret_key)
        return row if row["status"] in ("succeeded", "failed", "cancelled", "waiting") else None

    return await until(check)


async def _events(pool, run_id: str) -> list[dict]:
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT type FROM run_events WHERE run_id=%s ORDER BY seq", (run_id,))).fetchall()
    return rows


async def test_a_queued_run_is_picked_up_and_finished(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    await worker_factory(ScriptedLLM(["요약본"]))

    row = await _status(pool, run_id)

    assert (row["status"], row["outputs"]) == ("succeeded", {"result": "요약본"})
    assert row["lease_owner"] is None and row["finished_at"] is not None
    types = [e["type"] for e in await _events(pool, run_id)]
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


# B1: fencing on the terminal write. The row is claimed as worker-1, then hijacked to worker-2 before
# worker-1 is given a chance to finish it; worker-1 must write nothing to the run row or the event log.
async def test_lease_fencing_drops_a_terminal_write_from_the_old_owner(pool, redis):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    async with pool.connection() as conn:
        row = await run_db.claim_next(conn, owner="worker-1", lease_sec=30)
        await conn.execute("UPDATE runs SET lease_owner='worker-2' WHERE id=%s", (run_id,))

    worker = _make_worker(pool, redis, ScriptedLLM(["요약본"]), owner="worker-1")
    await worker._execute(row)  # awaited directly: no claim loop involved, no race to arrange

    async with pool.connection() as conn:
        final = await run_db.get_run(conn, run_id)
    types = {e["type"] for e in await _events(pool, run_id)}

    assert (final["lease_owner"], final["status"], final["finished_at"]) == ("worker-2", "running", None)
    assert "run_succeeded" not in types


# B2: two `_execute` calls racing on the same claimed row must let only one of them write a node's attempt
# — the Postgres unique key on node_runs is what two genuinely colliding workers actually collide on. The
# loser must be treated as a lost lease, not an infrastructure failure: no terminal write, no expired
# lease, no error-level log. `attempts_so_far` is forced to 0 so both calls always compute the *same*
# attempt number for whatever node they are on, which turns the race into a guaranteed collision instead
# of a timing-dependent one (the real race is a narrow read-then-insert gap that a plain asyncio.gather
# does not reliably land in).
async def test_a_duplicate_attempt_is_treated_as_a_lost_lease(pool, redis, caplog, monkeypatch):
    from engine.events.recorder import PostgresRecorder

    async def always_first_attempt(self, node_id, exec_index):
        return 0

    monkeypatch.setattr(PostgresRecorder, "attempts_so_far", always_first_attempt)

    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    async with pool.connection() as conn:
        row = await run_db.claim_next(conn, owner="worker-1", lease_sec=30)

    worker = _make_worker(pool, redis, ScriptedLLM(["요약본", "요약본"]), owner="worker-1")
    with caplog.at_level(logging.INFO, logger="engine.worker.worker"):
        await asyncio.gather(worker._execute(dict(row)), worker._execute(dict(row)))

    async with pool.connection() as conn:
        node_rows = await (await conn.execute(
            "SELECT node_id, exec_index, attempt FROM node_runs WHERE run_id=%s", (run_id,))).fetchall()
    keys = [(r["node_id"], r["exec_index"], r["attempt"]) for r in node_rows]
    assert len(keys) == len(set(keys))  # no attempt was opened twice

    terminal_types = {"run_succeeded", "run_failed", "run_cancelled"}
    types = [e["type"] for e in await _events(pool, run_id)]
    assert sum(1 for t in types if t in terminal_types) == 1

    # the loser must be recognized as a plain lost race (an info-level LeaseLost), not logged and handled
    # as an infrastructure failure — dropping the `except LeaseLost` branch changes exactly this.
    assert any("lease lost" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


# B3: the reaper (not the worker) owns `run_recovered` — it is the event for the act of recovering, done
# once by the reaper when it requeues an abandoned run. A worker claiming that requeued run still writes a
# plain `run_started`, carrying how many times the run has been recovered so far, for every attempt to run.
async def test_a_recovered_run_still_starts_with_run_started(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET recovery_count=1 WHERE id=%s", (run_id,))
    await worker_factory(ScriptedLLM(["요약본"]))

    await _status(pool, run_id)
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT type, payload FROM run_events WHERE run_id=%s ORDER BY seq", (run_id,))).fetchall()
    assert rows[0]["type"] == "run_started"
    assert rows[0]["payload"] == {"recoveryCount": 1}
    assert "run_recovered" not in [row["type"] for row in rows]  # the worker itself never writes this


# B4: one worker runs two differently-shaped workflows; the compiled-workflow cache must be keyed by the
# actual dsl_hash, or the second run would silently execute the first run's graph.
async def test_two_different_workflows_share_a_worker_without_cache_collision(pool, worker_factory):
    run_a = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    run_b = await make_run(pool, dsl=CHAIN_B, status="queued", inputs={"topic": "AI"})

    def responder(model, messages, schema):
        return "요약본" if "요약" in messages[-1].content else "번역본"

    await worker_factory(ScriptedLLM(responder))

    row_a = await _status(pool, run_a)
    row_b = await _status(pool, run_b)

    assert row_a["outputs"] == {"result": "요약본"}
    assert row_b["outputs"] == {"translated": "번역본"}


# B5: events must actually reach Redis under the run's real id (a raw uuid.UUID key breaks every publish).
async def test_events_are_published_on_the_run_channel(pool, redis, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    pubsub = redis.pubsub()
    await pubsub.subscribe(run_channel(run_id))
    await worker_factory(ScriptedLLM(["요약본"]))

    seen: list[str] = []
    async with asyncio.timeout(15):
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            seen.append(json.loads(message["data"])["type"])
            if seen[-1] == "run_succeeded":
                break
    await pubsub.aclose()

    assert seen[0] == "run_started"
    assert seen[-1] == "run_succeeded"


# B6: store_run_data=False must clear `inputs` once the run reaches a terminal state.
async def test_store_run_data_false_clears_inputs_on_finish(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"}, store_run_data=False)
    await worker_factory(ScriptedLLM(["요약본"]))

    row = await _status(pool, run_id)
    assert row["status"] == "succeeded"
    assert row["inputs"] is None


# B7: a stored DSL that fails validation must fail the run cleanly (not raise past the worker), the
# run_started event must still precede run_failed, and the run's FlagGuard must not leak.
async def test_invalid_stored_dsl_fails_the_run_and_clears_the_guard(pool, worker_factory):
    broken = {**CHAIN, "edges": CHAIN["edges"][:1]}  # the "end" node becomes unreachable
    run_id = await make_run(pool, dsl=broken, status="queued")
    worker = await worker_factory(ScriptedLLM([]))

    row = await _status(pool, run_id)
    assert row["status"] == "failed"
    assert row["error"]["code"] == "NODE_FAILED"
    types = [e["type"] for e in await _events(pool, run_id)]
    assert types == ["run_started", "run_failed"]
    assert worker._guards == {}


# B8: a resume_payload that execute_run rejects (here: the run never started) must not strand the run in
# `running` with the lease held — it goes back to `waiting` with the lease released and the payload gone.
async def test_stale_resume_payload_is_rejected_back_to_waiting(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET resume_payload=%s WHERE id=%s",
            (json.dumps({"nodeId": "human_approval_1", "execIndex": 1, "response": {"decision": "approve"}}),
             run_id),
        )
    await worker_factory(ScriptedLLM(["요약본"]))

    row = await _status(pool, run_id)
    assert row["status"] == "waiting"
    assert row["lease_owner"] is None
    assert row["resume_payload"] is None


# B9: an infrastructure failure inside execute_run (not a NodeError) must expire the lease so recovery can
# retry the run, and must leave the run row in `running` rather than crashing the worker task.
async def test_infrastructure_error_expires_the_lease_for_recovery(pool, worker_factory, monkeypatch):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})

    async def broken(*args, **kwargs):
        raise RuntimeError("infra boom")

    monkeypatch.setattr("engine.worker.worker.execute_run", broken)
    await worker_factory(ScriptedLLM(["요약본"]))

    async def check():
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT status, lease_expires_at <= now() AS expired FROM runs WHERE id=%s", (run_id,)
            )).fetchone()
        return row if row["expired"] else None

    row = await until(check)
    assert row["status"] == "running"


# B10: the workflow version disappearing between queueing and claiming must fail the run, not kill the
# worker task. FK constraints forbid literally deleting a version a run still references, and (unlike the
# textbook "skip the check unless the referencing column changed" optimization) Postgres here re-validates
# the FK on every later write to the row, not just the one that sets it — so the trigger has to stay off for
# every write this run receives, not just the one that points it at the bogus id.
async def test_missing_version_fails_the_run_instead_of_crashing_the_worker(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    bogus_version_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute("ALTER TABLE runs DISABLE TRIGGER ALL")
        try:
            await conn.execute("UPDATE runs SET workflow_version_id=%s WHERE id=%s", (bogus_version_id, run_id))
            await worker_factory(ScriptedLLM([]))
            row = await _status(pool, run_id)
        finally:
            await conn.execute("ALTER TABLE runs ENABLE TRIGGER ALL")

    assert row["status"] == "failed"
    assert row["error"]["code"] == "NODE_FAILED"
