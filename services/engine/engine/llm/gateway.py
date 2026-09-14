from __future__ import annotations

import json
import math
from typing import Any

from jsonschema import Draft202012Validator

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage, ChatResult, RawLLM, TokenSink

REPAIR_PROMPT = (
    "직전 출력이 요구된 JSON 스키마를 만족하지 않습니다: {error}\n"
    "설명 없이 스키마를 만족하는 JSON만 다시 출력하세요."
)
MAX_ERROR_CHARS = 1000  # validation feedback sent back to a small model and kept in the node error
MAX_MESSAGE_CHARS = 200  # one jsonschema message (they embed the offending value)
MAX_ECHO_CHARS = 4000  # how much of an invalid answer is shown back to the model in a repair turn


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON 표준이 아닌 값입니다: {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"표현할 수 없는 숫자입니다: {text[:20]}")
    return value


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _check(text: str, validator: Draft202012Validator) -> tuple[Any, str | None]:
    """(data, None) when `text` is JSON satisfying the schema, else (None, a short Korean problem description)."""
    try:
        data = json.loads(text, parse_float=_finite_float, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:  # invalid JSON, NaN/Infinity/1e400, >4300-digit ints, deep nesting
        return None, _clip(f"JSON이 아닙니다 ({getattr(exc, 'msg', None) or exc})", MAX_ERROR_CHARS)
    try:
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    except RecursionError:  # valid JSON nested deeper than a recursive schema can be checked
        return None, "JSON 중첩이 너무 깊어 검증할 수 없습니다"
    if not errors:
        return data, None
    problems = "; ".join(
        f"{'/'.join(map(str, e.path)) or '(root)'}: {_clip(e.message, MAX_MESSAGE_CHARS)}" for e in errors[:5]
    )
    return None, _clip(problems, MAX_ERROR_CHARS)


class LLMGateway:
    """LLMClient over a RawLLM: structured output with validation and a bounded repair loop."""

    def __init__(self, raw: RawLLM, *, max_repairs: int = 2) -> None:
        self._raw = raw
        self._max_repairs = max_repairs

    async def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
        on_token: TokenSink | None = None,
    ) -> ChatResult:
        if schema is None:
            return await self._raw.complete(
                model=model, messages=messages, format=None, temperature=temperature, on_token=on_token
            )
        validator = Draft202012Validator(schema)
        conversation = list(messages)
        tokens_in = tokens_out = 0
        problem = ""
        for _ in range(self._max_repairs + 1):
            result = await self._raw.complete(
                model=model, messages=conversation, format=schema, temperature=temperature, on_token=None
            )
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            data, problem = _check(result.text, validator)
            if problem is None:
                return ChatResult(text=result.text, data=data, tokens_in=tokens_in, tokens_out=tokens_out)
            conversation = [
                *conversation,
                ChatMessage("assistant", _clip(result.text, MAX_ECHO_CHARS)),
                ChatMessage("user", REPAIR_PROMPT.format(error=problem)),
            ]
        raise NodeError(ErrorCode.STRUCTURED_OUTPUT_FAILED, f"구조화 출력 검증 실패: {problem}", retryable=True)
