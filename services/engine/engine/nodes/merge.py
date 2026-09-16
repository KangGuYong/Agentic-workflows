from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from engine.errors import ErrorCode, NodeError
from engine.jsondata import clip, schema_problems
from engine.nodes.base import NodeContext, NodeResult, NodeSpec
from engine.templates.env import json_value


class MergeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["object", "list"] = "object"


class MergeNode(NodeSpec):
    type = "merge"
    label = "합치기"
    category = "Logic"
    Config = MergeConfig

    def output_schema(self, config: MergeConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        if config.mode == "list":
            branches: dict[str, Any] = {"type": "array", "items": {}}
        else:
            branches = {"type": "object", "properties": dict(pred_schemas), "required": list(pred_schemas)}
            if schema_problems(_wrap(branches)):  # the combined schema is too large or deep for the subset
                branches["properties"] = {pred: {} for pred in pred_schemas}
            else:
                branches["properties"] = copy.deepcopy(branches["properties"])  # never alias a node's config
        return _wrap(branches)

    async def execute(self, ctx: NodeContext, config: MergeConfig, rendered: dict[str, Any]) -> NodeResult:
        missing = [pred for pred in ctx.pred_ids if pred not in ctx.outputs]
        if missing:  # the compiler joins only on predecessors that always produce an output
            raise NodeError(ErrorCode.NODE_FAILED, f"합칠 노드의 출력이 없습니다: {', '.join(missing)}", retryable=False)
        try:  # a copy, so no node can change another node's stored output
            outputs = {pred: json_value(ctx.outputs[pred]) for pred in ctx.pred_ids}
        except Exception as exc:  # too large (SecurityError) or nested too deeply (RecursionError)
            message = f"합칠 출력을 복사할 수 없습니다 ({clip(str(exc), 100)})"
            raise NodeError(ErrorCode.NODE_FAILED, message, retryable=False) from exc
        return NodeResult({"branches": outputs if config.mode == "object" else list(outputs.values())})


def _wrap(branches: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": {"branches": branches}, "required": ["branches"]}
