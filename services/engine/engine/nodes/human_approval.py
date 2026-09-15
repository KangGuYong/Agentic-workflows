from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from engine.errors import ErrorCode, NodeError
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField
from engine.templates.env import json_value

MAX_COMMENT_CHARS = 10_000
_ANSWER_KEYS = frozenset({"decision", "comment", "editedValue", "reviewedAt"})


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
        return ["approve", "reject"]

    def template_fields(self, config: HumanApprovalConfig) -> list[TemplateField]:
        fields = [TemplateField("message", config.message, "string")]
        if config.review:
            fields.append(TemplateField("review", config.review, "any"))
        return fields

    def route(self, config: HumanApprovalConfig, output: dict[str, Any]) -> str:
        return output["decision"]

    def output_schema(self, config: HumanApprovalConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["approve", "reject"]},
                "comment": {"type": "string"},
                "editedValue": {},
                "reviewedAt": {"type": "string"},
            },
            "required": ["decision", "comment", "editedValue", "reviewedAt"],
        }

    async def execute(self, ctx: NodeContext, config: HumanApprovalConfig, rendered: dict[str, Any]) -> NodeResult:
        if ctx.interrupt is None:
            raise NodeError(ErrorCode.NODE_FAILED, "승인 대기를 지원하지 않는 실행 환경입니다", retryable=False)
        review = rendered.get("review")
        answer = await ctx.interrupt(
            {
                "nodeId": ctx.node_id,
                "execIndex": ctx.exec_index,
                "message": rendered["message"],
                "review": review,
                "allowEdit": config.allowEdit,
            }
        )
        _check_answer(answer, config)
        edited = answer.get("editedValue", review)
        try:
            edited = json_value(edited)  # JSON data only, size-bounded, and never shared with the caller
        except Exception as exc:
            raise NodeError(ErrorCode.NODE_FAILED, f"잘못된 승인 응답입니다: editedValue ({exc})", retryable=False) from exc
        return NodeResult(
            {
                "decision": answer["decision"],
                "comment": answer.get("comment") or "",
                "editedValue": edited,
                "reviewedAt": answer.get("reviewedAt") or datetime.now(UTC).isoformat(),
            }
        )


def _check_answer(answer: Any, config: HumanApprovalConfig) -> None:
    """The resume answer comes from a reviewer through the API: check its shape before trusting it."""

    def invalid(reason: str) -> NodeError:
        return NodeError(ErrorCode.NODE_FAILED, f"잘못된 승인 응답입니다: {reason}", retryable=False)

    if not isinstance(answer, dict):
        raise invalid("객체가 아닙니다")
    unknown = sorted(str(key) for key in answer.keys() - _ANSWER_KEYS)
    if unknown:
        raise invalid(f"알 수 없는 필드 {', '.join(unknown)[:100]}")
    if answer.get("decision") not in ("approve", "reject"):
        raise invalid("decision은 approve 또는 reject여야 합니다")
    comment = answer.get("comment")
    if comment is not None and (not isinstance(comment, str) or len(comment) > MAX_COMMENT_CHARS):
        raise invalid(f"comment는 {MAX_COMMENT_CHARS}자 이하의 문자열이어야 합니다")
    reviewed_at = answer.get("reviewedAt")
    if reviewed_at is not None and (not isinstance(reviewed_at, str) or len(reviewed_at) > 64):
        raise invalid("reviewedAt은 64자 이하의 문자열이어야 합니다")
    if "editedValue" in answer and not config.allowEdit:
        raise NodeError(ErrorCode.NODE_FAILED, "수정이 허용되지 않은 승인 노드입니다", retryable=False)
