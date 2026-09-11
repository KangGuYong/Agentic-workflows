from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

TokenSink = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class ChatResult:
    text: str
    data: Any = None  # parsed JSON when a schema was requested
    tokens_in: int = 0
    tokens_out: int = 0


class LLMClient(Protocol):
    """What nodes call. With `schema`, `data` holds a value validated against it."""

    async def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
        on_token: TokenSink | None = None,
    ) -> ChatResult: ...


class RawLLM(Protocol):
    """Transport to a model server. `format` is a JSON Schema the server should constrain output to."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        format: dict[str, Any] | None,
        temperature: float,
        on_token: TokenSink | None,
    ) -> ChatResult: ...
