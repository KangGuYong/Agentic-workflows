from __future__ import annotations

from typing import Any

from engine.errors import ErrorCode, NodeError
from engine.jsondata import ValidationBudgetExceeded, clip, parse_json, schema_violations
from engine.llm.base import ChatMessage, ChatResult, RawLLM, TokenSink

REPAIR_PROMPT = (
    "직전 출력이 요구된 JSON 스키마를 만족하지 않습니다: {error}\n"
    "설명 없이 스키마를 만족하는 JSON만 다시 출력하세요."
)
MAX_ERROR_CHARS = 1000  # validation feedback sent back to a small model and kept in the node error
MAX_ECHO_CHARS = 4000  # how much of an invalid answer is shown back to the model in a repair turn


def _check(text: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
    """(data, None) when `text` is JSON satisfying the schema, else (None, a short Korean problem description)."""
    try:
        data = parse_json(text)
    except ValueError as exc:
        return None, clip(f"JSON이 아닙니다 ({exc})", MAX_ERROR_CHARS)
    try:
        violations = schema_violations(schema, data)
    except ValidationBudgetExceeded as exc:  # the model cannot repair this, so do not spend repair calls on it
        raise NodeError(ErrorCode.OUTPUT_TOO_LARGE, f"구조화 출력 검증 실패: {exc}", retryable=False) from exc
    if violations:
        return None, clip("; ".join(violations), MAX_ERROR_CHARS)
    return data, None


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
        conversation = list(messages)
        tokens_in = tokens_out = 0
        problem = ""
        for _ in range(self._max_repairs + 1):
            result = await self._raw.complete(
                model=model, messages=conversation, format=schema, temperature=temperature, on_token=None
            )
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            data, problem = _check(result.text, schema)
            if problem is None:
                return ChatResult(text=result.text, data=data, tokens_in=tokens_in, tokens_out=tokens_out)
            conversation = [
                *conversation,
                ChatMessage("assistant", clip(result.text, MAX_ECHO_CHARS)),
                ChatMessage("user", REPAIR_PROMPT.format(error=problem)),
            ]
        raise NodeError(ErrorCode.STRUCTURED_OUTPUT_FAILED, f"구조화 출력 검증 실패: {problem}", retryable=True)
