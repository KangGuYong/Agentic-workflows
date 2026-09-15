import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from engine.compiler.build import compile_workflow
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import execute_run
from engine.validator import validate
from tests.helpers import load_golden

PATTERNS = ["chaining", "routing", "parallel", "evaluator_loop", "hitl"]


def _deps(llm, run_id: str = "run-1") -> RunDeps:
    return RunDeps(run_id=run_id, llm=llm, recorder=InMemoryRecorder())


@pytest.mark.parametrize("name", PATTERNS)
def test_golden_fixtures_validate_cleanly(name):
    assert validate(load_golden(name)) == []


async def test_chaining_passes_outputs_forward_and_streams_tokens():
    compiled = compile_workflow(load_golden("chaining"), checkpointer=InMemorySaver())
    llm = ScriptedLLM(["개요", "본문"])
    deps = _deps(llm)

    outcome = await execute_run(compiled, deps=deps, inputs={"topic": "AI"})

    assert (outcome.status, outcome.outputs) == ("succeeded", {"result": "본문"})
    assert llm.prompts() == ["AI 글의 개요를 써줘", "다음 개요로 본문을 써줘:\n개요"]
    assert {"type": "node_token", "nodeId": "llm_1", "execIndex": 1, "attempt": None, "text": "개요"} in deps.recorder.events


@pytest.mark.parametrize(
    ("category", "responses", "answer"),
    [
        ("tech", [{"category": "tech", "reason": "r"}, "기술 답변"], "기술 답변"),
        ("billing", [{"category": "billing", "reason": "r"}, "결제 답변"], "결제 답변"),
        ("default", [{"category": "default", "reason": "r"}], "담당 부서를 찾지 못했습니다."),
    ],
)
async def test_routing_takes_exactly_one_branch(category, responses, answer):
    compiled = compile_workflow(load_golden("routing"), checkpointer=InMemorySaver())
    deps = _deps(ScriptedLLM(responses))

    outcome = await execute_run(compiled, deps=deps, inputs={"question": "질문"})

    assert outcome.status == "succeeded"
    assert outcome.outputs == {"category": category, "answer": answer}
    ran = {record.node_id for record in deps.recorder.records}
    assert len(ran & {"llm_billing", "llm_tech", "template_1"}) == 1


def _parallel_responder(model, messages, schema):
    prompt = messages[-1].content
    if prompt.startswith("종합"):
        return "종합 결과"
    return "장점 목록" if "장점" in prompt else "단점 목록"


async def test_parallel_branches_merge_in_declaration_order():
    compiled = compile_workflow(load_golden("parallel"), checkpointer=InMemorySaver())
    llm = ScriptedLLM(_parallel_responder)

    outcome = await execute_run(compiled, deps=_deps(llm), inputs={"topic": "재택근무"})

    assert outcome.status == "succeeded"
    assert outcome.outputs["summary"] == "종합 결과"
    assert list(json.loads(outcome.outputs["branchOrder"])) == ["llm_cons", "llm_pros"]
    assert "장점: 장점 목록\n단점: 단점 목록" in llm.prompts()[-1]


async def test_evaluator_loop_stops_when_the_score_passes():
    compiled = compile_workflow(load_golden("evaluator_loop"), checkpointer=InMemorySaver())
    llm = ScriptedLLM(["초안1", {"score": 5, "feedback": "더 구체적으로"}, "초안2", {"score": 9, "feedback": "좋음"}])

    outcome = await execute_run(compiled, deps=_deps(llm), inputs={"topic": "AI"})

    assert (outcome.status, outcome.outputs) == ("succeeded", {"text": "초안2", "score": 9})
    assert "이전 피드백: 없음" in llm.prompts()[0]
    assert "이전 피드백: 더 구체적으로" in llm.prompts()[2]


async def test_evaluator_loop_exits_when_iterations_are_exhausted():
    compiled = compile_workflow(load_golden("evaluator_loop"), checkpointer=InMemorySaver())
    script = []
    for i in range(1, 4):
        script += [f"초안{i}", {"score": 3, "feedback": "부족"}]
    deps = _deps(ScriptedLLM(script))

    outcome = await execute_run(compiled, deps=deps, inputs={"topic": "AI"})

    assert (outcome.status, outcome.outputs) == ("succeeded", {"text": "초안3", "score": 3})
    assert [r.exec_index for r in deps.recorder.for_node("llm_gen")] == [1, 2, 3]
    assert deps.recorder.for_node("condition_1")[-1].meta == {"handle": "true", "loopExhausted": True}


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"decision": "approve", "editedValue": "수정 원고"}, {"final": "수정 원고", "decision": "approve", "note": ""}),
        ({"decision": "reject", "comment": "다시"}, {"final": "원고", "decision": "reject", "note": "반려되었습니다"}),
    ],
)
async def test_hitl_waits_then_resumes_without_duplicate_attempts(answer, expected):
    compiled = compile_workflow(load_golden("hitl"), checkpointer=InMemorySaver())
    deps = _deps(ScriptedLLM([]))

    first = await execute_run(compiled, deps=deps, inputs={"draft": "원고"})
    assert first.status == "waiting"
    assert first.waiting == {
        "nodeId": "human_approval_1",
        "execIndex": 1,
        "message": "초안을 검토해 주세요",
        "review": "원고",
        "allowEdit": True,
    }

    # the API sends the approval's target with the answer (spec 5.6 resume body)
    second = await execute_run(compiled, deps=deps, resume={"nodeId": "human_approval_1", "execIndex": 1, **answer})

    assert (second.status, second.outputs) == ("succeeded", expected)
    records = deps.recorder.for_node("human_approval_1")
    assert [(r.attempt, r.status) for r in records] == [(1, "succeeded")]


LOOP_EXIT_PARALLEL = {
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}},
        {"id": "llm_gen", "type": "llm", "config": {"model": "m", "prompt": "{{start.topic}} 초안"}},
        {"id": "condition_1", "type": "condition",
         "config": {"conditions": [{"left": "{{llm_gen.text}}", "op": "contains", "right": "완성"}]}},
        {"id": "llm_a", "type": "llm", "config": {"model": "m", "prompt": "요약: {{llm_gen.text}}"}},
        {"id": "llm_b", "type": "llm", "config": {"model": "m", "prompt": "제목: {{llm_gen.text}}"}},
        {"id": "merge_1", "type": "merge"},
        {"id": "end", "type": "end", "config": {"outputs": {"merged": "{{merge_1.branches | tojson}}"}}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "llm_gen"},
        {"id": "e2", "source": "llm_gen", "target": "condition_1"},
        {"id": "back", "source": "condition_1", "sourceHandle": "false", "target": "llm_gen", "maxIterations": 2},
        {"id": "e3", "source": "condition_1", "sourceHandle": "true", "target": "llm_a"},
        {"id": "e4", "source": "condition_1", "sourceHandle": "true", "target": "llm_b"},
        {"id": "e5", "source": "llm_a", "target": "merge_1"},
        {"id": "e6", "source": "llm_b", "target": "merge_1"},
        {"id": "e7", "source": "merge_1", "target": "end"},
    ],
}


def _loop_exit_responder(model, messages, schema):
    prompt = messages[-1].content
    if prompt.startswith("요약"):
        return "요약본"
    if prompt.startswith("제목"):
        return "제목"
    return "초안"


@pytest.mark.parametrize("finishes", [True, False], ids=["exit-on-true", "exit-when-exhausted"])
async def test_a_loop_exit_can_fan_out_and_merges_once(finishes):
    assert validate(LOOP_EXIT_PARALLEL) == []
    compiled = compile_workflow(LOOP_EXIT_PARALLEL, checkpointer=InMemorySaver())
    drafts = iter(["완성 초안"] if finishes else ["초안1", "초안2", "초안3"])

    def responder(model, messages, schema):
        answer = _loop_exit_responder(model, messages, schema)
        return next(drafts) if answer == "초안" else answer

    deps = _deps(ScriptedLLM(responder))
    outcome = await execute_run(compiled, deps=deps, inputs={"topic": "AI"})

    assert outcome.status == "succeeded"
    assert json.loads(outcome.outputs["merged"]) == {"llm_a": {"text": "요약본"}, "llm_b": {"text": "제목"}}
    assert len(deps.recorder.for_node("llm_gen")) == (1 if finishes else 3)
    assert [len(deps.recorder.for_node(node)) for node in ("llm_a", "llm_b", "merge_1", "end")] == [1, 1, 1, 1]


SELF_LOOP = {
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "condition_1", "type": "condition", "config": {"conditions": [{"left": "a", "op": "==", "right": "b"}]}},
        {"id": "end", "type": "end", "config": {"outputs": {"result": "{{condition_1.result}}"}}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "condition_1"},
        {"id": "self", "source": "condition_1", "sourceHandle": "false", "target": "condition_1", "maxIterations": 2},
        {"id": "e2", "source": "condition_1", "sourceHandle": "true", "target": "end"},
    ],
}


async def test_a_condition_self_loop_stops_at_its_limit():
    assert validate(SELF_LOOP) == []
    compiled = compile_workflow(SELF_LOOP, checkpointer=InMemorySaver())
    deps = _deps(ScriptedLLM([]))

    outcome = await execute_run(compiled, deps=deps, inputs={})

    assert (outcome.status, outcome.outputs) == ("succeeded", {"result": False})
    assert [r.meta for r in deps.recorder.for_node("condition_1")] == [
        {"handle": "false", "loopExhausted": False},
        {"handle": "false", "loopExhausted": False},
        {"handle": "true", "loopExhausted": True},
    ]
