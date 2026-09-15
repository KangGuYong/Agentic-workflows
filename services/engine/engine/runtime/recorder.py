from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from engine.nodes.base import Usage


@dataclass
class NodeRunRecord:
    node_id: str
    exec_index: int
    attempt: int
    status: str  # running | succeeded | defaulted | failed | waiting
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    meta: dict[str, Any] = field(default_factory=dict)


class Recorder(Protocol):
    """Observation log of node executions (spec 3.3 node_runs, 7.1 events). Never the source of truth."""

    async def attempts_so_far(self, node_id: str, exec_index: int) -> int: ...

    async def find_waiting(self, node_id: str, exec_index: int) -> int | None:
        """Attempt number of a `waiting` record for this execution, if any."""

    async def node_started(self, node_id: str, exec_index: int, attempt: int, input: dict[str, Any] | None) -> None: ...

    async def node_succeeded(
        self, node_id: str, exec_index: int, attempt: int, output: dict[str, Any], usage: Usage,
        *, defaulted: bool, meta: dict[str, Any],
    ) -> None: ...

    async def node_failed(
        self, node_id: str, exec_index: int, attempt: int, error: dict[str, Any], *, will_retry: bool
    ) -> None: ...

    async def node_waiting(self, node_id: str, exec_index: int, attempt: int, payload: dict[str, Any]) -> None: ...

    async def node_token(self, node_id: str, exec_index: int, text: str) -> None: ...


class InMemoryRecorder:
    def __init__(self) -> None:
        self.records: list[NodeRunRecord] = []
        self.events: list[dict[str, Any]] = []

    def for_node(self, node_id: str) -> list[NodeRunRecord]:
        return [record for record in self.records if record.node_id == node_id]

    def _find(self, node_id: str, exec_index: int, attempt: int) -> NodeRunRecord:
        for record in self.records:
            if (record.node_id, record.exec_index, record.attempt) == (node_id, exec_index, attempt):
                return record
        raise KeyError((node_id, exec_index, attempt))

    def _emit(self, event_type: str, node_id: str, exec_index: int, attempt: int | None, **payload: Any) -> None:
        self.events.append(
            {"type": event_type, "nodeId": node_id, "execIndex": exec_index, "attempt": attempt, **payload}
        )

    async def attempts_so_far(self, node_id: str, exec_index: int) -> int:
        return sum(1 for r in self.records if r.node_id == node_id and r.exec_index == exec_index)

    async def find_waiting(self, node_id: str, exec_index: int) -> int | None:
        for record in self.records:
            if record.node_id == node_id and record.exec_index == exec_index and record.status == "waiting":
                return record.attempt
        return None

    async def node_started(self, node_id: str, exec_index: int, attempt: int, input: dict[str, Any] | None) -> None:
        self.records.append(NodeRunRecord(node_id, exec_index, attempt, "running", input=input))
        self._emit("node_started", node_id, exec_index, attempt)

    async def node_succeeded(
        self, node_id: str, exec_index: int, attempt: int, output: dict[str, Any], usage: Usage,
        *, defaulted: bool, meta: dict[str, Any],
    ) -> None:
        record = self._find(node_id, exec_index, attempt)
        record.status = "defaulted" if defaulted else "succeeded"
        record.output = output
        record.usage = usage
        record.meta = meta
        self._emit("node_finished", node_id, exec_index, attempt, defaulted=defaulted, **meta)

    async def node_failed(
        self, node_id: str, exec_index: int, attempt: int, error: dict[str, Any], *, will_retry: bool
    ) -> None:
        record = self._find(node_id, exec_index, attempt)
        record.status = "failed"
        record.error = error
        self._emit("node_failed", node_id, exec_index, attempt, error=error, willRetry=will_retry)

    async def node_waiting(self, node_id: str, exec_index: int, attempt: int, payload: dict[str, Any]) -> None:
        record = self._find(node_id, exec_index, attempt)
        record.status = "waiting"
        record.meta = {"waiting": payload}
        self._emit("node_waiting", node_id, exec_index, attempt, payload=payload)

    async def node_token(self, node_id: str, exec_index: int, text: str) -> None:
        self._emit("node_token", node_id, exec_index, None, text=text)
