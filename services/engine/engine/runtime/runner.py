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
    """The resume answer is unusable: it names no approval (nodeId/execIndex), names one of a run that never
    started, or does not fit the approval that is waiting for it. Nothing was sent to the graph, so the
    run keeps waiting and a corrected answer can still be submitted."""


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

    - `inputs` are used only for a run without a checkpoint; an existing run ignores them.
    - `resume` must name the approval it answers (`nodeId`, `execIndex`). When that approval is no longer
      waiting, the answer was already used before a crash, so the run simply continues from its checkpoint
      (it never answers a later approval). An answer that does not fit the waiting approval raises
      ResumeRejected.

    Raises (the worker handles these; no RunOutcome is produced):
    - LeaseLost: this worker no longer owns the run. Stop without writing a status; the new owner continues.
    - ResumeRejected: see above.
    - EngineFault or any unexpected exception: an infrastructure failure or engine bug. Release the run for
      crash recovery.
    """
    config = {"configurable": {"thread_id": deps.run_id}, "recursion_limit": compiled.recursion_limit}
    snapshot = await compiled.graph.aget_state(config)
    graph_input: Any = None  # continue from the checkpoint
    if not snapshot.values:
        if resume is not None:
            raise ResumeRejected("시작되지 않은 실행에는 승인 응답을 보낼 수 없습니다")
        graph_input = initial_state(inputs or {})
    elif resume is not None:
        target = _resume_target(resume)
        waiting = _pending_interrupt(snapshot)
        if waiting is not None and _same_target(target, waiting):
            # LangGraph keeps the first resume value of an interrupt, so a bad answer would fail every later
            # attempt: check it against the waiting payload before it reaches the graph.
            try:
                resume_output(resume, waiting)
            except NodeError as exc:
                raise ResumeRejected(exc.message) from exc
            graph_input = Command(resume=resume)
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


def _resume_target(resume: Any) -> tuple[str, int]:
    node_id = resume.get("nodeId") if isinstance(resume, dict) else None
    exec_index = resume.get("execIndex") if isinstance(resume, dict) else None
    if not isinstance(node_id, str) or not isinstance(exec_index, int) or isinstance(exec_index, bool):
        raise ResumeRejected("승인 응답에는 대상 노드(nodeId)와 실행 순번(execIndex)이 필요합니다")
    return node_id, exec_index


def _same_target(target: tuple[str, int], waiting: dict[str, Any]) -> bool:
    exec_index = waiting.get("execIndex")
    return waiting.get("nodeId") == target[0] and type(exec_index) is int and exec_index == target[1]


def _pending_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """Payload of the pending approval (phase 2 keeps approvals out of parallel regions, so at most one)."""
    interrupts = [item for task in snapshot.tasks for item in task.interrupts]
    return interrupts[0].value if interrupts else None
