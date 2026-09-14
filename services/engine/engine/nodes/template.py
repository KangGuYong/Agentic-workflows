from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.nodes.base import TEMPLATE, TEXT_OUTPUT_SCHEMA, NodeContext, NodeResult, NodeSpec, TemplateField


class TemplateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(json_schema_extra=TEMPLATE)
    format: Literal["text", "json"] = "text"


class TemplateNode(NodeSpec):
    type = "template"
    label = "템플릿"
    category = "Logic"
    Config = TemplateConfig

    def template_fields(self, config: TemplateConfig) -> list[TemplateField]:
        return [TemplateField("template", config.template, "string" if config.format == "text" else "json")]

    def output_schema(self, config: TemplateConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        if config.format == "text":
            return TEXT_OUTPUT_SCHEMA
        return {"type": "object", "properties": {"data": {}}, "required": ["data"]}

    async def execute(self, ctx: NodeContext, config: TemplateConfig, rendered: dict[str, Any]) -> NodeResult:
        # format "json" renders to a parsed value (target "json"), so a rendered string is data, never re-parsed.
        return NodeResult({"text" if config.format == "text" else "data": rendered["template"]})
