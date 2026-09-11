import pytest

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage, ChatResult
from engine.llm.gateway import LLMGateway

SCHEMA = {"type": "object", "properties": {"score": {"type": "number"}}, "required": ["score"]}
MSG = [ChatMessage("user", "평가해줘")]


class FakeRaw:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[dict] = []

    async def complete(self, *, model, messages, format, temperature, on_token):
        self.calls.append({"messages": list(messages), "format": format, "on_token": on_token})
        return ChatResult(text=self.texts.pop(0), tokens_in=10, tokens_out=5)


async def test_plain_chat_passes_through_with_token_sink():
    raw = FakeRaw(["안녕"])

    async def sink(text: str) -> None:
        return None

    result = await LLMGateway(raw).chat(model="m", messages=MSG, on_token=sink)

    assert result.text == "안녕"
    assert result.data is None
    assert raw.calls[0]["format"] is None
    assert raw.calls[0]["on_token"] is sink


async def test_structured_output_is_parsed_and_not_streamed():
    raw = FakeRaw(['{"score": 8}'])
    result = await LLMGateway(raw).chat(model="m", messages=MSG, schema=SCHEMA)
    assert result.data == {"score": 8}
    assert raw.calls[0]["format"] == SCHEMA
    assert raw.calls[0]["on_token"] is None


async def test_repairs_invalid_output_then_succeeds():
    raw = FakeRaw(["not json", '{"score": "high"}', '{"score": 7}'])

    result = await LLMGateway(raw).chat(model="m", messages=MSG, schema=SCHEMA)

    assert result.data == {"score": 7}
    assert (result.tokens_in, result.tokens_out) == (30, 15)
    third_call = raw.calls[2]["messages"]
    assert third_call[-2] == ChatMessage("assistant", '{"score": "high"}')
    assert "score" in third_call[-1].content


async def test_gives_up_after_max_repairs():
    raw = FakeRaw(["x", "y", "z"])
    with pytest.raises(NodeError) as exc:
        await LLMGateway(raw, max_repairs=2).chat(model="m", messages=MSG, schema=SCHEMA)
    assert exc.value.code == ErrorCode.STRUCTURED_OUTPUT_FAILED
    assert exc.value.retryable is True
    assert len(raw.calls) == 3


async def test_non_standard_or_pathological_json_is_repaired():
    # NaN is not JSON; 100k nested arrays raise RecursionError; a 5000-digit int exceeds Python's int limit.
    raw = FakeRaw(['{"score": NaN}', "[" * 100_000, "1" * 5000, '{"score": 5}'])

    result = await LLMGateway(raw, max_repairs=3).chat(model="m", messages=MSG, schema=SCHEMA)

    assert result.data == {"score": 5}
    assert len(raw.calls) == 4
