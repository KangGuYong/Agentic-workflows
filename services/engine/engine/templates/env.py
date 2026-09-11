"""Sandboxed Jinja2 environment shared by the parser (validation) and the renderer (execution)."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jinja2 import ChainableUndefined, StrictUndefined, Undefined
from jinja2.exceptions import SecurityError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from engine.dsl.types import to_text

ALLOWED_FILTERS = frozenset({"default", "tojson", "length", "upper", "lower", "trim", "join", "round"})
MAX_SEQUENCE_LENGTH = 100_000  # longest string/list a `*` repetition may produce
MAX_EXPONENT = 64


class ChainableStrictUndefined(ChainableUndefined, StrictUndefined):
    """`a.b` on a missing `a` stays undefined (so `| default()` works); printing/iterating/testing it fails."""

    __slots__ = ()


class TemplateEnvironment(ImmutableSandboxedEnvironment):
    """Sandbox tuned for workflow data: dotted access reads mapping keys, `*` and `**` are size-capped."""

    intercepted_binops = frozenset({"*", "**"})

    def getattr(self, obj: Any, attribute: str) -> Any:
        # `{{ llm_1.data.items }}` must read the "items" key, never the dict method of that name.
        if isinstance(obj, Mapping):
            try:
                return obj[attribute]
            except (KeyError, TypeError):
                return self.undefined(obj=obj, name=attribute)
        return super().getattr(obj, attribute)

    def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
        if operator == "*":
            size = _repeat_size(left, right)
            if size is not None and size > MAX_SEQUENCE_LENGTH:
                raise SecurityError(f"반복 결과가 너무 큽니다 (최대 {MAX_SEQUENCE_LENGTH})")
        elif operator == "**" and isinstance(right, (int, float)) and abs(right) > MAX_EXPONENT:
            raise SecurityError(f"거듭제곱 지수가 너무 큽니다 (최대 {MAX_EXPONENT})")
        return super().call_binop(context, operator, left, right)


def _repeat_size(left: Any, right: Any) -> int | None:
    for sequence, count in ((left, right), (right, left)):
        if isinstance(sequence, (str, list, tuple)) and isinstance(count, int) and not isinstance(count, bool):
            return len(sequence) * count
    return None


def _finalize(value: Any) -> Any:
    return value if isinstance(value, str) else to_text(value)


def _tojson(value: Any) -> str:
    if isinstance(value, Undefined):
        value._fail_with_undefined_error()
    return json.dumps(value, ensure_ascii=False)


def make_env() -> TemplateEnvironment:
    env = TemplateEnvironment(
        undefined=ChainableStrictUndefined,
        autoescape=False,
        finalize=_finalize,
        keep_trailing_newline=True,
    )
    builtin = env.filters
    env.filters = {name: builtin[name] for name in ALLOWED_FILTERS if name != "tojson"}
    env.filters["tojson"] = _tojson
    env.tests = {}
    env.globals = {}
    return env


ENV = make_env()
