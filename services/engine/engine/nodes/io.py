from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.errors import ErrorCode, NodeError
from engine.jsondata import ValidationBudgetExceeded
from engine.nodes.base import (
    NodeContext,
    NodeResult,
    NodeSpec,
    TemplateField,
    check_object_schema,
    schema_violations,
)

_OUTPUT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class StartConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    @field_validator("inputs")
    @classmethod
    def _object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return check_object_schema(value, "입력 스키마")


class StartNode(NodeSpec):
    type = "start"
    label = "시작"
    category = "IO"
    Config = StartConfig

    def output_schema(self, config: StartConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return config.inputs

    async def execute(self, ctx: NodeContext, config: StartConfig, rendered: dict[str, Any]) -> NodeResult:
        try:
            violations = schema_violations(config.inputs, ctx.inputs)
        except ValidationBudgetExceeded as exc:
            raise NodeError(ErrorCode.NODE_FAILED, f"실행 입력을 검증할 수 없습니다: {exc}", retryable=False) from exc
        if violations:
            raise NodeError(
                ErrorCode.TYPE_MISMATCH,
                "실행 입력이 입력 스키마와 맞지 않습니다: " + "; ".join(violations),
                retryable=False,
            )
        return NodeResult(copy.deepcopy(ctx.inputs))


class EndConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outputs: dict[str, str] = Field(default_factory=dict)

    @field_validator("outputs")
    @classmethod
    def _output_names(cls, value: dict[str, str]) -> dict[str, str]:
        bad = [key for key in value if not _OUTPUT_KEY.fullmatch(key)]
        if bad:
            raise ValueError(f"출력 이름 형식이 잘못되었습니다: {', '.join(bad)}")
        return value


class EndNode(NodeSpec):
    type = "end"
    label = "끝"
    category = "IO"
    Config = EndConfig

    def handles(self, config: EndConfig) -> list[str]:
        return []

    def template_fields(self, config: EndConfig) -> list[TemplateField]:
        return [TemplateField(f"outputs.{key}", source, "any") for key, source in config.outputs.items()]

    def output_schema(self, config: EndConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {"type": "object", "properties": {key: {} for key in config.outputs}}

    async def execute(self, ctx: NodeContext, config: EndConfig, rendered: dict[str, Any]) -> NodeResult:
        return NodeResult({key: rendered[f"outputs.{key}"] for key in config.outputs})
