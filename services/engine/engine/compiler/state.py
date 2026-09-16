from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    """Reducer: shallow merge, so parallel nodes can write disjoint keys in the same superstep."""
    return {**(left or {}), **(right or {})}


class RunState(TypedDict, total=False):
    inputs: dict[str, Any]
    outputs: Annotated[dict[str, Any], merge_dicts]  # node id -> latest output
    routes: Annotated[dict[str, list[str]], merge_dicts]  # branch node id -> chosen next node ids
    loop_counters: Annotated[dict[str, int], merge_dicts]  # back-edge id -> traversals so far (never reset)
    exec_counts: Annotated[dict[str, int], merge_dicts]  # node id -> completed executions


def initial_state(inputs: dict[str, Any]) -> RunState:
    return {"inputs": inputs, "outputs": {}, "routes": {}, "loop_counters": {}, "exec_counts": {}}
