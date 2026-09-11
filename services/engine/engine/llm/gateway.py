from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage, ChatResult, RawLLM, TokenSink

REPAIR_PROMPT = (
    "직전 출력이 요구된 JSON 스키마를 만족하지 않습니다: {error}\n"
    "설명 없이 스키마를 만족하는 JSON만 다시 출력하세요."
)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON 표준이 아닌 값입니다: {name}")


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
        last_error = ""
        for _ in range(self._max_repairs + 1):
            result = await self._raw.complete(
                model=model, messages=conversation, format=schema, temperature=temperature, on_token=None
            )
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            try:
                data = json.loads(result.text, parse_constant=_reject_constant)
            except (ValueError, RecursionError) as exc:  # invalid JSON, NaN/Infinity, >4300-digit ints, deep nesting
                last_error = f"JSON이 아닙니다 ({getattr(exc, 'msg', None) or exc})"
            else:
                errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
                if not errors:
                    return ChatResult(text=result.text, data=data, tokens_in=tokens_in, tokens_out=tokens_out)
                last_error = "; ".join(
                    f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors[:5]
                )
            conversation = [
                *conversation,
                ChatMessage("assistant", result.text),
                ChatMessage("user", REPAIR_PROMPT.format(error=last_error)),
            ]
        raise NodeError(ErrorCode.STRUCTURED_OUTPUT_FAILED, f"구조화 출력 검증 실패: {last_error}", retryable=True)
