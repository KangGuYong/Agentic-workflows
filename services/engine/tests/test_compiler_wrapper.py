import dataclasses

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from engine.compiler.state import build_state_type, initial_state, outputs_of
from engine.compiler.wrapper import NodePlan, _fallback_output, backoff_delay, make_node_fn
from engine.dsl.models import Edge, Node, Policy, RetrySpec
from engine.errors import EngineFault, ErrorCode, NodeError, NodeFailedError, RunCancelled
from engine.llm.scripted import ScriptedLLM
from engine.nodes.base import NodeResult, NodeSpec
from engine.nodes.classifier import ClassifierNode
from engine.nodes.condition import ConditionNode
from engine.nodes.llm import LLMNode
from engine.nodes.merge import MergeNode
from engine.nodes.template import TemplateNode
from engine.runtime.deps import RunDeps
from engine.runtime.guard import FlagGuard
from engine.runtime.recorder import DuplicateAttempt, InMemoryRecorder

LLM_CONFIG = {"model": "m", "prompt": "{{start.topic}}"}


def _plan(spec, raw_config, *, policy=None, handle_edges=None, back=frozenset(), node_id="n") -> NodePlan:
    config = spec.parse_config(raw_config)
    return NodePlan(
        node=Node(id=node_id, type=spec.type, config=raw_config),
        spec=spec,
        config=config,
        policy=policy,
        pred_ids=(),
        handle_edges=handle_edges or {handle: [] for handle in spec.handles(config)},
        back_edge_ids=back,
    )


def _deps(llm=None, guard=None) -> tuple[RunDeps, InMemoryRecorder, list[float]]:
    recorder = InMemoryRecorder()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    deps = RunDeps(run_id="r1", llm=llm or ScriptedLLM([]), recorder=recorder, sleep=fake_sleep)
    if guard is not None:
        deps.guard = guard
    return deps, recorder, sleeps


async def _run(plan: NodePlan, deps: RunDeps, *, start=None, loop_counters=None, outputs=None) -> dict:
    node_ids = {"start", "n", "a", "b", plan.node.id}
    graph = StateGraph(build_state_type(node_ids), context_schema=RunDeps)
    graph.add_node(plan.node.id, make_node_fn(plan))
    graph.add_edge(START, plan.node.id)
    graph.add_edge(plan.node.id, END)
    app = graph.compile(checkpointer=InMemorySaver())
    state = initial_state({})
    state["out_start"] = start or {"topic": "AI"}
    for node_id, value in (outputs or {}).items():
        state[f"out_{node_id}"] = value
    state["loop_counters"] = loop_counters or {}
    return await app.ainvoke(state, {"configurable": {"thread_id": "t"}}, context=deps)


def test_backoff_delay():
    assert backoff_delay(RetrySpec(backoff="fixed", initialDelaySec=3), 4) == 3
    assert backoff_delay(RetrySpec(initialDelaySec=2), 1) == 2
    assert backoff_delay(RetrySpec(initialDelaySec=2), 3) == 8
    assert backoff_delay(RetrySpec(initialDelaySec=30), 5) == 60


async def test_success_writes_state_and_records_rendered_input():
    deps, recorder, _ = _deps(ScriptedLLM(["답"]))
    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)
    assert outputs_of(result)["n"] == {"text": "답"}
    assert result["exec_counts"] == {"n": 1}
    assert recorder.records[0].status == "succeeded"
    assert recorder.records[0].input == {"prompt": "AI"}


async def test_retryable_error_is_retried_with_backoff():
    llm = ScriptedLLM([NodeError(ErrorCode.LLM_UNAVAILABLE, "down", retryable=True), "답"])
    deps, recorder, sleeps = _deps(llm)
    policy = Policy(retry=RetrySpec(maxAttempts=3, initialDelaySec=1.5))

    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=policy), deps)

    assert outputs_of(result)["n"] == {"text": "답"}
    assert [(r.attempt, r.status) for r in recorder.records] == [(1, "failed"), (2, "succeeded")]
    assert sleeps == [1.5]
    assert [e["willRetry"] for e in recorder.events if e["type"] == "node_failed"] == [True]


async def test_exhausted_retries_fail_the_node():
    down = NodeError(ErrorCode.LLM_UNAVAILABLE, "down", retryable=True)
    deps, recorder, sleeps = _deps(ScriptedLLM([down, down, down]))

    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)

    assert exc.value.error.code == ErrorCode.LLM_UNAVAILABLE
    assert [r.status for r in recorder.records] == ["failed", "failed", "failed"]
    assert sleeps == [2.0, 4.0]


async def test_non_retryable_error_is_not_retried():
    deps, recorder, _ = _deps(ScriptedLLM([ValueError("bad")]))
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)
    assert exc.value.error.code == ErrorCode.NODE_FAILED
    assert len(recorder.records) == 1


async def test_on_error_default_uses_default_output():
    deps, recorder, _ = _deps(ScriptedLLM([ValueError("bad")]))
    policy = Policy(retry=RetrySpec(maxAttempts=1), onError="default", defaultOutput={"text": "기본"})

    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=policy), deps)

    assert outputs_of(result)["n"] == {"text": "기본"}
    assert recorder.records[-1].status == "defaulted"


async def test_timeout():
    deps, _, _ = _deps(ScriptedLLM(["늦음"], delay=5))
    policy = Policy(timeoutSec=1, retry=RetrySpec(maxAttempts=1))
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=policy), deps)
    assert exc.value.error.code == ErrorCode.NODE_TIMEOUT


async def test_template_error_is_recorded_as_failed_attempt():
    deps, recorder, _ = _deps()
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(TemplateNode(), {"template": "{{start.nope}}"}), deps)
    assert exc.value.error.code == ErrorCode.TEMPLATE_ERROR
    assert (recorder.records[0].status, recorder.records[0].input) == ("failed", None)


async def test_output_too_large():
    # each branch fits the renderer's 1,000,000-char cap, but the merged output does not fit 1 MB
    deps, _, _ = _deps()
    plan = dataclasses.replace(_plan(MergeNode(), {}), pred_ids=("a", "b"))
    with pytest.raises(NodeFailedError) as exc:
        await _run(plan, deps, start={"topic": "AI"}, outputs={"a": {"text": "x" * 600_000}, "b": {"text": "y" * 600_000}})
    assert exc.value.error.code == ErrorCode.OUTPUT_TOO_LARGE


async def test_branch_node_writes_route_and_loop_counter():
    edges = {
        "true": [Edge(id="exit", source="n", sourceHandle="true", target="end")],
        "false": [Edge(id="back", source="n", sourceHandle="false", target="gen", maxIterations=2)],
    }
    config = {"conditions": [{"left": "{{start.n}}", "op": ">=", "right": "3"}]}
    plan = _plan(ConditionNode(), config, handle_edges=edges, back=frozenset({"back"}))

    deps, recorder, _ = _deps()
    result = await _run(plan, deps, start={"n": 1})
    assert result["routes"] == {"n": ["gen"]}
    assert result["loop_counters"] == {"back": 1}
    assert recorder.records[0].meta == {"handle": "false", "loopExhausted": False}

    deps, recorder, _ = _deps()
    result = await _run(plan, deps, start={"n": 1}, loop_counters={"back": 2})
    assert result["routes"] == {"n": ["end"]}
    assert recorder.records[0].meta == {"handle": "true", "loopExhausted": True}


async def test_cancelled_guard_stops_before_execution():
    guard = FlagGuard()
    guard.cancel()
    deps, recorder, _ = _deps(ScriptedLLM(["답"]), guard=guard)
    with pytest.raises(RunCancelled):
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)
    assert recorder.records == []


class _EchoConfig(BaseModel):
    pass


class _Echo(NodeSpec):
    """Returns a fixed output, to exercise the wrapper's output checks."""

    type = "echo"
    label = "echo"
    category = "Test"
    Config = _EchoConfig

    def __init__(self, output: dict) -> None:
        self.output = output

    def output_schema(self, config, pred_schemas):
        return {"type": "object"}

    async def execute(self, ctx, config, rendered):
        return NodeResult(self.output)


@pytest.mark.parametrize(
    "output",
    [{"x": float("nan")}, {"x": {1, 2}}, {"x": "a\x00b"}, {"x": "\ud800"}],
    ids=["nan", "set", "nul", "surrogate"],
)
async def test_output_that_cannot_be_stored_fails_the_node(output):
    deps, recorder, _ = _deps()
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(_Echo(output), {}), deps)
    assert exc.value.error.code == ErrorCode.NODE_FAILED
    assert recorder.records[0].status == "failed"


async def test_rendered_text_that_cannot_be_stored_is_a_template_error():
    deps, recorder, _ = _deps()
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(TemplateNode(), {"template": "{{start.s}}"}), deps, start={"s": "a\x00b"})
    assert exc.value.error.code == ErrorCode.TEMPLATE_ERROR
    assert (recorder.records[0].status, recorder.records[0].input) == ("failed", None)


def test_default_output_is_a_fresh_copy_for_every_use():
    policy = Policy(onError="default", defaultOutput={"text": "기본", "tags": ["a"]})
    plan = _plan(LLMNode(), LLM_CONFIG, policy=policy)
    first = _fallback_output(plan)
    first["tags"].append("b")
    assert _fallback_output(plan) == {"text": "기본", "tags": ["a"]}
    assert policy.defaultOutput == {"text": "기본", "tags": ["a"]}


async def test_default_output_is_checked_before_use():
    deps, _, _ = _deps(ScriptedLLM([ValueError("bad")]))
    policy = Policy(retry=RetrySpec(maxAttempts=1), onError="default", defaultOutput={"text": "t" * 1_100_000})
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=policy), deps)
    assert exc.value.error.code == ErrorCode.OUTPUT_TOO_LARGE


async def test_replay_of_a_waited_execution_retries_with_a_fresh_attempt_number():
    llm = ScriptedLLM([NodeError(ErrorCode.LLM_UNAVAILABLE, "down", retryable=True), "답"])
    deps, recorder, _ = _deps(llm)
    # the log of an earlier worker: attempt 1 waited and failed, attempt 2 was opened as well
    await recorder.node_started("n", 1, 1, None)
    await recorder.node_waiting("n", 1, 1, {"message": "m"})
    await recorder.node_failed("n", 1, 1, {"code": "NODE_FAILED", "message": "x"}, will_retry=True)
    await recorder.node_started("n", 1, 2, None)

    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)

    assert outputs_of(result)["n"] == {"text": "답"}
    assert [(r.attempt, r.status) for r in recorder.records] == [(1, "failed"), (2, "running"), (3, "succeeded")]
    assert [e["type"] for e in recorder.events].count("node_waiting") == 1


async def test_a_duplicate_attempt_stops_the_node_like_a_lost_lease():
    deps, recorder, _ = _deps(ScriptedLLM(["답"]))
    await recorder.node_started("n", 1, 1, None)  # another worker opened the same attempt meanwhile

    async def stale_count(node_id: str, exec_index: int) -> int:
        return 0

    recorder.attempts_so_far = stale_count
    with pytest.raises(DuplicateAttempt):
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)


class _FlakyRecorder(InMemoryRecorder):
    def __init__(self, broken: str) -> None:
        super().__init__()
        self.broken = broken

    async def node_token(self, node_id, exec_index, text):
        if self.broken == "node_token":
            raise ConnectionError("redis publish failed")
        await super().node_token(node_id, exec_index, text)

    async def find_waiting(self, node_id, exec_index):
        if self.broken == "find_waiting":
            raise ConnectionError("db connection reset")
        return await super().find_waiting(node_id, exec_index)

    async def node_started(self, node_id, exec_index, attempt, input):
        if self.broken == "node_started":
            raise ConnectionError("db connection reset")
        await super().node_started(node_id, exec_index, attempt, input)


class _Streamer(_Echo):
    async def execute(self, ctx, config, rendered):
        for piece in ("가", "나", "다", "라"):
            await ctx.on_token(piece)
        return NodeResult(self.output)


async def test_a_failed_token_publish_does_not_fail_the_node(caplog):
    deps, _, _ = _deps()
    deps.recorder = _FlakyRecorder("node_token")
    result = await _run(_plan(_Streamer({"text": "가나다라"}), {}), deps)
    assert outputs_of(result)["n"] == {"text": "가나다라"}
    assert len([r for r in caplog.records if "node_token failed" in r.getMessage()]) == 1


@pytest.mark.parametrize("broken", ["node_started", "find_waiting"])
async def test_a_recorder_failure_escapes_as_an_engine_fault_not_a_node_error(broken):
    deps, _, _ = _deps(ScriptedLLM(["답"]))
    deps.recorder = _FlakyRecorder(broken)
    with pytest.raises(EngineFault):
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)
    assert deps.recorder.events == []


async def test_a_timeout_is_retried_and_can_succeed():
    deps, recorder, _ = _deps(ScriptedLLM([TimeoutError(), "답"]))
    policy = Policy(timeoutSec=5, retry=RetrySpec(maxAttempts=2, initialDelaySec=0.5))
    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=policy), deps)
    assert outputs_of(result)["n"] == {"text": "답"}
    assert [(r.attempt, r.status, (r.error or {}).get("code")) for r in recorder.records] == [
        (1, "failed", "NODE_TIMEOUT"), (2, "succeeded", None)]


async def test_classifier_on_error_default_routes_through_its_default_handle():
    edges = {
        "a": [Edge(id="ea", source="n", sourceHandle="a", target="llm_a")],
        "default": [Edge(id="ed", source="n", sourceHandle="default", target="end")],
    }
    config = {"model": "m", "input": "{{start.topic}}", "categories": [{"id": "a", "description": "A"}]}
    policy = Policy(retry=RetrySpec(maxAttempts=1), onError="default")
    deps, recorder, _ = _deps(ScriptedLLM([ValueError("bad")]))

    result = await _run(_plan(ClassifierNode(), config, policy=policy, handle_edges=edges), deps)

    assert result["routes"] == {"n": ["end"]}
    assert (recorder.records[-1].status, recorder.records[-1].meta["handle"]) == ("defaulted", "default")


async def test_rendering_goes_through_the_deps_hook_when_one_is_set():
    seen: list[dict] = []

    async def render(fields, outputs):
        seen.append({field.path: field.source for field in fields})
        return {field.path: "치환됨" for field in fields}

    deps, _, _ = _deps(ScriptedLLM(["답"]))
    deps.render = render

    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)

    assert seen and outputs_of(result)["n"] == {"text": "답"}
    assert deps.llm.calls[0]["messages"][-1].content == "치환됨"


async def test_a_render_deadline_fails_the_node_without_retrying():
    async def render(fields, outputs):
        raise TimeoutError("render deadline")

    deps, recorder, _ = _deps(ScriptedLLM(["답"]))
    deps.render = render
    plan = _plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy)

    with pytest.raises(NodeFailedError) as exc:
        await _run(plan, deps)

    assert exc.value.error.code == ErrorCode.TEMPLATE_ERROR
    assert len(recorder.records) == 1  # a deadline is not retryable: it would just happen again
