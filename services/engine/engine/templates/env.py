"""Sandboxed Jinja2 environment shared by the parser (validation) and the renderer (execution)."""
from __future__ import annotations

import json
from typing import Any

from jinja2 import ChainableUndefined, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from engine.dsl.types import to_text

ALLOWED_FILTERS = frozenset({"default", "tojson", "length", "upper", "lower", "trim", "join", "round"})


class ChainableStrictUndefined(ChainableUndefined, StrictUndefined):
    """`a.b` on a missing `a` stays undefined (so `| default()` works); printing/iterating/testing it fails."""

    __slots__ = ()


def _finalize(value: Any) -> Any:
    return value if isinstance(value, str) else to_text(value)


def _tojson(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent)


def make_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(
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
