"""The rerank node (knowledge-base design §5.2): reorder search hits with a cross-encoder."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from engine.dsl.models import Policy, RetrySpec
from engine.errors import ErrorCode, NodeError
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField, Usage
from engine.nodes.kb_search import OUTPUT_SCHEMA, join_context

MAX_HITS = 100


class RerankConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    hits: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    topN: int = Field(3, ge=1, le=20, strict=True)
    minScore: float = Field(0.0, ge=0, le=1)  # 0 means no filter; see KbSearchConfig for why not Optional


class RerankNode(NodeSpec):
    type = "rerank"
    label = "리랭킹"
    category = "AI"
    Config = RerankConfig
    default_policy = Policy(timeoutSec=60, retry=RetrySpec(maxAttempts=3))

    def template_fields(self, config: RerankConfig) -> list[TemplateField]:
        return [TemplateField("query", config.query, "string"), TemplateField("hits", config.hits, "array")]

    def output_schema(self, config: RerankConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return OUTPUT_SCHEMA  # same shape as kb_search, so a prompt can take either

    async def execute(self, ctx: NodeContext, config: RerankConfig, rendered: dict[str, Any]) -> NodeResult:
        hits = _check_hits(rendered["hits"])
        if not hits:
            return NodeResult({"hits": [], "context": ""}, Usage())
        if ctx.rerank is None:
            raise NodeError(ErrorCode.NODE_FAILED, "리랭커가 설정되지 않았습니다 (RERANK_BASE_URL)", retryable=False)
        pairs = await ctx.rerank.rerank(rendered["query"], [hit["text"] for hit in hits])
        reranked = [{**hits[index], "score": score} for index, score in pairs if score >= config.minScore]
        reranked = reranked[: config.topN]
        return NodeResult({"hits": reranked, "context": join_context(reranked)}, Usage())


def _check_hits(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_HITS or not all(
        isinstance(hit, dict) and isinstance(hit.get("text"), str) for hit in value
    ):
        raise NodeError(ErrorCode.TEMPLATE_ERROR,
                        f"hits는 text를 가진 항목의 목록이어야 합니다 (최대 {MAX_HITS}개). 보통 {{{{ 지식검색노드.hits }}}}를 넣습니다",
                        retryable=False)
    return value
