"""Static analysis of template fields: references, whole-value detection, forbidden constructs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from jinja2 import nodes
from jinja2.exceptions import TemplateSyntaxError

from engine.templates.env import ALLOWED_FILTERS, ENV

_FORBIDDEN = (
    nodes.Call,
    nodes.Assign,
    nodes.AssignBlock,
    nodes.Macro,
    nodes.CallBlock,
    nodes.FilterBlock,
    nodes.Include,
    nodes.Import,
    nodes.FromImport,
    nodes.Extends,
    nodes.Block,
    nodes.With,
    nodes.Scope,
    nodes.Test,
)
_WHOLE = re.compile(r"\s*\{\{-?(.*?)-?\}\}\s*", re.DOTALL)


class TemplateParseError(ValueError):
    """The template is not valid Jinja syntax."""


@dataclass(frozen=True)
class Ref:
    root: str  # node id, "start", or "secret"
    path: tuple[str, ...]  # field path below the root (a dynamic index cuts the path short)
    has_default: bool  # wrapped directly in `| default(...)`
    direct: bool  # printed directly by {{ }} (not a loop/if operand, not filtered)


@dataclass(frozen=True)
class ParsedTemplate:
    refs: tuple[Ref, ...]
    whole_value: Ref | None  # the template is exactly `{{ ref }}` or `{{ ref | default(x) }}`
    expression: str | None  # expression source of a whole-value template
    problems: tuple[str, ...]  # forbidden constructs or filters (Korean messages)


def _chain(expr: nodes.Node) -> tuple[str, tuple[str, ...], bool] | None:
    """(root, path, exact) of a Name/Getattr/Getitem chain; exact=False once a dynamic index cut the path."""
    if isinstance(expr, nodes.Name):
        return expr.name, (), True
    if isinstance(expr, nodes.Getattr):
        base = _chain(expr.node)
        if base is None:
            return None
        root, path, exact = base
        return (root, (*path, expr.attr), True) if exact else base
    if isinstance(expr, nodes.Getitem):
        base = _chain(expr.node)
        if base is None:
            return None
        root, path, exact = base
        key = expr.arg.value if isinstance(expr.arg, nodes.Const) else None
        if exact and isinstance(key, (str, int)) and not isinstance(key, bool):
            return root, (*path, str(key)), True
        return root, path, False
    return None


@lru_cache(maxsize=4096)
def parse_template(source: str) -> ParsedTemplate:
    try:
        ast = ENV.parse(source)
    except TemplateSyntaxError as exc:
        raise TemplateParseError(f"{exc.lineno}행: {exc.message}") from exc

    problems: list[str] = []
    for node in ast.find_all(_FORBIDDEN):
        problems.append(f"허용되지 않은 구문입니다: {type(node).__name__}")
    for node in ast.find_all(nodes.Filter):
        if node.name not in ALLOWED_FILTERS:
            problems.append(f"허용되지 않은 필터입니다: {node.name}")
    for node in ast.find_all(nodes.Getattr):
        if node.attr.startswith("_"):
            problems.append(f"밑줄로 시작하는 속성은 사용할 수 없습니다: {node.attr}")

    local_names = {n.name for n in ast.find_all(nodes.Name) if n.ctx != "load"} | {"loop"}
    inner = {id(n.node) for n in ast.find_all((nodes.Getattr, nodes.Getitem))}
    defaulted = {id(f.node) for f in ast.find_all(nodes.Filter) if f.name == "default"}
    direct: set[int] = set()
    for output in ast.find_all(nodes.Output):
        for expr in output.nodes:
            direct.add(id(expr))
            if isinstance(expr, nodes.Filter) and expr.name == "default":
                direct.add(id(expr.node))

    refs: list[Ref] = []
    for node in ast.find_all((nodes.Getattr, nodes.Getitem, nodes.Name)):
        if id(node) in inner or (isinstance(node, nodes.Name) and node.ctx != "load"):
            continue
        chain = _chain(node)
        if chain is None or chain[0] in local_names:
            continue
        refs.append(Ref(chain[0], chain[1], id(node) in defaulted, id(node) in direct))

    whole: Ref | None = None
    expression: str | None = None
    body = ast.body
    if len(body) == 1 and isinstance(body[0], nodes.Output) and len(body[0].nodes) == 1:
        expr = body[0].nodes[0]
        is_default = isinstance(expr, nodes.Filter) and expr.name == "default"
        target = expr.node if is_default else expr
        chain = _chain(target)
        match = _WHOLE.fullmatch(source)
        if chain is not None and chain[2] and chain[0] not in local_names and match:
            whole = Ref(chain[0], chain[1], is_default, True)
            expression = match.group(1).strip()
    return ParsedTemplate(tuple(refs), whole, expression, tuple(problems))
