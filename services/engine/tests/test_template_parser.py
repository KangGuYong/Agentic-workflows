import pytest

from engine.templates.parser import Ref, TemplateParseError, parse_template


def test_extracts_refs_with_paths_in_source_order():
    parsed = parse_template("안녕 {{ start.name }}, {{ llm_1.data.items[0].title }}")
    assert [(r.root, r.path) for r in parsed.refs] == [
        ("start", ("name",)),
        ("llm_1", ("data", "items", "0", "title")),
    ]
    assert parsed.whole_value is None
    assert parsed.problems == ()


def test_whole_value_reference():
    parsed = parse_template("{{ llm_1.text }}")
    assert parsed.whole_value == Ref("llm_1", ("text",), has_default=False, direct=True)
    assert parsed.expression == "llm_1.text"


def test_whole_value_with_default():
    parsed = parse_template("{{ llm_2.text | default('') }}")
    assert parsed.whole_value == Ref("llm_2", ("text",), has_default=True, direct=True)
    assert parsed.expression == "llm_2.text | default('')"


def test_dynamic_index_is_not_a_whole_value():
    parsed = parse_template("{{ m.branches[i] }}")
    assert parsed.whole_value is None
    assert [(r.root, r.path) for r in parsed.refs] == [("m", ("branches",)), ("i", ())]


def test_loop_variables_are_not_refs():
    parsed = parse_template("{% for x in merge_1.branches %}{{ x.text }}{{ loop.index }}{% endfor %}")
    assert parsed.refs == (Ref("merge_1", ("branches",), has_default=False, direct=False),)


def test_direct_flag_distinguishes_printed_refs_from_filtered_ones():
    parsed = parse_template("{{ a.obj }} {{ a.obj | tojson }}")
    assert [r.direct for r in parsed.refs] == [True, False]


@pytest.mark.parametrize(
    "source",
    ["{{ x.__class__ }}", "{{ a | safe }}", "{{ range(3) }}", "{% set y = 1 %}", "{% if a is defined %}{% endif %}"],
)
def test_reports_forbidden_constructs(source):
    assert parse_template(source).problems != ()


def test_syntax_error_raises():
    with pytest.raises(TemplateParseError):
        parse_template("{{ a. }}")


def test_plain_text():
    parsed = parse_template("그냥 텍스트")
    assert parsed.refs == ()
    assert parsed.whole_value is None


def test_loop_variable_does_not_hide_a_reference_after_the_loop():
    parsed = parse_template("{% for llm_1 in start.items %}{{ llm_1 }}{% endfor %}{{ llm_1.text }}")
    assert [(r.root, r.path) for r in parsed.refs] == [("start", ("items",)), ("llm_1", ("text",))]


def test_loop_else_is_outside_the_loop_scope():
    parsed = parse_template("{% for x in start.xs %}{{ x }}{% else %}{{ x.y }}{% endfor %}")
    assert [(r.root, r.path) for r in parsed.refs] == [("start", ("xs",)), ("x", ("y",))]


def test_underscore_string_keys_are_forbidden():
    assert parse_template("{{ start['__class__'] }}").problems != ()
    assert parse_template("{{ start['topic'] }}").problems == ()


def test_loop_nesting_limit():
    one = "{% for a in start.x %}{{ a }}{% endfor %}"
    two = "{% for a in start.x %}{% for b in a %}{{ b }}{% endfor %}{% endfor %}"
    assert parse_template(one).problems == ()
    assert parse_template(two).problems != ()


def test_overlong_and_deeply_nested_templates_raise_parse_errors():
    with pytest.raises(TemplateParseError):
        parse_template("x" * 20_001)
    with pytest.raises(TemplateParseError):
        parse_template("{{ " + "(" * 3000 + "1" + ")" * 3000 + " }}")
