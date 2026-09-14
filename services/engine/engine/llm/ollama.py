from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage, ChatResult, TokenSink

MAX_RESPONSE_CHARS = 8_000_000  # a stuck model can generate forever; closing the stream also stops generation
CONNECT_TIMEOUT = 10.0  # Ollama is on the local network: fail fast when it is down


class OllamaRaw:
    """RawLLM over Ollama's native /api/chat endpoint."""

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None, timeout: float = 600) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        format: dict[str, Any] | None,
        temperature: float,
        on_token: TokenSink | None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": on_token is not None,
            "options": {"temperature": temperature},
        }
        if format is not None:
            body["format"] = format
        try:
            async with self._client.stream("POST", f"{self._base}/api/chat", json=body) as response:
                if response.status_code >= 400:
                    _raise_for_status(response.status_code, "\n".join([line async for line in _lines(response)]), model)
                if on_token is None:
                    data = _decode("\n".join([line async for line in _lines(response)]))
                    return _result(_content(data), data)
                return await _collect(response, on_token)
        except httpx.RequestError as exc:  # transport, protocol and content-decoding failures
            raise _unavailable(f"Ollama 연결 실패: {exc}") from exc


async def _lines(response: httpx.Response) -> AsyncIterator[str]:
    """Response text split into lines; stops (closing the stream) once MAX_RESPONSE_CHARS have arrived."""
    buffer = ""
    size = 0
    async for text in response.aiter_text():
        size += len(text)
        if size > MAX_RESPONSE_CHARS:
            raise NodeError(
                ErrorCode.OUTPUT_TOO_LARGE, f"Ollama 응답이 너무 큽니다 (최대 {MAX_RESPONSE_CHARS}자)", retryable=False
            )
        buffer += text
        if "\n" in text:
            *complete, buffer = buffer.split("\n")
            for line in complete:
                yield line
    yield buffer


async def _collect(response: httpx.Response, on_token: TokenSink) -> ChatResult:
    parts: list[str] = []
    async for line in _lines(response):
        if not line.strip():
            continue
        chunk = _decode(line)
        piece = _content(chunk)
        if piece:
            parts.append(piece)
            await on_token(piece)
        if chunk.get("done"):
            return _result("".join(parts), chunk)
    raise _unavailable("Ollama 스트림이 완료 전에 끊겼습니다")


def _unavailable(message: str) -> NodeError:
    return NodeError(ErrorCode.LLM_UNAVAILABLE, message, retryable=True)


def _decode(text: str) -> dict[str, Any]:
    """One Ollama response object (a whole body or one NDJSON line); malformed or error payloads are retryable."""
    try:
        chunk = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise _unavailable(f"Ollama 응답을 해석할 수 없습니다: {text[:200]!r}") from exc
    if not isinstance(chunk, dict):
        raise _unavailable("Ollama 응답 형식이 올바르지 않습니다")
    if "error" in chunk:
        raise _unavailable(f"Ollama 오류: {str(chunk['error'])[:200]}")
    return chunk


def _content(chunk: dict[str, Any]) -> str:
    message = chunk.get("message")
    content = message.get("content", "") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise _unavailable("Ollama 응답에 message.content가 없습니다")
    return content


def _count(chunk: dict[str, Any], key: str) -> int:
    """Token counts are metrics only: anything but a non-negative int counts as 0."""
    value = chunk.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _result(text: str, final: dict[str, Any]) -> ChatResult:
    return ChatResult(text=text, tokens_in=_count(final, "prompt_eval_count"), tokens_out=_count(final, "eval_count"))


def _raise_for_status(status: int, text: str, model: str) -> None:
    if status == 404:
        raise NodeError(ErrorCode.NODE_FAILED, f"모델을 찾을 수 없습니다: {model}", retryable=False)
    if status == 429 or status >= 500:
        raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"Ollama 오류 HTTP {status}", retryable=True)
    raise NodeError(ErrorCode.NODE_FAILED, f"Ollama 요청 오류 HTTP {status}: {text[:200]}", retryable=False)
