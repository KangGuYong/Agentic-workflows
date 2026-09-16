"""End-to-end scenarios: a real API plus a real worker, no internal shortcuts (Task 16).

These are the tests the design's completion criteria are written against.
"""
import asyncio
import json
import pathlib

import pytest

from engine.llm.scripted import ScriptedLLM
from tests.test_api_control import HITL, _wait_status
from tests.test_api_events import _collect, _ids
from tests.test_api_runs import _saved

GOLDEN = pathlib.Path(__file__).parent / "golden"


def _responder(model, messages, schema):
    """Answers any golden workflow: matches whatever the schema asks for, otherwise text.

    A classifier's schema asks for a `category` (one of a fixed enum); the evaluator's asks for a
    `score`/`feedback` pair, which must clear the golden workflow's ">= 8" gate or the loop keeps
    retrying (still correct, just slower and less deterministic to wait on). Anything else -- a plain
    LLM node -- has no schema at all and gets scripted text back.
    """
    if schema is None:
        return "생성된 내용"
    properties = schema.get("properties", {})
    if "category" in properties:
        options = properties["category"].get("enum")
        return {"category": options[0]} if options else {}
    return {name: (10 if spec.get("type") == "number" else "생성된 내용") for name, spec in properties.items()}


def _inputs_for(dsl: dict) -> dict:
    """Fill the start node's declared inputs with strings."""
    schema = next((node.get("config", {}).get("inputs") for node in dsl["nodes"] if node["type"] == "start"), None)
    properties = (schema or {}).get("properties", {})
    return {name: "값" for name in properties}


@pytest.mark.parametrize("name", ["chaining", "routing", "parallel", "evaluator_loop"])
async def test_a_golden_workflow_runs_through_the_api(api, worker_factory, name):
    dsl = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    workflow_id = await _saved(api, dsl=dsl)
    await worker_factory(ScriptedLLM(_responder))

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": _inputs_for(dsl), "revision": 2})).json()

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["outputs"]


async def test_a_dead_worker_is_recovered_and_finished_nodes_are_not_re_run(api, pool, worker_factory):
    workflow_id = await _saved(api)
    stuck = _Stuck()
    dying = await worker_factory(stuck, owner="dying", lease_sec=1, reaper_interval_sec=0.2)
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await asyncio.wait_for(stuck.started.wait(), 20)

    await dying.stop()  # the process dies mid-node; the lease is left behind
    await worker_factory(ScriptedLLM(["요약본"]), owner="healthy", lease_sec=30, reaper_interval_sec=0.2)

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["recoveryCount"] >= 1
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()["nodeRuns"]
    starts = [node for node in nodes if node["nodeId"] == "start"]
    assert len(starts) == 1  # the checkpointed node was not executed again
    assert [node["status"] for node in nodes if node["nodeId"] == "llm_1"][-1] == "succeeded"


async def test_an_approval_survives_a_worker_restart(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    first = await worker_factory(ScriptedLLM([]), owner="before")
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, created["runId"], "waiting")

    await first.stop()
    await api.post(f"/runs/{created['runId']}/resume",
                   json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"})
    await worker_factory(ScriptedLLM([]), owner="after")

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["outputs"] == {"final": "원고"}
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
    assert ids and ids == sorted(ids)


async def test_two_workers_share_the_queue_without_double_running(api, worker_factory):
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


class _Stuck:
    """Never returns: the worker holding this run has to be recovered."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def chat(self, **kwargs):
        self.started.set()
        await asyncio.sleep(3600)
