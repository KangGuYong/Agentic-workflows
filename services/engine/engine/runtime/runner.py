from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from engine.compiler.build import CompiledWorkflow
from engine.compiler.state import initial_state
from engine.errors import ErrorCode, LeaseLost, NodeError, NodeFailedError, RunCancelled
from engine.nodes.human_approval import resume_output
from engine.runtime.deps import RunDeps


class ResumeRejected(Exception):
    """The resume answer does not fit the waiting approval, or nothing is waiting. Nothing was sent to the
    graph, so the run keeps waiting and a corrected answer can still be submitted."""


@dataclass
class RunOutcome:
    status: Literal["succeeded", "waiting", "failed", "cancelled"]
    outputs: dict[str, Any] | None = None
    waiting: dict[str, Any] | None = None  # interrupt payload: nodeId, execIndex, message, review, allowEdit
    error: dict[str, Any] | None = None  # {"code", "message", "nodeId"?}


async def execute_run(
    compiled: CompiledWorkflow,
    *,
    deps: RunDeps,
    inputs: dict[str, Any] | None = None,
    resume: dict[str, Any] | None = None,
) -> RunOutcome:
    """Start, resume, or continue (crash recovery / manual retry) the run whose thread id is deps.run_id.

    Raises LeaseLost when this worker no longer owns the run: the caller must stop without writing a
    status, because the new owner continues the run from the checkpoint.
    """
    config = {"configurable": {"thread_id": deps.run_id}, "recursion_limit": compiled.recursion_limit}
    if resume is not None:
        # LangGraph keeps the first resume value of an interrupt, so a bad answer would fail every later
        # attempt: check it against the waiting payload before it reaches the graph.
        waiting = _pending_interrupt(await compiled.graph.aget_state(config))
        if waiting is None:
            raise ResumeRejected("승인을 기다리는 실행이 아닙니다")
        try:
            resume_output(resume, waiting)
        except NodeError as exc:
            raise ResumeRejected(exc.message) from exc
        graph_input: Any = Command(resume=resume)
    elif not (await compiled.graph.aget_state(config)).values:
        graph_input = initial_state(inputs or {})
    else:
        graph_input = None
    try:
        await compiled.graph.ainvoke(graph_input, config, context=deps, durability="sync")
    except NodeFailedError as exc:
        return RunOutcome("failed", error={**exc.error.to_dict(), "nodeId": exc.node_id})
    except GraphRecursionError:
        return RunOutcome(
            "failed",
            error={"code": str(ErrorCode.ENGINE_RECURSION_LIMIT), "message": "실행 단계 한도를 초과했습니다"},
        )
    except LeaseLost:
        raise
    except RunCancelled:
        return RunOutcome("cancelled")

    snapshot = await compiled.graph.aget_state(config)
    waiting = _pending_interrupt(snapshot)
    if waiting is not None:
        return RunOutcome("waiting", waiting=waiting)
    if snapshot.next:
        return RunOutcome(
            "failed",
            error={"code": str(ErrorCode.NODE_FAILED), "message": f"실행이 끝나지 않았습니다: {', '.join(snapshot.next)}"},
        )
    return RunOutcome("succeeded", outputs=snapshot.values.get("outputs", {}).get("end", {}))


def _pending_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """Payload of the pending approval (phase 2 keeps approvals out of parallel regions, so at most one)."""
    interrupts = [item for task in snapshot.tasks for item in task.interrupts]
    return interrupts[0].value if interrupts else None
