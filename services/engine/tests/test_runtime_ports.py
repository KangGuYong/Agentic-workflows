import pytest

from engine.errors import RunCancelled
from engine.nodes.base import Usage
from engine.runtime.guard import FlagGuard, NoopGuard
from engine.runtime.recorder import InMemoryRecorder


async def test_recorder_tracks_attempt_lifecycle():
    recorder = InMemoryRecorder()
    await recorder.node_started("llm_1", 1, 1, {"prompt": "p"})
    await recorder.node_failed("llm_1", 1, 1, {"code": "LLM_UNAVAILABLE", "message": "x"}, will_retry=True)
    await recorder.node_started("llm_1", 1, 2, {"prompt": "p"})
    await recorder.node_succeeded("llm_1", 1, 2, {"text": "t"}, Usage(3, 4), defaulted=False, meta={})

    assert await recorder.attempts_so_far("llm_1", 1) == 2
    assert [r.status for r in recorder.for_node("llm_1")] == ["failed", "succeeded"]
    assert recorder.for_node("llm_1")[1].usage == Usage(3, 4)
    assert [e["type"] for e in recorder.events] == ["node_started", "node_failed", "node_started", "node_finished"]
    assert recorder.events[1]["willRetry"] is True


async def test_recorder_waiting_and_defaulted():
    recorder = InMemoryRecorder()
    await recorder.node_started("human_approval_1", 1, 1, {})
    await recorder.node_waiting("human_approval_1", 1, 1, {"message": "m"})
    assert await recorder.find_waiting("human_approval_1", 1) == 1
    assert await recorder.find_waiting("human_approval_1", 2) is None

    await recorder.node_succeeded("human_approval_1", 1, 1, {"decision": "approve"}, Usage(), defaulted=True, meta={"handle": "approve"})
    assert recorder.for_node("human_approval_1")[0].status == "defaulted"
    assert await recorder.find_waiting("human_approval_1", 1) is None
    assert recorder.events[-1]["handle"] == "approve"


def test_guards():
    NoopGuard().check()
    guard = FlagGuard()
    guard.check()
    guard.cancel()
    with pytest.raises(RunCancelled):
        guard.check()
