from __future__ import annotations

from dataclasses import dataclass, field

from engine.dsl.models import Edge

_OPPOSITE = {"true": "false", "false": "true"}


@dataclass(frozen=True)
class RouteDecision:
    handle: str  # handle actually taken (differs from the chosen one when a loop is exhausted)
    targets: list[str]
    counters: dict[str, int] = field(default_factory=dict)  # back-edge traversal counts to write to state
    loop_exhausted: bool = False


def resolve_route(
    node_type: str,
    chosen: str,
    handle_edges: dict[str, list[Edge]],
    back_edge_ids: frozenset[str],
    loop_counters: dict[str, int],
) -> RouteDecision:
    """Spec 5.8: take the chosen handle; a back-edge at its limit falls through to the exit handle."""
    if chosen not in handle_edges:
        raise ValueError(f"알 수 없는 출력입니다: {chosen}")
    edges = handle_edges[chosen]
    loop = next((edge for edge in edges if edge.id in back_edge_ids), None)
    if loop is None:
        return RouteDecision(chosen, [edge.target for edge in edges])
    used = loop_counters.get(loop.id, 0)
    if used >= (loop.maxIterations or 0):  # back-edges always carry maxIterations (phase 2)
        exit_handle = "default" if node_type == "classifier" else _OPPOSITE.get(chosen)
        if exit_handle is None or exit_handle not in handle_edges:  # phase 2 only allows condition/classifier loops
            raise ValueError(f"'{node_type}' 노드의 '{chosen}' 반복에는 빠져나갈 출력이 없습니다")
        return RouteDecision(exit_handle, [edge.target for edge in handle_edges[exit_handle]], loop_exhausted=True)
    return RouteDecision(chosen, [loop.target], {loop.id: used + 1})
