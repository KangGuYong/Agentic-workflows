import json

import httpx
import pytest

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage
from engine.llm.ollama import MAX_RESPONSE_CHARS, OllamaRaw

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


async def _sink(text: str) -> None:
    return None


async def test_invalid_token_counts_count_as_zero():
    body = {"message": {"content": "응답"}, "done": True, "prompt_eval_count": "12", "eval_count": None}
    raw = _raw(lambda request: httpx.Response(200, json=body))
    result = await raw.complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=None)
    assert (result.text, result.tokens_in, result.tokens_out) == ("응답", 0, 0)


def _lazy(content: bytes) -> httpx.ByteStream:
    """A body decoded while it is read, like a network response (content= would decode inside the mock handler)."""
    return httpx.ByteStream(content)


@pytest.mark.parametrize("streaming", [False, True])
async def test_undecodable_content_encoding_is_retryable(streaming):
    gzip = {"Content-Encoding": "gzip"}
    raw = _raw(lambda request: httpx.Response(200, headers=gzip, stream=_lazy(b"not gzip")))
    with pytest.raises(NodeError) as exc:
        await raw.complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=_sink if streaming else None)
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert exc.value.retryable is True


@pytest.mark.parametrize(
    ("status", "headers", "content", "code", "retryable"),
    [
        (400, {"Content-Encoding": "gzip"}, b"not gzip", ErrorCode.NODE_FAILED, False),
        (500, {}, b"x" * (MAX_RESPONSE_CHARS + 10), ErrorCode.LLM_UNAVAILABLE, True),
    ],
    ids=["undecodable-4xx-body", "oversized-5xx-body"],
)
async def test_error_body_problems_do_not_change_classification(status, headers, content, code, retryable):
    raw = _raw(lambda request: httpx.Response(status, headers=headers, stream=_lazy(content)))
    with pytest.raises(NodeError) as exc:
        await raw.complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=None)
    assert exc.value.code == code
    assert exc.value.retryable is retryable


@pytest.mark.parametrize("streaming", [False, True])
async def test_oversized_response_stops_reading(streaming):
    line = json.dumps({"message": {"content": "x" * (MAX_RESPONSE_CHARS + 10)}, "done": False})
    raw = _raw(lambda request: httpx.Response(200, content=line.encode()))
    with pytest.raises(NodeError) as exc:
        await raw.complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=_sink if streaming else None)
    assert exc.value.code == ErrorCode.OUTPUT_TOO_LARGE
    assert exc.value.retryable is False


class _TimesOutMidStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"message": {"content": "a"}, "done": false}\n'
        raise httpx.ReadTimeout("slow")


async def test_read_timeout_mid_stream_is_retryable():
    raw = _raw(lambda request: httpx.Response(200, stream=_TimesOutMidStream()))
    with pytest.raises(NodeError) as exc:
        await raw.complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=_sink)
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE


async def test_token_sink_errors_propagate_unchanged():
    async def failing_sink(text: str) -> None:
        raise RuntimeError("sink broke")

    body = b'{"message": {"content": "a"}, "done": false}\n{"message": {"content": ""}, "done": true}\n'
    raw = _raw(lambda request: httpx.Response(200, content=body))
    with pytest.raises(RuntimeError, match="sink broke"):
        await raw.complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=failing_sink)
