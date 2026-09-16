import pytest
from pydantic import ValidationError

from engine.errors import ErrorCode, NodeError
from engine.jsondata import ValidationBudgetExceeded
from engine.nodes.base import TemplateField
from engine.nodes.io import EndNode, StartNode
from engine.nodes.registry import NodeRegistry, default_registry
from engine.nodes.template import TemplateNode
from tests.helpers import make_ctx

INPUTS_SCHEMA = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}


async def test_start_returns_inputs_and_checks_schema():
    spec = StartNode()
    config = spec.parse_config({"inputs": INPUTS_SCHEMA})

    result = await spec.execute(make_ctx(inputs={"topic": "AI"}), config, {})

    assert result.output == {"topic": "AI"}
    assert spec.output_schema(config, {}) == INPUTS_SCHEMA
    with pytest.raises(NodeError) as exc:
        await spec.execute(make_ctx(inputs={}), config, {})
    assert exc.value.code == ErrorCode.TYPE_MISMATCH


@pytest.mark.parametrize("schema", [{"type": "string"}, {"type": 5}])
def test_start_rejects_invalid_input_schema(schema):
    with pytest.raises(ValidationError):
        StartNode().parse_config({"inputs": schema})


async def test_end_maps_rendered_outputs():
    spec = EndNode()
    config = spec.parse_config({"outputs": {"result": "{{llm_1.text}}"}})

    assert spec.handles(config) == []
    assert spec.template_fields(config) == [TemplateField("outputs.result", "{{llm_1.text}}", "any")]
    result = await spec.execute(make_ctx(), config, {"outputs.result": "본문"})
    assert result.output == {"result": "본문"}


@pytest.mark.parametrize("name", ["1bad", "result\n", "a-b", "x" * 65])
def test_end_rejects_bad_output_names(name):
    with pytest.raises(ValidationError):
        EndNode().parse_config({"outputs": {name: "x"}})


async def test_template_text_output():
    spec = TemplateNode()
    config = spec.parse_config({"template": "안녕 {{start.name}}"})
    assert spec.template_fields(config)[0].target == "string"
    result = await spec.execute(make_ctx(), config, {"template": "안녕 철수"})
    assert result.output == {"text": "안녕 철수"}


async def test_template_json_output_is_the_rendered_value():
    spec = TemplateNode()
    config = spec.parse_config({"template": '{"a": {{start.n}}}', "format": "json"})
    assert spec.template_fields(config)[0].target == "json"
    assert (await spec.execute(make_ctx(), config, {"template": {"a": 1}})).output == {"data": {"a": 1}}
    # a rendered string is data, never parsed again
    assert (await spec.execute(make_ctx(), config, {"template": '{"admin": true}'})).output == {"data": '{"admin": true}'}


def test_registry_lookup():
    registry = default_registry()
    assert registry.get("start").type == "start"
    assert registry.get("nope") is None
    assert {"start", "end", "template"} <= {spec.type for spec in registry.all()}


def test_registry_rejects_duplicate_types():
    with pytest.raises(ValueError):
        NodeRegistry([StartNode(), StartNode()])


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"code": {"type": "string", "pattern": "^(a+)+$"}}},
        {"type": "object", "properties": {"tags": {"type": "array", "uniqueItems": True}}},
        {"type": "object", "$ref": "#"},
        {"type": "object", "anyOf": [{"type": "string"}]},
    ],
    ids=["pattern", "uniqueItems", "ref", "root-anyOf"],
)
def test_start_rejects_schemas_outside_the_tenant_subset(schema):
    with pytest.raises(ValidationError):
        StartNode().parse_config({"inputs": schema})


async def test_start_input_violations_are_short():
    spec = StartNode()
    config = spec.parse_config({"inputs": {"type": "object", "properties": {"topic": {"type": "string", "maxLength": 5}}}})
    with pytest.raises(NodeError) as exc:
        await spec.execute(make_ctx(inputs={"topic": "x" * 100_000}), config, {})
    assert exc.value.code == ErrorCode.TYPE_MISMATCH
    assert len(exc.value.message) < 1000


async def test_start_output_does_not_share_inputs():
    spec = StartNode()
    config = spec.parse_config({"inputs": {"type": "object"}})
    inputs = {"tags": ["a"]}

    result = await spec.execute(make_ctx(inputs=inputs), config, {})
    result.output["tags"].append("b")

    assert inputs == {"tags": ["a"]}


async def test_start_input_too_expensive_to_validate_is_not_a_type_mismatch(monkeypatch):
    def too_expensive(schema, value):
        raise ValidationBudgetExceeded("너무 복잡")

    monkeypatch.setattr("engine.nodes.io.schema_violations", too_expensive)
    spec = StartNode()
    with pytest.raises(NodeError) as exc:
        await spec.execute(make_ctx(inputs={}), spec.parse_config({}), {})
    assert (exc.value.code, exc.value.retryable) == (ErrorCode.NODE_FAILED, False)
