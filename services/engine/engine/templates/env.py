"""Sandboxed Jinja2 environment shared by the parser (validation) and the renderer (execution).

Resource rule: no operator or filter can build a value larger than O(template length + data size) —
arithmetic operators work on numbers only, integer powers are size-bounded, `join` is size-checked,
`round` precision is clamped, and loops cannot nest (see parser.MAX_LOOP_DEPTH). The renderer caps
output size. CPU is NOT fully bounded: one loop whose body scans the data costs O(data size²) with
little output; bounding that needs a render deadline in the worker (Plan 2).
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from jinja2 import ChainableUndefined, StrictUndefined, Undefined
from jinja2.exceptions import SecurityError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from engine.dsl.types import to_text

ALLOWED_FILTERS = frozenset({"default", "tojson", "length", "upper", "lower", "trim", "join", "round"})
MAX_OUTPUT_CHARS = 1_000_000  # largest string a template may produce (join here, rendering in render.py)
MAX_POWER_BITS = 4096  # largest integer `**` may produce
MAX_ROUND_PRECISION = 15  # a float holds ~17 significant digits


class ChainableStrictUndefined(ChainableUndefined, StrictUndefined):
    """`a.b` on a missing `a` stays undefined (so `| default()` works); printing/iterating/testing it fails."""

    __slots__ = ()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class TemplateEnvironment(ImmutableSandboxedEnvironment):
    """Sandbox tuned for workflow data: mapping keys win over attributes, arithmetic is numbers-only."""

    intercepted_binops = frozenset({"*", "**", "%"})

    def getattr(self, obj: Any, attribute: str) -> Any:
        # `{{ llm_1.data.items }}` must read the "items" key, never the dict method of that name.
        if isinstance(obj, Mapping):
            try:
                return obj[attribute]
            except (KeyError, TypeError):
                return self.undefined(obj=obj, name=attribute)
        return super().getattr(obj, attribute)

    def getitem(self, obj: Any, argument: Any) -> Any:
        if isinstance(obj, Mapping) and isinstance(argument, str):
            try:
                return obj[argument]
            except KeyError:
                return self.undefined(obj=obj, name=argument)
        return super().getitem(obj, argument)

    def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
        for operand in (left, right):
            if isinstance(operand, Undefined):
                operand._fail_with_undefined_error()
        if not (_is_number(left) and _is_number(right)):
            raise SecurityError(f"'{operator}' 연산은 숫자끼리만 사용할 수 있습니다")
        if (
            operator == "**"
            and isinstance(left, int)
            and isinstance(right, int)
            and right > 0
            and abs(left) > 1
            and abs(left).bit_length() * right > MAX_POWER_BITS
        ):
            raise SecurityError("거듭제곱 결과가 너무 큽니다")
        return super().call_binop(context, operator, left, right)


def _finalize(value: Any) -> Any:
    return value if isinstance(value, str) else to_text(value)


def _tojson(value: Any) -> str:
    if isinstance(value, Undefined):
        value._fail_with_undefined_error()
    return json.dumps(value, ensure_ascii=False)


def _join(value: Any, separator: Any = "") -> str:
    if isinstance(value, Undefined):
        value._fail_with_undefined_error()
    items = [to_text(item) for item in value]
    sep = to_text(separator)
    size = sum(len(item) for item in items) + len(sep) * max(len(items) - 1, 0)
    if size > MAX_OUTPUT_CHARS:
        raise SecurityError(f"join 결과가 너무 큽니다 (최대 {MAX_OUTPUT_CHARS}자)")
    return sep.join(items)


def _round(value: Any, precision: Any = 0, method: str = "common") -> float:
    if isinstance(value, Undefined):
        value._fail_with_undefined_error()
    if not _is_number(value):
        raise TypeError(f"round는 숫자에만 사용할 수 있습니다: {value!r}")
    if not isinstance(precision, int) or isinstance(precision, bool) or not 0 <= precision <= MAX_ROUND_PRECISION:
        raise ValueError(f"round 자릿수는 0~{MAX_ROUND_PRECISION} 사이의 정수여야 합니다")
    if method == "common":
        return round(value, precision)
    if method not in ("ceil", "floor"):
        raise ValueError("round 방식은 common, ceil, floor 중 하나여야 합니다")
    factor = 10.0**precision
    rounding = math.ceil if method == "ceil" else math.floor
    return rounding(value * factor) / factor


def make_env() -> TemplateEnvironment:
    env = TemplateEnvironment(
        undefined=ChainableStrictUndefined,
        autoescape=False,
        finalize=_finalize,
        keep_trailing_newline=True,
    )
    builtin = env.filters
    env.filters = {name: builtin[name] for name in ALLOWED_FILTERS - {"tojson", "join", "round"}}
    env.filters["tojson"] = _tojson
    env.filters["join"] = _join
    env.filters["round"] = _round
    env.tests = {}
    env.globals = {}
    return env


ENV = make_env()
