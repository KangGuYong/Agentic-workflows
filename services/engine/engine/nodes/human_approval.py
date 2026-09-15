from __future__ import annotations

from datetime import UTC, datetime
from itertools import islice
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from engine.errors import ErrorCode, NodeError
from engine.jsondata import check_storable, check_text, clip, json_kind
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField
from engine.templates.env import json_value

DECISIONS = ("approve", "reject")
MAX_COMMENT_CHARS = 10_000
MAX_TIMESTAMP_CHARS = 64
_ANSWER_KEYS = frozenset({"nodeId", "execIndex", "decision", "comment", "editedValue", "reviewedAt"})


class HumanApprovalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    review: str = Field("", json_schema_extra=TEMPLATE)
    allowEdit: bool = False


class HumanApprovalNode(NodeSpec):
    """Pauses the run until a person approves or rejects.

    Re-executes from the top on resume (LangGraph semantics), so everything before
    `ctx.interrupt` must be side-effect free.
    """

    type = "human_approval"
    label = "사람 승인"
    category = "Human"
    Config = HumanApprovalConfig
    is_branch = True

    def handles(self, config: HumanApprovalConfig) -> list[str]:
        return list(DECISIONS)

    def template_fields(self, config: HumanApprovalConfig) -> list[TemplateField]:
        fields = [TemplateField("message", config.message, "string")]
        if config.review:
            fields.append(TemplateField("review", config.review, "any"))
        return fields

    def route(self, config: HumanApprovalConfig, output: dict[str, Any]) -> str:
        decision = output.get("decision")
        if decision not in DECISIONS:  # holds for execute's output; guards any other client
            raise NodeError(ErrorCode.NODE_FAILED, f"알 수 없는 승인 결과입니다: {str(decision)[:40]}", retryable=False)
        return decision

    def output_schema(self, config: HumanApprovalConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": list(DECISIONS)},
                "comment": {"type": "string", "maxLength": MAX_COMMENT_CHARS},
                "editedValue": {},
                "reviewedAt": {"type": "string"},
            },
            "required": ["decision", "comment", "editedValue", "reviewedAt"],
            "additionalProperties": False,
        }

    async def execute(self, ctx: NodeContext, config: HumanApprovalConfig, rendered: dict[str, Any]) -> NodeResult:
        if ctx.interrupt is None:
            raise NodeError(ErrorCode.NODE_FAILED, "승인 대기를 지원하지 않는 실행 환경입니다", retryable=False)
        if not rendered["message"].strip():
            raise NodeError(ErrorCode.TEMPLATE_ERROR, "승인 안내 메시지가 비어 있습니다", retryable=False)
        waiting = {
            "nodeId": ctx.node_id,
            "execIndex": ctx.exec_index,
            "message": rendered["message"],
            "review": rendered.get("review"),
            "allowEdit": config.allowEdit,
        }
        answer = await ctx.interrupt(waiting)
        return NodeResult(resume_output(answer, waiting))


def resume_output(answer: Any, waiting: dict[str, Any]) -> dict[str, Any]:
    """Check a resume answer against the interrupt payload (`waiting`) it answers and build the node output.

    The answer comes from a reviewer through the public API, so nothing in it is trusted. `nodeId` and
    `execIndex` must match the waiting approval when given; they are optional here, but `execute_run`
    requires them to find the approval. `reviewedAt` is meant to be
    set by the API when it accepts the answer (never copied from the request); absent, the current time is
    used. The API can call this before queueing the run; the node calls it again on resume.
    Raises NodeError(NODE_FAILED) for an answer that cannot be used.
    """
    if not isinstance(answer, dict):
        raise _invalid("객체가 아닙니다")
    unknown = answer.keys() - _ANSWER_KEYS
    if unknown:
        names = ", ".join(sorted(clip(str(key), 40) for key in islice(unknown, 5)))
        raise _invalid(f"알 수 없는 필드가 있습니다 ({names})")
    for key in ("nodeId", "execIndex"):
        if key in answer and not (type(answer[key]) is type(waiting[key]) and answer[key] == waiting[key]):
            raise _invalid(f"대기 중인 승인과 {key}가 다릅니다")
    decision = answer.get("decision")
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise _invalid("decision은 approve 또는 reject여야 합니다")
    return {
        "decision": decision,
        "comment": _text(answer.get("comment"), "comment", MAX_COMMENT_CHARS),
        "editedValue": _edited_value(answer, waiting),
        "reviewedAt": _reviewed_at(answer.get("reviewedAt")),
    }


def _invalid(reason: str) -> NodeError:
    return NodeError(ErrorCode.NODE_FAILED, f"잘못된 승인 응답입니다: {reason}", retryable=False)


def _text(value: Any, name: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise _invalid(f"{name}은 {limit}자 이하의 문자열이어야 합니다")
    try:
        check_text(value)
    except ValueError as exc:
        raise _invalid(f"{name}: {exc}") from exc
    return value


def _edited_value(answer: dict[str, Any], waiting: dict[str, Any]) -> Any:
    review = waiting.get("review")
    if "editedValue" not in answer:
        try:
            return json_value(review)  # a copy of the rendered review value, never shared
        except Exception as exc:
            raise _invalid(f"검토 값을 복사할 수 없습니다 ({clip(str(exc), 100)})") from exc
    if not waiting.get("allowEdit"):
        raise NodeError(ErrorCode.NODE_FAILED, "수정이 허용되지 않은 승인 노드입니다", retryable=False)
    try:
        edited = json_value(answer["editedValue"])  # JSON data only, size-bounded, never shared with the caller
        check_storable(edited)
    except Exception as exc:  # ValueError, TypeError, SecurityError (too large) or RecursionError (too deep)
        raise _invalid(f"editedValue를 사용할 수 없습니다 ({clip(str(exc), 100)})") from exc
    if review is not None and json_kind(edited) != json_kind(review):
        raise _invalid(f"editedValue는 검토 값과 같은 {json_kind(review)} 타입이어야 합니다")
    return edited


def _reviewed_at(value: Any) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_CHARS:
        raise _invalid(f"reviewedAt은 {MAX_TIMESTAMP_CHARS}자 이하의 문자열이어야 합니다")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid("reviewedAt은 ISO 8601 시각이어야 합니다") from None
    if moment.tzinfo is None:
        raise _invalid("reviewedAt에는 시간대가 있어야 합니다")
    try:
        return moment.astimezone(UTC).isoformat()
    except OverflowError:  # e.g. 0001-01-01T00:00:00+01:00 falls before year 1 in UTC
        raise _invalid("reviewedAt이 표현할 수 있는 범위를 벗어났습니다") from None
