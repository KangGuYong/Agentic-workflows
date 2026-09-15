"""Phase 2: reachability, handles, loops (back-edges) and parallel regions."""
from __future__ import annotations

from dataclasses import dataclass

from engine.dsl.models import Edge, WorkflowDSL
from engine.validator.issues import Issue, error
from engine.validator.structure import MAX_ISSUES, ParsedNode

MAX_FAN_OUT = 10
MAX_BACK_EDGES = 10
LOOP_SOURCES = frozenset({"condition", "classifier"})


@dataclass
class Graph:
    nodes: dict[str, ParsedNode]
    edges: list[Edge]  # declaration order
    out: dict[str, dict[str, list[Edge]]]  # node -> handle -> edges (declaration order)
    incoming: dict[str, list[Edge]]  # forward in-edges only (back-edges excluded), declaration order
    back_edges: dict[str, Edge]
    reachable: set[str]
    order: list[str]  # topological order of reachable nodes over forward edges


def _adjacency(nodes: dict[str, ParsedNode], edges: list[Edge]) -> dict[str, list[Edge]]:
    adjacency: dict[str, list[Edge]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        adjacency[edge.source].append(edge)
    return adjacency


def build_graph(dsl: WorkflowDSL, parsed: dict[str, ParsedNode]) -> Graph:
    """Requires a structurally valid DSL (phase 1 without errors)."""
    edges = list(dsl.edges)
    out = {node_id: {handle: [] for handle in pn.spec.handles(pn.config)} for node_id, pn in parsed.items()}
    for edge in edges:
        out[edge.source][edge.sourceHandle].append(edge)
    adjacency = _adjacency(parsed, edges)
    back = _back_edge_ids(adjacency)
    reachable = _reach("start", adjacency)
    incoming: dict[str, list[Edge]] = {node_id: [] for node_id in parsed}
    for edge in edges:
        if edge.id not in back and edge.source in reachable:
            incoming[edge.target].append(edge)
    order = _topological_order(list(parsed), incoming, reachable)
    return Graph(parsed, edges, out, incoming, {e.id: e for e in edges if e.id in back}, reachable, order)


def _back_edge_ids(adjacency: dict[str, list[Edge]]) -> set[str]:
    state: dict[str, int] = {}  # 1 = on DFS stack, 2 = finished
    back: set[str] = set()

    def visit(node_id: str) -> None:
        state[node_id] = 1
        for edge in adjacency[node_id]:
            mark = state.get(edge.target, 0)
            if mark == 1:
                back.add(edge.id)
            elif mark == 0:
                visit(edge.target)
        state[node_id] = 2

    visit("start")
    return back


def _reach(origin: str, adjacency: dict[str, list[Edge]]) -> set[str]:
    seen = {origin}
    stack = [origin]
    while stack:
        for edge in adjacency[stack.pop()]:
            if edge.target not in seen:
                seen.add(edge.target)
                stack.append(edge.target)
    return seen


def _topological_order(node_ids: list[str], incoming: dict[str, list[Edge]], reachable: set[str]) -> list[str]:
    indegree = {node_id: len(incoming[node_id]) for node_id in reachable}
    children: dict[str, list[str]] = {node_id: [] for node_id in reachable}
    for node_id in reachable:
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
    return issues[:MAX_ISSUES]


def _check_reachability(graph: Graph) -> list[Issue]:
    reverse: dict[str, list[Edge]] = {node_id: [] for node_id in graph.nodes}
    for edge in graph.edges:
        reverse[edge.target].append(Edge(id=edge.id, source=edge.target, target=edge.source))
    reaches_end = _reach("end", reverse)
    issues = []
    for node_id in graph.nodes:
        if node_id not in graph.reachable:
            issues.append(error("UNREACHABLE_FROM_START", "시작 노드에서 도달할 수 없는 노드입니다", nodeId=node_id))
        elif node_id not in reaches_end:
            issues.append(error("CANNOT_REACH_END", "끝 노드에 도달할 수 없는 노드입니다", nodeId=node_id))
    return issues


def _check_handles(graph: Graph) -> list[Issue]:
    return [
        error("HANDLE_NOT_CONNECTED", f"'{handle}' 출력이 연결되지 않았습니다", nodeId=node_id, field=f"handles.{handle}")
        for node_id, handles in graph.out.items()
        for handle, edges in handles.items()
        if not edges
    ]


def _check_loops(graph: Graph) -> list[Issue]:
    issues: list[Issue] = []
    if len(graph.back_edges) > MAX_BACK_EDGES:
        issues.append(error("LIMIT_EXCEEDED", f"되돌아가는 연결은 최대 {MAX_BACK_EDGES}개까지 사용할 수 있습니다"))
    for edge in graph.back_edges.values():
        if graph.nodes[edge.source].spec.type not in LOOP_SOURCES:
            issues.append(error("ILLEGAL_CYCLE", "순환 연결은 조건/분류 노드에서만 시작할 수 있습니다", edgeId=edge.id))
        elif edge.maxIterations is None:
            issues.append(error("BACK_EDGE_NO_LIMIT", "되돌아가는 연결에는 최대 반복 횟수(maxIterations)가 필요합니다", edgeId=edge.id))
    for edge in graph.edges:
        if edge.id not in graph.back_edges and edge.maxIterations is not None:
            issues.append(
                error("MAX_ITERATIONS_ON_FORWARD_EDGE", "최대 반복 횟수(maxIterations)는 되돌아가는 연결에만 지정할 수 있습니다",
                      edgeId=edge.id)
            )
    for node_id, handles in graph.out.items():
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


def _in_cycle(graph: Graph, node_id: str) -> bool:
    adjacency = _adjacency(graph.nodes, graph.edges)
    seen: set[str] = set()
    stack = [edge.target for edge in adjacency[node_id]]
    while stack:
        current = stack.pop()
        if current == node_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(edge.target for edge in adjacency[current])
    return False


def _walk_branch(graph: Graph, first: Edge) -> tuple[str, str] | Issue:
    """Follow one fan-out branch to its merge. Returns (merge_id, closing_edge_id) or an Issue."""
    back_targets = {edge.target for edge in graph.back_edges.values()}
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
        out_edges = [e for edges in graph.out[target_id].values() for e in edges]
        is_plain = (
            not target.spec.is_branch
            and target.spec.type not in ("start", "end")
            and len(graph.incoming[target_id]) == 1
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


def _check_parallel(graph: Graph) -> list[Issue]:
    issues: list[Issue] = []
    claimed_merges: set[str] = set()
    for node_id, handles in graph.out.items():
        for handle, edges in handles.items():
            if len(edges) < 2:
                continue
            where = {"nodeId": node_id, "field": f"handles.{handle}"}
            if len(edges) > MAX_FAN_OUT:
                issues.append(error("LIMIT_EXCEEDED", f"병렬 분기는 최대 {MAX_FAN_OUT}개까지 사용할 수 있습니다", **where))
            if _in_cycle(graph, node_id):
                issues.append(error("PARALLEL_IN_LOOP", "반복(루프) 안에서는 병렬 분기를 사용할 수 없습니다", **where))
                continue
            merges: set[str] = set()
            closing: set[str] = set()
            branch_issues: list[Issue] = []
            for edge in edges:
                result = _walk_branch(graph, edge)
                if isinstance(result, Issue):
                    branch_issues.append(result)
                else:
                    merges.add(result[0])
                    closing.add(result[1])
            claimed_merges |= merges
            if branch_issues:
                issues.extend(branch_issues)
                continue
            if len(merges) != 1:
                issues.append(error("INVALID_PARALLEL_REGION", "병렬 분기는 모두 같은 합치기 노드로 모여야 합니다", **where))
                continue
            merge_id = next(iter(merges))
            if {edge.id for edge in graph.incoming[merge_id]} != closing:
                issues.append(error("INVALID_PARALLEL_REGION",
                                    f"합치기 노드 '{merge_id}'에는 이 병렬 분기만 들어올 수 있습니다", nodeId=merge_id))
    for node_id, pn in graph.nodes.items():
        if pn.spec.type == "merge" and node_id in graph.reachable and node_id not in claimed_merges:
            issues.append(error("MERGE_WITHOUT_FAN_OUT",
                                "합치기 노드는 한 출력에서 갈라진 병렬 분기를 모아야 합니다", nodeId=node_id))
    return issues
