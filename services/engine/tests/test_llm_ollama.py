import json

import httpx
import pytest

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage
from engine.llm.ollama import OllamaRaw

MSG = [ChatMessage("user", "hi")]


def _raw(handler) -> OllamaRaw:
    return OllamaRaw("http://ollama:11434/", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_non_stream_request_and_response():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "응답"}, "done": True,
                  "prompt_eval_count": 12, "eval_count": 7},
        )

    result = await _raw(handler).complete(
        model="qwen2.5:14b", messages=MSG, format={"type": "object"}, temperature=0.2, on_token=None
    )

    assert seen["url"] == "http://ollama:11434/api/chat"
    assert seen["body"] == {
        "model": "qwen2.5:14b",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "options": {"temperature": 0.2},
        "format": {"type": "object"},
    }
    assert (result.text, result.tokens_in, result.tokens_out) == ("응답", 12, 7)


async def test_streaming_emits_tokens_and_collects_counts():
    lines = [
        {"message": {"content": "안"}, "done": False},
        {"message": {"content": "녕"}, "done": False},
        {"message": {"content": ""}, "done": True, "prompt_eval_count": 3, "eval_count": 2},
    ]
    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        assert "format" not in json.loads(request.content)
        return httpx.Response(200, content=body)

    tokens: list[str] = []

    async def sink(text: str) -> None:
        tokens.append(text)

    result = await _raw(handler).complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=sink)

    assert tokens == ["안", "녕"]
    assert result.text == "안녕"
    assert (result.tokens_in, result.tokens_out) == (3, 2)


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(404, ErrorCode.NODE_FAILED, False), (400, ErrorCode.NODE_FAILED, False),
     (429, ErrorCode.LLM_UNAVAILABLE, True), (503, ErrorCode.LLM_UNAVAILABLE, True)],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_http_errors_map_to_node_errors(status, code, retryable, streaming):
    async def sink(text: str) -> None:
        return None

    raw = _raw(lambda request: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(NodeError) as exc:
        await raw.complete(model="m", messages=MSG, format=None, temperature=0.7,
                           on_token=sink if streaming else None)
    assert exc.value.code == code
    assert exc.value.retryable is retryable


async def test_connection_error_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(NodeError) as exc:
        await _raw(handler).complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=None)
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert exc.value.retryable is True


@pytest.mark.parametrize(
    "body", [b"<html>proxy</html>", b'{"done": true}', b'{"error": "model crashed"}', b"[1, 2]"]
)
async def test_malformed_response_is_retryable(body):
    raw = _raw(lambda request: httpx.Response(200, content=body))
    with pytest.raises(NodeError) as exc:
        await raw.complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=None)
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert exc.value.retryable is True


@pytest.mark.parametrize(
    "body",
    [
        b'{"message": {"content": "a"}, "done": false}\nnot-json\n',
        b'{"message": {"content": "a"}, "done": false}\n',
        b'{"error": "boom"}\n',
    ],
)
async def test_broken_stream_is_retryable(body):
    async def sink(text: str) -> None:
        return None

    raw = _raw(lambda request: httpx.Response(200, content=body))
    with pytest.raises(NodeError) as exc:
        await raw.complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=sink)
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert exc.value.retryable is True
