import pytest

from engine.errors import ErrorCode, NodeError
from engine.jsondata import MAX_JSON_DEPTH, schema_problems
from engine.nodes.human_approval import HumanApprovalNode, resume_output
from engine.nodes.merge import MergeNode
from engine.nodes.registry import default_registry
from tests.helpers import make_ctx

OUTPUTS = {"llm_1": {"text": "a"}, "llm_2": {"text": "b"}}
WAITING = {"nodeId": "human_approval_1", "execIndex": 1, "message": "검토", "review": "초안", "allowEdit": True}


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
    assert spec.output_schema(config, {"llm_1": {"type": "object"}})["properties"]["branches"] == {
        "type": "array",
        "items": {},
    }


async def test_human_approval_interrupt_payload_and_output():
    captured: dict = {}

    async def fake_interrupt(payload: dict) -> dict:
        captured.update(payload)
        return {"decision": "approve", "comment": "좋아요", "editedValue": "수정본", "reviewedAt": "2026-09-11T09:00:00+09:00"}

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
        "reviewedAt": "2026-09-11T00:00:00+00:00",
    }
    assert spec.route(config, result.output) == "approve"
    assert schema_problems(spec.output_schema(config, {})) == []


async def test_human_approval_defaults_edited_value_to_review():
    async def fake_interrupt(payload: dict) -> dict:
        return {"decision": "reject"}

    spec = HumanApprovalNode()
    config = spec.parse_config({"message": "검토", "review": "{{start.draft}}"})

    result = await spec.execute(make_ctx(interrupt=fake_interrupt), config, {"message": "검토", "review": "초안"})

    assert result.output["editedValue"] == "초안"
    assert result.output["comment"] == ""
    assert result.output["reviewedAt"].endswith("+00:00")
    assert spec.route(config, result.output) == "reject"


@pytest.mark.parametrize(
    ("answer", "allow_edit"),
    [
        ({"decision": "maybe"}, False),
        ("yes", False),
        ({"decision": "approve", "editedValue": "x"}, False),
        ({"decision": "approve", "comment": {"x": 1}}, False),
        ({"decision": "approve", "comment": "c" * 10_001}, False),
        ({"decision": "approve", "comment": "a\x00b"}, False),
        ({"decision": "approve", "reviewedAt": 12345}, False),
        ({"decision": "approve", "reviewedAt": "not-a-date"}, False),
        ({"decision": "approve", "reviewedAt": "2026-09-11T00:00:00"}, False),
        ({"decision": "approve", "reviewedAt": "0001-01-01T00:00:00+01:00"}, False),
        ({"decision": "approve", "extra": "?"}, False),
        ({"decision": "approve", "nodeId": "human_approval_2"}, False),
        ({"decision": "approve", "execIndex": True}, False),
        ({"decision": "approve", "editedValue": float("nan")}, True),
        ({"decision": "approve", "editedValue": {1, 2}}, True),
        ({"decision": "approve", "editedValue": "\ud800"}, True),
        ({"decision": "approve", "editedValue": 123}, True),
    ],
    ids=["decision", "not-object", "edit-not-allowed", "comment-type", "comment-length", "comment-nul",
         "reviewed-at-type", "reviewed-at-format", "reviewed-at-naive", "reviewed-at-overflow", "unknown-key",
         "other-node", "other-exec-index", "edited-nan", "edited-not-json", "edited-surrogate", "edited-type"],
)
async def test_human_approval_rejects_invalid_answers(answer, allow_edit):
    async def fake_interrupt(payload: dict):
        return answer

    spec = HumanApprovalNode()
    config = spec.parse_config({"message": "검토", "review": "{{start.draft}}", "allowEdit": allow_edit})
    ctx = make_ctx(node_id="human_approval_1", interrupt=fake_interrupt)
    with pytest.raises(NodeError) as exc:
        await spec.execute(ctx, config, {"message": "검토", "review": "초안"})
    assert (exc.value.code, exc.value.retryable) == (ErrorCode.NODE_FAILED, False)


def test_resume_output_accepts_the_api_resume_body():
    body = {"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve", "editedValue": "수정본"}

    output = resume_output(body, WAITING)

    assert (output["decision"], output["comment"], output["editedValue"]) == ("approve", "", "수정본")


def test_resume_output_never_shares_the_edited_value():
    edited = {"items": [1]}
    waiting = {**WAITING, "review": {"items": []}}

    output = resume_output({"decision": "approve", "editedValue": edited}, waiting)
    output["editedValue"]["items"].append(2)

    assert edited == {"items": [1]}


@pytest.mark.parametrize("output", [{}, {"decision": None}, {"decision": "maybe"}, {"decision": ["approve"]}])
def test_human_approval_route_rejects_outputs_outside_its_handles(output):
    spec = HumanApprovalNode()
    with pytest.raises(NodeError):
        spec.route(spec.parse_config({"message": "검토"}), output)


async def test_human_approval_needs_an_interrupt_and_a_message():
    calls: list[dict] = []

    async def fake_interrupt(payload: dict) -> dict:
        calls.append(payload)
        return {"decision": "approve"}

    spec = HumanApprovalNode()
    config = spec.parse_config({"message": "{{ start.note | default('') }}"})
    with pytest.raises(NodeError) as no_interrupt:
        await spec.execute(make_ctx(), config, {"message": "검토"})
    with pytest.raises(NodeError) as blank:
        await spec.execute(make_ctx(interrupt=fake_interrupt), config, {"message": " "})

    assert no_interrupt.value.code == ErrorCode.NODE_FAILED
    assert (blank.value.code, calls) == (ErrorCode.TEMPLATE_ERROR, [])


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


async def test_merge_reports_an_output_nested_too_deeply_to_copy():
    nested: list = []
    for _ in range(100_000):
        nested = [nested]
    spec = MergeNode()

    with pytest.raises(NodeError) as exc:
        await spec.execute(make_ctx(outputs={"llm_1": {"v": nested}}, pred_ids=["llm_1"]), spec.parse_config({}), {})

    assert exc.value.code == ErrorCode.NODE_FAILED


def _nested_schema(depth: int) -> dict:
    schema: dict = {"type": "string"}
    for _ in range(depth - 1):
        schema = {"type": "object", "properties": {"x": schema}}
    return schema


def test_merge_output_schema_stays_in_the_subset_and_copies_predecessor_schemas():
    spec = MergeNode()
    config = spec.parse_config({})
    shallow = {"type": "object", "properties": {"text": {"type": "string"}}}
    deep = _nested_schema(32)
    assert schema_problems(deep) == []

    combined = spec.output_schema(config, {"llm_1": shallow})
    too_deep = spec.output_schema(config, {"llm_1": deep, "llm_2": shallow})

    assert combined["properties"]["branches"]["properties"]["llm_1"] == shallow
    assert combined["properties"]["branches"]["properties"]["llm_1"] is not shallow
    assert too_deep["properties"]["branches"]["properties"] == {"llm_1": {}, "llm_2": {}}
    assert too_deep["properties"]["branches"]["required"] == ["llm_1", "llm_2"]
    assert schema_problems(too_deep) == []


def test_merge_of_merges_keeps_its_schema_small():
    import time

    spec = MergeNode()
    config = spec.parse_config({})
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    started = time.perf_counter()
    for _ in range(40):
        schema = spec.output_schema(config, {"merge_a": schema, "merge_b": schema})

    assert schema_problems(schema) == []
    assert time.perf_counter() - started < 5


def _levels(count: int) -> list:
    value: list = []
    for _ in range(count - 1):
        value = [value]
    return value


def test_resume_output_accepts_exactly_what_the_node_output_can_keep():
    waiting = {**WAITING, "review": []}
    output = resume_output({"decision": "approve", "editedValue": _levels(MAX_JSON_DEPTH - 1)}, waiting)
    assert output["editedValue"] == _levels(MAX_JSON_DEPTH - 1)
    with pytest.raises(NodeError, match="중첩"):  # inside the output it would be one level too deep
        resume_output({"decision": "approve", "editedValue": _levels(MAX_JSON_DEPTH)}, waiting)
