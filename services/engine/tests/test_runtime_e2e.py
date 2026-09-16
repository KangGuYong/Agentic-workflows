"""End-to-end scenarios: a real API plus a real worker, no internal shortcuts (Task 16).

These are the tests the design's completion criteria are written against.
"""
import asyncio
import json
import pathlib

import pytest

from engine.llm.scripted import ScriptedLLM
from tests.test_api_control import _wait_status
from tests.test_api_events import _collect, _ids
from tests.test_api_runs import _saved

GOLDEN = pathlib.Path(__file__).parent / "golden"

# Ordered (needle, text) pairs for `_responder`'s schema-less branch: each entry's needle is a literal
# fragment of one golden node's own prompt template (never of the "값" filler `_inputs_for` puts in it),
# so the text handed back identifies which node asked for it. Order matters where one node's prompt is a
# substring of another's own output embedded downstream -- "종합해줘" (llm_sum) must be checked before
# "장점"/"단점" (llm_pros/llm_cons), since llm_sum's own prompt quotes both of their answers back.
_PROMPT_TEXT = [
    ("다음 개요로 본문을 써줘", "본문_텍스트"),
    ("글의 개요를 써줘", "개요_텍스트"),
    ("종합해줘", "종합_텍스트"),
    ("장점", "장점_텍스트"),
    ("단점", "단점_텍스트"),
    ("결제 문의에 답해줘", "결제_답변"),
    ("기술 문의에 답해줘", "기술_답변"),
]


def _responder(model, messages, schema):
    """Answers a golden workflow with text or structured data that identifies which node asked for it, so
    a swapped branch, a wrong route or a replaced output shows up in what the run actually produced --
    not just that it produced something non-empty. A classifier's schema asks for a `category` (one of a
    fixed enum); anything else with a schema is the evaluator's `score`/`feedback` pair or similar and gets
    a generic fill (the evaluator_loop scenario below uses its own stateful responder instead, so the loop
    actually loops). A plain LLM node has no schema at all: it is matched against its own literal prompt
    text via `_PROMPT_TEXT`, since that template text -- not the "값" filler -- is what distinguishes one
    node's prompt from another's across the golden workflows.
    """
    if schema is None:
        prompt = messages[-1].content
        for needle, text in _PROMPT_TEXT:
            if needle in prompt:
                return text
        return "생성된 내용"  # a plain LLM node outside the golden set (e.g. the two-workers test's own DSL)
    properties = schema.get("properties", {})
    if "category" in properties:
        options = properties["category"].get("enum")
        return {"category": options[0], "reason": "이유"} if options else {"category": "default", "reason": "이유"}
    return {name: (10 if spec.get("type") == "number" else "생성된 내용") for name, spec in properties.items()}


def _inputs_for(dsl: dict) -> dict:
    """Fill the start node's declared inputs with strings."""
    schema = next((node.get("config", {}).get("inputs") for node in dsl["nodes"] if node["type"] == "start"), None)
    properties = (schema or {}).get("properties", {})
    return {name: "값" for name in properties}


_EXPECTED_OUTPUTS = {
    "chaining": {"result": "본문_텍스트"},
    "routing": {"category": "billing", "answer": "결제_답변"},
    "parallel": {
        "summary": "종합_텍스트",
        "branchOrder": json.dumps({"llm_cons": {"text": "단점_텍스트"}, "llm_pros": {"text": "장점_텍스트"}},
                                  ensure_ascii=False),
    },
}


@pytest.mark.parametrize("name", ["chaining", "routing", "parallel"])
async def test_a_golden_workflow_runs_through_the_api(api, worker_factory, name):
    dsl = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    workflow_id = await _saved(api, dsl=dsl)
    await worker_factory(ScriptedLLM(_responder))

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": _inputs_for(dsl), "revision": 2})).json()

    run = await _wait_status(api, created["runId"], "succeeded")
    # Exact expected outputs, not mere truthiness: replacing a run's outputs wholesale with junk, forcing
    # the classifier to always route "default", or crossing the parallel branches would each still leave
    # `run["outputs"]` a non-empty dict but would not match this map (Task 16 review, G2).
    assert run["outputs"] == _EXPECTED_OUTPUTS[name]


async def test_the_evaluator_loop_golden_workflow_actually_traverses_its_back_edge(api, worker_factory):
    """The generic `_responder` above always scores 10, which clears the loop's ">= 8" gate on the very
    first pass -- so with it, this parametrization was in practice a five-node linear graph that happened
    to contain a condition node, and its back edge (`maxIterations: 2`) was never taken; inverting the
    condition node's routing was invisible to the suite (Task 16 review, G3). Score low once, then high,
    with a per-test counter, so the loop is actually driven around its back edge before it exits normally.
    """
    dsl = json.loads((GOLDEN / "evaluator_loop.json").read_text(encoding="utf-8"))
    workflow_id = await _saved(api, dsl=dsl)
    calls = {"eval": 0}

    def responder(model, messages, schema):
        if schema is not None:  # llm_eval's score/feedback schema
            calls["eval"] += 1
            return {"score": 5, "feedback": "부족"} if calls["eval"] == 1 else {"score": 9, "feedback": "좋음"}
        return f"초안{calls['eval'] + 1}"  # llm_gen: distinguishes the first draft from the retried one

    await worker_factory(ScriptedLLM(responder))
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": _inputs_for(dsl), "revision": 2})).json()

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["outputs"] == {"text": "초안2", "score": 9}
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()["nodeRuns"]
    assert len([node for node in nodes if node["nodeId"] == "llm_gen"]) == 2  # the back edge ran once
    conditions = [node for node in nodes if node["nodeId"] == "condition_1"]
    assert conditions[0]["meta"]["handle"] == "false"  # the first pass failed the gate and looped back


async def test_a_dead_worker_is_recovered_and_finished_nodes_are_not_re_run(api, pool, worker_factory):
    workflow_id = await _saved(api)
    stuck = _Stuck()
    dying = await worker_factory(stuck, owner="dying", lease_sec=1, reaper_interval_sec=0.2)
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await asyncio.wait_for(stuck.started.wait(), 20)

    # This is a graceful shutdown, not a crash: `Worker._execute`'s own `CancelledError` handler hands the
    # lease back itself (`expire_lease`, under `asyncio.shield`) before `stop()` returns -- measured at
    # 0.047s after `stop()` returns, the lease is already expired. `lease_sec=1` is therefore dead
    # configuration here; this test exercises only the graceful-shutdown recovery path. See
    # `test_a_worker_that_crashes_before_handing_back_its_lease_is_recovered` below for the abrupt-death
    # path the reaper actually exists for (Task 16 review, G6).
    await dying.stop()
    await worker_factory(ScriptedLLM(["요약본"]), owner="healthy", lease_sec=30, reaper_interval_sec=0.2)

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["recoveryCount"] >= 1
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()["nodeRuns"]
    starts = [node for node in nodes if node["nodeId"] == "start"]
    assert len(starts) == 1  # the checkpointed node was not executed again
    assert [node["status"] for node in nodes if node["nodeId"] == "llm_1"][-1] == "succeeded"


async def test_a_worker_that_crashes_before_handing_back_its_lease_is_recovered(api, worker_factory, monkeypatch):
    """The abrupt-death counterpart to the graceful-shutdown recovery above. Asyncio's cancellation always
    resumes a cancelled task at its next await point and runs its `except CancelledError` handler -- there
    is no way, short of controlling the process or the event loop itself, to cancel a worker's task from
    outside and skip `_execute`'s own lease handoff the way a real `kill -9` or an OOM eviction would.
    The one thing an actual crash prevents that *is* reachable from here is that handoff's write landing:
    force it to fail -- exactly what a dead process leaves behind, a write that never reaches Postgres --
    and let the original lease expire on its own, unrenewed, the way `tests/test_worker_reaper.py`
    constructs the same row state directly against the reaper. Here it is produced by a real worker dying
    mid-node and recovered through the real API, the real worker pool and the real reaper (Task 16 review,
    G6).
    """
    import engine.worker.worker as worker_module

    real_expire_lease = worker_module.run_db.expire_lease

    async def never_lands(conn, *, run_id, owner):
        if owner == "dying":
            raise ConnectionError("simulated: the process is gone before this write reaches postgres")
        return await real_expire_lease(conn, run_id=run_id, owner=owner)

    monkeypatch.setattr(worker_module.run_db, "expire_lease", never_lands)

    workflow_id = await _saved(api)
    stuck = _Stuck()
    dying = await worker_factory(stuck, owner="dying", lease_sec=1, reaper_interval_sec=0.2)
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await asyncio.wait_for(stuck.started.wait(), 20)

    # _execute's CancelledError handler runs for real and tries to hand the lease back, but the write
    # above never lands -- the original lease (renewed by the heartbeat up to this instant) is left to
    # expire on its own, with nobody left to renew it.
    await dying.stop()
    await worker_factory(ScriptedLLM(["요약본"]), owner="healthy", lease_sec=30, reaper_interval_sec=0.2)

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["recoveryCount"] >= 1
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()["nodeRuns"]
    starts = [node for node in nodes if node["nodeId"] == "start"]
    assert len(starts) == 1  # the checkpointed node was not executed again
    assert [node["status"] for node in nodes if node["nodeId"] == "llm_1"][-1] == "succeeded"


@pytest.mark.parametrize(
    ("decision", "answer_extra", "expected"),
    [
        ("approve", {"editedValue": "수정 원고"}, {"final": "수정 원고", "decision": "approve", "note": ""}),
        ("reject", {"comment": "다시"}, {"final": "원고", "decision": "reject", "note": "반려되었습니다"}),
    ],
)
async def test_an_approval_survives_a_worker_restart(api, worker_factory, decision, answer_extra, expected):
    # The golden `hitl.json` (not the hand-written HITL DSL from test_api_control) so the API-run scenario
    # covers the same fifth golden workflow the design's completion criteria list (design 11), including
    # its `template_rejected` node -- and both decisions, so a `reject` answer runs through the worker at
    # least once (Task 16 review, G5).
    dsl = json.loads((GOLDEN / "hitl.json").read_text(encoding="utf-8"))
    workflow_id = await _saved(api, dsl=dsl)
    first = await worker_factory(ScriptedLLM([]), owner="before")
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, created["runId"], "waiting")

    await first.stop()
    await api.post(f"/runs/{created['runId']}/resume",
                   json={"nodeId": "human_approval_1", "execIndex": 1, "decision": decision, **answer_extra})
    await worker_factory(ScriptedLLM([]), owner="after")

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["outputs"] == expected
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()["nodeRuns"]
    approvals = [node for node in nodes if node["nodeId"] == "human_approval_1"]
    assert len(approvals) == 1  # the waiting attempt was reused, not re-opened


async def test_progress_is_visible_over_sse_while_the_run_is_in_flight(api, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"], delay=0.2))
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    events = await asyncio.wait_for(_collect(api, f"/runs/{created['runId']}/events"), 30)

    types = [event["data"]["type"] for event in events]
    assert types.count("node_started") == 3 and types[-1] == "run_succeeded"
    # `_ids` drops the id-less `node_token` events (Task 14's review): indexing event["id"] over the raw
    # list raises KeyError the first time a real run streams a token.
    ids = _ids(events)
    # The contiguous-range form (as `test_api_events.py` already uses), not `ids == sorted(ids)`: a sorted
    # check alone admits both a gap (dropping every `node_finished` still passes) and a duplicate
    # (duplicating every `node_finished` still passes), since a list with either shape can still be sorted
    # (Task 16 review, G4).
    assert ids and ids == list(range(ids[0], ids[0] + len(ids)))


async def test_two_workers_share_the_queue_without_double_running(api, pool, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(_responder), owner="w1")
    await worker_factory(ScriptedLLM(_responder), owner="w2")

    created = [
        (await api.post(f"/workflows/{workflow_id}/runs",
                        json={"inputs": {"topic": f"주제 {index}"}, "revision": 2})).json()["runId"]
        for index in range(4)
    ]

    for run_id in created:
        await _wait_status(api, run_id, "succeeded")
        nodes = (await api.get(f"/runs/{run_id}/nodes")).json()["nodeRuns"]
        assert len([node for node in nodes if node["nodeId"] == "llm_1"]) == 1
        # The one-row check above is enforced by the schema's own
        # `UNIQUE (run_id, node_id, exec_index, attempt)` plus `ON CONFLICT DO NOTHING` in the recorder --
        # not by the claim protocol -- so it cannot by itself catch two workers both starting the same run
        # (proven: with `FOR UPDATE SKIP LOCKED` removed from `claim_next`, runs recorded two `run_started`
        # events while still leaving exactly one `llm_1` row). Assert the claim protocol's own guarantee
        # directly instead (Task 16 review, G1).
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT count(*) AS n FROM run_events WHERE run_id=%s AND type='run_started'",
                (run_id,))).fetchone()
        assert row["n"] == 1


class _Stuck:
    """Never returns: the worker holding this run has to be recovered."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def chat(self, **kwargs):
        self.started.set()
        await asyncio.sleep(3600)
