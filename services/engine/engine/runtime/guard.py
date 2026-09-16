from __future__ import annotations

from typing import Protocol

from engine.errors import LeaseLost, RunCancelled


class RunGuard(Protocol):
    def check(self) -> None:
        """Raise RunCancelled when a cancel was requested, or LeaseLost (a RunCancelled) when this worker lost
        the run's lease. A lost lease must not end the run: the new owner continues it."""


class NoopGuard:
    def check(self) -> None:
        return None


class FlagGuard:
    """In-process guard; Plan 2 sets it from the Redis control channel and the lease heartbeat."""

    def __init__(self) -> None:
        self.cancelled = False
        self.lease_lost = False

    def cancel(self) -> None:
        self.cancelled = True

    def lose_lease(self) -> None:
        self.lease_lost = True

    def check(self) -> None:
        if self.lease_lost:
            raise LeaseLost()
        if self.cancelled:
            raise RunCancelled()
