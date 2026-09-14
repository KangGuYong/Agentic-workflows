from __future__ import annotations

import operator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.dsl.types import Target, to_text
from engine.jsondata import json_equal, parse_json
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField

Op = Literal["==", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "is_empty", "is_not_empty"]
NUMERIC_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
UNARY_OPS = frozenset({"is_empty", "is_not_empty"})


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str = Field(json_schema_extra=TEMPLATE)
    op: Op
    right: str = Field("", json_schema_extra=TEMPLATE)


class ConditionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conditions: list[Condition] = Field(min_length=1, max_length=10)
    combinator: Literal["and", "or"] = "and"


def operand_targets(op: str) -> tuple[Target, Target]:
    if op in NUMERIC_OPS:
        return "number", "number"
    if op in ("contains", "not_contains"):
        return "string|array", "any"
    return "any", "any"


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


def evaluate(op: str, left: Any, right: Any) -> bool:
    if op == "is_empty":
        return _is_empty(left)
    if op == "is_not_empty":
        return not _is_empty(left)
    if op == "==":
        return _loose_eq(left, right)
    if op == "!=":
        return not _loose_eq(left, right)
    if op in NUMERIC_OPS:
        return NUMERIC_OPS[op](left, right)
    found = to_text(right) in left if isinstance(left, str) else any(_loose_eq(item, right) for item in left)
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
        return "true" if output["result"] else "false"

    def output_schema(self, config: ConditionConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {"type": "object", "properties": {"result": {"type": "boolean"}}, "required": ["result"]}

    async def execute(self, ctx: NodeContext, config: ConditionConfig, rendered: dict[str, Any]) -> NodeResult:
        results = [
            evaluate(cond.op, rendered[f"conditions.{i}.left"], rendered.get(f"conditions.{i}.right"))
            for i, cond in enumerate(config.conditions)
        ]
        result = all(results) if config.combinator == "and" else any(results)
        return NodeResult({"result": result})
