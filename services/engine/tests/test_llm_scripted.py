import pytest

from engine.llm.base import ChatMessage, ChatResult
from engine.llm.scripted import ScriptedLLM

MSG = [ChatMessage("user", "질문")]


async def test_list_script_is_consumed_in_order():
    llm = ScriptedLLM(["첫째", {"score": 1}, ChatResult(text="셋째", tokens_out=3)])
    assert (await llm.chat(model="m", messages=MSG)).text == "첫째"
    assert (await llm.chat(model="m", messages=MSG)).data == {"score": 1}
    assert (await llm.chat(model="m", messages=MSG)).tokens_out == 3
    with pytest.raises(AssertionError):
        await llm.chat(model="m", messages=MSG)


async def test_exceptions_are_raised():
    llm = ScriptedLLM([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        await llm.chat(model="m", messages=MSG)


async def test_responder_function_and_call_log():
    llm = ScriptedLLM(lambda model, messages, schema: f"{model}:{messages[-1].content}")
    result = await llm.chat(model="qwen", messages=MSG, temperature=0.1)
    assert result.text == "qwen:질문"
    assert llm.calls[0]["temperature"] == 0.1
    assert llm.prompts() == ["질문"]


async def test_text_responses_are_streamed_to_token_sink():
    tokens: list[str] = []

    async def sink(text: str) -> None:
        tokens.append(text)

    await ScriptedLLM(["토큰"]).chat(model="m", messages=MSG, on_token=sink)
    assert tokens == ["토큰"]


async def test_structured_responses_are_copies_and_calls_record_streaming():
    scripted = {"score": {"value": 1}}
    llm = ScriptedLLM([scripted, "텍스트"])

    async def sink(text: str) -> None:
        return None

    result = await llm.chat(model="m", messages=MSG)
    result.data["score"]["value"] = 99
    await llm.chat(model="m", messages=MSG, on_token=sink)

    assert scripted == {"score": {"value": 1}}
    assert [call["streamed"] for call in llm.calls] == [False, True]
