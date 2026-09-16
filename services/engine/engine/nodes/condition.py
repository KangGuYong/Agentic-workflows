from __future__ import annotations

import operator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.dsl.types import Target, to_text
from engine.errors import ErrorCode, NodeError
from engine.jsondata import json_equal, parse_json
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField

Op = Literal["==", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "is_empty", "is_not_empty"]
NUMERIC_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
CONTAINS_OPS = frozenset({"contains", "not_contains"})
UNARY_OPS = frozenset({"is_empty", "is_not_empty"})


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str = Field(json_schema_extra=TEMPLATE)
    op: Op
    right: str = Field("", json_schema_extra=TEMPLATE)

    @model_validator(mode="after")
    def _right_for_contains(self) -> Condition:
        if self.op in CONTAINS_OPS and not self.right:  # "" is contained in every string
            raise ValueError(f"'{self.op}' 조건에는 찾을 값(right)이 필요합니다")
        return self


class ConditionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conditions: list[Condition] = Field(min_length=1, max_length=10)
    combinator: Literal["and", "or"] = "and"


def operand_targets(op: str) -> tuple[Target, Target]:
    if op in NUMERIC_OPS:
        return "number", "number"
    if op in CONTAINS_OPS:
        return "string|array", "any"
    return "any", "any"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _loose_eq(a: Any, b: Any) -> bool:
    """JSON equality, except that a string also matches the value it spells ("5" == 5, "true" == true, "" == null)."""
    if isinstance(a, str) == isinstance(b, str):
        return json_equal(a, b)
    text, value = (a, b) if isinstance(a, str) else (b, a)
    try:
        return json_equal(parse_json(text), value)
    except ValueError:
        return to_text(value) == text


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _contains(container: Any, needle: Any) -> bool:
    if isinstance(container, str):
        # a missing (null) needle must not match every string through to_text(None) == ""
        return needle is not None and to_text(needle) in container
    if isinstance(container, list):
        return any(_loose_eq(item, needle) for item in container)
    raise TypeError(f"contains의 왼쪽 값은 문자열이나 배열이어야 합니다 ({type(container).__name__})")


def evaluate(op: str, left: Any, right: Any) -> bool:
    """Evaluate one condition on rendered operands. Raises TypeError when operand types do not fit `op`."""
    if op == "is_empty":
        return _is_empty(left)
    if op == "is_not_empty":
        return not _is_empty(left)
    if op == "==":
        return _loose_eq(left, right)
    if op == "!=":
        return not _loose_eq(left, right)
    if op in NUMERIC_OPS:
        if not (_is_number(left) and _is_number(right)):
            raise TypeError(f"'{op}' 조건은 숫자끼리만 비교할 수 있습니다")
        return NUMERIC_OPS[op](left, right)
    found = _contains(left, right)
    return found if op == "contains" else not found


class ConditionNode(NodeSpec):
    type = "condition"
    label = "조건"
    category = "Logic"
    Config = ConditionConfig
    is_branch = True

    def handles(self, config: ConditionConfig) -> list[str]:
        return ["true", "false"]

    def template_fields(self, config: ConditionConfig) -> list[TemplateField]:
        fields: list[TemplateField] = []
        for index, cond in enumerate(config.conditions):
            left_target, right_target = operand_targets(cond.op)
            fields.append(TemplateField(f"conditions.{index}.left", cond.left, left_target))
            if cond.op not in UNARY_OPS:
                fields.append(TemplateField(f"conditions.{index}.right", cond.right, right_target))
        return fields

    def route(self, config: ConditionConfig, output: dict[str, Any]) -> str:
        result = output.get("result")
        if not isinstance(result, bool):
            raise NodeError(ErrorCode.NODE_FAILED, "조건 결과가 true/false가 아닙니다", retryable=False)
        return "true" if result else "false"

    def output_schema(self, config: ConditionConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {"type": "object", "properties": {"result": {"type": "boolean"}}, "required": ["result"]}

    async def execute(self, ctx: NodeContext, config: ConditionConfig, rendered: dict[str, Any]) -> NodeResult:
        results = []
        for index, cond in enumerate(config.conditions):
            left, right = rendered[f"conditions.{index}.left"], rendered.get(f"conditions.{index}.right")
            try:
                results.append(evaluate(cond.op, left, right))
            except TypeError as exc:
                raise NodeError(ErrorCode.TYPE_MISMATCH, f"조건 {index + 1}: {exc}", retryable=False) from exc
            except RecursionError as exc:
                message = f"조건 {index + 1}: 비교할 값의 중첩이 너무 깊습니다"
                raise NodeError(ErrorCode.NODE_FAILED, message, retryable=False) from exc
        result = all(results) if config.combinator == "and" else any(results)
        return NodeResult({"result": result})
