from __future__ import annotations

import json
from typing import Any

import httpx

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage, ChatResult, TokenSink


class OllamaRaw:
    """RawLLM over Ollama's native /api/chat endpoint."""

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None, timeout: float = 600) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

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
        url = f"{self._base}/api/chat"
        try:
            if on_token is None:
                response = await self._client.post(url, json=body)
                _raise_for_status(response, model)
                data = _decode(response.text)
                return ChatResult(
                    text=_content(data),
                    tokens_in=data.get("prompt_eval_count", 0),
                    tokens_out=data.get("eval_count", 0),
                )
            return await self._stream(url, body, model, on_token)
        except httpx.TransportError as exc:
            raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"Ollama 연결 실패: {exc}", retryable=True) from exc

    async def _stream(self, url: str, body: dict[str, Any], model: str, on_token: TokenSink) -> ChatResult:
        parts: list[str] = []
        async with self._client.stream("POST", url, json=body) as response:
            if response.status_code >= 400:
                await response.aread()
                _raise_for_status(response, model)
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                chunk = _decode(line)
                piece = _content(chunk)
                if piece:
                    parts.append(piece)
                    await on_token(piece)
                if chunk.get("done"):
                    return ChatResult(
                        text="".join(parts),
                        tokens_in=chunk.get("prompt_eval_count", 0),
                        tokens_out=chunk.get("eval_count", 0),
                    )
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
        raise _unavailable(f"Ollama 오류: {chunk['error']}")
    return chunk


def _content(chunk: dict[str, Any]) -> str:
    message = chunk.get("message")
    content = message.get("content", "") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise _unavailable("Ollama 응답에 message.content가 없습니다")
    return content


def _raise_for_status(response: httpx.Response, model: str) -> None:
    status = response.status_code
    if status < 400:
        return
    if status == 404:
        raise NodeError(ErrorCode.NODE_FAILED, f"모델을 찾을 수 없습니다: {model}", retryable=False)
    if status == 429 or status >= 500:
        raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"Ollama 오류 HTTP {status}", retryable=True)
    raise NodeError(ErrorCode.NODE_FAILED, f"Ollama 요청 오류 HTTP {status}: {response.text[:200]}", retryable=False)
