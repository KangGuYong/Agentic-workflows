import pytest

from engine.nodes.condition import ConditionNode, evaluate
from tests.helpers import make_ctx


@pytest.mark.parametrize(
    ("op", "left", "right", "expected"),
    [
        ("==", "approve", "approve", True),
        ("==", 5, "5", True),
        ("==", 5.0, "5", True),
        ("==", True, "true", True),
        ("==", "a", "b", False),
        ("!=", "a", "b", True),
        (">=", 8, 8.0, True),
        ("<", 3, 8.0, True),
        ("contains", "hello world", "world", True),
        ("contains", ["a", "b"], "b", True),
        ("not_contains", ["a"], "b", True),
        ("is_empty", "", None, True),
        ("is_empty", [], None, True),
        ("is_not_empty", {"a": 1}, None, True),
        ("==", True, 1, False),
        ("==", 0, "false", False),
        ("==", [1], [True], False),
        ("==", {"a": 1}, '{"a": 1.0}', True),
        ("==", 1000, "1_000", False),
        ("==", None, "", True),
        ("==", None, "null", True),
        ("contains", [True], 1, False),
        ("contains", [1, "x"], "1", True),
    ],
)
def test_evaluate(op, left, right, expected):
    assert evaluate(op, left, right) is expected


async def test_condition_combinators_and_routing():
    spec = ConditionNode()
    raw = {
        "conditions": [
            {"left": "{{llm_eval.score}}", "op": ">=", "right": "8"},
            {"left": "{{llm_eval.ok}}", "op": "==", "right": "true"},
        ],
        "combinator": "and",
    }
    config = spec.parse_config(raw)
    assert [(f.path, f.target) for f in spec.template_fields(config)] == [
        ("conditions.0.left", "number"),
        ("conditions.0.right", "number"),
        ("conditions.1.left", "any"),
        ("conditions.1.right", "any"),
    ]
    rendered = {"conditions.0.left": 9, "conditions.0.right": 8.0, "conditions.1.left": True, "conditions.1.right": "true"}

    result = await spec.execute(make_ctx(), config, rendered)
    assert result.output == {"result": True}
    assert spec.route(config, result.output) == "true"

    rendered["conditions.0.left"] = 3
    assert (await spec.execute(make_ctx(), config, rendered)).output == {"result": False}
    or_config = spec.parse_config({**raw, "combinator": "or"})
    assert (await spec.execute(make_ctx(), or_config, rendered)).output == {"result": True}


def test_unary_ops_have_no_right_field():
    spec = ConditionNode()
    config = spec.parse_config({"conditions": [{"left": "{{a.b}}", "op": "is_empty"}]})
    assert [f.path for f in spec.template_fields(config)] == ["conditions.0.left"]
    assert spec.handles(config) == ["true", "false"]
