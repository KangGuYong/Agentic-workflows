import pytest

from engine.dsl.types import coerce_runtime, compat, kinds_of, resolve_path, to_text

OBJ = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "score": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "meta": {"type": "object"},
        "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["text"],
}


def test_kinds_of_basic_and_unions():
    assert kinds_of({"type": "integer"}) == {"number"}
    assert kinds_of({"type": ["string", "null"]}) == {"string", "null"}
    assert kinds_of(OBJ["properties"]["maybe"]) == {"string", "null"}
    assert kinds_of({"enum": ["a", "b"]}) == {"string"}
    assert kinds_of({"properties": {}}) == {"object"}
    assert kinds_of({}) == {"unknown"}
    assert kinds_of(None) == {"unknown"}


def test_resolve_path():
    assert resolve_path(OBJ, ()) == OBJ
    assert resolve_path(OBJ, ("text",)) == {"type": "string"}
    assert resolve_path(OBJ, ("tags", "0", "name")) == {"type": "string"}
    assert resolve_path(OBJ, ("nope",)) is None
    assert resolve_path(OBJ, ("text", "deeper")) is None
    assert resolve_path(OBJ, ("meta", "anything")) == {}
    assert resolve_path({}, ("a", "b")) == {}


def test_resolve_path_through_nullable_object():
    schema = {"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}}}, {"type": "null"}]}
    assert resolve_path(schema, ("a",)) == {"type": "string"}


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ({"string"}, "string", "ok"),
        ({"number"}, "string", "ok"),
        ({"object"}, "string", "warning"),
        ({"unknown"}, "string", "ok"),
        ({"string"}, "number", "error"),
        ({"number"}, "number", "ok"),
        ({"unknown"}, "number", "warning"),
        ({"number", "null"}, "number", "warning"),
        ({"array"}, "string|array", "ok"),
        ({"unknown"}, "string|array", "warning"),
        ({"number"}, "string|array", "error"),
        ({"object"}, "any", "ok"),
        ({"string", "object"}, "number", "error"),
    ],
)
def test_compat(source, target, expected):
    assert compat(source, target) == expected


def test_to_text():
    assert to_text("a") == "a"
    assert to_text(3) == "3"
    assert to_text(2.5) == "2.5"
    assert to_text(True) == "true"
    assert to_text(None) == ""
    assert to_text({"k": "값"}) == '{"k": "값"}'
    assert to_text([1, 2]) == "[1, 2]"


def test_coerce_runtime_accepts_compatible_values():
    assert coerce_runtime({"a": 1}, "any") == {"a": 1}
    assert coerce_runtime(5, "string") == "5"
    assert coerce_runtime("7.5", "number") == 7.5
    assert coerce_runtime(3, "number") == 3
    assert coerce_runtime([1], "string|array") == [1]


@pytest.mark.parametrize(("value", "target"), [("abc", "number"), (True, "number"), (None, "number"), (5, "string|array")])
def test_coerce_runtime_rejects_incompatible_values(value, target):
    with pytest.raises(TypeError):
        coerce_runtime(value, target)
