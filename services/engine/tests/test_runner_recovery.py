from langgraph.checkpoint.memory import InMemorySaver

from engine.compiler.build import compile_workflow
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.guard import FlagGuard
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import execute_run
from tests.helpers import load_golden


async def test_manual_retry_resumes_from_the_failed_node():
    compiled = compile_workflow(load_golden("chaining"), checkpointer=InMemorySaver())
    recorder = InMemoryRecorder()
    failing = RunDeps(run_id="run-r", llm=ScriptedLLM(["개요", ValueError("모델 오류")]), recorder=recorder)

    failed = await execute_run(compiled, deps=failing, inputs={"topic": "AI"})
    assert failed.status == "failed"
    assert (failed.error["code"], failed.error["nodeId"]) == ("NODE_FAILED", "llm_2")

    retry = RunDeps(run_id="run-r", llm=ScriptedLLM(["본문"]), recorder=recorder)
    outcome = await execute_run(compiled, deps=retry)

    assert (outcome.status, outcome.outputs) == ("succeeded", {"result": "본문"})
    assert len(recorder.for_node("llm_1")) == 1
    assert [(r.attempt, r.status) for r in recorder.for_node("llm_2")] == [(1, "failed"), (2, "succeeded")]


async def test_completed_parallel_sibling_is_kept_after_a_failure():
    compiled = compile_workflow(load_golden("parallel"), checkpointer=InMemorySaver())
    calls = {"cons": 0}

    def responder(model, messages, schema):
        prompt = messages[-1].content
        if prompt.startswith("종합"):
            return "종합 결과"
        if "단점" in prompt:
            calls["cons"] += 1
            return ValueError("일시 오류") if calls["cons"] == 1 else "단점 목록"
        return "장점 목록"

    llm = ScriptedLLM(responder)
    deps = RunDeps(run_id="run-p", llm=llm, recorder=InMemoryRecorder())

    failed = await execute_run(compiled, deps=deps, inputs={"topic": "재택근무"})
    assert (failed.status, failed.error["nodeId"]) == ("failed", "llm_cons")

    outcome = await execute_run(compiled, deps=deps)

    assert outcome.status == "succeeded"
    assert sum(1 for prompt in llm.prompts() if "장점" in prompt and not prompt.startswith("종합")) == 1


async def test_cancelled_run():
    compiled = compile_workflow(load_golden("chaining"), checkpointer=InMemorySaver())
    guard = FlagGuard()
    guard.cancel()
    deps = RunDeps(run_id="run-c", llm=ScriptedLLM([]), recorder=InMemoryRecorder(), guard=guard)

    outcome = await execute_run(compiled, deps=deps, inputs={"topic": "AI"})

    assert outcome.status == "cancelled"
