from __future__ import annotations

from engine.nodes.base import NodeSpec


class NodeRegistry:
    def __init__(self, specs: list[NodeSpec]) -> None:
        self._specs = {spec.type: spec for spec in specs}

    def get(self, node_type: str) -> NodeSpec | None:
        return self._specs.get(node_type)

    def all(self) -> list[NodeSpec]:
        return list(self._specs.values())


def default_registry() -> NodeRegistry:
    from engine.nodes.io import EndNode, StartNode
    from engine.nodes.template import TemplateNode

    return NodeRegistry([StartNode(), EndNode(), TemplateNode()])
