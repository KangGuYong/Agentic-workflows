import pytest

from engine.errors import LeaseLost, RunCancelled
from engine.nodes.base import Usage
from engine.runtime.guard import FlagGuard, NoopGuard
from engine.runtime.recorder import DuplicateAttempt, InMemoryRecorder


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
    # an execution that waited stays "resumed" after it finished, so a crash replay opens no new attempt
    assert await recorder.find_waiting("human_approval_1", 1) == 1
    assert recorder.events[-1]["handle"] == "approve"


def test_guards():
    NoopGuard().check()
    guard = FlagGuard()
    guard.check()
    guard.cancel()
    with pytest.raises(RunCancelled):
        guard.check()


async def test_recorder_rejects_a_duplicate_attempt():
    recorder = InMemoryRecorder()
    await recorder.node_started("llm_1", 1, 1, None)
    with pytest.raises(DuplicateAttempt):
        await recorder.node_started("llm_1", 1, 1, None)
    assert issubclass(DuplicateAttempt, LeaseLost)  # another worker owns the run: stop, never a node error


async def test_recorder_stores_json_copies_and_rejects_other_values():
    recorder = InMemoryRecorder()
    rendered = {"prompt": ["a"]}
    await recorder.node_started("llm_1", 1, 1, rendered)
    rendered["prompt"].append("b")
    assert recorder.for_node("llm_1")[0].input == {"prompt": ["a"]}
    with pytest.raises((TypeError, ValueError)):
        await recorder.node_succeeded("llm_1", 1, 1, {"x": float("nan")}, Usage(), defaulted=False, meta={})
    record = recorder.for_node("llm_1")[0]
    assert (record.status, record.output, len(recorder.events)) == ("running", None, 1)  # a rejected write changes nothing
    with pytest.raises(TypeError):
        await recorder.node_started("llm_2", 1, 1, {"x": {1, 2}})


async def test_find_waiting_returns_the_latest_attempt_that_waited():
    recorder = InMemoryRecorder()
    await recorder.node_started("human_approval_1", 1, 1, None)
    await recorder.node_failed("human_approval_1", 1, 1, {"code": "NODE_FAILED", "message": "x"}, will_retry=True)
    assert await recorder.find_waiting("human_approval_1", 1) is None
    await recorder.node_started("human_approval_1", 1, 2, None)
    await recorder.node_waiting("human_approval_1", 1, 2, {"message": "m"})
    await recorder.node_failed("human_approval_1", 1, 2, {"code": "NODE_FAILED", "message": "x"}, will_retry=False)
    assert await recorder.find_waiting("human_approval_1", 1) == 2


async def test_token_events_carry_no_attempt():
    recorder = InMemoryRecorder()
    await recorder.node_token("llm_1", 1, "안")
    assert recorder.events == [{"type": "node_token", "nodeId": "llm_1", "execIndex": 1, "attempt": None, "text": "안"}]


def test_lost_lease_is_distinguished_from_cancel():
    guard = FlagGuard()
    guard.lose_lease()
    with pytest.raises(LeaseLost):
        guard.check()
    assert issubclass(LeaseLost, RunCancelled)
