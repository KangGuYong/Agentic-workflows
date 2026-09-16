import pytest
from pydantic import ValidationError

from engine.dsl.models import Policy, RetrySpec, WorkflowDSL, dsl_hash, merge_policy
from engine.errors import ErrorCode, NodeError


def _dsl(**overrides) -> dict:
    base = {
        "nodes": [
            {"id": "start", "type": "start", "label": "시작", "position": {"x": 0, "y": 0}},
            {"id": "end", "type": "end", "config": {"outputs": {"r": "{{start.x}}"}}},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "end"}],
    }
    base.update(overrides)
    return base


def test_parses_minimal_dsl_with_defaults():
    dsl = WorkflowDSL.model_validate(_dsl())
    assert dsl.version == "1"
    assert dsl.settings.storeRunData is True
    assert dsl.edges[0].sourceHandle == "out"
    assert dsl.nodes[1].policy is None


@pytest.mark.parametrize("bad_id", ["Start", "1abc", "a-b", "x" * 41, ""])
def test_rejects_invalid_node_ids(bad_id):
    raw = _dsl()
    raw["nodes"][0]["id"] = bad_id
    with pytest.raises(ValidationError):
        WorkflowDSL.model_validate(raw)


def test_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        WorkflowDSL.model_validate(_dsl(extra=1))


def test_rejects_max_iterations_out_of_range():
    raw = _dsl()
    raw["edges"][0]["maxIterations"] = 21
    with pytest.raises(ValidationError):
        WorkflowDSL.model_validate(raw)


def test_hash_ignores_position_and_label_but_not_config():
    base = WorkflowDSL.model_validate(_dsl())
    moved = _dsl()
    moved["nodes"][0]["position"] = {"x": 500, "y": 1}
    moved["nodes"][0]["label"] = "다른 이름"
    changed = _dsl()
    changed["nodes"][1]["config"] = {"outputs": {"r": "{{start.y}}"}}
    assert dsl_hash(base) == dsl_hash(WorkflowDSL.model_validate(moved))
    assert dsl_hash(base) != dsl_hash(WorkflowDSL.model_validate(changed))


def test_merge_policy_overrides_only_given_fields():
    default = Policy(timeoutSec=120, retry=RetrySpec(maxAttempts=3))
    merged = merge_policy(default, {"timeoutSec": 30, "retry": {"maxAttempts": 1}})
    assert merged.timeoutSec == 30
    assert merged.retry.maxAttempts == 1
    assert merged.retry.backoff == "exponential"
    assert merged.onError == "fail"


def test_merge_policy_without_override_returns_default():
    default = Policy()
    assert merge_policy(default, None) is default


def test_merge_policy_rejects_invalid_values():
    with pytest.raises(ValidationError):
        merge_policy(Policy(), {"retry": {"maxAttempts": 9}})


def test_node_error_serializes_code_and_message():
    err = NodeError(ErrorCode.NODE_TIMEOUT, "시간 초과", retryable=True)
    assert err.to_dict() == {"code": "NODE_TIMEOUT", "message": "시간 초과"}
    assert err.retryable is True
