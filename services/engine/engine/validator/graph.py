"""Phase 2: reachability, handles, loops (back-edges) and parallel regions.

Back-edges are the edges that carry `maxIterations`: the user (through the editor) declares which edge
closes a loop. Every other edge is a forward edge, and the forward edges must form a DAG. This makes the
verdict independent of the order edges were declared in, so redrawing an edge never changes it.
"""
from __future__ import annotations

from dataclasses import dataclass

from engine.dsl.models import Edge, WorkflowDSL
from engine.validator.issues import Issue, bounded, error
from engine.validator.structure import ParsedNode

MAX_FAN_OUT = 10
MAX_BACK_EDGES = 10
LOOP_SOURCES = frozenset({"condition", "classifier"})


@dataclass
class Graph:
    nodes: dict[str, ParsedNode]
    edges: list[Edge]  # declaration order
    out: dict[str, dict[str, list[Edge]]]  # node -> handle -> edges (declaration order)
    incoming: dict[str, list[Edge]]  # forward in-edges from reachable nodes (back-edges excluded), declaration order
    back_edges: dict[str, Edge]  # edges with maxIterations
    reachable: set[str]
    order: list[str]  # topological order of reachable nodes over forward edges


def build_graph(dsl: WorkflowDSL, parsed: dict[str, ParsedNode]) -> Graph:
    """Requires a structurally valid DSL (phase 1 without errors)."""
    edges = list(dsl.edges)
    out = {node_id: {handle: [] for handle in pn.spec.handles(pn.config)} for node_id, pn in parsed.items()}
    for edge in edges:
        out[edge.source][edge.sourceHandle].append(edge)
    back_edges = {edge.id: edge for edge in edges if edge.maxIterations is not None}
    reachable = _reach("start", _successors(parsed, edges))
    incoming: dict[str, list[Edge]] = {node_id: [] for node_id in parsed}
    for edge in edges:
        if edge.id not in back_edges and edge.source in reachable:
            incoming[edge.target].append(edge)
    order = _topological_order(list(parsed), incoming, reachable)
    return Graph(parsed, edges, out, incoming, back_edges, reachable, order)


def _successors(nodes: dict[str, ParsedNode], edges: list[Edge]) -> dict[str, list[str]]:
    successors: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        successors[edge.source].append(edge.target)
    return successors


def _forward_successors(graph: Graph) -> dict[str, list[str]]:
    return _successors(graph.nodes, [edge for edge in graph.edges if edge.id not in graph.back_edges])


def _reach(origin: str | set[str], successors: dict[str, list[str]]) -> set[str]:
    seen = {origin} if isinstance(origin, str) else set(origin)
    stack = list(seen)
    while stack:
        for target in successors[stack.pop()]:
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


def _topological_order(node_ids: list[str], incoming: dict[str, list[Edge]], reachable: set[str]) -> list[str]:
    """Kahn's algorithm, ties broken by node declaration order. Nodes on a forward cycle are left out."""
    indegree = {node_id: len(incoming[node_id]) for node_id in reachable}
    children: dict[str, list[str]] = {node_id: [] for node_id in reachable}
    for node_id in node_ids:
        if node_id in reachable:
            for edge in incoming[node_id]:
                children[edge.source].append(node_id)
    queue = [node_id for node_id in node_ids if node_id in reachable and indegree[node_id] == 0]
    order: list[str] = []
    while queue:
        node_id = queue.pop(0)
        order.append(node_id)
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return order


def check_graph(graph: Graph) -> list[Issue]:
    issues = [
        *_check_reachability(graph),
        *_check_handles(graph),
        *_check_loops(graph),
        *_check_parallel(graph),
    ]
    return bounded(issues)  # one mistake found along several paths is reported once


def _dead_ends(graph: Graph) -> set[str]:
    """Reachable nodes with an unconnected handle (reported as HANDLE_NOT_CONNECTED)."""
    return {
        node_id
        for node_id in graph.reachable
        if any(not edges for edges in graph.out[node_id].values())
    }


def _check_reachability(graph: Graph) -> list[Issue]:
    predecessors: dict[str, list[str]] = {node_id: [] for node_id in graph.nodes}
    for edge in graph.edges:
        predecessors[edge.target].append(edge.source)
    dead_ends = _dead_ends(graph)
    cycles = set().union(*(nodes for nodes, _ in _forward_cycles(graph)))
    # A node that only fails to reach `end` because of a mistake after it is not blamed: a dead end
    # (reported here and as HANDLE_NOT_CONNECTED) or a forward cycle (reported by _check_loops).
    explained = _reach("end", predecessors) | (_reach(dead_ends | cycles, predecessors) - dead_ends)
    issues = []
    for node_id in graph.nodes:
        if node_id not in graph.reachable:
            issues.append(error("UNREACHABLE_FROM_START", "시작 노드에서 도달할 수 없는 노드입니다", nodeId=node_id))
        elif node_id not in explained:
            issues.append(error("CANNOT_REACH_END", "끝 노드에 도달할 수 없는 노드입니다", nodeId=node_id))
    return issues


def _check_handles(graph: Graph) -> list[Issue]:
    return [
        error("HANDLE_NOT_CONNECTED", f"'{handle}' 출력이 연결되지 않았습니다", nodeId=node_id, field=f"handles.{handle}")
        for node_id, handles in graph.out.items()
        if node_id in graph.reachable  # an unreachable node is already reported once
        for handle, edges in handles.items()
        if not edges
    ]


def _check_loops(graph: Graph) -> list[Issue]:
    issues: list[Issue] = [*_forward_cycle_issues(graph)]
    if len(graph.back_edges) > MAX_BACK_EDGES:
        issues.append(error("LIMIT_EXCEEDED", f"되돌아가는 연결은 최대 {MAX_BACK_EDGES}개까지 사용할 수 있습니다"))
    forward = _forward_successors(graph)
    for edge in graph.back_edges.values():
        if edge.source not in _reach(edge.target, forward):
            issues.append(
                error("MAX_ITERATIONS_ON_FORWARD_EDGE",
                      "최대 반복 횟수(maxIterations)는 반복 구간을 닫는 되돌아가는 연결에만 지정할 수 있습니다",
                      edgeId=edge.id)
            )
        elif graph.nodes[edge.source].spec.type not in LOOP_SOURCES:
            issues.append(error("ILLEGAL_CYCLE", "되돌아가는 연결은 조건/분류 노드에서만 시작할 수 있습니다", edgeId=edge.id))
    for node_id, handles in graph.out.items():
        if graph.nodes[node_id].spec.type not in LOOP_SOURCES:  # already reported as ILLEGAL_CYCLE
            continue
        loop_handles = [h for h, edges in handles.items() if any(e.id in graph.back_edges for e in edges)]
        if len(loop_handles) > 1:
            issues.append(error("BACK_EDGE_HANDLE_CONFLICT", "되돌아가는 연결은 한 출력에서만 나갈 수 있습니다",
                                nodeId=node_id))
        for handle in loop_handles:
            if len(handles[handle]) != 1:
                issues.append(error("BACK_EDGE_HANDLE_CONFLICT",
                                    "되돌아가는 연결이 있는 출력에는 다른 연결을 둘 수 없습니다",
                                    nodeId=node_id, field=f"handles.{handle}"))
            if graph.nodes[node_id].spec.type == "classifier" and handle == "default":
                issues.append(error("BACK_EDGE_HANDLE_CONFLICT",
                                    "분류 노드의 default 출력은 되돌아가는 연결이 될 수 없습니다",
                                    nodeId=node_id, field="handles.default"))
    return issues


def _forward_cycles(graph: Graph) -> list[tuple[frozenset[str], list[Edge]]]:
    """Cycles made only of forward edges, as (component nodes, forward edges inside it), in edge order."""
    forward_edges = [edge for edge in graph.edges if edge.id not in graph.back_edges]
    forward = _successors(graph.nodes, forward_edges)
    reach = {node_id: _reach(node_id, forward) for node_id in graph.nodes}
    components: dict[frozenset[str], list[Edge]] = {}
    for edge in forward_edges:
        if edge.source in reach[edge.target] and (edge.source != edge.target or edge.target in forward[edge.source]):
            component = frozenset(node for node in reach[edge.target] if edge.target in reach[node])
            components.setdefault(component, []).append(edge)
    return list(components.items())


def _distances(graph: Graph) -> dict[str, int]:
    """Forward-edge hop distance from `start` (breadth-first)."""
    forward = _forward_successors(graph)
    distance = {"start": 0}
    queue = ["start"]
    while queue:
        node_id = queue.pop(0)
        for target in forward[node_id]:
            if target not in distance:
                distance[target] = distance[node_id] + 1
                queue.append(target)
    return distance


def _forward_cycle_issues(graph: Graph) -> list[Issue]:
    """A loop whose closing edge has no maxIterations, or an illegal cycle (no condition/classifier on it)."""
    issues: list[Issue] = []
    distance = _distances(graph)
    far = len(graph.nodes) + 1
    for _, edges in _forward_cycles(graph):
        candidates = [edge for edge in edges if graph.nodes[edge.source].spec.type in LOOP_SOURCES]
        if not candidates:
            issues.append(error("ILLEGAL_CYCLE", "반복 구간은 조건/분류 노드에서 되돌아가는 연결로만 만들 수 있습니다",
                                edgeId=edges[0].id))
            continue
        # The closing edge goes back toward start; routing edges inside the loop body go further away.
        closing = [e for e in candidates if distance.get(e.target, far) <= distance.get(e.source, far)] or candidates
        issues.extend(
            error("BACK_EDGE_NO_LIMIT", "반복 구간을 닫는 연결에는 최대 반복 횟수(maxIterations)가 필요합니다",
                  edgeId=edge.id)
            for edge in closing
        )
    return issues


def _in_cycle(successors: dict[str, list[str]], node_id: str) -> bool:
    return any(node_id in _reach(target, successors) for target in successors[node_id])


def _walk_branch(
    graph: Graph, source: str, handle: str, first: Edge, back_targets: set[str], region: set[str]
) -> tuple[str, str] | Issue:
    """Follow one fan-out branch to its merge. Returns (merge_id, closing_edge_id) or an Issue.
    `region` holds the nodes reachable from this fan-out's branches (forward edges)."""
    edge = first
    steps = 0
    while True:
        target_id = edge.target
        target = graph.nodes[target_id]
        if target.spec.type == "merge":
            if steps == 0:
                return error("INVALID_PARALLEL_REGION", "병렬 분기에는 합치기 전에 노드가 하나 이상 있어야 합니다",
                             edgeId=first.id)
            return target_id, edge.id
        fan_out = {e.id for e in graph.out[source][handle]}
        joined = graph.incoming[target_id]
        if len(joined) > 1 and all(e.id in fan_out or e.source in region for e in joined):
            return error(
                "INVALID_PARALLEL_REGION",
                f"'{source}'에서 갈라진 분기가 합치기 노드 없이 '{target_id}'에서 만나 중복 실행됩니다. "
                "분기들을 합치기 노드로 모아 주세요",
                nodeId=source, field=f"handles.{handle}",
            )
        out_edges = [e for edges in graph.out[target_id].values() for e in edges]
        is_plain = (
            not target.spec.is_branch
            and target.spec.type not in ("start", "end")
            and len(graph.incoming[target_id]) == 1  # joined from elsewhere: a merge could wait forever
            and target_id not in back_targets
            and len(out_edges) == 1
            and out_edges[0].id not in graph.back_edges
        )
        steps += 1
        if not is_plain or steps > len(graph.nodes):
            return error(
                "INVALID_PARALLEL_REGION",
                "병렬 분기는 조건·분류·승인 노드나 다른 분기·합류 없이 하나의 합치기 노드로 모여야 합니다",
                nodeId=target_id,
            )
        edge = out_edges[0]


def _first_merge(graph: Graph, edge: Edge) -> str | None:
    """The merge reached by following single out-edges from `edge`, if any."""
    for _ in range(len(graph.nodes)):
        if graph.nodes[edge.target].spec.type == "merge":
            return edge.target
        out_edges = [e for edges in graph.out[edge.target].values() for e in edges if e.id not in graph.back_edges]
        if len(out_edges) != 1:
            return None
        edge = out_edges[0]
    return None


def _check_parallel(graph: Graph) -> list[Issue]:
    issues: list[Issue] = []
    claimed_merges: set[str] = set()
    back_targets = {edge.target for edge in graph.back_edges.values()}
    successors = _successors(graph.nodes, graph.edges)
    forward = _forward_successors(graph)
    for node_id, handles in graph.out.items():
        for handle, all_edges in handles.items():
            edges = [edge for edge in all_edges if edge.id not in graph.back_edges]  # loop handles: _check_loops
            if len(edges) < 2 or node_id not in graph.reachable:
                continue
            where = {"nodeId": node_id, "field": f"handles.{handle}"}
            # the merge each branch heads for is explained by this fan-out, even when the region is broken
            claimed_merges |= {merge for edge in edges if (merge := _first_merge(graph, edge)) is not None}
            if len(edges) > MAX_FAN_OUT:
                issues.append(error("LIMIT_EXCEEDED", f"병렬 분기는 최대 {MAX_FAN_OUT}개까지 사용할 수 있습니다", **where))
            region = _reach({edge.target for edge in edges}, forward)  # nodes this fan-out's branches lead to
            merges: set[str] = set()
            closing: set[str] = set()
            branch_issues: list[Issue] = []
            for edge in edges:
                result = _walk_branch(graph, node_id, handle, edge, back_targets, region)
                if isinstance(result, Issue):
                    branch_issues.append(result)
                else:
                    merges.add(result[0])
                    closing.add(result[1])
            if branch_issues:
                issues.extend(branch_issues)
                continue
            if len(merges) != 1:
                issues.append(error("INVALID_PARALLEL_REGION", "병렬 분기는 모두 같은 합치기 노드로 모여야 합니다", **where))
                continue
            merge_id = next(iter(merges))
            if merge_id in back_targets or _in_cycle(successors, merge_id):
                issues.append(error("PARALLEL_IN_LOOP", "반복(루프) 안에서는 합치기 노드를 사용할 수 없습니다",
                                    nodeId=merge_id))
            elif {edge.id for edge in graph.incoming[merge_id]} != closing:
                issues.append(error("INVALID_PARALLEL_REGION",
                                    f"합치기 노드 '{merge_id}'에는 이 병렬 분기만 들어올 수 있습니다", nodeId=merge_id))
    for node_id, pn in graph.nodes.items():
        if pn.spec.type == "merge" and node_id in graph.reachable and node_id not in claimed_merges:
            issues.append(error("MERGE_WITHOUT_FAN_OUT",
                                "합치기 노드는 한 출력에서 갈라진 병렬 분기를 모아야 합니다", nodeId=node_id))
    return issues
