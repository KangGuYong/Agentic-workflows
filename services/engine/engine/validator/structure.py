"""Phase 1: ids, node types, configs, policies, edge references, size limits."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from engine.dsl.models import Node, Policy, WorkflowDSL, merge_policy
from engine.jsondata import StepBudget, ValidationBudgetExceeded, check_text, clip
from engine.nodes.base import NodeSpec, schema_violations
from engine.nodes.registry import NodeRegistry
from engine.templates.env import json_value
from engine.validator.issues import Issue, error

MAX_NODES = 100
MAX_EDGES = 300
MAX_ISSUES = 100  # issues reported by one phase; the rest are dropped
MAX_FIELD_ISSUES = 10  # pydantic errors reported for one config or policy
MAX_NAME_CHARS = 80  # tenant-chosen names (node types, edge ends, handles) quoted in messages
MAX_DEFAULT_OUTPUT_CHARS = 64_000  # a stand-in output needs far less than a real node output
RESERVED_IDS = frozenset(
    {
        # template roots: "secret", Jinja's "loop" and "self", the literals true/false/none, the operator "not"
        "secret", "loop", "self", "true", "false", "none", "not",
        # run state keys
        "inputs", "outputs", "routes", "loop_counters", "exec_counts",
        # names LangGraph refuses as node names when compiling (langgraph._internal._constants.RESERVED)
        "checkpoint_id", "checkpoint_map", "checkpoint_ns", "configurable",
    }
)
FIXED_IDS = {"start": "start", "end": "end"}  # node type -> required id
FIXED_LABELS = {"start": "시작", "end": "끝"}


@dataclass(frozen=True)
class ParsedNode:
    node: Node
    spec: NodeSpec
    config: BaseModel
    policy: Policy | None  # effective policy, owned by this ParsedNode; None for node types without policies


def pydantic_issues(exc: ValidationError, code: str, prefix: str, **where: Any) -> list[Issue]:
    what = {"INVALID_CONFIG": "설정 오류", "INVALID_POLICY": "실행 정책 오류"}.get(code, "워크플로 형식 오류")
    issues = []
    for err in exc.errors()[:MAX_FIELD_ISSUES]:
        location = clip(".".join(str(part) for part in err["loc"]), MAX_NAME_CHARS)
        issues.append(error(code, f"{what}: {clip(err['msg'])}", field=f"{prefix}{location}".rstrip("."), **where))
    return issues


def check_structure(
    dsl: WorkflowDSL, registry: NodeRegistry, budget: StepBudget | None = None
) -> tuple[list[Issue], dict[str, ParsedNode]]:
    """`budget` bounds the schema validation work of the whole workflow; later phases may share it."""
    budget = budget if budget is not None else StepBudget()
    issues: list[Issue] = []
    if len(dsl.nodes) > MAX_NODES:
        issues.append(error("LIMIT_EXCEEDED", f"노드는 최대 {MAX_NODES}개까지 사용할 수 있습니다"))
    if len(dsl.edges) > MAX_EDGES:
        issues.append(error("LIMIT_EXCEEDED", f"연결은 최대 {MAX_EDGES}개까지 사용할 수 있습니다"))
    if issues:  # the user has to remove nodes or edges first; node-level issues would only add noise and work
        return issues, {}

    parsed: dict[str, ParsedNode] = {}
    configs: dict[str, tuple[NodeSpec, BaseModel]] = {}  # every node whose config parsed, for handle checks
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
        configs[node.id] = (spec, config)
        policy, policy_issues = _effective_policy(node, spec, config, budget)
        issues.extend(policy_issues)
        if not policy_issues:
            parsed[node.id] = ParsedNode(node, spec, config, policy)

    if budget.remaining == 0:  # later schema checks were skipped rather than blamed on nodes that may be fine
        message = "워크플로의 기본 출력값을 검증하는 데 필요한 계산량이 너무 큽니다. 기본 출력값이나 출력 스키마를 줄여 주세요"
        issues.append(error("VALIDATION_TOO_COSTLY", message))
    for node_type, label in FIXED_LABELS.items():
        count = sum(1 for node in dsl.nodes if node.type == node_type)
        if count != 1:
            issues.append(error(f"{node_type.upper()}_COUNT", f"{label} 노드는 정확히 1개여야 합니다 (현재 {count}개)"))
    issues.extend(_check_edges(dsl, configs, node_ids))
    return issues[:MAX_ISSUES], parsed


def _check_id(node: Node) -> list[Issue]:
    if node.id in RESERVED_IDS:
        return [error("RESERVED_NODE_ID", f"'{node.id}'는 예약된 id입니다", nodeId=node.id)]
    fixed = FIXED_IDS.get(node.type)
    if fixed is not None and node.id != fixed:
        return [error("RESERVED_NODE_ID", f"{FIXED_LABELS[node.type]} 노드의 id는 '{fixed}'여야 합니다", nodeId=node.id)]
    if node.id in FIXED_IDS.values() and node.type != node.id:
        return [error("RESERVED_NODE_ID", f"'{node.id}' id는 {FIXED_LABELS[node.id]} 노드 전용입니다", nodeId=node.id)]
    return []


def _effective_policy(
    node: Node, spec: NodeSpec, config: BaseModel, budget: StepBudget
) -> tuple[Policy | None, list[Issue]]:
    if spec.default_policy is None:
        if node.policy:  # an empty policy object means no override, as in merge_policy
            return None, [
                error("POLICY_NOT_SUPPORTED", f"'{spec.label}' 노드는 실행 정책을 지원하지 않습니다",
                      nodeId=node.id, field="policy")
            ]
        return None, []
    try:
        policy = merge_policy(spec.default_policy, node.policy)
    except ValidationError as exc:
        return None, pydantic_issues(exc, "INVALID_POLICY", "policy.", nodeId=node.id)
    # A copy, so the node type's default policy is never shared; defaultOutput is replaced by a checked copy below.
    policy = policy.model_copy(update={"retry": policy.retry.model_copy()})
    if policy.defaultOutput is not None:  # checked even while onError is "fail", so switching it is safe
        checked, problem = _checked_default_output(spec, config, policy.defaultOutput, budget)
        if problem is not None:
            return None, [
                error("INVALID_POLICY", f"defaultOutput을 사용할 수 없습니다: {problem}",
                      nodeId=node.id, field="policy.defaultOutput")
            ]
        policy = policy.model_copy(update={"defaultOutput": checked})
    if policy.onError == "default" and policy.defaultOutput is None and spec.fallback_output(config) is None:
        return None, [
            error("DEFAULT_OUTPUT_REQUIRED", "onError가 default이면 defaultOutput이 필요합니다",
                  nodeId=node.id, field="policy.defaultOutput")
        ]
    return policy, []


def _checked_default_output(
    spec: NodeSpec, config: BaseModel, output: dict[str, Any], budget: StepBudget
) -> tuple[dict[str, Any] | None, str | None]:
    """A validated copy of `output` that can stand in for the node's output, or why it cannot."""
    try:
        checked = json_value(output, MAX_DEFAULT_OUTPUT_CHARS)  # JSON data only, size-bounded, a copy
        check_text(checked)  # storable as UTF-8 / jsonb
    except RecursionError:
        return None, "값의 중첩이 너무 깊습니다"
    except Exception as exc:  # ValueError/TypeError (not JSON data) or SecurityError (too large)
        return None, clip(str(exc))
    if budget.remaining == 0:  # reported once for the workflow (VALIDATION_TOO_COSTLY)
        return checked, None
    try:
        violations = schema_violations(spec.output_schema(config, {}), checked, budget=budget)
    except ValidationBudgetExceeded as exc:
        return None, clip(str(exc))
    if violations:
        return None, "노드 출력 형식과 맞지 않습니다 (" + "; ".join(violations) + ")"
    return checked, None


def _check_edges(
    dsl: WorkflowDSL, configs: dict[str, tuple[NodeSpec, BaseModel]], node_ids: set[str]
) -> list[Issue]:
    issues: list[Issue] = []
    edge_ids: set[str] = set()
    connections: set[tuple[str, str, str]] = set()
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
        connection = (edge.source, edge.sourceHandle, edge.target)
        if connection in connections:
            issues.append(error("DUPLICATE_EDGE", "같은 출력과 노드를 잇는 연결이 이미 있습니다", edgeId=edge.id))
            continue
        connections.add(connection)
        if edge.target == "start":
            issues.append(error("EDGE_INTO_START", "시작 노드로 들어오는 연결은 만들 수 없습니다", edgeId=edge.id))
        if edge.source not in configs:  # the source's config is invalid, so its handles are unknown
            continue
        spec, config = configs[edge.source]
        handles = spec.handles(config)
        if not handles:
            issues.append(
                error("EDGE_UNKNOWN_HANDLE", f"'{edge.source}' 노드에서는 연결을 시작할 수 없습니다", edgeId=edge.id)
            )
        elif edge.sourceHandle not in handles:
            handle = clip(edge.sourceHandle, MAX_NAME_CHARS)
            issues.append(
                error("EDGE_UNKNOWN_HANDLE", f"'{edge.source}' 노드에 '{handle}' 출력이 없습니다", edgeId=edge.id)
            )
    return issues
