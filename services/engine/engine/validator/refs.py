"""Phase 3: guaranteed-before sets, output schemas, template references and types."""
from __future__ import annotations

import math
from typing import Any

from engine.dsl.types import compat, kinds_of, resolve_path
from engine.jsondata import clip
from engine.nodes.base import TemplateField
from engine.templates.parser import ParsedTemplate, Ref, TemplateParseError, parse_template
from engine.validator.graph import Graph
from engine.validator.issues import Issue, error, warning

MAX_LABEL_CHARS = 80  # tenant-written references and literals quoted in messages


def compute_before(graph: Graph) -> dict[str, frozenset[str]]:
    """Nodes guaranteed to have run before each node (spec 4.4): ∩ over predecessors, ∪ for merge."""
    before: dict[str, frozenset[str]] = {}
    for node_id in graph.order:
        preds = [edge.source for edge in graph.incoming[node_id]]
        if not preds:
            before[node_id] = frozenset()
            continue
        sets = [before[pred] | {pred} for pred in preds]
        if graph.nodes[node_id].spec.type == "merge":
            before[node_id] = frozenset().union(*sets)
        else:
            before[node_id] = frozenset.intersection(*sets)
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
    if template_field.target == "number":
        if not parsed.refs and not _is_number(template_field.source):
            literal = clip(repr(template_field.source), MAX_LABEL_CHARS)
            issues.append(error("LITERAL_NOT_NUMBER", f"숫자가 필요합니다: {literal}", **where))
        elif parsed.refs and parsed.whole_value is None:
            issues.append(warning("TYPE_WARNING", "문자열로 조합된 값은 실행 시 숫자로 변환됩니다", **where))
    return issues


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
    if ref.root == "secret":
        return [error("SECRET_NOT_ALLOWED", "시크릿은 HTTP 요청 노드에서만 참조할 수 있습니다", **where)]
    if ref.root not in graph.nodes or ref.root == "end":
        return [error("REF_UNKNOWN_NODE", f"존재하지 않거나 참조할 수 없는 노드입니다: {ref.root}", **where)]
    issues: list[Issue] = []
    if ref.root not in guaranteed and not ref.has_default:
        issues.append(error(
            "REF_NOT_GUARANTEED",
            f"'{ref.root}' 노드가 항상 먼저 실행된다는 보장이 없습니다. | default(...)를 붙이세요: {label}",
            **where,
        ))
    sub_schema = resolve_path(schemas.get(ref.root), ref.path)
    if sub_schema is None:
        issues.append(error("REF_UNKNOWN_FIELD", f"'{ref.root}' 노드 출력에 없는 필드입니다: {label}", **where))
        return issues
    kinds = kinds_of(sub_schema)
    if ref.has_default:
        kinds = (kinds - {"null"}) or {"unknown"}
    shown = ", ".join(sorted(kinds))
    if parsed.whole_value == ref:
        severity = compat(kinds, template_field.target)
        if severity == "error":
            issues.append(error("TYPE_INCOMPATIBLE",
                                f"{label} 값({shown})은 {template_field.target} 필드에 넣을 수 없습니다", **where))
        elif severity == "warning":
            issues.append(warning("TYPE_WARNING",
                                  f"{label} 값({shown})이 {template_field.target} 형식인지 실행 시 확인합니다", **where))
    elif ref.direct and kinds & {"object", "array", "null"}:
        issues.append(warning("TYPE_WARNING",
                              f"{label} 값이 문자열로 바뀌어 들어갑니다 (객체·배열은 JSON, 빈 값은 빈 문자열)", **where))
    return issues
