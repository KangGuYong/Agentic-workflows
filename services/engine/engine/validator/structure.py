"""Phase 1: ids, node types, configs, policies, edge references, size limits."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from engine.dsl.models import Node, Policy, WorkflowDSL, merge_policy
from engine.jsondata import ValidationBudgetExceeded, check_text, clip
from engine.nodes.base import NodeSpec, schema_violations
from engine.nodes.registry import NodeRegistry
from engine.templates.env import json_value
from engine.validator.issues import Issue, error

MAX_NODES = 100
MAX_EDGES = 300
MAX_ISSUES = 100  # issues reported by one phase; the rest are dropped
MAX_FIELD_ISSUES = 10  # pydantic errors reported for one config or policy
MAX_NAME_CHARS = 80  # tenant-chosen names (node types, edge ends, handles) quoted in messages
# Template roots ("secret", Jinja's "loop" and "self", the literals true/false/none, the operator "not")
# and LangGraph state keys cannot be node ids.
RESERVED_IDS = frozenset(
    {"secret", "loop", "self", "true", "false", "none", "not", "inputs", "outputs", "routes", "loop_counters",
     "exec_counts"}
)
FIXED_IDS = {"start": "start", "end": "end"}  # node type -> required id


@dataclass(frozen=True)
class ParsedNode:
    node: Node
    spec: NodeSpec
    config: BaseModel
    policy: Policy | None  # effective policy; None for node types without policies


def pydantic_issues(exc: ValidationError, code: str, prefix: str, **where: Any) -> list[Issue]:
    issues = []
    for err in exc.errors()[:MAX_FIELD_ISSUES]:
        location = clip(".".join(str(part) for part in err["loc"]), MAX_NAME_CHARS)
        issues.append(error(code, f"설정 오류: {clip(err['msg'])}", field=f"{prefix}{location}".rstrip("."), **where))
    return issues


def check_structure(dsl: WorkflowDSL, registry: NodeRegistry) -> tuple[list[Issue], dict[str, ParsedNode]]:
    issues: list[Issue] = []
    if len(dsl.nodes) > MAX_NODES:
        issues.append(error("LIMIT_EXCEEDED", f"노드는 최대 {MAX_NODES}개까지 사용할 수 있습니다"))
    if len(dsl.edges) > MAX_EDGES:
        issues.append(error("LIMIT_EXCEEDED", f"연결은 최대 {MAX_EDGES}개까지 사용할 수 있습니다"))
    if issues:  # checking an oversized workflow node by node would only produce more noise and work
        return issues, {}

    parsed: dict[str, ParsedNode] = {}
    node_ids: set[str] = set()
    for node in dsl.nodes:
        if node.id in node_ids:
            issues.append(error("DUPLICATE_NODE_ID", f"노드 id가 중복되었습니다: {node.id}", nodeId=node.id))
            continue
        node_ids.add(node.id)
        issues.extend(_check_id(node))
        spec = registry.get(node.type)
        if spec is None:
            message = f"알 수 없는 노드 종류입니다: {clip(node.type, MAX_NAME_CHARS)}"
            issues.append(error("UNKNOWN_NODE_TYPE", message, nodeId=node.id))
            continue
        try:
            config = spec.parse_config(node.config)
        except ValidationError as exc:
            issues.extend(pydantic_issues(exc, "INVALID_CONFIG", "config.", nodeId=node.id))
            continue
        policy, policy_issues = _effective_policy(node, spec, config)
        issues.extend(policy_issues)
        if not policy_issues:
            parsed[node.id] = ParsedNode(node, spec, config, policy)

    for node_type in FIXED_IDS:
        count = sum(1 for node in dsl.nodes if node.type == node_type)
        if count != 1:
            issues.append(
                error(f"{node_type.upper()}_COUNT", f"'{node_type}' 노드는 정확히 1개여야 합니다 (현재 {count}개)")
            )
    issues.extend(_check_edges(dsl, parsed, node_ids))
    return issues[:MAX_ISSUES], parsed


def _check_id(node: Node) -> list[Issue]:
    if node.id in RESERVED_IDS:
        return [error("RESERVED_NODE_ID", f"'{node.id}'는 예약된 id입니다", nodeId=node.id)]
    fixed = FIXED_IDS.get(node.type)
    if fixed is not None and node.id != fixed:
        return [error("RESERVED_NODE_ID", f"'{node.type}' 노드의 id는 '{fixed}'여야 합니다", nodeId=node.id)]
    if node.id in FIXED_IDS.values() and node.type != node.id:
        return [error("RESERVED_NODE_ID", f"'{node.id}' id는 {node.id} 노드 전용입니다", nodeId=node.id)]
    return []


def _effective_policy(node: Node, spec: NodeSpec, config: BaseModel) -> tuple[Policy | None, list[Issue]]:
    if spec.default_policy is None:
        if node.policy is not None:
            return None, [
                error("POLICY_NOT_SUPPORTED", f"'{spec.type}' 노드는 실행 정책을 지원하지 않습니다",
                      nodeId=node.id, field="policy")
            ]
        return None, []
    try:
        policy = merge_policy(spec.default_policy, node.policy)
    except ValidationError as exc:
        return None, pydantic_issues(exc, "INVALID_POLICY", "policy.", nodeId=node.id)
    if policy.onError == "default":
        output = policy.defaultOutput if policy.defaultOutput is not None else spec.fallback_output(config)
        if output is None:
            return None, [
                error("DEFAULT_OUTPUT_REQUIRED", "onError가 default이면 defaultOutput이 필요합니다",
                      nodeId=node.id, field="policy.defaultOutput")
            ]
        problem = _default_output_problem(spec, config, output)
        if problem:
            return None, [
                error("INVALID_POLICY", f"defaultOutput이 노드 출력 형식과 맞지 않습니다: {problem}",
                      nodeId=node.id, field="policy.defaultOutput")
            ]
    return policy, []


def _default_output_problem(spec: NodeSpec, config: BaseModel, output: dict[str, Any]) -> str | None:
    """Why `output` cannot stand in for the node's output (None when it can)."""
    try:
        check_text(json_value(output))  # JSON data only, at most MAX_OUTPUT_CHARS, storable text
    except Exception as exc:  # ValueError/TypeError, SecurityError (too large) or RecursionError (too deep)
        return clip(str(exc))
    try:
        violations = schema_violations(spec.output_schema(config, {}), output)
    except ValidationBudgetExceeded as exc:
        return clip(str(exc))
    return "; ".join(violations) or None


def _check_edges(dsl: WorkflowDSL, parsed: dict[str, ParsedNode], node_ids: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    edge_ids: set[str] = set()
    for edge in dsl.edges:
        if edge.id in edge_ids:
            issues.append(error("DUPLICATE_EDGE_ID", f"연결 id가 중복되었습니다: {edge.id}", edgeId=edge.id))
            continue
        edge_ids.add(edge.id)
        missing = [clip(end, MAX_NAME_CHARS) for end in (edge.source, edge.target) if end not in node_ids]
        if missing:
            issues.append(
                error("EDGE_UNKNOWN_NODE", f"연결이 존재하지 않는 노드를 가리킵니다: {', '.join(missing)}", edgeId=edge.id)
            )
            continue
        if edge.target == "start":
            issues.append(error("EDGE_INTO_START", "시작 노드로 들어오는 연결은 만들 수 없습니다", edgeId=edge.id))
        source = parsed.get(edge.source)
        if source is not None and edge.sourceHandle not in source.spec.handles(source.config):
            handle = clip(edge.sourceHandle, MAX_NAME_CHARS)
            issues.append(
                error("EDGE_UNKNOWN_HANDLE", f"'{edge.source}' 노드에 '{handle}' 출력이 없습니다", edgeId=edge.id)
            )
    return issues
