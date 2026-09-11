import pytest
from jinja2.exceptions import SecurityError, UndefinedError

from engine.templates.env import ENV

CTX = {
    "llm_1": {"data": {"items": [{"title": "T"}], "keys": "k", "count": 3}},
    "start": {"lst": [1, 2], "topic": "AI"},
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


def test_finalize_and_tojson():
    assert render("{{ start.lst }} {{ true }} {{ none }}") == "[1, 2] true "
    assert ENV.from_string("{{ v | tojson }}").render(v={"k": "값"}) == '{"k": "값"}'
    with pytest.raises(UndefinedError):
        render("{{ missing | tojson }}")


@pytest.mark.parametrize(
    "source",
    ["{{ 'a' * 1000000 }}", "{{ start.topic * 100000 }}", "{{ 2 ** 1000 }}", "{{ start.lst * 60000 }}"],
)
def test_size_capped_operators(source):
    with pytest.raises(SecurityError):
        render(source)


def test_small_repetition_is_allowed():
    assert render("{{ '-' * 3 }}{{ 2 ** 3 }}") == "---8"


@pytest.mark.parametrize("source", ["{{ start['__class__'] }}", "{{ start.lst.__class__ }}"])
def test_sandbox_blocks_dunder_access(source):
    with pytest.raises(SecurityError):
        render(source)


def test_immutable_sandbox_blocks_mutation():
    with pytest.raises(SecurityError):
        render("{{ start.lst.append(3) }}")
