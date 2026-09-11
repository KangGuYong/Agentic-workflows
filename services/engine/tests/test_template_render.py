import tracemalloc

import pytest

from engine.errors import ErrorCode
from engine.templates.env import ENV, MAX_OUTPUT_CHARS
from engine.templates.parser import parse_template
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


# --- 1. Only TemplateRenderError escapes, even for constructs that would otherwise crash the
# Python compiler (SyntaxError/RecursionError/IndentationError) or overflow str() on a huge int. ---

_ESCAPE_CTX = {"a": {"n": 1, "s": "x", "t": True, "b": {}, "l": [1, 2]}}
_UNARY = "{{ " + "-" * 400 + "a.n }}"
_FILTER_CHAIN = "{{ a.s" + " | upper" * 200 + " }}"
_LONG_PATH = "{{ a" + ".b" * 200 + " }}"
_NESTED_IF = "{% if a.t %}" * 120 + "x" + "{% endif %}" * 120
_HUGE_DEFAULT = "{{ a.nope | default(3**2000 * 3**2000 * 3**2000 * 3**2000 * 3**2000) }}"


@pytest.mark.parametrize("source", [_UNARY, _FILTER_CHAIN, _LONG_PATH, _NESTED_IF, _HUGE_DEFAULT])
def test_only_template_render_error_escapes_for_pathological_templates(source):
    with pytest.raises(TemplateRenderError):
        render_template(source, _ESCAPE_CTX, "string")


# --- 2. Property test: whatever the parser lets through must actually compile. This pins
# MAX_NESTING_DEPTH comfortably below the depths where Python's own compiler gives up. ---

_SHAPES = {
    "unary": lambda k: "{{ " + "-" * k + "a.n }}",
    "filter_chain": lambda k: "{{ a.s" + " | upper" * k + " }}",
    "path": lambda k: "{{ a" + ".b" * k + " }}",
    "nested_if": lambda k: "{% if a.t %}" * k + "x" + "{% endif %}" * k,
    "nested_if_in_loop": lambda k: (
        "{% for x in a.l %}" + "{% if a.t %}" * k + "x" + "{% endif %}" * k + "{% endfor %}"
    ),
    "nested_default": lambda k: "{{ " + "a.n | default(" * k + "a.n" + ")" * k + " }}",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_whatever_the_parser_accepts_actually_compiles(shape):
    build = _SHAPES[shape]
    for k in range(1, 121):
        source = build(k)
        try:
            parsed = parse_template(source)
        except Exception:  # a raised parse error is itself a rejection, nothing left to check
            continue
        if not parsed.problems:
            ENV.from_string(source)  # must not raise


# --- 3. Whole-value size cap: exact boundary, and container amplification. ---


def test_whole_value_string_at_exact_cap_passes():
    value = "x" * MAX_OUTPUT_CHARS
    assert render_template("{{ a.s }}", {"a": {"s": value}}, "string") == value


def test_whole_value_string_one_over_cap_raises():
    value = "x" * (MAX_OUTPUT_CHARS + 1)
    with pytest.raises(TemplateRenderError):
        render_template("{{ a.s }}", {"a": {"s": value}}, "string")


def test_whole_value_container_over_cap_raises():
    context = {"a": {"s": ["x" * 1000] * 1001}}
    with pytest.raises(TemplateRenderError):
        render_template("{{ a.s }}", context, "any")


# --- 4. Chained tojson cannot amplify output without bound, and stays cheap in memory. ---


def test_tojson_chain_is_rejected_and_memory_bounded():
    source = "{{ a.x | default(a.q" + " | tojson" * 30 + ") }}"
    context = {"a": {"q": '"'}}
    tracemalloc.start()
    try:
        with pytest.raises(TemplateRenderError):
            render_template(source, context, "string")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 * 1024 * 1024


# --- 5. A list literal referencing the same large value many times cannot bypass the streamed
# cap by building the whole thing in one chunk before it is ever streamed out. ---


def test_list_literal_amplification_is_rejected_streamed():
    big = "x" * 10_000
    context = {"a": {"s": big}}
    refs = ", ".join(["a.s"] * 200)
    source = "v={{ [" + refs + "] }}"
    tracemalloc.start()
    try:
        with pytest.raises(TemplateRenderError):
            render_template(source, context, "string")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024


def test_list_literal_amplification_is_rejected_whole_value():
    big = "x" * 10_000
    context = {"a": {"s": big}}
    refs = ", ".join(["a.s"] * 200)
    source = "{{ a.nope | default([" + refs + "]) }}"
    tracemalloc.start()
    try:
        with pytest.raises(TemplateRenderError):
            render_template(source, context, "any")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024


# --- 6. `~` is rejected outright; `+`/`-`/`*`/etc. are numbers-only. ---


def test_concat_operator_is_rejected():
    with pytest.raises(TemplateRenderError):
        render_template("{{ a.s ~ a.s }}", {"a": {"s": "x"}}, "string")


def test_plus_on_strings_is_rejected():
    with pytest.raises(TemplateRenderError):
        render_template("{{ a.s + a.s }}", {"a": {"s": "x"}}, "string")


def test_plus_on_numbers_renders():
    assert render_template("{{ a.n + 1 }}", {"a": {"n": 1}}, "string") == "2"


# --- 7. Only JSON data flows through: no bound methods, no inf, tuples/dicts get JSON-ified or
# rejected the moment they are not representable. ---


def test_method_via_dot_access_is_rejected_not_a_type_mismatch():
    with pytest.raises(TemplateRenderError) as exc_info:
        render_template("{{ start.topic.upper }}", CTX, "any")
    assert not isinstance(exc_info.value, TemplateTypeError)


def test_method_via_bracket_access_is_rejected_not_a_type_mismatch():
    with pytest.raises(TemplateRenderError) as exc_info:
        render_template("{{ start['topic']['upper'] }}", CTX, "any")
    assert not isinstance(exc_info.value, TemplateTypeError)


def test_infinite_default_value_is_rejected():
    with pytest.raises(TemplateRenderError):
        render_template("{{ x.nope | default(1e308 * 10) }}", {}, "any")


def test_tuple_default_value_becomes_a_json_list():
    assert render_template("{{ x.nope | default((1, 2)) }}", {}, "any") == [1, 2]


def test_dict_default_value_with_undefined_member_is_a_template_error():
    with pytest.raises(TemplateRenderError) as exc_info:
        render_template("{{ x.nope | default({'a': llm_9.x}) }}", {}, "object")
    assert exc_info.value.code == ErrorCode.TEMPLATE_ERROR


# --- 8. A whole-value result is a copy: it equals the context value but is never the same
# object, so a node mutating its own config can never reach back into upstream outputs. ---


def test_whole_value_result_is_a_copy_not_an_alias():
    data = {"k": [1, 2]}
    context = {"llm_1": {"data": data}}
    result = render_template("{{ llm_1.data }}", context, "any")
    assert result == data
    assert result is not data
    assert result["k"] is not data["k"]
    result["k"].append(3)
    assert data == {"k": [1, 2]}


# --- 9. Error codes: a missing reference is TEMPLATE_ERROR, a type mismatch is TYPE_MISMATCH. ---


def test_missing_reference_has_template_error_code():
    with pytest.raises(TemplateRenderError) as exc_info:
        render_template("{{ llm_9.text }}", CTX, "any")
    assert exc_info.value.code == ErrorCode.TEMPLATE_ERROR


def test_type_mismatch_has_type_mismatch_code():
    with pytest.raises(TemplateTypeError) as exc_info:
        render_template("{{ start.topic }}", CTX, "number")
    assert exc_info.value.code == ErrorCode.TYPE_MISMATCH


# --- 10. Falsy values are not treated as missing. ---

_FALSY_CTX = {"a": {"v": None, "f": False, "e": ""}}


def test_none_renders_empty_string_for_string_target_and_none_for_any():
    assert render_template("{{ a.v }}", _FALSY_CTX, "string") == ""
    assert render_template("{{ a.v }}", _FALSY_CTX, "any") is None


def test_falsy_values_interpolate_inline():
    result = render_template("[{{ a.v }}|{{ a.f }}|{{ a.e }}]", _FALSY_CTX, "string")
    assert result == "[|false|]"


def test_false_survives_any_target():
    assert render_template("{{ a.f }}", _FALSY_CTX, "any") is False


def test_default_only_replaces_undefined_not_falsy():
    # Jinja's `default` filter only substitutes when the value is undefined, not when it is falsy.
    assert render_template("{{ a.v | default('d') }}", _FALSY_CTX, "string") == ""


# --- 11. A context key named "self" (e.g. a node id) must not collide with the bound-method
# `self` parameter of `Template.generate`/`TemplateExpression.__call__` when the context dict is
# unpacked as kwargs. Passing the context positionally (`template.generate(context)`) fixes this
# for every template, whether or not it happens to mention "self".
#
# Note: `{{ self.a }}` itself can still never read a context value named "self" - Jinja2's own
# compiler hard-reserves the *template-source* identifier "self" for block introspection
# (`jinja2/compiler.py`: `if "self" in find_undeclared(...): ref = frame.symbols.declare_parameter
# ("self"); ... TemplateReference(context)`), unconditionally shadowing any context variable of
# that name before our environment ever gets a chance to resolve it. That is a structural Jinja2
# limitation, not something reachable from this fix.


def test_context_key_named_self_does_not_crash_rendering():
    context = {"self": {"a": 1}, "other": {"b": 2}}
    assert render_template("{{ other.b }}", context, "any") == 2


# --- 12. `loop.index` still works, and negative indexing into a list still works. ---


def test_loop_index_and_negative_list_indexing():
    context = {"llm_1": {"data": {"k": [1, 2]}}}
    result = render_template(
        "{% for x in llm_1.data.k %}{{ loop.index }}:{{ x }} {% endfor %}", context, "string"
    )
    assert result == "1:1 2:2 "
    assert render_template("{{ llm_1.data.k[-1] }}", context, "string") == "2"
