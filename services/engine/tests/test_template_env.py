import pytest
from jinja2.exceptions import SecurityError, UndefinedError

from engine.templates.env import ENV, MAX_OUTPUT_CHARS

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
    with pytest.raises(SecurityError):
        render("{{ start.lst.__class__ }}")


def test_immutable_sandbox_blocks_mutation():
    with pytest.raises(SecurityError):
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
