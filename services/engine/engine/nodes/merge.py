from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from engine.errors import ErrorCode, NodeError
from engine.nodes.base import NodeContext, NodeResult, NodeSpec


class MergeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["object", "list"] = "object"


class MergeNode(NodeSpec):
    type = "merge"
    label = "합치기"
    category = "Logic"
    Config = MergeConfig

    def output_schema(self, config: MergeConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        if config.mode == "object":
            branches = {
                "type": "object",
                "properties": dict(pred_schemas),
                "required": list(pred_schemas),
            }
        else:
            branches = {"type": "array", "items": {}}
        return {"type": "object", "properties": {"branches": branches}, "required": ["branches"]}

    async def execute(self, ctx: NodeContext, config: MergeConfig, rendered: dict[str, Any]) -> NodeResult:
        missing = [pred for pred in ctx.pred_ids if pred not in ctx.outputs]
        if missing:  # the compiler joins only on predecessors that always produce an output
            raise NodeError(ErrorCode.NODE_FAILED, f"합칠 노드의 출력이 없습니다: {', '.join(missing)}", retryable=False)
        outputs = {pred: copy.deepcopy(ctx.outputs[pred]) for pred in ctx.pred_ids}  # never share upstream outputs
        return NodeResult({"branches": outputs if config.mode == "object" else list(outputs.values())})
