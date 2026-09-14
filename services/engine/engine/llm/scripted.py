from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from typing import Any

from engine.llm.base import ChatMessage, ChatResult, TokenSink

Response = ChatResult | Exception | dict | str
Responder = Callable[[str, list[ChatMessage], dict | None], Response]


class ScriptedLLM:
    """Test double for LLMClient.

    `script` is a list of responses consumed in call order, or a function
    `(model, messages, schema) -> response` for order-independent (parallel) tests.
    A dict response is structured data, a str is text, an Exception is raised.
    """

    def __init__(self, script: list[Response] | Responder, *, delay: float = 0) -> None:
        self._script: list[Response] | Responder = script if callable(script) else list(script)
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
        on_token: TokenSink | None = None,
    ) -> ChatResult:
        self.calls.append({
            "model": model,
            "messages": list(messages),
            "schema": schema,
            "temperature": temperature,
            "streamed": on_token is not None,
        })
        if self._delay:
            await asyncio.sleep(self._delay)
        if callable(self._script):
            response = self._script(model, list(messages), schema)
        else:
            if not self._script:
                raise AssertionError("ScriptedLLM: no scripted response left")
            response = self._script.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            data = copy.deepcopy(response)  # callers may mutate results; the script must stay intact
            return ChatResult(text=json.dumps(data, ensure_ascii=False), data=data)
        if isinstance(response, str):
            response = ChatResult(text=response)
        if on_token is not None and response.data is None:
            await on_token(response.text)
        return response

    def prompts(self) -> list[str]:
        """The last message of every call, in call order."""
        return [call["messages"][-1].content for call in self.calls]
