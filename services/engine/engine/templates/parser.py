"""Static analysis of template fields: references, whole-value detection, forbidden constructs."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from jinja2 import nodes
from jinja2.exceptions import TemplateSyntaxError

from engine.templates.env import ALLOWED_FILTERS, ENV

MAX_TEMPLATE_LENGTH = 20_000
MAX_LOOP_DEPTH = 1
# Python fails to compile Jinja's generated code around AST depth ~200 for expression chains
# (unary/filter/path chains) and ~102 for nested `{% if %}` blocks. 50 keeps >=2x margin on both.
MAX_NESTING_DEPTH = 50
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
    """The template is not valid Jinja syntax, or is too long / too deeply nested to analyze."""


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


def _loop_names(target: nodes.Node) -> set[str]:
    if isinstance(target, nodes.Name):
        return {target.name}
    return {name.name for name in target.find_all(nodes.Name)}


def _walk(node: nodes.Node, scope: frozenset[str], visit: Callable[[nodes.Node, frozenset[str]], None]) -> None:
    """Pre-order walk that knows which names are loop variables at each point (loop scope ≠ else scope)."""
    if isinstance(node, nodes.For):
        _walk(node.iter, scope, visit)
        inner = scope | _loop_names(node.target) | {"loop"}
        for child in node.body:
            _walk(child, inner, visit)
        if node.test is not None:
            _walk(node.test, inner, visit)
        for child in node.else_:
            _walk(child, scope, visit)
        return
    visit(node, scope)
    for child in node.iter_child_nodes():
        _walk(child, scope, visit)


def _loop_depth(node: nodes.Node) -> int:
    deepest = max((_loop_depth(child) for child in node.iter_child_nodes()), default=0)
    return deepest + 1 if isinstance(node, nodes.For) else deepest


def _ast_depth(node: nodes.Node) -> int:
    """Depth of the whole AST; the node passed in (typically the `Template` node) counts as 1."""
    deepest = max((_ast_depth(child) for child in node.iter_child_nodes()), default=0)
    return deepest + 1


@lru_cache(maxsize=4096)
def parse_template(source: str) -> ParsedTemplate:
    if len(source) > MAX_TEMPLATE_LENGTH:
        raise TemplateParseError(f"템플릿이 너무 깁니다 (최대 {MAX_TEMPLATE_LENGTH}자)")
    try:
        return _analyze(source)
    except TemplateSyntaxError as exc:
        raise TemplateParseError(f"{exc.lineno}행: {exc.message}") from exc
    except RecursionError as exc:
        raise TemplateParseError("템플릿 중첩이 너무 깊습니다") from exc


def _analyze(source: str) -> ParsedTemplate:
    ast = ENV.parse(source)

    problems: list[str] = []
    for node in ast.find_all(_FORBIDDEN):
        problems.append(f"허용되지 않은 구문입니다: {type(node).__name__}")
    for node in ast.find_all(nodes.Filter):
        if node.name not in ALLOWED_FILTERS:
            problems.append(f"허용되지 않은 필터입니다: {node.name}")
    for node in ast.find_all(nodes.Getattr):
        if node.attr.startswith("_"):
            problems.append(f"밑줄로 시작하는 속성은 사용할 수 없습니다: {node.attr}")
    for node in ast.find_all(nodes.Getitem):
        key = node.arg.value if isinstance(node.arg, nodes.Const) else None
        if isinstance(key, str) and key.startswith("_"):
            problems.append(f"밑줄로 시작하는 키는 사용할 수 없습니다: {key}")
    if _loop_depth(ast) > MAX_LOOP_DEPTH:
        problems.append(f"반복문은 최대 {MAX_LOOP_DEPTH}단계까지만 중첩할 수 있습니다")
    if _ast_depth(ast) > MAX_NESTING_DEPTH:
        problems.append(f"템플릿 중첩이 너무 깊습니다 (최대 {MAX_NESTING_DEPTH}단계)")
    if next(ast.find_all(nodes.Concat), None) is not None:
        problems.append("'~' 연산자는 사용할 수 없습니다. 값을 이어 붙이려면 {{ a }}{{ b }}처럼 나란히 쓰세요")

    inner = {id(n.node) for n in ast.find_all((nodes.Getattr, nodes.Getitem))}
    defaulted = {id(f.node) for f in ast.find_all(nodes.Filter) if f.name == "default"}
    direct: set[int] = set()
    for output in ast.find_all(nodes.Output):
        for expr in output.nodes:
            direct.add(id(expr))
            if isinstance(expr, nodes.Filter) and expr.name == "default":
                direct.add(id(expr.node))

    refs: list[Ref] = []

    def collect(node: nodes.Node, scope: frozenset[str]) -> None:
        if not isinstance(node, (nodes.Getattr, nodes.Getitem, nodes.Name)) or id(node) in inner:
            return
        if isinstance(node, nodes.Name) and node.ctx != "load":
            return
        chain = _chain(node)
        if chain is not None and chain[0] not in scope:
            refs.append(Ref(chain[0], chain[1], id(node) in defaulted, id(node) in direct))

    _walk(ast, frozenset(), collect)

    whole: Ref | None = None
    expression: str | None = None
    body = ast.body
    if len(body) == 1 and isinstance(body[0], nodes.Output) and len(body[0].nodes) == 1:
        expr = body[0].nodes[0]
        is_default = isinstance(expr, nodes.Filter) and expr.name == "default"
        target = expr.node if is_default else expr
        chain = _chain(target)
        match = _WHOLE.fullmatch(source)
        if chain is not None and chain[2] and match:
            whole = Ref(chain[0], chain[1], is_default, True)
            expression = match.group(1).strip()
    return ParsedTemplate(tuple(refs), whole, expression, tuple(problems))
