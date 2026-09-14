"""JSON handling for untrusted input: model output, rendered templates and tenant-authored JSON Schemas.

Parsing accepts standard JSON only. Tenant schemas use a small JSON Schema subset (no references,
regular expressions or `uniqueItems`) with bounded size, depth, fan-out and length/count bounds, and
are validated by the subset validator below instead of a general JSON Schema library: `anyOf`/`oneOf`
stop at the deciding branch, at most MAX_VIOLATIONS messages are collected, messages never copy the
data, data is only descended as deep as the schema, and scalar enum options are looked up in a set.
Every validation call and every enum/const/required/property scan is charged to a step budget
(MAX_VALIDATION_STEPS), so one validation costs bounded time whatever the accepted schema (measured at
0.5-3.2 microseconds per step), and O(depth) memory. Wide unions spend steps fastest: data whose items
match late in a 16-branch anyOf exhausts the default budget at about 0.4 MB.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_MESSAGE_CHARS = 200  # one validation message
MAX_VIOLATIONS = 5
MAX_SCHEMA_CHARS = 32_000  # serialized size of one tenant schema
MAX_SCHEMA_DEPTH = 32  # nesting of subschemas, and of enum/const values
MAX_SCHEMA_NODES = 256  # subschemas in one schema
MAX_SCHEMA_BRANCHES = 16  # entries of one anyOf/oneOf
MAX_SCHEMA_LIST = 256  # entries of one enum or required list (duplicates allowed in enum)
MAX_SCHEMA_BOUND = 10_000  # length/count bounds; Ollama expands them into grammar rules
MAX_SCHEMA_PROBLEMS = 10
MAX_VALIDATION_STEPS = 1_000_000  # work in one validation: at most about 3 s of pure Python

_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array", "null"})
_ANNOTATIONS = frozenset({"title", "description", "default", "examples", "$comment", "format"})
_NUMBER_BOUNDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
_COUNT_BOUNDS = ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties")
_BAD_TEXT = re.compile("[\x00\ud800-\udfff]")  # NUL and lone surrogates cannot be stored or sent as UTF-8


class _Enough(Exception):
    """Enough violations were collected."""


def clip(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if _is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else type(value).__name__


# ---------------------------------------------------------------- parsing


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON 표준이 아닌 값입니다: {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"표현할 수 없는 숫자입니다: {clip(text, 20)}")
    return value


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"중복된 키가 있습니다: {clip(key, 40)}")
        result[key] = value
    return result


def _check_text(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _BAD_TEXT.search(item):
                raise ValueError("NUL 문자나 짝이 없는 서로게이트는 사용할 수 없습니다")
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())


def parse_json(text: str) -> Any:
    """Standard JSON only. NaN/Infinity, overflowing numbers, >4300-digit ints, duplicate keys,
    NUL or lone surrogates, and too-deep nesting raise ValueError."""
    try:
        value = json.loads(
            text, parse_float=_finite_float, parse_constant=_reject_constant, object_pairs_hook=_unique_keys
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{exc.msg} ({exc.lineno}행 {exc.colno}열)") from None
    except RecursionError:
        raise ValueError("JSON 중첩이 너무 깊습니다") from None
    _check_text(value)
    return value


# ---------------------------------------------------------------- tenant schemas


def _too_deep(value: Any, limit: int) -> bool:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return False


class _SchemaChecker:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.nodes = 0

    def add(self, message: str) -> None:
        if len(self.problems) < MAX_SCHEMA_PROBLEMS and message not in self.problems:
            self.problems.append(message)

    def check(self, schema: Any, depth: int, where: str) -> None:
        self.nodes += 1
        if self.nodes == MAX_SCHEMA_NODES + 1:
            self.add(f"스키마가 너무 복잡합니다 (하위 스키마 최대 {MAX_SCHEMA_NODES}개)")
        if isinstance(schema, bool):
            return
        if not isinstance(schema, dict):
            self.add(f"{where}: 스키마는 객체나 true/false여야 합니다")
            return
        if depth > MAX_SCHEMA_DEPTH:
            self.add(f"스키마 중첩이 너무 깊습니다 (최대 {MAX_SCHEMA_DEPTH}단계)")
            return
        for key, value in schema.items():
            if key.startswith("x-") or key in _ANNOTATIONS:  # annotations and editor extensions never validate
                continue
            self.keyword(key, value, depth, where)

    def keyword(self, key: str, value: Any, depth: int, where: str) -> None:
        if key == "type":
            names = [value] if isinstance(value, str) else value
            if (
                not isinstance(names, list)
                or not names
                or any(not isinstance(name, str) or name not in _TYPES for name in names)
                or len(set(names)) != len(names)
            ):
                self.add(f"{where}: type은 {', '.join(sorted(_TYPES))} 중에서 중복 없이 골라야 합니다")
        elif key == "properties":
            if not isinstance(value, dict):
                self.add(f"{where}: properties는 객체여야 합니다")
                return
            for name, child in value.items():
                self.check(child, depth + 1, f"{where}.{clip(name, 40)}")
        elif key == "required":
            if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
                self.add(f"{where}: required는 문자열 배열이어야 합니다")
            elif len(set(value)) != len(value) or len(value) > MAX_SCHEMA_LIST:
                self.add(f"{where}: required는 중복 없이 최대 {MAX_SCHEMA_LIST}개입니다")
        elif key in ("items", "additionalProperties"):
            self.check(value, depth + 1, f"{where}.{key}")
        elif key in ("anyOf", "oneOf"):
            if not isinstance(value, list) or not value:
                self.add(f"{where}: {key}는 비어 있지 않은 배열이어야 합니다")
                return
            if len(value) > MAX_SCHEMA_BRANCHES:
                self.add(f"{where}: {key} 조건은 최대 {MAX_SCHEMA_BRANCHES}개입니다")
            for index, child in enumerate(value):
                self.check(child, depth + 1, f"{where}.{key}[{index}]")
        elif key == "enum":
            if not isinstance(value, list) or not value or len(value) > MAX_SCHEMA_LIST:
                self.add(f"{where}: enum은 1~{MAX_SCHEMA_LIST}개 값의 배열이어야 합니다")
            elif any(_too_deep(option, MAX_SCHEMA_DEPTH) for option in value):
                self.add(f"{where}: enum 값의 중첩이 너무 깊습니다 (최대 {MAX_SCHEMA_DEPTH}단계)")
        elif key == "const":
            if _too_deep(value, MAX_SCHEMA_DEPTH):
                self.add(f"{where}: const 값의 중첩이 너무 깊습니다 (최대 {MAX_SCHEMA_DEPTH}단계)")
        elif key in _NUMBER_BOUNDS:
            if not _is_number(value):
                self.add(f"{where}: {key}는 숫자여야 합니다")
        elif key in _COUNT_BOUNDS:
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_SCHEMA_BOUND:
                self.add(f"{where}: {key}는 0~{MAX_SCHEMA_BOUND} 사이의 정수여야 합니다")
        else:
            self.add(f"지원하지 않는 스키마 키워드입니다: {clip(key, 40)}")


def schema_problems(schema: Any) -> list[str]:
    """Why a tenant JSON Schema is rejected ([] when it is accepted): malformed, too large or complex,
    or outside the supported subset. At most MAX_SCHEMA_PROBLEMS messages, in schema order."""
    if not isinstance(schema, dict):
        return ["JSON Schema는 객체여야 합니다"]
    try:
        size = len(json.dumps(schema, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return ["JSON으로 표현할 수 없는 스키마입니다"]
    if size > MAX_SCHEMA_CHARS:
        return [f"스키마가 너무 큽니다 (최대 {MAX_SCHEMA_CHARS}자)"]
    checker = _SchemaChecker()
    checker.check(schema, 1, "(root)")
    return checker.problems


# ---------------------------------------------------------------- validation


def json_equal(a: Any, b: Any) -> bool:
    """JSON equality: 1 == 1.0, but true != 1 and "1" != 1, also inside arrays and objects."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) or _is_number(b):
        return _is_number(a) and _is_number(b) and a == b
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(map(json_equal, a, b))
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(json_equal(v, b[k]) for k, v in a.items())
    return type(a) is type(b) and a == b


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return _is_number(value) and (isinstance(value, int) or value.is_integer())
    if name == "number":
        return _is_number(value)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return isinstance(value, {"string": str, "array": list, "object": dict}[name])


def _path_text(path: list[str | int]) -> str:
    return clip("/".join(clip(str(part), 40) for part in path)) or "(root)"


def _scalar_key(value: Any) -> tuple[str, Any] | None:
    """Hashable identity of a JSON scalar under JSON equality (1 == 1.0, true != 1); None for containers."""
    if isinstance(value, bool):
        return ("boolean", value)
    if _is_number(value):
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if value is None:
        return ("null", None)
    return None


def _size(value: Any) -> int:
    """JSON nodes in a value (enum/const values are depth-bounded by schema_problems)."""
    if isinstance(value, list):
        return 1 + sum(map(_size, value))
    if isinstance(value, dict):
        return 1 + sum(map(_size, value.values()))
    return 1


class _Invalid(Exception):
    """A branch being tested by `is_valid` failed."""


class _TooMuchWork(Exception):
    """The validation step budget ran out."""


class ValidationBudgetExceeded(Exception):
    """Validating a value would take more than the step budget (the value is too large or complex)."""


class _Validator:
    """One validation run: collects messages, tracks the data path, and charges its work to a step budget."""

    def __init__(self, limit: int, max_steps: int) -> None:
        self.limit = limit
        self.max_steps = max_steps
        self.found: list[str] = []
        self.path: list[str | int] = []
        self.steps = 0
        self.quiet = 0  # > 0 while testing anyOf/oneOf branches: failures only need a yes/no
        self._enums: dict[int, tuple[list[Any], frozenset[tuple[str, Any]], list[tuple[Any, int]]]] = {}

    def charge(self, steps: int) -> None:
        self.steps += steps
        if self.steps > self.max_steps:
            raise _TooMuchWork

    def fail(self, message: str) -> None:
        if self.quiet:
            raise _Invalid
        self.found.append(clip(f"{_path_text(self.path)}: {message}"))
        if len(self.found) >= self.limit:
            raise _Enough

    def enum_contains(self, options: list[Any], value: Any) -> bool:
        entry = self._enums.get(id(options))
        if entry is None or entry[0] is not options:  # the entry keeps `options` alive, so ids cannot be reused
            scalars = frozenset(key for key in map(_scalar_key, options) if key is not None)
            containers = [(option, _size(option)) for option in options if _scalar_key(option) is None]
            entry = self._enums[id(options)] = (options, scalars, containers)
        _, scalars, containers = entry
        key = _scalar_key(value)
        if key is not None:
            return key in scalars
        for option, size in containers:
            self.charge(size)
            if json_equal(value, option):
                return True
        return False

    def is_valid(self, schema: Any, value: Any) -> bool:
        depth = len(self.path)
        self.quiet += 1
        try:
            self.validate(schema, value)
        except _Invalid:
            return False
        finally:
            self.quiet -= 1
            del self.path[depth:]
        return True

    def validate(self, schema: Any, value: Any) -> None:
        self.charge(1)
        if schema is True:
            return
        if schema is False:
            self.fail("값이 허용되지 않습니다")
            return
        declared = schema.get("type")
        if declared is not None:
            names = (declared,) if isinstance(declared, str) else declared
            if not any(_is_type(value, name) for name in names):
                self.fail(f"{' 또는 '.join(names)} 타입이어야 하지만 {_kind(value)} 값입니다")
                return  # the remaining keywords would only restate the mismatch
        if "enum" in schema and not self.enum_contains(schema["enum"], value):
            self.fail("허용된 값 중 하나여야 합니다")
        if "const" in schema:
            self.charge(_size(schema["const"]))
            if not json_equal(value, schema["const"]):
                self.fail("정해진 값과 같아야 합니다")
        if "anyOf" in schema and not any(self.is_valid(branch, value) for branch in schema["anyOf"]):
            self.fail("anyOf 조건 중 하나 이상을 만족해야 합니다")
        if "oneOf" in schema:
            matches = 0
            for branch in schema["oneOf"]:
                if self.is_valid(branch, value):
                    matches += 1
                    if matches > 1:
                        break
            if matches != 1:
                self.fail("oneOf 조건 중 정확히 하나를 만족해야 합니다")
        if _is_number(value):
            if "minimum" in schema and value < schema["minimum"]:
                self.fail(f"{schema['minimum']} 이상이어야 합니다")
            if "maximum" in schema and value > schema["maximum"]:
                self.fail(f"{schema['maximum']} 이하여야 합니다")
            if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
                self.fail(f"{schema['exclusiveMinimum']}보다 커야 합니다")
            if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
                self.fail(f"{schema['exclusiveMaximum']}보다 작아야 합니다")
        elif isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                self.fail(f"길이가 {schema['minLength']}자 이상이어야 합니다")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                self.fail(f"길이가 {schema['maxLength']}자 이하여야 합니다")
        elif isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                self.fail(f"항목이 {schema['minItems']}개 이상이어야 합니다")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                self.fail(f"항목이 {schema['maxItems']}개 이하여야 합니다")
            items = schema.get("items")
            if items is not None and items is not True:
                path = self.path
                for index, item in enumerate(value):
                    path.append(index)
                    self.validate(items, item)
                    path.pop()
        elif isinstance(value, dict):
            if "minProperties" in schema and len(value) < schema["minProperties"]:
                self.fail(f"필드가 {schema['minProperties']}개 이상이어야 합니다")
            if "maxProperties" in schema and len(value) > schema["maxProperties"]:
                self.fail(f"필드가 {schema['maxProperties']}개 이하여야 합니다")
            required = schema.get("required", ())
            self.charge(len(required))
            for name in required:
                if name not in value:
                    self.fail(f"필수 필드가 없습니다: {clip(name, 40)}")
            properties = schema.get("properties", {})
            if len(properties) <= len(value):  # walk the smaller side
                present = [name for name in properties if name in value]
            else:
                present = [name for name in value if name in properties]
            self.charge(min(len(properties), len(value)))
            path = self.path
            for name in present:
                path.append(name)
                self.validate(properties[name], value[name])
                path.pop()
            extra = schema.get("additionalProperties")
            if extra is not None and extra is not True:
                for name, item in value.items():
                    if name in properties:
                        continue
                    if extra is False:
                        self.fail(f"허용되지 않은 필드입니다: {clip(name, 40)}")
                    else:
                        path.append(name)
                        self.validate(extra, item)
                        path.pop()


def schema_violations(schema: dict[str, Any], value: Any, *, max_steps: int = MAX_VALIDATION_STEPS) -> list[str]:
    """Up to MAX_VIOLATIONS short violations of `value` against a schema accepted by `schema_problems`
    ([] when `value` is valid). Raises ValidationBudgetExceeded instead of blocking the caller when the
    check needs more than `max_steps` steps; that is not a violation the value's author can fix."""
    validator = _Validator(MAX_VIOLATIONS, max_steps)
    try:
        validator.validate(schema, value)
    except _Enough:
        pass
    except _TooMuchWork:
        raise ValidationBudgetExceeded("값이 너무 크거나 복잡해서 스키마 검증을 끝낼 수 없습니다") from None
    return validator.found
