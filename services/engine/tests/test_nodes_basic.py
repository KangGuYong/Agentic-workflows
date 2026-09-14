import pytest
from pydantic import ValidationError

from engine.errors import ErrorCode, NodeError
from engine.nodes.base import TemplateField
from engine.nodes.io import EndNode, StartNode
from engine.nodes.registry import default_registry
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


def test_end_rejects_bad_output_names():
    with pytest.raises(ValidationError):
        EndNode().parse_config({"outputs": {"1bad": "x"}})


async def test_template_text_output():
    spec = TemplateNode()
    config = spec.parse_config({"template": "안녕 {{start.name}}"})
    assert spec.template_fields(config)[0].target == "string"
    result = await spec.execute(make_ctx(), config, {"template": "안녕 철수"})
    assert result.output == {"text": "안녕 철수"}


async def test_template_json_output():
    spec = TemplateNode()
    config = spec.parse_config({"template": '{"a": {{start.n}}}', "format": "json"})
    assert spec.template_fields(config)[0].target == "any"
    assert (await spec.execute(make_ctx(), config, {"template": '{"a": 1}'})).output == {"data": {"a": 1}}
    assert (await spec.execute(make_ctx(), config, {"template": [1, 2]})).output == {"data": [1, 2]}
    with pytest.raises(NodeError) as exc:
        await spec.execute(make_ctx(), config, {"template": "{broken"})
    assert exc.value.code == ErrorCode.TEMPLATE_ERROR


def test_registry_lookup():
    registry = default_registry()
    assert registry.get("start").type == "start"
    assert registry.get("nope") is None
    assert {"start", "end", "template"} <= {spec.type for spec in registry.all()}


@pytest.mark.parametrize(
    "text", ["[" * 100_000, "1" * 5000, "NaN", '{"a": Infinity}'], ids=["deep", "huge-int", "nan", "infinity"]
)
async def test_template_json_rejects_non_standard_json(text):
    spec = TemplateNode()
    config = spec.parse_config({"template": "x", "format": "json"})
    with pytest.raises(NodeError) as exc:
        await spec.execute(make_ctx(), config, {"template": text})
    assert exc.value.code == ErrorCode.TEMPLATE_ERROR


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"code": {"type": "string", "pattern": "^(a+)+$"}}},
        {"type": "object", "properties": {"tags": {"type": "array", "uniqueItems": True}}},
        {"type": "object", "$ref": "#"},
    ],
    ids=["pattern", "uniqueItems", "ref"],
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
