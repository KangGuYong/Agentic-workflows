import pytest

from engine.errors import NodeError
from engine.nodes.human_approval import HumanApprovalNode
from engine.nodes.merge import MergeNode
from engine.nodes.registry import default_registry
from tests.helpers import make_ctx

OUTPUTS = {"llm_1": {"text": "a"}, "llm_2": {"text": "b"}}


async def test_merge_object_follows_pred_order():
    spec = MergeNode()
    config = spec.parse_config({})

    result = await spec.execute(make_ctx(outputs=OUTPUTS, pred_ids=["llm_2", "llm_1"]), config, {})

    assert list(result.output["branches"]) == ["llm_2", "llm_1"]
    schema = spec.output_schema(config, {"llm_2": {"type": "object"}, "llm_1": {"type": "object"}})
    assert list(schema["properties"]["branches"]["properties"]) == ["llm_2", "llm_1"]


async def test_merge_list():
    spec = MergeNode()
    config = spec.parse_config({"mode": "list"})
    result = await spec.execute(make_ctx(outputs=OUTPUTS, pred_ids=["llm_2", "llm_1"]), config, {})
    assert result.output == {"branches": [{"text": "b"}, {"text": "a"}]}


async def test_human_approval_interrupt_payload_and_output():
    captured: dict = {}

    async def fake_interrupt(payload: dict) -> dict:
        captured.update(payload)
        return {"decision": "approve", "comment": "좋아요", "editedValue": "수정본", "reviewedAt": "2026-09-11T00:00:00Z"}

    spec = HumanApprovalNode()
    config = spec.parse_config({"message": "검토해 주세요", "review": "{{start.draft}}", "allowEdit": True})
    ctx = make_ctx(node_id="human_approval_1", interrupt=fake_interrupt)

    result = await spec.execute(ctx, config, {"message": "검토해 주세요", "review": "초안"})

    assert captured == {
        "nodeId": "human_approval_1",
        "execIndex": 1,
        "message": "검토해 주세요",
        "review": "초안",
        "allowEdit": True,
    }
    assert result.output == {
        "decision": "approve",
        "comment": "좋아요",
        "editedValue": "수정본",
        "reviewedAt": "2026-09-11T00:00:00Z",
    }
    assert spec.route(config, result.output) == "approve"


async def test_human_approval_defaults_edited_value_to_review():
    async def fake_interrupt(payload: dict) -> dict:
        return {"decision": "reject"}

    spec = HumanApprovalNode()
    config = spec.parse_config({"message": "검토", "review": "{{start.draft}}"})

    result = await spec.execute(make_ctx(interrupt=fake_interrupt), config, {"message": "검토", "review": "초안"})

    assert result.output["editedValue"] == "초안"
    assert result.output["comment"] == ""
    assert result.output["reviewedAt"]
    assert spec.route(config, result.output) == "reject"


@pytest.mark.parametrize(
    ("answer", "allow_edit"),
    [
        ({"decision": "maybe"}, False),
        ("yes", False),
        ({"decision": "approve", "editedValue": "x"}, False),
        ({"decision": "approve", "comment": {"x": 1}}, False),
        ({"decision": "approve", "comment": "c" * 10_001}, False),
        ({"decision": "approve", "reviewedAt": 12345}, False),
        ({"decision": "approve", "extra": "?"}, False),
        ({"decision": "approve", "editedValue": float("nan")}, True),
        ({"decision": "approve", "editedValue": {1, 2}}, True),
    ],
    ids=["decision", "not-object", "edit-not-allowed", "comment-type", "comment-length", "reviewed-at-type",
         "unknown-key", "edited-nan", "edited-not-json"],
)
async def test_human_approval_rejects_invalid_answers(answer, allow_edit):
    async def fake_interrupt(payload: dict):
        return answer

    spec = HumanApprovalNode()
    config = spec.parse_config({"message": "검토", "allowEdit": allow_edit})
    with pytest.raises(NodeError):
        await spec.execute(make_ctx(interrupt=fake_interrupt), config, {"message": "검토"})


def test_registry_has_all_mvp_nodes():
    assert {spec.type for spec in default_registry().all()} == {
        "start", "end", "template", "llm", "classifier", "condition", "merge", "human_approval",
    }


async def test_merge_does_not_share_upstream_outputs():
    spec = MergeNode()
    outputs = {"llm_1": {"items": [1]}}

    result = await spec.execute(make_ctx(outputs=outputs, pred_ids=["llm_1"]), spec.parse_config({}), {})
    result.output["branches"]["llm_1"]["items"].append(2)

    assert outputs == {"llm_1": {"items": [1]}}


async def test_merge_reports_a_missing_predecessor_output():
    spec = MergeNode()
    with pytest.raises(NodeError):
        await spec.execute(make_ctx(outputs={}, pred_ids=["llm_1"]), spec.parse_config({}), {})
