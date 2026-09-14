"""Sandboxed Jinja2 environment shared by the parser (validation) and the renderer (execution).

Principle: templates operate on JSON data only. Every value a template builds is JSON data whose
serialized size is at most MAX_OUTPUT_CHARS, and every builder checks size before or while building:
arithmetic operators are numbers-only and integer results are limited to MAX_INT_BITS; `~` is rejected
by the parser; printing, `tojson` and `join` go through `json_value`; loops cannot nest and nesting
depth is bounded (parser.MAX_NESTING_DEPTH, parser.MAX_LOOP_DEPTH). CPU is NOT fully bounded: one loop
whose body scans the data costs O(data size²) with little output; bounding that needs a render deadline
in the worker (Plan 2).
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from jinja2 import ChainableUndefined, StrictUndefined, Undefined
from jinja2.exceptions import SecurityError
from jinja2.runtime import LoopContext
from jinja2.sandbox import ImmutableSandboxedEnvironment

from engine.dsl.types import to_text

ALLOWED_FILTERS = frozenset({"default", "tojson", "length", "upper", "lower", "trim", "join", "round"})
MAX_OUTPUT_CHARS = 1_000_000  # largest string a template may produce (join here, rendering in render.py)
MAX_INT_BITS = 4096  # largest integer `**` or `*` may produce
MAX_ROUND_PRECISION = 15  # a float holds ~17 significant digits
OUTPUT_TOO_LARGE_MESSAGE = f"렌더링 결과가 너무 깁니다 (최대 {MAX_OUTPUT_CHARS}자)"


class ChainableStrictUndefined(ChainableUndefined, StrictUndefined):
    """`a.b` on a missing `a` stays undefined (so `| default()` works); printing/iterating/testing it fails."""

    __slots__ = ()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class TemplateEnvironment(ImmutableSandboxedEnvironment):
    """Sandbox tuned for workflow data: mapping keys win over attributes, arithmetic is numbers-only."""

    intercepted_binops = frozenset({"+", "-", "*", "/", "//", "%", "**"})

    def getattr(self, obj: Any, attribute: str) -> Any:
        # `{{ llm_1.data.items }}` must read the "items" key, never the dict method of that name.
        if isinstance(obj, Mapping):
            try:
                return obj[attribute]
            except (KeyError, TypeError):
                return self.undefined(obj=obj, name=attribute)
        if isinstance(obj, LoopContext):
            # `loop.index`, `loop.first`, etc. must keep working inside `{% for %}`.
            return super().getattr(obj, attribute)
        # No attribute access on any other Python object (blocks bound methods, dunder access, ...).
        return self.undefined(obj=obj, name=attribute)

    def getitem(self, obj: Any, argument: Any) -> Any:
        if isinstance(obj, Mapping):
            try:
                return obj[argument]
            except (KeyError, TypeError):
                return self.undefined(obj=obj, name=argument)
        if isinstance(obj, (list, tuple, str)) and (
            (isinstance(argument, int) and not isinstance(argument, bool)) or isinstance(argument, slice)
        ):
            try:
                return obj[argument]
            except (IndexError, TypeError, ValueError):
                return self.undefined(obj=obj, name=argument)
        # Never fall back to `super().getitem` - it falls back to attribute access, so
        # `a.s['upper']` would otherwise return the bound `str.upper` method.
        return self.undefined(obj=obj, name=argument)

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
            and abs(left).bit_length() * right > MAX_INT_BITS
        ):
            raise SecurityError("거듭제곱 결과가 너무 큽니다")
        if (
            operator == "*"
            and isinstance(left, int)
            and isinstance(right, int)
            and left.bit_length() + right.bit_length() > MAX_INT_BITS
        ):
            raise SecurityError("곱셈 결과가 너무 큽니다")
        return super().call_binop(context, operator, left, right)


def json_value(value: Any, limit: int = MAX_OUTPUT_CHARS) -> Any:
    """Return a validated deep copy of `value` that is JSON-only data: None, bool, int, finite float,
    str, list (from list or tuple), or dict with str keys (from any Mapping).

    While copying, keeps a running lower bound of the JSON-serialized length of the value (content
    length for str/int/float, small fixed costs for None/bool, one char per list/dict bracket and
    separator, one char per dict key's colon) and raises SecurityError the instant that bound exceeds
    `limit` - before visiting the rest of the value, so a list of 10,000 references to a 1 MB string
    fails after about one element. Quote characters around strings and dict keys are deliberately not
    charged: this keeps the bound a valid (if slightly loose) lower bound while letting a value that
    will be used verbatim (e.g. a "string"-target whole-value result) fill the cap exactly - the exact
    JSON-encoded size is still checked precisely by `_tojson` and by render.py's final length check.

    Undefined anywhere raises the underlying UndefinedError; a non-finite float, a non-str dict key, or
    any other type raises ValueError/TypeError.
    """
    size = 0

    def spend(amount: int) -> None:
        nonlocal size
        size += amount
        if size > limit:
            raise SecurityError(f"값이 너무 큽니다 (최대 {limit}자)")

    def visit(v: Any) -> Any:
        if isinstance(v, Undefined):
            v._fail_with_undefined_error()
        if v is None:
            spend(4)
            return None
        if isinstance(v, bool):
            spend(4 if v else 5)
            return v
        if isinstance(v, int):
            spend(len(str(v)))
            return v
        if isinstance(v, float):
            if not math.isfinite(v):
                raise ValueError("유한하지 않은 숫자는 사용할 수 없습니다")
            spend(len(repr(v)))
            return v
        if isinstance(v, str):
            spend(len(v))
            return v
        if isinstance(v, (list, tuple)):
            spend(2)  # "[" + "]"
            out: list[Any] = []
            for i, item in enumerate(v):
                if i:
                    spend(1)  # ","
                out.append(visit(item))
            return out
        if isinstance(v, Mapping):
            spend(2)  # "{" + "}"
            result: dict[str, Any] = {}
            for i, (key, item) in enumerate(v.items()):
                if not isinstance(key, str):
                    raise TypeError(f"템플릿 값으로 쓸 수 없는 형식입니다: {type(key).__name__}")
                if i:
                    spend(1)  # ","
                spend(len(key) + 1)  # key content + ":"
                result[key] = visit(item)
            return result
        raise TypeError(f"템플릿 값으로 쓸 수 없는 형식입니다: {type(v).__name__}")

    return visit(value)


def _finalize(value: Any) -> Any:
    if isinstance(value, str):
        return value
    text = to_text(json_value(value))
    if len(text) > MAX_OUTPUT_CHARS:
        raise SecurityError(OUTPUT_TOO_LARGE_MESSAGE)
    return text


def _finalize_json(value: Any) -> str:
    """Output of `{{ }}` in JSON templates: the value as JSON, so data can never add JSON structure."""
    text = json.dumps(json_value(value), ensure_ascii=False)
    if len(text) > MAX_OUTPUT_CHARS:
        raise SecurityError(OUTPUT_TOO_LARGE_MESSAGE)
    return text


def _tojson(value: Any) -> str:
    text = json.dumps(json_value(value), ensure_ascii=False)
    if len(text) > MAX_OUTPUT_CHARS:
        raise SecurityError(f"tojson 결과가 너무 큽니다 (최대 {MAX_OUTPUT_CHARS}자)")
    return text


def _join(value: Any, separator: Any = "") -> str:
    items = [to_text(item) for item in json_value(value)]
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


def make_env(finalize: Callable[[Any], Any] = _finalize) -> TemplateEnvironment:
    env = TemplateEnvironment(
        undefined=ChainableStrictUndefined,
        autoescape=False,
        finalize=finalize,
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
JSON_ENV = make_env(_finalize_json)  # for templates whose target is "json"
