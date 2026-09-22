from __future__ import annotations

from engine.nodes.base import NodeSpec


class NodeRegistry:
    def __init__(self, specs: list[NodeSpec]) -> None:
        self._specs: dict[str, NodeSpec] = {}
        for spec in specs:
            if spec.type in self._specs:
                raise ValueError(f"duplicate node type: {spec.type}")
            self._specs[spec.type] = spec

    def get(self, node_type: str) -> NodeSpec | None:
        return self._specs.get(node_type)

    def all(self) -> list[NodeSpec]:
        return list(self._specs.values())


def default_registry() -> NodeRegistry:
    from engine.nodes.classifier import ClassifierNode
    from engine.nodes.condition import ConditionNode
    from engine.nodes.http_request import HttpRequestNode
    from engine.nodes.human_approval import HumanApprovalNode
    from engine.nodes.io import EndNode, StartNode
    from engine.nodes.kb_search import KbSearchNode
    from engine.nodes.llm import LLMNode
    from engine.nodes.merge import MergeNode
    from engine.nodes.template import TemplateNode

    return NodeRegistry(
        [
            StartNode(),
            EndNode(),
            TemplateNode(),
            LLMNode(),
            ClassifierNode(),
            ConditionNode(),
            MergeNode(),
            HumanApprovalNode(),
            HttpRequestNode(),
            KbSearchNode(),
        ]
    )
