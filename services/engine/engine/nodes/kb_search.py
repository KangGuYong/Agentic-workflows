"""The kb_search node (knowledge-base design §5.1).

Embeds the question and searches inside the node, so the vector never enters run state: the output is
the matching text, a score, and where it came from.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from engine.dsl.models import Policy, RetrySpec
from engine.errors import EngineFault, ErrorCode, NodeError
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField, Usage

KNOWLEDGE_BASE = {"x-knowledge-base": True}  # the editor renders a knowledge base picker
SEPARATOR = "\n\n---\n\n"
HIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "score": {"type": "number"},
        "heading": {},  # string or null; `{}` like http_request's body, since the validator reads single types
        "file": {"type": "string"},
    },
    "required": ["text", "score", "heading", "file"],
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"hits": {"type": "array", "items": HIT_SCHEMA}, "context": {"type": "string"}},
    "required": ["hits", "context"],
}


def join_context(hits: list[dict[str, Any]]) -> str:
    """One string a prompt can take whole: `{{ kb_search_1.context }}`."""
    return SEPARATOR.join(hit["text"] for hit in hits)


class KbSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledgeBase: str = Field(min_length=1, max_length=64, json_schema_extra=KNOWLEDGE_BASE)
    query: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    topK: int = Field(5, ge=1, le=20, strict=True)
    # 0 means no filter. A plain float rather than `float | None`: pydantic renders Optional as an
    # `anyOf`, and RJSF draws its own branch selector for those (see collapseOptionalSchemas).
    minScore: float = Field(0.0, ge=0, le=1)


class KbSearchNode(NodeSpec):
    type = "kb_search"
    label = "지식 검색"
    category = "AI"
    Config = KbSearchConfig
    default_policy = Policy(timeoutSec=60, retry=RetrySpec(maxAttempts=3))

    def template_fields(self, config: KbSearchConfig) -> list[TemplateField]:
        return [TemplateField("query", config.query, "string")]

    def output_schema(self, config: KbSearchConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return OUTPUT_SCHEMA

    async def execute(self, ctx: NodeContext, config: KbSearchConfig, rendered: dict[str, Any]) -> NodeResult:
        query = rendered["query"]
        if not query.strip():
            raise NodeError(ErrorCode.TEMPLATE_ERROR, "검색할 질문이 비어 있습니다", retryable=False)
        if ctx.kb is None:
            raise EngineFault("kb_search needs a knowledge base store")
        kb = await ctx.kb.get(config.knowledgeBase)
        if kb is None:
            raise NodeError(ErrorCode.NODE_FAILED, "지식베이스를 찾을 수 없습니다", retryable=False)
        [embedding] = await ctx.llm.embed(model=kb["embed_model"], texts=[query])
        hits = await ctx.kb.search(config.knowledgeBase, embedding, config.topK)
        hits = [hit for hit in hits if hit["score"] >= config.minScore]
        return NodeResult({"hits": hits, "context": join_context(hits)}, Usage())
