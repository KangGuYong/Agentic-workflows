import pytest
from pydantic import ValidationError

from engine.jsondata import schema_problems
from engine.llm.base import ChatMessage, ChatResult
from engine.llm.scripted import ScriptedLLM
from engine.nodes.base import Usage
from engine.nodes.classifier import ClassifierNode
from engine.nodes.llm import LLMNode
from engine.nodes.registry import default_registry
from tests.helpers import make_ctx

SCORE_SCHEMA = {"type": "object", "properties": {"score": {"type": "number"}}, "required": ["score"]}
CATEGORIES = [{"id": "billing", "description": "결제"}, {"id": "tech", "description": "기술"}]


async def test_llm_text_output_messages_and_tokens():
    llm = ScriptedLLM([ChatResult(text="답", tokens_in=4, tokens_out=2)])
    spec = LLMNode()
    config = spec.parse_config({"model": "qwen2.5:14b", "system": "너는 작가", "prompt": "{{start.topic}} 써줘"})
    tokens: list[str] = []

    async def sink(text: str) -> None:
        tokens.append(text)

    result = await spec.execute(make_ctx(llm=llm, on_token=sink), config, {"system": "너는 작가", "prompt": "AI 써줘"})

    assert result.output == {"text": "답"}
    assert result.usage == Usage(4, 2)
    assert llm.calls[0]["model"] == "qwen2.5:14b"
    assert llm.calls[0]["messages"] == [ChatMessage("system", "너는 작가"), ChatMessage("user", "AI 써줘")]
    assert llm.calls[0]["schema"] is None
    assert tokens == ["답"]
    assert [f.path for f in spec.template_fields(config)] == ["prompt", "system"]
    assert spec.default_policy.retry.maxAttempts == 3


async def test_llm_structured_output():
    llm = ScriptedLLM([{"score": 9}])
    spec = LLMNode()
    config = spec.parse_config({"model": "m", "prompt": "p", "outputSchema": SCORE_SCHEMA})

    result = await spec.execute(make_ctx(llm=llm), config, {"prompt": "p"})

    assert result.output == {"score": 9}
    assert llm.calls[0]["schema"] == SCORE_SCHEMA
    assert spec.output_schema(config, {}) == SCORE_SCHEMA


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string"},
        {"type": 3},
        {"type": "object", "properties": {"code": {"type": "string", "pattern": "^(a+)+$"}}},
    ],
    ids=["not-object", "malformed", "outside-subset"],
)
def test_llm_rejects_invalid_output_schema(schema):
    with pytest.raises(ValidationError):
        LLMNode().parse_config({"model": "m", "prompt": "p", "outputSchema": schema})


async def test_classifier_routes_by_category():
    llm = ScriptedLLM([{"category": "tech", "reason": "코드 질문"}])
    spec = ClassifierNode()
    config = spec.parse_config({"model": "m", "input": "{{start.q}}", "categories": CATEGORIES})

    result = await spec.execute(make_ctx(llm=llm), config, {"input": "버그가 있어요"})

    assert spec.handles(config) == ["billing", "tech", "default"]
    assert spec.route(config, result.output) == "tech"
    assert llm.calls[0]["schema"]["properties"]["category"]["enum"] == ["billing", "tech", "default"]
    assert "- billing: 결제" in llm.calls[0]["messages"][0].content
    assert llm.calls[0]["messages"][1] == ChatMessage("user", "버그가 있어요")
    assert spec.fallback_output(config) == {"category": "default", "reason": "error"}


@pytest.mark.parametrize(
    "categories",
    [[{"id": "default", "description": "x"}], [{"id": "a", "description": "x"}, {"id": "a", "description": "y"}]],
)
def test_classifier_rejects_reserved_or_duplicate_ids(categories):
    with pytest.raises(ValidationError):
        ClassifierNode().parse_config({"model": "m", "input": "i", "categories": categories})


def test_registry_contains_ai_nodes():
    assert {"llm", "classifier"} <= {spec.type for spec in default_registry().all()}


async def test_llm_omits_a_system_prompt_that_renders_empty():
    llm = ScriptedLLM(["답"])
    spec = LLMNode()
    config = spec.parse_config({"model": "m", "system": "{{ start.persona | default('') }}", "prompt": "p"})

    await spec.execute(make_ctx(llm=llm), config, {"system": "  ", "prompt": "p"})

    assert llm.calls[0]["messages"] == [ChatMessage("user", "p")]


def test_classifier_output_schema_stays_in_the_validated_subset():
    spec = ClassifierNode()
    config = spec.parse_config({"model": "m", "input": "i", "categories": CATEGORIES})
    assert schema_problems(spec.output_schema(config, {})) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "m" * 201},
        {"categories": [{"id": "a", "description": "d" * 501}]},
        {"instructions": "i" * 4001},
        {"categories": [{"id": f"c{i}", "description": "d"} for i in range(21)]},
    ],
    ids=["model", "description", "instructions", "category-count"],
)
def test_classifier_fields_are_bounded(overrides):
    with pytest.raises(ValidationError):
        ClassifierNode().parse_config({"model": "m", "input": "i", "categories": CATEGORIES, **overrides})
