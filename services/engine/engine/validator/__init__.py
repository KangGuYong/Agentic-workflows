"""Three-phase DSL validation (spec 6.1): structure → graph → references/types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from engine.dsl.models import WorkflowDSL
from engine.jsondata import StepBudget, check_text
from engine.nodes.registry import NodeRegistry, default_registry
from engine.validator.graph import Graph, build_graph, check_graph
from engine.validator.issues import Issue, error, has_errors
from engine.validator.refs import check_refs, compute_before, compute_schemas
from engine.validator.structure import MAX_ISSUES, check_structure, pydantic_issues

__all__ = ["Analysis", "Issue", "analyze", "has_errors", "validate"]


@dataclass
class Analysis:
    issues: list[Issue]
    dsl: WorkflowDSL | None = None
    graph: Graph | None = None  # set once phases 1–2 passed


def analyze(raw: dict[str, Any] | WorkflowDSL, registry: NodeRegistry | None = None) -> Analysis:
    """Run the phases in order, stopping after the first phase with errors. Issues are de-duplicated and
    capped at MAX_ISSUES. Schema validation in every phase shares one step budget."""
    registry = registry or default_registry()
    if isinstance(raw, WorkflowDSL):
        dsl = raw
    else:
        try:
            dsl = WorkflowDSL.model_validate(raw)
        except ValidationError as exc:
            return Analysis(pydantic_issues(exc, "DSL_INVALID", ""))
    try:
        data = dsl.model_dump(mode="json")
    except (ValueError, RecursionError):  # pydantic refuses values nested too deeply to serialize
        return Analysis([error("DSL_INVALID", "워크플로 형식 오류: 값의 중첩이 너무 깊습니다")], dsl)
    try:
        check_text(data)  # the DSL is stored as jsonb and its text reaches node outputs
    except ValueError as exc:
        return Analysis([error("DSL_INVALID", f"워크플로 형식 오류: {exc}")], dsl)
    issues, parsed = check_structure(dsl, registry, StepBudget())
    if has_errors(issues):
        return Analysis(_final(issues), dsl)
    graph = build_graph(dsl, parsed)
    issues += check_graph(graph)
    if has_errors(issues):
        return Analysis(_final(issues), dsl)
    issues += check_refs(graph, compute_before(graph), compute_schemas(graph))
    return Analysis(_final(issues), dsl, graph)


def _final(issues: list[Issue]) -> list[Issue]:
    return list(dict.fromkeys(issues))[:MAX_ISSUES]


def validate(raw: dict[str, Any] | WorkflowDSL, registry: NodeRegistry | None = None) -> list[Issue]:
    return analyze(raw, registry).issues
