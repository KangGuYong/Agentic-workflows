import time
import tracemalloc

import pytest

from engine.jsondata import (
    MAX_SCHEMA_BOUND,
    MAX_SCHEMA_BRANCHES,
    MAX_SCHEMA_CHARS,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_NODES,
    MAX_SCHEMA_PROBLEMS,
    MAX_VIOLATIONS,
    clip,
    parse_json,
    schema_problems,
    schema_violations,
)

# ---------------------------------------------------------------- parse_json


@pytest.mark.parametrize(
    "text",
    [
        "NaN",
        '{"a": Infinity}',
        '{"a": -Infinity}',
        '{"a": 1e400}',
        "1" * 5000,
        "[" * 100_000,
        "{broken",
        '{"admin": false, "admin": true}',
        '"\\u0000"',
        '{"k": "\\ud800"}',
        '{"\\ud800": 1}',
    ],
    ids=["nan", "infinity", "-infinity", "overflow", "huge-int", "deep", "broken", "duplicate-key", "nul",
         "lone-surrogate", "surrogate-key"],
)
def test_parse_json_rejects_non_standard_or_unsafe_input(text):
    with pytest.raises(ValueError):
        parse_json(text)


def test_parse_json_accepts_standard_json():
    assert parse_json('{"a": [1, 2.5, -0, "x😀", null, true], "b": {}}') == {
        "a": [1, 2.5, 0, "x😀", None, True],
        "b": {},
    }


def test_clip():
    assert clip("abc", 5) == "abc"
    assert clip("abcdef", 3) == "abc…"


# ---------------------------------------------------------------- schema_problems


def test_typical_editor_schema_is_accepted():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "이름", "minLength": 1, "format": "email"},
            "score": {"type": ["number", "null"], "minimum": 0, "exclusiveMaximum": 10.5},
            "count": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "maxItems": 10},
            "meta": {"type": "object", "additionalProperties": {"type": "string"}},
            "choice": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "kind": {"oneOf": [{"const": "x"}, {"const": "y"}]},
            "pattern": {"type": "string"},
            "$ref": {"items": True},
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
@pytest.mark.parametrize(
    "wrap",
    [
        lambda field: {"type": "object", "properties": {"field": field}},
        lambda field: {"type": "array", "items": field},
        lambda field: {"type": "object", "additionalProperties": field},
        lambda field: {"anyOf": [{"type": "null"}, field]},
        lambda field: {"oneOf": [field]},
    ],
    ids=["properties", "items", "additionalProperties", "anyOf", "oneOf"],
)
def test_keywords_outside_the_subset_are_rejected_wherever_they_hide(field, keyword, wrap):
    problems = schema_problems(wrap(field))
    assert any(keyword in problem for problem in problems)


def test_depth_boundary():
    def nested(depth):
        schema: dict = {"type": "string"}
        for _ in range(depth - 1):
            schema = {"type": "object", "properties": {"a": schema}}
        return schema

    assert schema_problems(nested(MAX_SCHEMA_DEPTH)) == []
    assert "중첩" in schema_problems(nested(MAX_SCHEMA_DEPTH + 1))[0]


@pytest.mark.parametrize(
    ("schema", "fragment"),
    [
        ({"type": 5}, "type"),
        ({"type": "object", "required": "name"}, "required"),
        ({"type": "object", "properties": []}, "properties"),
        ({"anyOf": []}, "anyOf"),
        ({"type": "string", "maxLength": MAX_SCHEMA_BOUND + 1}, "maxLength"),
        ({"type": "array", "minItems": -1}, "minItems"),
        ({"type": "number", "minimum": "0"}, "minimum"),
        ({"type": "object", "properties": {"a": 5}}, "스키마"),
        ({"anyOf": [{"type": "null"}] * (MAX_SCHEMA_BRANCHES + 1)}, "anyOf"),
        ({"type": "array", "items": {"anyOf": [{"type": "null"}] * 16}, "properties": {
            f"p{i}": {"anyOf": [{"type": "null"}] * 16} for i in range(MAX_SCHEMA_NODES // 16)}}, "복잡"),
        ({"type": "object", "description": "x" * MAX_SCHEMA_CHARS}, "큽니다"),
        ({"type": "object", "default": float("nan")}, "JSON"),
    ],
    ids=["type", "required", "properties", "empty-anyOf", "bound", "negative-bound", "number-bound",
         "non-schema", "branches", "nodes", "size", "nan"],
)
def test_malformed_or_oversized_schemas_are_rejected(schema, fragment):
    problems = schema_problems(schema)
    assert problems and any(fragment in problem for problem in problems)


def test_enum_values_are_depth_bounded():
    value: list = []
    for _ in range(MAX_SCHEMA_DEPTH + 5):
        value = [value]
    assert "중첩" in schema_problems({"type": "array", "enum": [value]})[0]


def test_problems_are_capped_and_in_schema_order():
    schema = {"type": "object", **{f"bad{i:02d}": 1 for i in range(50)}}
    problems = schema_problems(schema)
    assert len(problems) == MAX_SCHEMA_PROBLEMS
    assert problems[0].endswith("bad00") and problems[1].endswith("bad01")
    assert schema_problems([]) != []


# ---------------------------------------------------------------- schema_violations


def test_violations_cover_the_subset():
    schema = {
        "type": "object",
        "properties": {
            "s": {"type": "string", "minLength": 2, "maxLength": 3},
            "n": {"type": "integer", "minimum": 1, "exclusiveMaximum": 5},
            "l": {"type": "array", "items": {"type": "boolean"}, "maxItems": 2},
            "e": {"enum": [1, "one", [True]]},
            "c": {"const": {"k": 1}},
            "a": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "o": {"oneOf": [{"type": "number"}, {"type": "integer"}]},
        },
        "required": ["s", "n"],
        "additionalProperties": False,
    }
    valid = {"s": "ab", "n": 4.0, "l": [True], "e": [True], "c": {"k": 1.0}, "a": None, "o": 1.5}
    assert schema_violations(schema, valid) == []
    assert schema_violations(schema, {"s": "a", "n": 5}) == [
        "s: 길이가 2자 이상이어야 합니다",
        "n: 5보다 작아야 합니다",
    ]
    assert schema_violations(schema, {"s": "ab", "n": 1, "e": True, "c": {"k": True}}) == [
        "e: 허용된 값 중 하나여야 합니다",
        "c: 정해진 값과 같아야 합니다",
    ]
    assert schema_violations(schema, {"s": "ab", "n": 1, "a": 1, "o": 1, "x": 0}) == [
        "a: anyOf 조건 중 하나 이상을 만족해야 합니다",
        "o: oneOf 조건 중 정확히 하나를 만족해야 합니다",
        "(root): 허용되지 않은 필드입니다: x",
    ]
    assert schema_violations(schema, {"n": True, "l": [1, 2, 3]}) == [
        "(root): 필수 필드가 없습니다: s",
        "n: integer 타입이어야 하지만 boolean 값입니다",
        "l: 항목이 2개 이하여야 합니다",
        "l/0: boolean 타입이어야 하지만 number 값입니다",
        "l/1: boolean 타입이어야 하지만 number 값입니다",
    ]


def test_violations_are_short_and_never_copy_data():
    schema = {"type": "object", "properties": {"a": {"type": "string", "maxLength": 1}, "b": {"type": "number"}}}
    violations = schema_violations(schema, {"b": "x" * 100_000, "a": "y" * 100_000})
    assert [violation.split(":")[0] for violation in violations] == ["a", "b"]
    assert all("yyyy" not in v and "xxxx" not in v for v in violations)
    assert len(schema_violations(schema, {"x" * 100_000: 1, "a": 1})[0]) < 300


def _measure(schema, value):
    tracemalloc.start()
    started = time.perf_counter()
    violations = schema_violations(schema, value)
    elapsed = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return violations, elapsed, peak


def test_many_failing_items_stop_after_enough_violations():
    schema = {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "string"}}}}
    violations, elapsed, peak = _measure(schema, {"a": [0] * 1_000_000})
    assert len(violations) == MAX_VIOLATIONS
    assert elapsed < 1 and peak < 1_000_000


def test_failing_branches_cost_neither_time_nor_memory():
    schema = {"type": "array", "items": {"anyOf": [{"type": "null"}] * MAX_SCHEMA_BRANCHES}}
    assert schema_problems(schema) == []
    violations, elapsed, peak = _measure(schema, [0] * 5)
    assert len(violations) == MAX_VIOLATIONS and elapsed < 1 and peak < 1_000_000


def test_valid_large_data_is_linear():
    schema = {
        "type": "array",
        "items": {"type": "object", "properties": {"v": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}},
    }
    data = [{"v": i} for i in range(200_000)]
    started = time.perf_counter()  # measured without tracemalloc, which slows Python code about 10x
    assert schema_violations(schema, data) == []
    assert time.perf_counter() - started < 5
