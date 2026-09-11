from __future__ import annotations

from functools import lru_cache
from typing import Any

from jinja2 import Template, Undefined
from jinja2.exceptions import TemplateError

from engine.dsl.types import Target, coerce_runtime
from engine.errors import ErrorCode
from engine.templates.env import ENV, MAX_OUTPUT_CHARS
from engine.templates.parser import TemplateParseError, parse_template

# Failures raised while evaluating a template that are not Jinja errors,
# e.g. OverflowError from `round`, TypeError from `tojson` on unserializable values.
_EVALUATION_ERRORS = (TemplateError, TypeError, ValueError, ArithmeticError)


class TemplateRenderError(Exception):
    code = ErrorCode.TEMPLATE_ERROR


class TemplateTypeError(TemplateRenderError):
    code = ErrorCode.TYPE_MISMATCH


@lru_cache(maxsize=4096)
def _template(source: str) -> Template:
    return ENV.from_string(source)


@lru_cache(maxsize=4096)
def _expression(expression: str):
    return ENV.compile_expression(expression, undefined_to_none=False)


def _render_bounded(template: Template, context: dict[str, Any]) -> str:
    """Stream the output and stop as soon as it exceeds MAX_OUTPUT_CHARS."""
    chunks: list[str] = []
    size = 0
    for chunk in template.generate(**context):
        size += len(chunk)
        if size > MAX_OUTPUT_CHARS:
            raise TemplateRenderError(f"렌더링 결과가 너무 깁니다 (최대 {MAX_OUTPUT_CHARS}자)")
        chunks.append(chunk)
    return "".join(chunks)


def render_template(source: str, context: dict[str, Any], target: Target) -> Any:
    """Render one template field. Whole-value templates keep the referenced value's type."""
    try:
        parsed = parse_template(source)
    except TemplateParseError as exc:
        raise TemplateRenderError(str(exc)) from exc
    if parsed.problems:
        raise TemplateRenderError("; ".join(parsed.problems))
    try:
        if parsed.whole_value is not None:
            value = _expression(parsed.expression)(**context)
            if isinstance(value, Undefined):
                raise TemplateRenderError(f"참조한 값이 없습니다: {parsed.expression}")
        else:
            value = _render_bounded(_template(source), context)
    except _EVALUATION_ERRORS as exc:  # includes UndefinedError and SecurityError
        raise TemplateRenderError(str(exc)) from exc
    try:
        return coerce_runtime(value, target)
    except TypeError as exc:
        raise TemplateTypeError(str(exc)) from exc
