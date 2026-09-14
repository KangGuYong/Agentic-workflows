from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.dsl.models import Policy, RetrySpec
from engine.llm.base import ChatMessage
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField, Usage

SYSTEM_PROMPT = (
    "입력을 아래 카테고리 중 하나로 분류하세요. 어느 카테고리에도 맞지 않으면 \"default\"를 고르세요.\n"
    "카테고리:\n{categories}\n{instructions}"
    "category에는 카테고리 id만, reason에는 한 문장 근거만 JSON으로 출력하세요."
)


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    description: str = Field(min_length=1, max_length=500)


class ClassifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=200)
    input: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    categories: list[Category] = Field(min_length=1, max_length=20)
    instructions: str = Field("", max_length=4000)  # goes into a small model's system prompt
    temperature: float = Field(0.0, ge=0, le=2)

    @model_validator(mode="after")
    def _unique_ids(self) -> ClassifierConfig:
        ids = [c.id for c in self.categories]
        if "default" in ids:
            raise ValueError("'default'는 예약된 카테고리 id입니다")
        if len(set(ids)) != len(ids):
            raise ValueError("카테고리 id가 중복되었습니다")
        return self


class ClassifierNode(NodeSpec):
    type = "classifier"
    label = "분류"
    category = "AI"
    Config = ClassifierConfig
    default_policy = Policy(timeoutSec=60, retry=RetrySpec(maxAttempts=3))
    is_branch = True

    def handles(self, config: ClassifierConfig) -> list[str]:
        return [c.id for c in config.categories] + ["default"]

    def template_fields(self, config: ClassifierConfig) -> list[TemplateField]:
        return [TemplateField("input", config.input, "string")]

    def route(self, config: ClassifierConfig, output: dict[str, Any]) -> str:
        return output["category"]

    def fallback_output(self, config: ClassifierConfig) -> dict[str, Any]:
        return {"category": "default", "reason": "error"}

    def output_schema(self, config: ClassifierConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": self.handles(config)},
                "reason": {"type": "string"},
            },
            "required": ["category", "reason"],
            "additionalProperties": False,
        }

    async def execute(self, ctx: NodeContext, config: ClassifierConfig, rendered: dict[str, Any]) -> NodeResult:
        categories = "\n".join(f"- {c.id}: {c.description}" for c in config.categories)
        instructions = f"{config.instructions}\n" if config.instructions else ""
        result = await ctx.llm.chat(
            model=config.model,
            messages=[
                ChatMessage("system", SYSTEM_PROMPT.format(categories=categories, instructions=instructions)),
                ChatMessage("user", rendered["input"]),
            ],
            schema=self.output_schema(config, {}),
            temperature=config.temperature,
        )
        return NodeResult(result.data, Usage(result.tokens_in, result.tokens_out))
