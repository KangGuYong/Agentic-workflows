from __future__ import annotations

from functools import lru_cache
from typing import Any

from jinja2 import Template, Undefined
from jinja2.environment import TemplateExpression

from engine.dsl.types import Target, coerce_runtime
from engine.errors import ErrorCode
from engine.jsondata import parse_json
from engine.templates.env import ENV, JSON_ENV, MAX_OUTPUT_CHARS, OUTPUT_TOO_LARGE_MESSAGE, json_value
from engine.templates.parser import TemplateParseError, parse_template


class TemplateRenderError(Exception):
    code = ErrorCode.TEMPLATE_ERROR


class TemplateTypeError(TemplateRenderError):
    code = ErrorCode.TYPE_MISMATCH


@lru_cache(maxsize=4096)
def _template(source: str, json_text: bool) -> Template:
    return (JSON_ENV if json_text else ENV).from_string(source)


@lru_cache(maxsize=4096)
def _expression(expression: str) -> TemplateExpression:
    return ENV.compile_expression(expression, undefined_to_none=False)


def _render_bounded(template: Template, context: dict[str, Any]) -> str:
    """Stream the output and stop as soon as it exceeds MAX_OUTPUT_CHARS."""
    chunks: list[str] = []
    size = 0
    for chunk in template.generate(context):
        size += len(chunk)
        if size > MAX_OUTPUT_CHARS:
            raise TemplateRenderError(OUTPUT_TOO_LARGE_MESSAGE)
        chunks.append(chunk)
    return "".join(chunks)


def render_template(source: str, context: dict[str, Any], target: Target) -> Any:
    """Render one template field. Whole-value templates keep the referenced value's type.

    Target "json": the template is JSON text in which every `{{ }}` inserts a JSON value (so data can never
    add JSON structure), and the result is the parsed value; a whole-value template returns the value itself.
    """
    try:
        parsed = parse_template(source)
    except TemplateParseError as exc:
        raise TemplateRenderError(str(exc)) from exc
    if parsed.problems:
        raise TemplateRenderError("; ".join(parsed.problems))
    try:
        if parsed.whole_value is not None:
            # Pass the context positionally: `**context` would collide with `generate`'s/
            # `TemplateExpression.__call__`'s own `self` parameter if a node were named "self".
            value = _expression(parsed.expression)(context)
            if isinstance(value, Undefined):
                raise TemplateRenderError(f"참조한 값이 없습니다: {parsed.expression}")
            # JSON data only, size-bounded, and a copy so nodes never share context objects.
            value = json_value(value)
        else:
            value = _render_bounded(_template(source, target == "json"), context)
            if target == "json":
                try:
                    value = parse_json(value)
                except ValueError as exc:
                    raise TemplateRenderError(f"JSON 템플릿 결과가 올바른 JSON이 아닙니다: {exc}") from exc
    except TemplateRenderError:
        raise
    except Exception as exc:
        # Templates are untrusted: any failure - SyntaxError, RecursionError, MemoryError, a
        # SecurityError from json_value, an UndefinedError, ... - is reported as a template error,
        # never a raw exception.
        raise TemplateRenderError(str(exc) or type(exc).__name__) from exc
    try:
        result = coerce_runtime(value, target)
    except TypeError as exc:
        raise TemplateTypeError(str(exc)) from exc
    except Exception as exc:
        raise TemplateRenderError(str(exc) or type(exc).__name__) from exc
    if isinstance(result, str) and len(result) > MAX_OUTPUT_CHARS:
        raise TemplateRenderError(OUTPUT_TOO_LARGE_MESSAGE)
    return result
