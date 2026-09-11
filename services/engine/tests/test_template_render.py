import pytest

from engine.templates.render import TemplateRenderError, TemplateTypeError, render_template

CTX = {
    "start": {"topic": "AI", "n": "3"},
    "llm_1": {"text": "본문", "data": {"k": [1, 2]}},
    "cond": {"result": True},
}


def test_string_interpolation():
    assert render_template("주제: {{ start.topic }}", CTX, "string") == "주제: AI"


def test_objects_interpolate_as_json():
    assert render_template("v={{ llm_1.data }}", CTX, "string") == 'v={"k": [1, 2]}'


def test_whole_value_bool_into_string_field():
    assert render_template("{{ cond.result }}", CTX, "string") == "true"


def test_whole_value_keeps_type():
    assert render_template("{{ llm_1.data }}", CTX, "any") == {"k": [1, 2]}


def test_whole_value_number_coercion():
    assert render_template("{{ start.n }}", CTX, "number") == 3.0


def test_default_for_node_that_did_not_run():
    assert render_template("{{ llm_9.text | default('없음') }}", CTX, "string") == "없음"
    assert render_template("x{{ llm_9.text | default('') }}y", CTX, "string") == "xy"


@pytest.mark.parametrize("source", ["{{ llm_9.text }}", "a {{ llm_9.text }}", "{{ start.nope }}"])
def test_missing_reference_fails(source):
    with pytest.raises(TemplateRenderError):
        render_template(source, CTX, "any")


def test_type_mismatch_is_a_distinct_error():
    with pytest.raises(TemplateTypeError):
        render_template("{{ start.topic }}", CTX, "number")


def test_forbidden_construct_fails():
    with pytest.raises(TemplateRenderError):
        render_template("{{ start.__class__ }}", CTX, "string")


def test_tojson_keeps_unicode():
    assert render_template("{{ llm_1 | tojson }}", CTX, "string") == '{"text": "본문", "data": {"k": [1, 2]}}'


def test_for_loop():
    assert render_template("{% for x in llm_1.data.k %}{{ x }},{% endfor %}", CTX, "string") == "1,2,"


def test_filter_arithmetic_errors_become_render_errors():
    with pytest.raises(TemplateRenderError):
        render_template("{{ start.big | round(15, 'ceil') }}", {"start": {"big": 1e308}}, "string")


def test_rendered_output_is_capped():
    context = {"start": {"items": ["x" * 1000] * 2000}}
    with pytest.raises(TemplateRenderError):
        render_template("{% for x in start.items %}{{ x }}{% endfor %}", context, "string")
