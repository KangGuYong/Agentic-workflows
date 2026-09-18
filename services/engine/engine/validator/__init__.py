"""Three-phase DSL validation (spec 6.1): structure → graph → references/types."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from engine.dsl.models import WorkflowDSL
from engine.jsondata import StepBudget, check_text
from engine.nodes.registry import NodeRegistry, default_registry
from engine.validator.graph import Graph, build_graph, check_graph
from engine.validator.issues import Issue, bounded, error, has_errors
from engine.validator.refs import check_refs, compute_before, compute_schemas
from engine.validator.structure import check_structure, pydantic_issues

__all__ = ["Analysis", "Issue", "analyze", "compute_before", "compute_schemas", "has_errors",
           "validate"]

MAX_DSL_BYTES = 512 * 1024  # spec 11.1; also bounds template parsing work in phase 3


@dataclass
class Analysis:
    issues: list[Issue]
    dsl: WorkflowDSL | None = None
    graph: Graph | None = None  # set once phases 1–2 passed; callers must still check has_errors(issues)


def analyze(raw: dict[str, Any] | WorkflowDSL, registry: NodeRegistry | None = None) -> Analysis:
    """Run the phases in order, stopping after the first phase with errors. Issues are de-duplicated, errors
    come first, and at most MAX_ISSUES are kept. Schema validation in every phase shares one step budget."""
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
    # measured without filled-in defaults, close to the body the API receives
    stored = dsl.model_dump(mode="json", exclude_defaults=True)
    if len(json.dumps(stored, ensure_ascii=False).encode("utf-8")) > MAX_DSL_BYTES:
        return Analysis([error("LIMIT_EXCEEDED", f"워크플로가 너무 큽니다 (최대 {MAX_DSL_BYTES // 1024}KB)")], dsl)
    issues, parsed = check_structure(dsl, registry, StepBudget())
    if has_errors(issues):
        return Analysis(bounded(issues), dsl)
    graph = build_graph(dsl, parsed)
    issues += check_graph(graph)
    if has_errors(issues):
        return Analysis(bounded(issues), dsl)
    issues += check_refs(graph, compute_before(graph), compute_schemas(graph))
    return Analysis(bounded(issues), dsl, graph)


def validate(raw: dict[str, Any] | WorkflowDSL, registry: NodeRegistry | None = None) -> list[Issue]:
    return analyze(raw, registry).issues
