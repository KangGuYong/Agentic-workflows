from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from engine.errors import LeaseLost
from engine.jsondata import check_text
from engine.nodes.base import Usage


@dataclass
class NodeRunRecord:
    node_id: str
    exec_index: int
    attempt: int
    status: str  # running | succeeded | defaulted | failed | waiting (Plan 2 also closes rows as cancelled)
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    meta: dict[str, Any] = field(default_factory=dict)
    waited: bool = False  # this attempt recorded node_waiting at some point, even if it has finished since


class DuplicateAttempt(LeaseLost):
    """node_started for an attempt that already exists: another worker owns the run (Postgres unique key).
    A LeaseLost, so the node wrapper stops instead of treating it as a node error."""


class Recorder(Protocol):
    """Observation log of node executions for ONE run (spec 3.3 node_runs, 7.1 events). Never the source of truth.

    An implementation is bound to a single run (`RunDeps.run_id`); its methods take no run id. Values are
    stored as JSON (jsonb in Plan 2), so inputs, outputs, errors and payloads must be JSON data.
    """

    async def attempts_so_far(self, node_id: str, exec_index: int) -> int: ...

    async def find_waiting(self, node_id: str, exec_index: int) -> int | None:
        """Latest attempt of this execution that recorded node_waiting, even if it has finished since.

        An execution that waited is being resumed, or replayed after a crash; it must not open a new attempt
        or record node_waiting again.
        """

    async def node_started(self, node_id: str, exec_index: int, attempt: int, input: dict[str, Any] | None) -> None:
        """Open an attempt. Raises DuplicateAttempt when the attempt already exists."""

    async def node_succeeded(
        self, node_id: str, exec_index: int, attempt: int, output: dict[str, Any], usage: Usage,
        *, defaulted: bool, meta: dict[str, Any],
    ) -> None: ...

    async def node_failed(
        self, node_id: str, exec_index: int, attempt: int, error: dict[str, Any], *, will_retry: bool
    ) -> None: ...

    async def node_waiting(self, node_id: str, exec_index: int, attempt: int, payload: dict[str, Any]) -> None: ...

    async def node_token(self, node_id: str, exec_index: int, text: str) -> None:
        """Best effort: the wrapper logs and ignores a failure here (tokens are a live preview only)."""


def _stored(value: Any) -> Any:
    """What a jsonb column would hold: a JSON copy. Raises TypeError/ValueError for anything else, including
    text jsonb rejects (NUL, lone surrogates)."""
    if value is None:
        return None
    copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    check_text(copied)
    return copied


class InMemoryRecorder:
    """Test double as strict as the Postgres recorder: unique attempts and JSON-only values.

    Events are flat dicts (meta and payload keys at the top level). Spec 7.1 nests them under `payload`;
    the Plan 2 recorder does that when it writes run_events.
    """

    def __init__(self) -> None:
        self.records: list[NodeRunRecord] = []
        self.events: list[dict[str, Any]] = []

    def for_node(self, node_id: str) -> list[NodeRunRecord]:
        return [record for record in self.records if record.node_id == node_id]

    def _find(self, node_id: str, exec_index: int, attempt: int) -> NodeRunRecord | None:
        for record in self.records:
            if (record.node_id, record.exec_index, record.attempt) == (node_id, exec_index, attempt):
                return record
        return None

    def _get(self, node_id: str, exec_index: int, attempt: int) -> NodeRunRecord:
        record = self._find(node_id, exec_index, attempt)
        if record is None:
            raise KeyError((node_id, exec_index, attempt))
        return record

    @staticmethod
    def _event(event_type: str, node_id: str, exec_index: int, attempt: int | None, **payload: Any) -> dict[str, Any]:
        event = {"type": event_type, "nodeId": node_id, "execIndex": exec_index, "attempt": attempt}
        return {**event, **_stored(payload)}

    async def attempts_so_far(self, node_id: str, exec_index: int) -> int:
        return sum(1 for r in self.records if r.node_id == node_id and r.exec_index == exec_index)

    async def find_waiting(self, node_id: str, exec_index: int) -> int | None:
        attempts = [r.attempt for r in self.records if (r.node_id, r.exec_index) == (node_id, exec_index) and r.waited]
        return max(attempts, default=None)

    # Each write builds every stored value first and changes the record only when all of them are JSON,
    # like a Postgres UPDATE that either applies completely or rolls back.

    async def node_started(self, node_id: str, exec_index: int, attempt: int, input: dict[str, Any] | None) -> None:
        if self._find(node_id, exec_index, attempt) is not None:
            raise DuplicateAttempt((node_id, exec_index, attempt))
        record = NodeRunRecord(node_id, exec_index, attempt, "running", input=_stored(input))
        event = self._event("node_started", node_id, exec_index, attempt)
        self.records.append(record)
        self.events.append(event)

    async def node_succeeded(
        self, node_id: str, exec_index: int, attempt: int, output: dict[str, Any], usage: Usage,
        *, defaulted: bool, meta: dict[str, Any],
    ) -> None:
        record = self._get(node_id, exec_index, attempt)
        stored_output, stored_meta = _stored(output), _stored(meta)
        event = self._event("node_finished", node_id, exec_index, attempt, defaulted=defaulted, **meta)
        record.status = "defaulted" if defaulted else "succeeded"
        record.output, record.usage, record.meta = stored_output, usage, stored_meta
        self.events.append(event)

    async def node_failed(
        self, node_id: str, exec_index: int, attempt: int, error: dict[str, Any], *, will_retry: bool
    ) -> None:
        record = self._get(node_id, exec_index, attempt)
        stored_error = _stored(error)
        event = self._event("node_failed", node_id, exec_index, attempt, error=error, willRetry=will_retry)
        record.status, record.error = "failed", stored_error
        self.events.append(event)

    async def node_waiting(self, node_id: str, exec_index: int, attempt: int, payload: dict[str, Any]) -> None:
        record = self._get(node_id, exec_index, attempt)
        stored_payload = _stored(payload)
        event = self._event("node_waiting", node_id, exec_index, attempt, payload=payload)
        record.status, record.waited, record.meta = "waiting", True, {"waiting": stored_payload}
        self.events.append(event)

    async def node_token(self, node_id: str, exec_index: int, text: str) -> None:
        self.events.append(self._event("node_token", node_id, exec_index, None, text=text))
