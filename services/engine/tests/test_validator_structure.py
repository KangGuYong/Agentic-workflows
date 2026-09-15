import pytest

from engine.dsl.models import WorkflowDSL
from engine.nodes.registry import default_registry
from engine.validator.structure import check_structure


def _base() -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "p"}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "llm_1"},
            {"id": "e2", "source": "llm_1", "target": "end"},
        ],
    }


def _codes(raw: dict) -> list[str]:
    issues, _ = check_structure(WorkflowDSL.model_validate(raw), default_registry())
    return [issue.code for issue in issues]


def test_valid_structure_parses_configs_and_effective_policies():
    issues, parsed = check_structure(WorkflowDSL.model_validate(_base()), default_registry())
    assert issues == []
    assert parsed["llm_1"].policy.retry.maxAttempts == 3
    assert parsed["llm_1"].config.model == "m"
    assert parsed["start"].policy is None


def test_partial_policy_override_is_merged():
    raw = _base()
    raw["nodes"][1]["policy"] = {"timeoutSec": 30}
    _, parsed = check_structure(WorkflowDSL.model_validate(raw), default_registry())
    assert parsed["llm_1"].policy.timeoutSec == 30
    assert parsed["llm_1"].policy.retry.maxAttempts == 3


def test_classifier_default_on_error_needs_no_default_output():
    raw = _base()
    raw["nodes"][1] = {
        "id": "classifier_1",
        "type": "classifier",
        "config": {"model": "m", "input": "x", "categories": [{"id": "a", "description": "A"}]},
        "policy": {"onError": "default"},
    }
    raw["edges"] = [
        {"id": "e1", "source": "start", "target": "classifier_1"},
        {"id": "e2", "source": "classifier_1", "sourceHandle": "a", "target": "end"},
        {"id": "e3", "source": "classifier_1", "sourceHandle": "default", "target": "end"},
    ]
    assert _codes(raw) == []


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda r: r["nodes"].append({"id": "llm_1", "type": "template", "config": {"template": "x"}}), "DUPLICATE_NODE_ID"),
        (lambda r: r["nodes"].append({"id": "secret", "type": "template", "config": {"template": "x"}}), "RESERVED_NODE_ID"),
        (lambda r: r["nodes"].append({"id": "end_2", "type": "end"}), "END_COUNT"),
        (lambda r: r["nodes"].append({"id": "x_1", "type": "magic"}), "UNKNOWN_NODE_TYPE"),
        (lambda r: r["nodes"][1].update(config={}), "INVALID_CONFIG"),
        (lambda r: r["nodes"][0].update(policy={"timeoutSec": 5}), "POLICY_NOT_SUPPORTED"),
        (lambda r: r["nodes"][1].update(policy={"retry": {"maxAttempts": 0}}), "INVALID_POLICY"),
        (lambda r: r["nodes"][1].update(policy={"onError": "default"}), "DEFAULT_OUTPUT_REQUIRED"),
        (lambda r: r["nodes"][1].update(policy={"onError": "default", "defaultOutput": {"wrong": 1}}), "INVALID_POLICY"),
        (lambda r: r["edges"].append({"id": "e1", "source": "start", "target": "end"}), "DUPLICATE_EDGE_ID"),
        (lambda r: r["edges"].append({"id": "e9", "source": "llm_1", "target": "ghost"}), "EDGE_UNKNOWN_NODE"),
        (lambda r: r["edges"].append({"id": "e9", "source": "llm_1", "target": "start"}), "EDGE_INTO_START"),
        (lambda r: r["edges"].append({"id": "e9", "source": "llm_1", "sourceHandle": "true", "target": "end"}), "EDGE_UNKNOWN_HANDLE"),
        (lambda r: r["edges"].append({"id": "e9", "source": "end", "target": "llm_1"}), "EDGE_UNKNOWN_HANDLE"),
        (lambda r: r["nodes"].extend({"id": f"template_{i}", "type": "template", "config": {"template": "x"}} for i in range(100)), "LIMIT_EXCEEDED"),
    ],
)
def test_reports_structural_problems(mutate, code):
    raw = _base()
    mutate(raw)
    assert code in _codes(raw)


def test_start_node_must_use_fixed_id():
    raw = _base()
    raw["nodes"][0]["id"] = "begin"
    raw["edges"][0]["source"] = "begin"
    assert "RESERVED_NODE_ID" in _codes(raw)


def test_issue_serialization_omits_empty_locations():
    issues, _ = check_structure(
        WorkflowDSL.model_validate({**_base(), "nodes": _base()["nodes"] + [{"id": "x_1", "type": "magic"}]}),
        default_registry(),
    )
    assert issues[0].to_dict() == {
        "severity": "error",
        "code": "UNKNOWN_NODE_TYPE",
        "message": "알 수 없는 노드 종류입니다: magic",
        "nodeId": "x_1",
    }


@pytest.mark.parametrize("node_id", ["self", "true", "false", "none", "not"])
def test_template_keywords_cannot_be_node_ids(node_id):
    raw = _base()
    raw["nodes"].append({"id": node_id, "type": "template", "config": {"template": "x"}})
    assert "RESERVED_NODE_ID" in _codes(raw)


@pytest.mark.parametrize(
    "default_output",
    [
        {"text": "t", "extra": float("nan")},
        {"text": "a\x00b"},
        {"text": "x" * 1_000_001},
        {"text": "t", "extra": {1, 2}},
    ],
    ids=["nan", "nul", "too-large", "not-json"],
)
def test_default_output_must_be_storable_json(default_output):
    raw = _base()
    raw["nodes"][1]["policy"] = {"onError": "default", "defaultOutput": default_output}
    assert _codes(raw) == ["INVALID_POLICY"]


def test_default_output_nested_too_deeply_is_an_invalid_policy():
    nested: dict = {}
    for _ in range(5000):
        nested = {"x": nested}
    raw = _base()
    raw["nodes"][1]["policy"] = {"onError": "default", "defaultOutput": {"text": "t", "extra": nested}}
    assert _codes(raw) == ["INVALID_POLICY"]


def test_default_output_too_costly_to_validate_is_an_invalid_policy():
    branches = [{"type": "object", "required": [f"z{i}"]} for i in range(15)] + [{"type": "integer"}]
    raw = _base()
    raw["nodes"][1]["config"]["outputSchema"] = {
        "type": "object",
        "properties": {"a": {"type": "array", "items": {"anyOf": branches}}},
        "required": ["a"],
    }
    raw["nodes"][1]["policy"] = {"onError": "default", "defaultOutput": {"a": list(range(100_000))}}
    assert _codes(raw) == ["INVALID_POLICY"]


def test_issues_are_bounded_in_number_and_size():
    raw = _base()
    raw["nodes"][1]["config"].update({f"k{i}": 1 for i in range(1000)})
    raw["nodes"].append({"id": "x_1", "type": "t" * 100_000})
    raw["edges"].append({"id": "e9", "source": "llm_1", "sourceHandle": "h" * 100_000, "target": "end"})
    raw["edges"].extend({"id": f"g{i}", "source": "s" * 10_000, "target": "end"} for i in range(250))

    issues, _ = check_structure(WorkflowDSL.model_validate(raw), default_registry())

    assert len(issues) <= 100
    assert len([i for i in issues if i.code == "INVALID_CONFIG"]) == 10
    assert all(len(str(issue.to_dict())) < 500 for issue in issues)


def test_oversized_workflows_are_rejected_before_checking_nodes():
    raw = _base()
    raw["edges"].extend({"id": f"g{i}", "source": "llm_1", "target": "ghost"} for i in range(400))
    issues, parsed = check_structure(WorkflowDSL.model_validate(raw), default_registry())
    assert ([issue.code for issue in issues], parsed) == (["LIMIT_EXCEEDED"], {})
