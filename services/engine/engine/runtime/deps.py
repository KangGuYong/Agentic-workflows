from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from engine.llm.base import LLMClient
from engine.runtime.guard import NoopGuard, RunGuard
from engine.runtime.recorder import Recorder


@dataclass
class RunDeps:
    """Per-run dependencies, passed to LangGraph as `context=` (compiled graphs are shared across runs).

    Never share one instance between runs: `recorder` and `guard` are bound to `run_id`.
    """

    run_id: str
    llm: LLMClient
    recorder: Recorder
    guard: RunGuard = field(default_factory=NoopGuard)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
