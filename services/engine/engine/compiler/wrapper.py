"""The single LangGraph node function used for every DSL node (spec 5.3 node wrapper)."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, TypeVar

from langgraph.errors import GraphBubbleUp
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel

from engine.compiler.routing import RouteDecision, resolve_route
from engine.compiler.state import RunState
from engine.dsl.models import Edge, Node, Policy, RetrySpec
from engine.errors import EngineFault, ErrorCode, NodeError, NodeFailedError, RunCancelled
from engine.jsondata import check_text, clip
from engine.nodes.base import NodeContext, NodeResult, NodeSpec, TemplateField
from engine.runtime.deps import RunDeps
from engine.templates.render import TemplateRenderError, render_template

MAX_OUTPUT_BYTES = 1_000_000
CONTROL_FLOW = (GraphBubbleUp, RunCancelled, EngineFault)  # never node errors: they end or pause the node call

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class NodePlan:
    node: Node
    spec: NodeSpec
    config: BaseModel
    policy: Policy | None
    pred_ids: tuple[str, ...]  # forward predecessors in edge declaration order
    handle_edges: dict[str, list[Edge]]
    back_edge_ids: frozenset[str]


def backoff_delay(retry: RetrySpec, failed_tries: int) -> float:
    if retry.backoff == "fixed":
        return retry.initialDelaySec
    return min(retry.initialDelaySec * 2 ** (failed_tries - 1), 60.0)


def _render(fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
    rendered = {f.path: render_template(f.source, outputs, f.target) for f in fields}
    try:
        check_text(rendered)  # recorded as the attempt's input (jsonb) and passed on to outputs and payloads
    except ValueError as exc:
        raise NodeError(ErrorCode.TEMPLATE_ERROR, f"템플릿 결과를 사용할 수 없습니다: {exc}", retryable=False) from exc
    return rendered


def _check_output(output: dict[str, Any]) -> None:
    """A node output is stored in run state and as jsonb: strict JSON, storable text, at most MAX_OUTPUT_BYTES."""
    try:
        text = json.dumps(output, ensure_ascii=False, allow_nan=False)
        check_text(output)
    except (TypeError, ValueError, RecursionError) as exc:
        raise NodeError(ErrorCode.NODE_FAILED, f"노드 출력을 저장할 수 없습니다: {clip(str(exc))}", retryable=False) from exc
    size = len(text.encode("utf-8"))
    if size > MAX_OUTPUT_BYTES:
        raise NodeError(ErrorCode.OUTPUT_TOO_LARGE, f"노드 출력이 너무 큽니다 ({size} bytes)", retryable=False)


def _as_node_error(exc: Exception, timeout: float | None) -> NodeError:
    if isinstance(exc, NodeError):
        return exc
    if isinstance(exc, TimeoutError):
        limit = f"{timeout:g}초 " if timeout else ""
        return NodeError(ErrorCode.NODE_TIMEOUT, f"제한 시간 {limit}초과", retryable=True)
    if isinstance(exc, TemplateRenderError):
        return NodeError(exc.code, clip(str(exc), 1000), retryable=False)
    return NodeError(ErrorCode.NODE_FAILED, clip(f"{type(exc).__name__}: {exc}", 1000), retryable=False)


def _decide(plan: NodePlan, output: dict[str, Any], loop_counters: dict[str, int]) -> RouteDecision | None:
    if not plan.spec.is_branch:
        return None
    try:
        chosen = plan.spec.route(plan.config, output)
        return resolve_route(plan.spec.type, chosen, plan.handle_edges, plan.back_edge_ids, loop_counters)
    except (KeyError, ValueError) as exc:  # a routing bug, not a type problem in the user's data
        raise NodeError(ErrorCode.NODE_FAILED, f"분기를 결정할 수 없습니다: {clip(str(exc))}", retryable=False) from exc


def _fallback_output(plan: NodePlan) -> dict[str, Any] | None:
    """A fresh copy per use: a compiled workflow (and its policy) is shared by every run."""
    if plan.policy is None or plan.policy.onError != "default":
        return None
    if plan.policy.defaultOutput is not None:
        return copy.deepcopy(plan.policy.defaultOutput)  # checked by _check_output before use
    return plan.spec.fallback_output(plan.config)


async def _recorded(call: Awaitable[T]) -> T:
    """Await a recorder read or write; an infrastructure failure becomes EngineFault, never a node error."""
    try:
        return await call
    except RunCancelled:
        raise
    except Exception as exc:
        raise EngineFault(f"recorder call failed: {type(exc).__name__}") from exc


def _context(
    plan: NodePlan, deps: RunDeps, state: RunState, exec_index: int, attempt: int, resumed: bool
) -> NodeContext:
    node_id = plan.node.id
    lost_tokens = 0

    async def on_token(text: str) -> None:
        nonlocal lost_tokens
        try:  # tokens are a best-effort live preview (spec 7.2): a failed publish never fails the node
            await deps.recorder.node_token(node_id, exec_index, text)
        except Exception:  # noqa: BLE001
            lost_tokens += 1
            if lost_tokens == 1:  # one warning per attempt, not one per token
                log.warning("node_token failed for %s#%s attempt %s", node_id, exec_index, attempt, exc_info=True)

    async def wait_for_human(payload: dict[str, Any]) -> Any:
        # On resume LangGraph re-runs the node and interrupt() returns the resume value instead of raising.
        # A node that waits must not retry (human_approval accepts no policy): a second interrupt() in the
        # same call would wait again without a node_waiting record.
        if not resumed:
            await _recorded(deps.recorder.node_waiting(node_id, exec_index, attempt, payload))
        return interrupt(payload)

    return NodeContext(
        run_id=deps.run_id,
        node_id=node_id,
        exec_index=exec_index,
        attempt=attempt,
        inputs=state.get("inputs", {}),
        outputs=state.get("outputs", {}),
        pred_ids=list(plan.pred_ids),
        llm=deps.llm,
        on_token=on_token,
        interrupt=wait_for_human,
    )


async def _succeed(
    plan: NodePlan,
    deps: RunDeps,
    exec_index: int,
    attempt: int,
    result: NodeResult,
    decision: RouteDecision | None,
    *,
    defaulted: bool,
) -> dict[str, Any]:
    node_id = plan.node.id
    write: dict[str, Any] = {"outputs": {node_id: result.output}, "exec_counts": {node_id: exec_index}}
    meta: dict[str, Any] = {}
    if decision is not None:
        write["routes"] = {node_id: decision.targets}
        if decision.counters:
            write["loop_counters"] = decision.counters
        meta = {"handle": decision.handle, "loopExhausted": decision.loop_exhausted}
    await _recorded(deps.recorder.node_succeeded(
        node_id, exec_index, attempt, result.output, result.usage, defaulted=defaulted, meta=meta
    ))
    return write


def make_node_fn(plan: NodePlan):
    fields = plan.spec.template_fields(plan.config)
    node_id = plan.node.id
    max_attempts = plan.policy.retry.maxAttempts if plan.policy else 1
    timeout = plan.policy.timeoutSec if plan.policy else None

    async def node_fn(state: RunState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
        deps = runtime.context
        recorder = deps.recorder
        deps.guard.check()
        exec_index = state.get("exec_counts", {}).get(node_id, 0) + 1
        outputs = state.get("outputs", {})
        loop_counters = state.get("loop_counters", {})
        waited = await _recorded(recorder.find_waiting(node_id, exec_index))
        # An execution that already waited is being resumed (or replayed after a crash) for this whole call:
        # it reuses the waited attempt first and never records node_waiting again, even on a retry.
        resumed = waited is not None
        attempt = waited if resumed else await _recorded(recorder.attempts_so_far(node_id, exec_index)) + 1
        started = resumed
        error: NodeError | None = None
        rendered: dict[str, Any] | None

        for tries in range(1, max_attempts + 1):
            error = None
            try:
                rendered = _render(fields, outputs)
            except Exception as exc:  # noqa: BLE001 - a template failure is this attempt's node error
                rendered, error = None, _as_node_error(exc, timeout)
            if not started:
                await _recorded(recorder.node_started(node_id, exec_index, attempt, rendered))
                started = True
            if error is None:
                try:
                    ctx = _context(plan, deps, state, exec_index, attempt, resumed)
                    async with asyncio.timeout(timeout):
                        result = await plan.spec.execute(ctx, plan.config, rendered)
                    _check_output(result.output)
                    decision = _decide(plan, result.output, loop_counters)
                except CONTROL_FLOW:
                    raise
                except Exception as exc:  # noqa: BLE001 - every other failure is a node error
                    error = _as_node_error(exc, timeout)
                else:
                    return await _succeed(plan, deps, exec_index, attempt, result, decision, defaulted=False)

            will_retry = error.retryable and tries < max_attempts
            await _recorded(recorder.node_failed(node_id, exec_index, attempt, error.to_dict(), will_retry=will_retry))
            if not will_retry:
                break
            # Plan 2 cancels the running task directly; this check covers an in-process FlagGuard.
            await deps.sleep(backoff_delay(plan.policy.retry, tries))
            deps.guard.check()
            # a new attempt number from the log, so a replay that reused a waited attempt never collides
            attempt = await _recorded(recorder.attempts_so_far(node_id, exec_index)) + 1
            started = False

        fallback = _fallback_output(plan)
        if fallback is None:
            raise NodeFailedError(node_id, error)
        try:
            _check_output(fallback)
            decision = _decide(plan, fallback, loop_counters)
        except NodeError as exc:
            raise NodeFailedError(node_id, exc) from exc
        return await _succeed(plan, deps, exec_index, attempt, NodeResult(fallback), decision, defaulted=True)

    return node_fn
