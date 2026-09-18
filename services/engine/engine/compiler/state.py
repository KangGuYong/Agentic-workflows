from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Any, TypedDict

OUT_PREFIX = "out_"


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    """Reducer: shallow merge, so parallel nodes can write disjoint keys in the same superstep."""
    return {**(left or {}), **(right or {})}


GLOBAL_CHANNELS: dict[str, Any] = {
    "inputs": dict[str, Any],
    "routes": Annotated[dict[str, list[str]], merge_dicts],  # branch node id -> chosen next node ids
    "loop_counters": Annotated[dict[str, int], merge_dicts],  # back-edge id -> traversals so far
    "exec_counts": Annotated[dict[str, int], merge_dicts],  # node id -> completed executions
}

RunState = TypedDict("RunState", GLOBAL_CHANNELS, total=False)  # type: ignore[misc]


def build_state_type(node_ids: Iterable[str]) -> type:
    """The global channels plus one last-value channel per node.

    One shared `outputs` channel would be written in full at every superstep, so checkpoint storage would
    grow with (steps x total output size). A node only ever writes its own channel, so no reducer is needed
    and parallel branches cannot collide.
    """
    channels = {OUT_PREFIX + node_id: dict[str, Any] for node_id in node_ids}
    return TypedDict("WorkflowState", {**GLOBAL_CHANNELS, **channels}, total=False)  # type: ignore[misc]


def initial_state(inputs: dict[str, Any]) -> dict[str, Any]:
    return {"inputs": inputs, "routes": {}, "loop_counters": {}, "exec_counts": {}}


def outputs_of(state: Mapping[str, Any]) -> dict[str, Any]:
    """Latest output of every node that ran, keyed by node id — what templates and routing read."""
    return {
        name[len(OUT_PREFIX):]: value
        for name, value in state.items()
        if name.startswith(OUT_PREFIX) and value is not None
    }
