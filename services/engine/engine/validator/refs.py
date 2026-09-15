"""Phase 3: guaranteed-before sets, output schemas, template references and types."""
from __future__ import annotations

import math
from typing import Any

from engine.dsl.types import compat, kinds_of, resolve_path
from engine.jsondata import clip, parse_json
from engine.nodes.base import TemplateField
from engine.templates.parser import ParsedTemplate, Ref, TemplateParseError, parse_template
from engine.templates.render import QUOTED_SUBSTITUTION
from engine.validator.graph import Graph
from engine.validator.issues import Issue, error, warning

MAX_LABEL_CHARS = 80  # tenant-written references and literals quoted in messages
KIND_NAMES = {
    "string": "문자열",
    "number": "숫자",
    "boolean": "참/거짓",
    "object": "객체",
    "array": "목록",
    "null": "빈 값(null)",
    "unknown": "알 수 없는 형식",
}
TARGET_NAMES = {**KIND_NAMES, "string|array": "문자열 또는 목록"}


def compute_before(graph: Graph) -> dict[str, frozenset[str]]:
    """Nodes guaranteed to have run before each node (spec 4.4): ∩ over predecessors, ∪ for merge.

    Back-edges count as predecessors too: in a loop that starts at its condition (start → condition →
    body → condition), the body's only predecessor is the condition, through the back-edge. Solved as a
    greatest fixpoint (like dominators), so loops converge. Merges keep the union rule; phase 2 keeps
    them out of loops, so their predecessors are forward edges only. `graph.order` is a topological order
    of forward edges, not necessarily the order nodes first run.
    """
    predecessors: dict[str, list[str]] = {node_id: [] for node_id in graph.reachable}
    for edge in graph.edges:
        if edge.source in graph.reachable:
            predecessors[edge.target].append(edge.source)
    everything = frozenset(graph.reachable)
    before = {node_id: frozenset() if node_id == "start" else everything for node_id in graph.reachable}
    changed = True
    while changed:
        changed = False
        for node_id in graph.order:
            if node_id == "start":
                continue
            sets = [before[pred] | {pred} for pred in predecessors[node_id]]
            if graph.nodes[node_id].spec.type == "merge":
                value = frozenset().union(*sets)
            else:
                value = frozenset.intersection(*sets) if sets else frozenset()
            if value != before[node_id]:
                before[node_id] = value
                changed = True
    return before


def compute_schemas(graph: Graph) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for node_id in graph.order:
        pn = graph.nodes[node_id]
        preds = {edge.source: schemas[edge.source] for edge in graph.incoming[node_id]}
        schemas[node_id] = pn.spec.output_schema(pn.config, preds)
    return schemas


def check_refs(graph: Graph, before: dict[str, frozenset[str]], schemas: dict[str, dict[str, Any]]) -> list[Issue]:
    issues: list[Issue] = []
    for node_id in graph.order:
        pn = graph.nodes[node_id]
        for template_field in pn.spec.template_fields(pn.config):
            issues.extend(_check_field(node_id, template_field, graph, before[node_id], schemas))
    return issues


def _is_number(text: str) -> bool:
    """Whether coerce_runtime accepts `text` as a number (finite, so not "nan" or "1e400")."""
    try:
        return math.isfinite(float(text.strip()))
    except ValueError:
        return False


def _check_field(
    node_id: str,
    template_field: TemplateField,
    graph: Graph,
    guaranteed: frozenset[str],
    schemas: dict[str, dict[str, Any]],
) -> list[Issue]:
    where = {"nodeId": node_id, "field": f"config.{template_field.path}"}
    try:
        parsed = parse_template(template_field.source)
    except TemplateParseError as exc:
        return [error("TEMPLATE_SYNTAX", f"템플릿 문법 오류: {clip(str(exc))}", **where)]
    issues = [error("TEMPLATE_FORBIDDEN", clip(problem), **where) for problem in parsed.problems]
    for ref in parsed.refs:
        issues.extend(_check_ref(ref, parsed, template_field, graph, guaranteed, schemas, where))
    if template_field.target == "json":
        issues.extend(_check_json_template(template_field.source, parsed, where))
    if template_field.target == "number":
        if not parsed.refs and not _is_number(template_field.source):
            literal = clip(repr(template_field.source), MAX_LABEL_CHARS)
            issues.append(error("LITERAL_NOT_NUMBER", f"숫자가 필요합니다: {literal}", **where))
        elif parsed.refs and parsed.whole_value is None:
            issues.append(warning("TYPE_WARNING", "문자열로 조합된 값은 실행 시 숫자로 변환됩니다", **where))
    return issues


def _check_json_template(source: str, parsed: ParsedTemplate, where: dict[str, str]) -> list[Issue]:
    """JSON templates are parsed after rendering; catch what is already wrong before any run."""
    if "{{" not in source and "{%" not in source:
        try:
            parse_json(source)
        except ValueError as exc:
            return [error("TEMPLATE_SYNTAX", f"올바른 JSON이 아닙니다: {clip(str(exc))}", **where)]
        return []
    if QUOTED_SUBSTITUTION.search(source):
        return [warning("TYPE_WARNING",
                        "JSON 템플릿의 {{ }}는 JSON 값을 넣으므로 따옴표로 감싸지 마세요 (\"{{ x }}\" 대신 {{ x }})",
                        **where)]
    return []


def _kinds_text(kinds: set[str]) -> str:
    return ", ".join(KIND_NAMES.get(kind, kind) for kind in sorted(kinds))


def _check_ref(
    ref: Ref,
    parsed: ParsedTemplate,
    template_field: TemplateField,
    graph: Graph,
    guaranteed: frozenset[str],
    schemas: dict[str, dict[str, Any]],
    where: dict[str, str],
) -> list[Issue]:
    label = clip(".".join((ref.root, *ref.path)), MAX_LABEL_CHARS)
    root = clip(ref.root, MAX_LABEL_CHARS)
    if ref.root == "secret":
        return [error("SECRET_NOT_ALLOWED", "시크릿은 HTTP 요청 노드에서만 참조할 수 있습니다", **where)]
    if ref.root not in graph.nodes or ref.root == "end":
        return [error("REF_UNKNOWN_NODE", f"존재하지 않거나 참조할 수 없는 노드입니다: {root}", **where)]
    issues: list[Issue] = []
    if ref.root not in guaranteed and not ref.has_default:
        if ref.root == where["nodeId"]:
            message = f"이 노드의 이전 실행 결과는 첫 실행 때 없습니다. | default(...)를 붙이세요: {label}"
        else:
            message = f"'{root}' 노드가 항상 먼저 실행된다는 보장이 없습니다. | default(...)를 붙이세요: {label}"
        issues.append(error("REF_NOT_GUARANTEED", message, **where))
    sub_schema = resolve_path(schemas.get(ref.root), ref.path)
    if sub_schema is None:
        issues.append(error("REF_UNKNOWN_FIELD", f"'{root}' 노드 출력에 없는 필드입니다: {label}", **where))
        return issues
    kinds = kinds_of(sub_schema)
    if ref.has_default:
        # Jinja's default(x) replaces only a missing value; null stays unless default(x, true) is used.
        if ref.default_replaces_null:
            kinds = kinds - {"null"}
        kinds = kinds | {ref.default_kind or "unknown"}
    shown = _kinds_text(kinds)
    target = TARGET_NAMES.get(template_field.target, template_field.target)
    if parsed.whole_value == ref:
        severity = compat(kinds, template_field.target)
        if severity == "error":
            issues.append(error("TYPE_INCOMPATIBLE", f"{label} 값({shown})은 {target} 칸에 넣을 수 없습니다", **where))
        elif severity == "warning":
            issues.append(warning("TYPE_WARNING", f"{label} 값({shown})이 {target}인지 실행할 때 확인합니다", **where))
    elif ref.direct and kinds & {"object", "array", "null"}:
        issues.append(warning("TYPE_WARNING",
                              f"{label} 값이 문자열로 바뀌어 들어갑니다 (객체·배열은 JSON, 빈 값은 빈 문자열)", **where))
    return issues
