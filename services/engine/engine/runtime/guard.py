from __future__ import annotations

from typing import Protocol

from engine.errors import RunCancelled


class RunGuard(Protocol):
    def check(self) -> None:
        """Raise RunCancelled when the run must stop (cancel requested, lease lost)."""


class NoopGuard:
    def check(self) -> None:
        return None


class FlagGuard:
    """In-process guard; Plan 2 sets it from the Redis control channel and the lease heartbeat."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def check(self) -> None:
        if self.cancelled:
            raise RunCancelled()
