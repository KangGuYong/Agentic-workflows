"""Pin the LangGraph behaviors the engine relies on (spec 5.3, 5.6, 5.7).

If a LangGraph upgrade breaks one of these, the engine's execution semantics break too.
"""
import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt


def _merge(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class S(TypedDict, total=False):
    outputs: Annotated[dict[str, Any], _merge]
    log: Annotated[list[str], operator.add]


def _node(name: str, calls: dict[str, int], fail_times: int = 0):
    async def fn(state: S) -> dict:
        calls[name] = calls.get(name, 0) + 1
        if calls[name] <= fail_times:
            raise RuntimeError(f"{name} failed")
        return {"outputs": {name: calls[name]}, "log": [name]}

    return fn


def _cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


async def test_join_edge_waits_for_all_branches_of_unequal_length():
    calls: dict[str, int] = {}
    g = StateGraph(S)
    for name in ["s", "a1", "a2", "b", "m"]:
        g.add_node(name, _node(name, calls))
    g.add_edge(START, "s")
    g.add_edge("s", "a1")
    g.add_edge("s", "b")
    g.add_edge("a1", "a2")
    g.add_edge(["a2", "b"], "m")
    g.add_edge("m", END)
    app = g.compile(checkpointer=InMemorySaver())

    result = await app.ainvoke({}, _cfg("join"))

    assert calls["m"] == 1
    assert result["log"][-1] == "m"


async def test_completed_parallel_sibling_is_not_rerun_after_failure():
    calls: dict[str, int] = {}
    g = StateGraph(S)
    g.add_node("s", _node("s", calls))
    g.add_node("ok", _node("ok", calls))
    g.add_node("bad", _node("bad", calls, fail_times=1))
    g.add_node("m", _node("m", calls))
    g.add_edge(START, "s")
    g.add_edge("s", "ok")
    g.add_edge("s", "bad")
    g.add_edge(["ok", "bad"], "m")
    g.add_edge("m", END)
    app = g.compile(checkpointer=InMemorySaver())

    with pytest.raises(RuntimeError, match="bad failed"):
        await app.ainvoke({}, _cfg("pw"), durability="sync")
    assert (await app.aget_state(_cfg("pw"))).next == ("bad",)

    await app.ainvoke(None, _cfg("pw"), durability="sync")

    assert calls == {"s": 1, "ok": 1, "bad": 2, "m": 1}


async def test_interrupted_node_reexecutes_from_start_on_resume():
    calls: dict[str, int] = {}

    async def approve(state: S) -> dict:
        calls["approve"] = calls.get("approve", 0) + 1
        answer = interrupt({"ask": "ok?"})
        return {"outputs": {"approve": answer}}

    g = StateGraph(S)
    g.add_node("approve", approve)
    g.add_edge(START, "approve")
    g.add_edge("approve", END)
    app = g.compile(checkpointer=InMemorySaver())

    await app.ainvoke({}, _cfg("hitl"))
    snapshot = await app.aget_state(_cfg("hitl"))
    assert snapshot.tasks[0].interrupts[0].value == {"ask": "ok?"}

    result = await app.ainvoke(Command(resume="yes"), _cfg("hitl"))

    assert result["outputs"]["approve"] == "yes"
    assert calls["approve"] == 2


@dataclass
class Deps:
    tag: str


async def test_runtime_context_is_per_invocation_and_fresh_thread_is_empty():
    seen: list[str] = []

    async def node(state: S, runtime: Runtime[Deps]) -> dict:
        seen.append(runtime.context.tag)
        return {"outputs": {"n": runtime.context.tag}}

    g = StateGraph(S, context_schema=Deps)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    app = g.compile(checkpointer=InMemorySaver())

    assert (await app.aget_state(_cfg("never-run"))).values == {}
    await app.ainvoke({}, _cfg("c1"), context=Deps("a"))
    await app.ainvoke({}, _cfg("c2"), context=Deps("b"))

    assert seen == ["a", "b"]
