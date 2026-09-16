import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from engine.compiler.build import WorkflowInvalid, compile_workflow
from engine.dsl.models import WorkflowDSL, dsl_hash
from engine.errors import EngineFault, LeaseLost
from engine.jsondata import check_text
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.guard import FlagGuard
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import ResumeRejected, execute_run
from engine.validator import validate

DSL = {
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "{{start.topic}}"}},
        {"id": "end", "type": "end", "config": {"outputs": {"result": "{{llm_1.text}}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"}, {"id": "e2", "source": "llm_1", "target": "end"}],
}


def test_invalid_workflow_is_rejected():
    broken = {**DSL, "edges": DSL["edges"][:1]}
    with pytest.raises(WorkflowInvalid) as exc:
        compile_workflow(broken, checkpointer=InMemorySaver())
    assert {issue.code for issue in exc.value.issues} >= {"HANDLE_NOT_CONNECTED"}


def test_compiled_metadata():
    compiled = compile_workflow(DSL, checkpointer=InMemorySaver())
    assert compiled.dsl_hash == dsl_hash(WorkflowDSL.model_validate(DSL))
    assert compiled.recursion_limit == 3 * (1 + 0) + 10


async def test_runs_to_completion_and_is_idempotent_when_called_again():
    compiled = compile_workflow(DSL, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-1", llm=ScriptedLLM(["결과"]), recorder=InMemoryRecorder())

    outcome = await execute_run(compiled, deps=deps, inputs={"topic": "AI"})
    again = await execute_run(compiled, deps=deps)

    assert (outcome.status, outcome.outputs) == ("succeeded", {"result": "결과"})
    assert (again.status, again.outputs) == ("succeeded", {"result": "결과"})
    assert len(deps.recorder.for_node("llm_1")) == 1


async def test_start_input_validation_fails_the_run():
    compiled = compile_workflow(DSL, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-2", llm=ScriptedLLM([]), recorder=InMemoryRecorder())
    outcome = await execute_run(compiled, deps=deps, inputs={})
    assert outcome.status == "failed"
    assert (outcome.error["code"], outcome.error["nodeId"]) == ("TYPE_MISMATCH", "start")


async def test_cancel_ends_the_run_but_a_lost_lease_is_raised_to_the_worker():
    compiled = compile_workflow(DSL, checkpointer=InMemorySaver())
    cancelled_guard, lost_guard = FlagGuard(), FlagGuard()
    cancelled_guard.cancel()
    lost_guard.lose_lease()

    cancelled = await execute_run(
        compiled,
        deps=RunDeps(run_id="run-3", llm=ScriptedLLM([]), recorder=InMemoryRecorder(), guard=cancelled_guard),
        inputs={"topic": "AI"},
    )
    assert cancelled.status == "cancelled"
    with pytest.raises(LeaseLost):
        await execute_run(
            compiled,
            deps=RunDeps(run_id="run-4", llm=ScriptedLLM([]), recorder=InMemoryRecorder(), guard=lost_guard),
            inputs={"topic": "AI"},
        )


HITL = {
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"draft": {"type": "string"}}, "required": ["draft"]}}},
        {"id": "human_approval_1", "type": "human_approval",
         "config": {"message": "검토해 주세요", "review": "{{start.draft}}", "allowEdit": True}},
        {"id": "end", "type": "end", "config": {"outputs": {"final": "{{human_approval_1.editedValue}}"}}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "human_approval_1"},
        {"id": "e2", "source": "human_approval_1", "sourceHandle": "approve", "target": "end"},
        {"id": "e3", "source": "human_approval_1", "sourceHandle": "reject", "target": "end"},
    ],
}


async def test_a_bad_resume_answer_is_rejected_before_it_reaches_the_graph():
    compiled = compile_workflow(HITL, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-5", llm=ScriptedLLM([]), recorder=InMemoryRecorder())
    first = await execute_run(compiled, deps=deps, inputs={"draft": "원고"})
    assert first.status == "waiting"

    target = {"nodeId": "human_approval_1", "execIndex": 1}
    for bad in ({**target, "decision": "maybe"}, {**target, "decision": "approve", "editedValue": 3},
                {"decision": "approve"}, {"nodeId": "human_approval_1", "execIndex": True, "decision": "approve"}):
        with pytest.raises(ResumeRejected):
            await execute_run(compiled, deps=deps, resume=bad)

    fixed = await execute_run(compiled, deps=deps, resume={**target, "decision": "approve", "editedValue": "수정본"})
    assert (fixed.status, fixed.outputs) == ("succeeded", {"final": "수정본"})


async def test_resume_on_a_run_that_never_started_is_rejected():
    compiled = compile_workflow(HITL, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-6", llm=ScriptedLLM([]), recorder=InMemoryRecorder())
    with pytest.raises(ResumeRejected):
        await execute_run(compiled, deps=deps, resume={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"})


class _CrashOnce(InMemoryRecorder):
    """Fails one node_succeeded for `node_id`, like a worker dying after the node ran."""

    def __init__(self, node_id: str) -> None:
        super().__init__()
        self.crash_node = node_id

    async def node_succeeded(self, node_id, exec_index, attempt, output, usage, *, defaulted, meta):
        if node_id == self.crash_node:
            self.crash_node = None
            raise ConnectionError("worker died")
        await super().node_succeeded(node_id, exec_index, attempt, output, usage, defaulted=defaulted, meta=meta)


async def test_a_replayed_answer_after_a_crash_continues_the_run():
    compiled = compile_workflow(HITL, checkpointer=InMemorySaver())
    recorder = _CrashOnce("end")
    deps = RunDeps(run_id="run-7", llm=ScriptedLLM([]), recorder=recorder)
    answer = {"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"}
    await execute_run(compiled, deps=deps, inputs={"draft": "원고"})

    with pytest.raises(EngineFault):  # the approval was checkpointed, then the worker died in `end`
        await execute_run(compiled, deps=deps, resume=answer)
    recovered = await execute_run(compiled, deps=deps, resume=answer)  # the stored answer is sent again

    assert (recovered.status, recovered.outputs) == ("succeeded", {"final": "원고"})


LOOPED_APPROVAL = {
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "human_approval_1", "type": "human_approval", "config": {"message": "승인할까요?"}},
        {"id": "condition_1", "type": "condition",
         "config": {"conditions": [{"left": "{{ human_approval_1.decision }}", "op": "==", "right": "approve"}]}},
        {"id": "end", "type": "end", "config": {"outputs": {"decision": "{{ human_approval_1.decision }}"}}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "human_approval_1"},
        {"id": "e2", "source": "human_approval_1", "sourceHandle": "approve", "target": "condition_1"},
        {"id": "e3", "source": "human_approval_1", "sourceHandle": "reject", "target": "condition_1"},
        {"id": "e4", "source": "condition_1", "sourceHandle": "true", "target": "end"},
        {"id": "back", "source": "condition_1", "sourceHandle": "false", "target": "human_approval_1",
         "maxIterations": 3},
    ],
}


async def test_a_replayed_answer_never_answers_the_next_approval():
    compiled = compile_workflow(LOOPED_APPROVAL, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-8", llm=ScriptedLLM([]), recorder=InMemoryRecorder())
    first = await execute_run(compiled, deps=deps, inputs={})
    reject = {"nodeId": "human_approval_1", "execIndex": 1, "decision": "reject"}

    second = await execute_run(compiled, deps=deps, resume=reject)
    replayed = await execute_run(compiled, deps=deps, resume=reject)  # stored answer sent again after a crash

    assert (first.waiting["execIndex"], second.waiting["execIndex"]) == (1, 2)
    assert (replayed.status, replayed.waiting["execIndex"]) == ("waiting", 2)
    assert [(r.exec_index, r.status) for r in deps.recorder.for_node("human_approval_1")] == [
        (1, "succeeded"), (2, "waiting")]


def _nested(depth: int) -> list:
    value: list = []
    for _ in range(depth):
        value = [value]
    return value


def _single(node: dict) -> dict:
    return {
        "nodes": [{"id": "start", "type": "start"}, node, {"id": "end", "type": "end"}],
        "edges": [{"id": "e1", "source": "start", "target": node["id"]}, {"id": "e2", "source": node["id"], "target": "end"}],
    }


DEEP_TEMPLATE = _single({"id": "template_1", "type": "template", "config": {"template": json.dumps(_nested(300)), "format": "json"}})
DEEP_LLM = _single({"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "p", "outputSchema": {"type": "object"}}})


@pytest.mark.parametrize(
    ("dsl", "llm", "code", "node_id"),
    [(DEEP_TEMPLATE, ScriptedLLM([]), "TEMPLATE_ERROR", "template_1"),
     (DEEP_LLM, ScriptedLLM([{"deep": _nested(300)}]), "NODE_FAILED", "llm_1")],
    ids=["template", "llm-output"],
)
async def test_a_value_nested_too_deeply_for_run_state_fails_the_node(dsl, llm, code, node_id):
    assert validate(dsl) == []
    compiled = compile_workflow(dsl, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-deep", llm=llm, recorder=InMemoryRecorder())
    outcome = await execute_run(compiled, deps=deps, inputs={})
    assert (outcome.status, outcome.error["code"], outcome.error["nodeId"]) == ("failed", code, node_id)
    assert "중첩" in outcome.error["message"]


@pytest.mark.parametrize("inputs", [{"x": _nested(300)}, {"x": "a\x00"}], ids=["deep", "nul"])
async def test_inputs_that_run_state_cannot_keep_fail_the_run_at_start(inputs):
    compiled = compile_workflow(_single({"id": "template_1", "type": "template", "config": {"template": "t"}}),
                                checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-inputs", llm=ScriptedLLM([]), recorder=InMemoryRecorder())
    outcome = await execute_run(compiled, deps=deps, inputs=inputs)
    assert (outcome.status, outcome.error["code"], outcome.error["nodeId"]) == ("failed", "NODE_FAILED", "start")
    assert deps.recorder.records == []


async def test_an_edited_value_nested_too_deeply_is_rejected_and_the_run_keeps_waiting():
    compiled = compile_workflow(HITL, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-edit", llm=ScriptedLLM([]), recorder=InMemoryRecorder())
    await execute_run(compiled, deps=deps, inputs={"draft": "원고"})
    target = {"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"}

    with pytest.raises(ResumeRejected, match="중첩"):
        await execute_run(compiled, deps=deps, resume={**target, "editedValue": _nested(300)})
    outcome = await execute_run(compiled, deps=deps, resume={**target, "editedValue": "수정본"})

    assert (outcome.status, outcome.outputs) == ("succeeded", {"final": "수정본"})


BAD_KEY_TEMPLATE = '{"a\\u0000": {{ start | length }}, "a\\u0000": 2}'


async def test_error_messages_never_quote_unsafe_text_from_a_template():
    dsl = _single({"id": "template_1", "type": "template", "config": {"template": BAD_KEY_TEMPLATE, "format": "json"}})
    static = _single({"id": "template_1", "type": "template",
                      "config": {"template": BAD_KEY_TEMPLATE.replace("{{ start | length }}", "1"), "format": "json"}})
    compiled = compile_workflow(dsl, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-bad-key", llm=ScriptedLLM([]), recorder=InMemoryRecorder())

    outcome = await execute_run(compiled, deps=deps, inputs={})

    assert (outcome.status, outcome.error["code"]) == ("failed", "TEMPLATE_ERROR")
    check_text(outcome.error)
    check_text([issue.to_dict() for issue in validate(static)])
