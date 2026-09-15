import pytest
from langgraph.checkpoint.memory import InMemorySaver

from engine.compiler.build import WorkflowInvalid, compile_workflow
from engine.dsl.models import WorkflowDSL, dsl_hash
from engine.errors import LeaseLost
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.guard import FlagGuard
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import ResumeRejected, execute_run

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

    for bad in ({"decision": "maybe"}, {"decision": "approve", "editedValue": 3}, {"decision": "approve", "nodeId": "x"}):
        with pytest.raises(ResumeRejected):
            await execute_run(compiled, deps=deps, resume=bad)

    fixed = await execute_run(compiled, deps=deps, resume={"decision": "approve", "editedValue": "수정본"})
    assert (fixed.status, fixed.outputs) == ("succeeded", {"final": "수정본"})
    with pytest.raises(ResumeRejected):
        await execute_run(compiled, deps=deps, resume={"decision": "approve"})
