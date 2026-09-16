from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from engine.compiler.state import build_state_type
from engine.compiler.wrapper import NodePlan, make_node_fn
from engine.dsl.models import WorkflowDSL, dsl_hash
from engine.nodes.registry import NodeRegistry
from engine.runtime.deps import RunDeps
from engine.validator import analyze
from engine.validator.issues import Issue, has_errors


class WorkflowInvalid(Exception):
    def __init__(self, issues: list[Issue]) -> None:
        errors = sum(1 for issue in issues if issue.severity == "error")
        super().__init__(f"workflow has {errors} validation error(s)")
        self.issues = issues


@dataclass(frozen=True)
class CompiledWorkflow:
    """A compiled graph bound to one checkpointer and one node registry. A cache keyed by `dsl_hash` alone is
    correct only with a single registry and checkpointer per process."""

    graph: CompiledStateGraph
    dsl: WorkflowDSL
    dsl_hash: str
    recursion_limit: int


def _router(node_id: str):
    def route(state: dict[str, Any]) -> list[str]:
        return state["routes"][node_id]

    return route


def compile_workflow(
    raw: dict[str, Any] | WorkflowDSL,
    *,
    checkpointer: BaseCheckpointSaver,
    registry: NodeRegistry | None = None,
) -> CompiledWorkflow:
    analysis = analyze(raw, registry)
    if has_errors(analysis.issues) or analysis.graph is None or analysis.dsl is None:
        raise WorkflowInvalid(analysis.issues)
    graph = analysis.graph
    back_edge_ids = frozenset(graph.back_edges)

    builder = StateGraph(build_state_type(graph.nodes), context_schema=RunDeps)
    for node_id, pn in graph.nodes.items():
        plan = NodePlan(
            node=pn.node,
            spec=pn.spec,
            config=pn.config,
            policy=pn.policy,
            pred_ids=tuple(edge.source for edge in graph.incoming[node_id]),
            handle_edges=graph.out[node_id],
            back_edge_ids=back_edge_ids,
        )
        builder.add_node(node_id, make_node_fn(plan))
    builder.add_edge(START, "start")
    builder.add_edge("end", END)
    for node_id, pn in graph.nodes.items():
        if pn.spec.type == "merge":
            builder.add_edge([edge.source for edge in graph.incoming[node_id]], node_id)
        elif pn.spec.is_branch:
            targets = sorted({edge.target for edges in graph.out[node_id].values() for edge in edges})
            builder.add_conditional_edges(node_id, _router(node_id), targets)
    for edge in graph.edges:
        if graph.nodes[edge.source].spec.is_branch or graph.nodes[edge.target].spec.type == "merge":
            continue
        builder.add_edge(edge.source, edge.target)

    iterations = sum(edge.maxIterations or 0 for edge in graph.back_edges.values())
    return CompiledWorkflow(
        graph=builder.compile(checkpointer=checkpointer),
        dsl=analysis.dsl,
        dsl_hash=dsl_hash(analysis.dsl),
        recursion_limit=len(graph.nodes) * (1 + iterations) + 10,
    )
