import pytest

from engine.jsondata import (
    MAX_SCHEMA_CHARS,
    MAX_SCHEMA_DEPTH,
    clip,
    parse_json,
    schema_problems,
    schema_violations,
)


@pytest.mark.parametrize(
    "text",
    ["NaN", '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e400}', "1" * 5000, "[" * 100_000, "{broken"],
    ids=["nan", "infinity", "-infinity", "overflow", "huge-int", "deep", "broken"],
)
def test_parse_json_rejects_non_standard_or_pathological_input(text):
    with pytest.raises(ValueError):
        parse_json(text)


def test_parse_json_accepts_standard_json():
    assert parse_json('{"a": [1, 2.5, -0, "x", null, true]}') == {"a": [1, 2.5, 0, "x", None, True]}


def test_clip():
    assert clip("abc", 5) == "abc"
    assert clip("abcdef", 3) == "abc…"


def test_schema_violations_are_short_and_ordered_by_path():
    schema = {"type": "object", "properties": {"a": {"type": "string", "maxLength": 1}, "b": {"type": "number"}}}

    violations = schema_violations(schema, {"b": "x", "a": "y" * 100_000})

    assert [violation.split(":")[0] for violation in violations] == ["a", "b"]
    assert all(len(violation) < 450 for violation in violations)
    assert schema_violations(schema, {"a": "y", "b": 1}) == []


def test_schema_violations_contain_recursion():
    schema = {"$defs": {"n": {"type": "array", "items": {"$ref": "#/$defs/n"}}}, "$ref": "#/$defs/n"}
    deep: list = []
    current = deep
    for _ in range(2000):
        current.append([])
        current = current[0]

    assert "중첩" in schema_violations(schema, deep)[0]


def test_typical_editor_schema_is_accepted():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "이름", "minLength": 1},
            "score": {"type": ["number", "null"], "minimum": 0},
            "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "maxItems": 10},
            "meta": {"type": "object", "additionalProperties": {"type": "string"}},
            "choice": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "pattern": {"type": "string"},
            "$ref": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
        "x-order": ["name", "score"],
    }
    assert schema_problems(schema) == []


@pytest.mark.parametrize(
    ("field", "keyword"),
    [
        ({"type": "string", "pattern": "^(a+)+$"}, "pattern"),
        ({"type": "object", "patternProperties": {"^x": {}}}, "patternProperties"),
        ({"type": "array", "uniqueItems": True}, "uniqueItems"),
        ({"$ref": "#"}, "$ref"),
        ({"$defs": {"a": {}}}, "$defs"),
        ({"allOf": [{"type": "string"}]}, "allOf"),
        ({"not": {"type": "string"}}, "not"),
        ({"type": "array", "contains": {"type": "string"}}, "contains"),
        ({"type": "object", "propertyNames": {"maxLength": 3}}, "propertyNames"),
    ],
)
def test_keywords_outside_the_subset_are_rejected(field, keyword):
    problems = schema_problems({"type": "object", "properties": {"field": {"anyOf": [field]}}})
    assert any(keyword in problem for problem in problems)


def test_invalid_oversized_too_deep_or_non_json_schemas_are_rejected():
    deep: dict = {"type": "object"}
    for _ in range(MAX_SCHEMA_DEPTH + 5):
        deep = {"type": "object", "properties": {"a": deep}}

    assert "올바른" in schema_problems({"type": 5})[0]
    assert "큽니다" in schema_problems({"type": "object", "description": "x" * MAX_SCHEMA_CHARS})[0]
    assert "중첩" in schema_problems(deep)[0]
    assert "JSON" in schema_problems({"type": "object", "default": float("nan")})[0]
    assert schema_problems([]) != []
