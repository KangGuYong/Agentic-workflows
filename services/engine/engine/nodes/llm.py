from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.dsl.models import Policy, RetrySpec
from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage
from engine.nodes.base import (
    MODEL_NAME,
    TEMPLATE,
    TEXT_OUTPUT_SCHEMA,
    NodeContext,
    NodeResult,
    NodeSpec,
    TemplateField,
    Usage,
    check_object_schema,
)


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(max_length=200, pattern=MODEL_NAME)
    system: str = Field("", json_schema_extra=TEMPLATE)
    prompt: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    temperature: float = Field(0.7, ge=0, le=2, strict=True)
    outputSchema: dict[str, Any] | None = None

    @field_validator("outputSchema")
    @classmethod
    def _object_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else check_object_schema(value, "outputSchema")


class LLMNode(NodeSpec):
    type = "llm"
    label = "LLM"
    category = "AI"
    Config = LLMConfig
    default_policy = Policy(timeoutSec=120, retry=RetrySpec(maxAttempts=3))

    def template_fields(self, config: LLMConfig) -> list[TemplateField]:
        fields = [TemplateField("prompt", config.prompt, "string")]
        if config.system:
            fields.append(TemplateField("system", config.system, "string"))
        return fields

    def output_schema(self, config: LLMConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return config.outputSchema or TEXT_OUTPUT_SCHEMA

    async def execute(self, ctx: NodeContext, config: LLMConfig, rendered: dict[str, Any]) -> NodeResult:
        if not rendered["prompt"].strip():
            raise NodeError(ErrorCode.TEMPLATE_ERROR, "프롬프트가 비어 있습니다", retryable=False)
        messages = []
        if config.system and rendered["system"].strip():  # a system template may render to nothing
            messages.append(ChatMessage("system", rendered["system"]))
        messages.append(ChatMessage("user", rendered["prompt"]))
        result = await ctx.llm.chat(
            model=config.model,
            messages=messages,
            schema=config.outputSchema,
            temperature=config.temperature,
            on_token=None if config.outputSchema else ctx.on_token,
        )
        output = result.data if config.outputSchema else {"text": result.text}
        return NodeResult(output, Usage(result.tokens_in, result.tokens_out))
