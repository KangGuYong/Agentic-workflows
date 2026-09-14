"""JSON handling for untrusted input: model output, rendered templates and tenant-authored JSON Schemas.

Parsing accepts standard JSON only. Tenant schemas are limited to a small keyword subset without
references, regular expressions or `uniqueItems`, and are size- and depth-bounded, so validating
data costs O(schema size x data size) and cannot recurse or backtrack.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

MAX_MESSAGE_CHARS = 200  # one validation message (jsonschema messages embed the offending value)
MAX_SCHEMA_CHARS = 32_000  # serialized size of one tenant schema
MAX_SCHEMA_DEPTH = 32  # nesting of subschemas

_SCHEMA_KEYWORDS = frozenset({"items", "additionalProperties"})  # value is one subschema (or a boolean)
_SCHEMA_LIST_KEYWORDS = frozenset({"anyOf", "oneOf"})
_SCHEMA_MAP_KEYWORDS = frozenset({"properties"})
ALLOWED_SCHEMA_KEYWORDS = (
    _SCHEMA_KEYWORDS
    | _SCHEMA_LIST_KEYWORDS
    | _SCHEMA_MAP_KEYWORDS
    | {
        "type", "required", "enum", "const", "format",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties",
        "title", "description", "default", "examples", "$comment",
    }
)


def clip(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON 표준이 아닌 값입니다: {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"표현할 수 없는 숫자입니다: {clip(text, 20)}")
    return value


def parse_json(text: str) -> Any:
    """Standard JSON only: NaN/Infinity, overflowing numbers, >4300-digit ints and too-deep nesting raise ValueError."""
    try:
        return json.loads(text, parse_float=_finite_float, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{exc.msg} ({exc.lineno}행 {exc.colno}열)") from None
    except RecursionError:
        raise ValueError("JSON 중첩이 너무 깊습니다") from None


def schema_violations(schema: dict[str, Any], value: Any) -> list[str]:
    """Up to five short violations of `schema` by `value`, ordered by path; [] when `value` is valid."""
    try:
        errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    except RecursionError:  # only reachable with schemas outside the tenant subset (e.g. recursive $ref)
        return ["(root): 값의 중첩이 너무 깊어 검증할 수 없습니다"]
    return [f"{clip('/'.join(map(str, e.path))) or '(root)'}: {clip(e.message)}" for e in errors[:5]]


def _subschemas(schema: dict[str, Any]) -> Iterator[Any]:
    for key in _SCHEMA_KEYWORDS & schema.keys():
        yield schema[key]
    for key in _SCHEMA_LIST_KEYWORDS & schema.keys():
        yield from schema[key]
    for key in _SCHEMA_MAP_KEYWORDS & schema.keys():
        yield from schema[key].values()


def _subset_problems(schema: Any, depth: int, problems: dict[str, None]) -> None:
    if not isinstance(schema, dict):  # boolean schemas
        return
    if depth > MAX_SCHEMA_DEPTH:
        problems[f"스키마 중첩이 너무 깊습니다 (최대 {MAX_SCHEMA_DEPTH}단계)"] = None
        return
    for key in schema.keys() - ALLOWED_SCHEMA_KEYWORDS:
        if not key.startswith("x-"):  # editor extensions such as x-template are allowed
            problems[f"지원하지 않는 스키마 키워드입니다: {clip(key, 40)}"] = None
    for child in _subschemas(schema):
        _subset_problems(child, depth + 1, problems)


def schema_problems(schema: Any) -> list[str]:
    """Why a tenant JSON Schema is rejected ([] when it is accepted): invalid, too large, or outside the subset."""
    if not isinstance(schema, dict):
        return ["JSON Schema는 객체여야 합니다"]
    try:
        size = len(json.dumps(schema, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return ["JSON으로 표현할 수 없는 스키마입니다"]
    if size > MAX_SCHEMA_CHARS:
        return [f"스키마가 너무 큽니다 (최대 {MAX_SCHEMA_CHARS}자)"]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"올바른 JSON Schema가 아닙니다: {clip(exc.message)}"]
    except RecursionError:
        return [f"스키마 중첩이 너무 깊습니다 (최대 {MAX_SCHEMA_DEPTH}단계)"]
    problems: dict[str, None] = {}  # ordered set
    _subset_problems(schema, 1, problems)
    return list(problems)
