import pytest

from engine.validator import analyze, validate

INPUTS = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
START = {"id": "start", "type": "start", "config": {"inputs": INPUTS}}
SCORE_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["score", "reason"],
}


def _codes(raw: dict) -> list[tuple[str, str]]:
    return [(issue.severity, issue.code) for issue in validate(raw)]


def chain(prompt: str, end_output: str = "{{llm_1.text}}") -> dict:
    return {
        "nodes": [
            START,
            {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": prompt}},
            {"id": "end", "type": "end", "config": {"outputs": {"r": end_output}}},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "llm_1"}, {"id": "e2", "source": "llm_1", "target": "end"}],
    }


def routing(end_output: str) -> dict:
    categories = [{"id": "a", "description": "A"}, {"id": "b", "description": "B"}]
    return {
        "nodes": [
            START,
            {"id": "classifier_1", "type": "classifier",
             "config": {"model": "m", "input": "{{start.topic}}", "categories": categories}},
            {"id": "llm_a", "type": "llm", "config": {"model": "m", "prompt": "A"}},
            {"id": "llm_b", "type": "llm", "config": {"model": "m", "prompt": "B"}},
            {"id": "end", "type": "end", "config": {"outputs": {"r": end_output}}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "classifier_1"},
            {"id": "e2", "source": "classifier_1", "sourceHandle": "a", "target": "llm_a"},
            {"id": "e3", "source": "classifier_1", "sourceHandle": "b", "target": "llm_b"},
            {"id": "e4", "source": "classifier_1", "sourceHandle": "default", "target": "end"},
            {"id": "e5", "source": "llm_a", "target": "end"},
            {"id": "e6", "source": "llm_b", "target": "end"},
        ],
    }


def parallel(end_output: str) -> dict:
    return {
        "nodes": [
            START,
            {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "1"}},
            {"id": "llm_2", "type": "llm", "config": {"model": "m", "prompt": "2"}},
            {"id": "merge_1", "type": "merge"},
            {"id": "end", "type": "end", "config": {"outputs": {"r": end_output}}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "llm_1"},
            {"id": "e2", "source": "start", "target": "llm_2"},
            {"id": "e3", "source": "llm_1", "target": "merge_1"},
            {"id": "e4", "source": "llm_2", "target": "merge_1"},
            {"id": "e5", "source": "merge_1", "target": "end"},
        ],
    }


def condition(left: str, right: str, op: str = ">=") -> dict:
    return {
        "nodes": [
            START,
            {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "p", "outputSchema": SCORE_SCHEMA}},
            {"id": "condition_1", "type": "condition",
             "config": {"conditions": [{"left": left, "op": op, "right": right}]}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "llm_1"},
            {"id": "e2", "source": "llm_1", "target": "condition_1"},
            {"id": "e3", "source": "condition_1", "sourceHandle": "true", "target": "end"},
            {"id": "e4", "source": "condition_1", "sourceHandle": "false", "target": "end"},
        ],
    }


def test_valid_chain_and_analysis():
    raw = chain("{{start.topic}}에 대해 써줘")
    assert validate(raw) == []
    analysis = analyze(raw)
    assert analysis.graph is not None
    assert analysis.graph.order == ["start", "llm_1", "end"]


def test_malformed_dsl():
    assert _codes({"nodes": "x", "edges": []})[0] == ("error", "DSL_INVALID")


def test_phases_stop_at_first_failing_phase():
    raw = chain("{{llm_9.text}}")
    raw["nodes"].append({"id": "x_1", "type": "magic"})
    assert _codes(raw) == [("error", "UNKNOWN_NODE_TYPE")]


def test_reference_errors():
    assert ("error", "REF_UNKNOWN_NODE") in _codes(chain("{{llm_9.text}}"))
    assert ("error", "REF_UNKNOWN_NODE") in _codes(chain("{{end.r}}"))
    assert ("error", "REF_UNKNOWN_FIELD") in _codes(chain("{{start.nope}}"))
    assert ("error", "SECRET_NOT_ALLOWED") in _codes(chain("{{secret.API_KEY}}"))
    assert ("error", "TEMPLATE_SYNTAX") in _codes(chain("{{ start. }}"))
    assert ("error", "TEMPLATE_FORBIDDEN") in _codes(chain("{{ start.topic | safe }}"))


def test_self_reference_requires_default():
    assert ("error", "REF_NOT_GUARANTEED") in _codes(chain("{{llm_1.text}}"))
    assert validate(chain("이전: {{llm_1.text | default('')}}")) == []


def test_object_interpolation_warns():
    assert _codes(chain("입력 전체: {{start}}")) == [("warning", "TYPE_WARNING")]
    assert validate(chain("입력 전체: {{start | tojson}}")) == []


def test_branch_only_reference_requires_default():
    assert ("error", "REF_NOT_GUARANTEED") in _codes(routing("{{llm_a.text}}"))
    assert validate(routing("{{llm_a.text | default('')}}{{llm_b.text | default('')}}")) == []
    assert validate(routing("{{classifier_1.category}}")) == []


def test_merge_guarantees_all_branches():
    assert validate(parallel("{{llm_1.text}} {{llm_2.text}}")) == []
    assert validate(parallel("{{merge_1.branches.llm_1.text}}")) == []
    assert ("error", "REF_UNKNOWN_FIELD") in _codes(parallel("{{merge_1.branches.llm_3.text}}"))


def test_condition_operand_types():
    assert validate(condition("{{llm_1.score}}", "8")) == []
    assert ("error", "TYPE_INCOMPATIBLE") in _codes(condition("{{llm_1.reason}}", "8"))
    assert ("error", "LITERAL_NOT_NUMBER") in _codes(condition("{{llm_1.score}}", "여덟"))
    assert ("warning", "TYPE_WARNING") in _codes(condition("점수 {{llm_1.score}}", "8"))
    assert ("error", "TYPE_INCOMPATIBLE") in _codes(condition("{{llm_1.score}}", "x", op="contains"))


def test_dsl_problems_read_as_workflow_format_errors():
    issues = validate({"nodes": "x", "edges": []})
    assert issues[0].message.startswith("워크플로 형식 오류: ")


@pytest.mark.parametrize("prompt", ["a\x00b", "\ud800"], ids=["nul", "surrogate"])
def test_text_that_cannot_be_stored_is_rejected(prompt):
    assert _codes(chain(prompt)) == [("error", "DSL_INVALID")]


def test_config_nested_too_deeply_is_rejected():
    nested: dict = {}
    for _ in range(5000):
        nested = {"x": nested}
    raw = chain("p")
    raw["nodes"][1]["config"]["extra"] = nested
    issues = validate(raw)
    assert [(issue.code, issue.message) for issue in issues] == [("DSL_INVALID", "워크플로 형식 오류: 값의 중첩이 너무 깊습니다")]


@pytest.mark.parametrize("literal", ["nan", "inf", "1e400"])
def test_number_literals_must_be_finite(literal):
    assert ("error", "LITERAL_NOT_NUMBER") in _codes(condition("{{llm_1.score}}", literal))


def test_quoted_references_and_literals_are_clipped():
    long_ref = validate(chain("{{ start.%s }}" % ("x" * 19_000)))
    long_literal = validate(condition("{{llm_1.score}}", "x" * 19_000))
    assert [issue.code for issue in long_ref] == ["REF_UNKNOWN_FIELD"]
    assert ("error", "LITERAL_NOT_NUMBER") in [(issue.severity, issue.code) for issue in long_literal]
    assert all(len(issue.message) < 300 for issue in long_ref + long_literal)


def test_issues_are_deduplicated_and_capped():
    assert _codes(chain(" ".join("{{ llm_9.t }}" for _ in range(500)))) == [("error", "REF_UNKNOWN_NODE")]
    assert len(validate(chain(" ".join("{{ llm_%d.t }}" % i for i in range(500))))) == 100


def test_analyze_shares_one_schema_step_budget(monkeypatch):
    import engine.validator as facade

    budgets = []
    real = facade.check_structure

    def spy(dsl, registry, budget=None):
        budgets.append(budget)
        return real(dsl, registry, budget)

    monkeypatch.setattr(facade, "check_structure", spy)
    analyze(chain("p"))
    assert len(budgets) == 1 and budgets[0] is not None


def while_loop(llm_prompt: str, left: str) -> dict:
    return {
        "nodes": [
            START,
            {"id": "condition_1", "type": "condition",
             "config": {"conditions": [{"left": left, "op": "is_empty"}]}},
            {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": llm_prompt}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "condition_1"},
            {"id": "e2", "source": "condition_1", "sourceHandle": "true", "target": "end"},
            {"id": "back", "source": "condition_1", "sourceHandle": "false", "target": "llm_1", "maxIterations": 2},
            {"id": "e3", "source": "llm_1", "target": "condition_1"},
        ],
    }


def test_loop_that_starts_at_its_condition_guarantees_what_ran_before_the_condition():
    assert validate(while_loop("{{start.topic}} 다시 {{condition_1.result}}", "{{start.topic}}")) == []


def test_loop_body_output_is_not_guaranteed_at_the_condition():
    assert ("error", "REF_NOT_GUARANTEED") in _codes(while_loop("p", "{{llm_1.text}}"))
    assert validate(while_loop("p", "{{llm_1.text | default('')}}")) == []


def _start_with(properties: dict, end_output: str, prompt: str = "p") -> dict:
    raw = chain(prompt, end_output)
    raw["nodes"][0] = {"id": "start", "type": "start", "config": {"inputs": {"type": "object", "properties": properties}}}
    return raw


def test_errors_are_never_crowded_out_by_warnings():
    properties = {f"o{i}": {"type": "object"} for i in range(120)}
    prompt = " ".join(f"{{{{ start.o{i} }}}}" for i in range(120))
    analysis = analyze(_start_with(properties, "{{ llm_1.nope }}", prompt))
    codes = [issue.code for issue in analysis.issues]
    assert len(codes) == 100
    assert codes[0] == "REF_UNKNOWN_FIELD"


@pytest.mark.parametrize(
    ("properties", "reference"),
    [
        ({"a": True}, "{{ start.a }}"),
        ({"a": {"type": "array", "items": True}}, "{{ start.a.0 }}"),
        ({"a": {"anyOf": [True, {"type": "null"}]}}, "{{ start.a }}"),
        ({"a": False}, "{{ start.a | default('') }}"),
    ],
    ids=["true-property", "true-items", "true-branch", "false-property"],
)
def test_boolean_subschemas_do_not_crash_validation(properties, reference):
    validate(_start_with(properties, reference))


def number_condition(left: str) -> dict:
    raw = condition(left, "1")
    raw["nodes"][0] = {"id": "start", "type": "start", "config": {"inputs": {
        "type": "object", "properties": {"n": {"type": ["number", "null"]}, "free": {}}}}}
    return raw


def test_default_keeps_null_unless_it_replaces_empty_values():
    assert ("warning", "TYPE_WARNING") in _codes(number_condition("{{ start.n | default(0) }}"))
    assert validate(number_condition("{{ start.n | default(0, true) }}")) == []


def test_default_value_type_is_checked():
    assert ("error", "TYPE_INCOMPATIBLE") in _codes(number_condition("{{ start.n | default('', true) }}"))


def test_whole_value_of_unknown_type_warns():
    assert _codes(number_condition("{{ start.free }}")) == [("warning", "TYPE_WARNING")]


def test_field_access_after_default_is_forbidden():
    assert ("error", "TEMPLATE_FORBIDDEN") in _codes(routing("{{ (llm_a | default({})).text }}"))


def json_template(template: str) -> dict:
    raw = chain("p")
    raw["nodes"][1] = {"id": "template_1", "type": "template", "config": {"template": template, "format": "json"}}
    raw["edges"] = [{"id": "e1", "source": "start", "target": "template_1"},
                    {"id": "e2", "source": "template_1", "target": "end"}]
    raw["nodes"][2] = {"id": "end", "type": "end", "config": {"outputs": {"r": "{{ template_1.data }}"}}}
    return raw


def test_json_templates_are_checked_before_running():
    assert _codes(json_template("{a: 1}")) == [("error", "TEMPLATE_SYNTAX")]
    assert _codes(json_template('{"a": "{{ start.topic }}"}')) == [("warning", "TYPE_WARNING")]
    assert validate(json_template('{"a": {{ start.topic }}, "b": [1, 2]}')) == []
    assert validate(json_template('{"a": 1}')) == []


def test_unknown_node_names_are_clipped():
    issues = validate(chain("{{ %s.text }}" % ("x" * 19_000)))
    assert [issue.code for issue in issues] == ["REF_UNKNOWN_NODE"]
    assert len(issues[0].message) < 200


def test_self_reference_message_explains_the_first_run():
    issues = validate(chain("{{ llm_1.text }}"))
    assert issues[0].message.startswith("이 노드의 이전 실행 결과는 첫 실행 때 없습니다")


def test_loop_body_referencing_itself_needs_default_even_in_a_while_loop():
    assert ("error", "REF_NOT_GUARANTEED") in _codes(while_loop("{{ llm_1.text }}", "{{ start.topic }}"))


def test_graph_is_only_set_after_phases_one_and_two_pass():
    raw = chain("p")
    raw["edges"].pop()
    assert analyze(raw).graph is None
    assert analyze(chain("{{ llm_9.text }}")).graph is not None


def test_oversized_workflows_are_rejected():
    raw = chain("x" * 19_000)
    raw["nodes"] += [
        {"id": f"template_{i}", "type": "template", "config": {"template": "y" * 19_000}} for i in range(30)
    ]
    assert _codes(raw) == [("error", "LIMIT_EXCEEDED")]


def test_json_template_with_only_a_comment_is_not_parsed_as_a_literal():
    assert validate(json_template('{"a": 1} {# note #}')) == []


def test_size_limit_ignores_filled_in_defaults():
    raw = chain("p")
    raw["nodes"] += [{"id": f"template_{i}", "type": "template", "config": {"template": "y" * 16_000}} for i in range(31)]
    raw["edges"] += [{"id": f"x{i}", "source": "llm_1", "target": f"template_{i}"} for i in range(250)]
    assert ("error", "LIMIT_EXCEEDED") not in _codes(raw)


def test_multibyte_text_counts_in_bytes():
    raw = chain("가" * 19_000)
    raw["nodes"] += [{"id": f"template_{i}", "type": "template", "config": {"template": "나" * 19_000}} for i in range(9)]
    assert _codes(raw) == [("error", "LIMIT_EXCEEDED")]
