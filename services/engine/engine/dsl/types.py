"""Type system for template references (spec 4.5), derived from JSON Schema."""
from __future__ import annotations

import json
from typing import Any, Literal

Target = Literal["string", "number", "boolean", "object", "array", "any", "string|array"]
Severity = Literal["ok", "warning", "error"]

_JSON_TO_KIND = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
    "null": "null",
}
_RANK = {"ok": 0, "warning": 1, "error": 2}


def kinds_of(schema: dict[str, Any] | None) -> set[str]:
    """Value kinds a JSON Schema admits. {"unknown"} when the schema says nothing."""
    if not schema:
        return {"unknown"}
    branches = (schema.get("anyOf") or []) + (schema.get("oneOf") or [])
    if branches:
        kinds: set[str] = set()
        for branch in branches:
            kinds |= kinds_of(branch)
        return kinds
    declared = schema.get("type")
    if declared is None:
        if "enum" in schema:
            return {_kind_of_value(v) for v in schema["enum"]}
        if "properties" in schema:
            return {"object"}
        if "items" in schema:
            return {"array"}
        return {"unknown"}
    names = declared if isinstance(declared, list) else [declared]
    return {_JSON_TO_KIND.get(name, "unknown") for name in names}


def _kind_of_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _strip_null(schema: dict[str, Any]) -> dict[str, Any]:
    branches = schema.get("anyOf") or schema.get("oneOf")
    if branches:
        non_null = [b for b in branches if kinds_of(b) != {"null"}]
        if len(non_null) == 1:
            return non_null[0]
    return schema


def _is_unknown(schema: dict[str, Any]) -> bool:
    return kinds_of(schema) == {"unknown"} and "properties" not in schema and "items" not in schema


def resolve_path(schema: dict[str, Any] | None, path: tuple[str, ...]) -> dict[str, Any] | None:
    """Sub-schema at `path`. `{}` means unknown (not checkable); `None` means the field does not exist."""
    current: dict[str, Any] = schema or {}
    for key in path:
        current = _strip_null(current)
        if _is_unknown(current):
            return {}
        if key.isdigit() and "items" in current:
            current = current["items"] or {}
            continue
        props = current.get("properties")
        if props is not None:
            if key in props:
                current = props[key]
                continue
            extra = current.get("additionalProperties")
            if isinstance(extra, dict):
                current = extra
                continue
            return None
        if "object" in kinds_of(current):
            return {}
        return None
    return current


def compat(source: set[str], target: Target) -> Severity:
    """Severity of passing a value of `source` kinds into a field expecting `target` (spec 4.5 table)."""
    if target == "any":
        return "ok"
    worst: Severity = "ok"
    for kind in source or {"unknown"}:
        severity = _compat_one(kind, target)
        if _RANK[severity] > _RANK[worst]:
            worst = severity
    return worst


def _compat_one(kind: str, target: str) -> Severity:
    if kind == "null":
        return "warning"
    if kind == "unknown":
        return "ok" if target == "string" else "warning"
    if target == "string":
        return "warning" if kind in ("object", "array") else "ok"
    if target == "string|array":
        return "ok" if kind in ("string", "array") else "error"
    return "ok" if kind == target else "error"


def to_text(value: Any) -> str:
    """How a value looks when interpolated into a string."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def coerce_runtime(value: Any, target: Target) -> Any:
    """Runtime type check for a rendered field. Raises TypeError with a Korean message on mismatch."""
    if target == "any":
        return value
    if target == "string":
        return to_text(value)
    if target == "number":
        if isinstance(value, bool) or value is None:
            raise TypeError(f"숫자가 필요하지만 {_kind_of_value(value)} 값을 받았습니다")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                raise TypeError(f"숫자가 필요하지만 {value!r} 문자열을 받았습니다") from None
        raise TypeError(f"숫자가 필요하지만 {_kind_of_value(value)} 값을 받았습니다")
    if target == "string|array":
        if isinstance(value, (str, list)):
            return value
        raise TypeError(f"문자열 또는 배열이 필요하지만 {_kind_of_value(value)} 값을 받았습니다")
    expected = {"boolean": bool, "object": dict, "array": list}[target]
    if isinstance(value, expected):
        return value
    raise TypeError(f"{target} 값이 필요하지만 {_kind_of_value(value)} 값을 받았습니다")
