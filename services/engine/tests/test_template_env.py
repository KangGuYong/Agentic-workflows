import time

import pytest
from jinja2.exceptions import SecurityError, UndefinedError

from engine.templates.env import ENV, MAX_OUTPUT_CHARS, json_value

CTX = {
    "llm_1": {"data": {"items": [{"title": "T"}], "keys": "k", "count": 3}},
    "start": {"lst": [1, 2], "topic": "AI", "many": ["x"] * (MAX_OUTPUT_CHARS // 2 + 1)},
}


def render(source: str) -> str:
    return ENV.from_string(source).render(**CTX)


def test_dotted_access_reads_mapping_keys_before_methods():
    assert render("{{ llm_1.data.items[0].title }}") == "T"
    assert render("{{ llm_1.data.keys }}-{{ llm_1.data.count }}") == "k-3"


def test_missing_mapping_key_is_undefined_not_a_method():
    assert render("{{ start.values | default('없음') }}") == "없음"
    with pytest.raises(UndefinedError):
        render("{{ start.values }}")


def test_bracket_access_on_missing_key_is_undefined():
    assert render("{{ start['keys'] | default('d') }}") == "d"
    with pytest.raises(UndefinedError):
        render("{{ start['__class__'] }}")


def test_finalize_and_tojson():
    assert render("{{ start.lst }} {{ true }} {{ none }}") == "[1, 2] true "
    assert ENV.from_string("{{ v | tojson }}").render(v={"k": "값"}) == '{"k": "값"}'
    with pytest.raises(UndefinedError):
        render("{{ missing | tojson }}")


def test_numeric_operators_are_allowed():
    assert render("{{ 3 * 4 }}|{{ 2 ** 3 }}|{{ 7 % 3 }}|{{ 2 ** -1 }}") == "12|8|1|0.5"


@pytest.mark.parametrize("source", ["{{ 'a' * 3 }}", "{{ start.lst * 2 }}", "{{ '%05d' % 3 }}", "{{ start.topic * 2 }}"])
def test_non_numeric_operators_are_rejected(source):
    with pytest.raises(SecurityError):
        render(source)


@pytest.mark.parametrize("source", ["{{ 2 ** 5000 }}", "{{ 10 ** 64 ** 64 ** 64 }}"])
def test_huge_powers_are_rejected(source):
    with pytest.raises(SecurityError):
        render(source)


def test_undefined_operand_is_an_undefined_error():
    with pytest.raises(UndefinedError):
        render("{{ missing * 2 }}")


def test_join_is_size_checked():
    assert render("{{ start.lst | join(',') }}") == "1,2"
    with pytest.raises(SecurityError):
        render("{{ start.many | join(',') }}")


def test_sandbox_blocks_dunder_access():
    # getattr() never calls super().getattr() for a non-Mapping/non-LoopContext object, so this
    # is now undefined (like any other attribute access on a list) rather than a sandbox violation.
    with pytest.raises(UndefinedError):
        render("{{ start.lst.__class__ }}")


def test_immutable_sandbox_blocks_mutation():
    # Same reasoning: `.append` on a list is undefined (never resolves to the real bound method),
    # so calling it fails as an undefined call rather than a sandboxed-mutation SecurityError.
    with pytest.raises(UndefinedError):
        render("{{ start.lst.append(3) }}")


def test_round_is_bounded_and_supports_methods():
    assert render("{{ 3.14159 | round(2) }}|{{ 3.141 | round(2, 'ceil') }}|{{ 3.149 | round(1, 'floor') }}") == "3.14|3.15|3.1"


@pytest.mark.parametrize(
    "source",
    ["{{ 1.5 | round(10000000, 'ceil') }}", "{{ 1.5 | round(-1) }}", "{{ 1.5 | round(2, 'up') }}", "{{ 'x' | round }}"],
)
def test_round_rejects_bad_arguments(source):
    with pytest.raises((TypeError, ValueError)):
        render(source)


def test_join_formats_items_like_printed_values():
    assert ENV.from_string("{{ v | join(',') }}").render(v=[None, True, {"k": 1}, "s"]) == ',true,{"k": 1},s'


@pytest.mark.parametrize("source", ["{{ start.topic + '!' }}", "{{ start.topic - 1 }}", "{{ start.topic / 2 }}"])
def test_plus_minus_div_on_non_numbers_are_rejected(source):
    with pytest.raises(SecurityError):
        render(source)


def test_huge_int_multiplication_is_rejected():
    with pytest.raises(SecurityError):
        render("{{ 3**2000 * 3**2000 }}")


def test_single_huge_power_still_works():
    # bit_length(3) * 2000 == 2 * 2000 == 4000 <= MAX_INT_BITS, so this alone is fine.
    assert render("{{ 3 ** 2000 }}") == str(3**2000)


def test_attribute_access_on_non_mapping_is_undefined():
    with pytest.raises(UndefinedError):
        render("{{ start.topic.upper }}")


def test_string_key_access_on_non_mapping_is_undefined():
    with pytest.raises(UndefinedError):
        render("{{ start.topic['upper'] }}")


def test_method_attribute_on_list_is_undefined():
    with pytest.raises(UndefinedError):
        render("{{ start.lst.append }}")


class TestJsonValue:
    def test_returns_an_equal_deep_copy_not_the_same_object(self):
        value = {"a": [1, 2, {"b": "c"}]}
        copy = json_value(value)
        assert copy == value
        assert copy is not value
        assert copy["a"] is not value["a"]
        assert copy["a"][2] is not value["a"][2]

    def test_tuple_becomes_list(self):
        assert json_value((1, 2, 3)) == [1, 2, 3]
        assert isinstance(json_value((1, 2, 3)), list)

    def test_nested_undefined_fails(self):
        with pytest.raises(UndefinedError):
            json_value({"a": ENV.undefined(name="x")})

    def test_non_finite_float_fails(self):
        with pytest.raises(ValueError):
            json_value(float("inf"))
        with pytest.raises(ValueError):
            json_value(float("nan"))

    def test_non_str_key_fails(self):
        with pytest.raises(TypeError):
            json_value({1: "a"})

    def test_unsupported_type_fails(self):
        with pytest.raises(TypeError):
            json_value(object())

    def test_size_budget_stops_early(self):
        huge = "x" * (1024 * 1024)
        data = [huge] * 10_000
        start = time.monotonic()
        with pytest.raises(SecurityError):
            json_value(data)
        assert time.monotonic() - start < 1.0


def test_tojson_over_limit_raises():
    with pytest.raises(SecurityError):
        ENV.from_string("{{ v | tojson }}").render(v=["x" * 1000] * 2000)


def test_printing_non_json_value_raises():
    # _finalize routes every non-str value through json_value(); an unsupported Python type
    # raises TypeError there (SecurityError is reserved for the size cap).
    with pytest.raises(TypeError):
        ENV.from_string("{{ v }}").render(v=object())
