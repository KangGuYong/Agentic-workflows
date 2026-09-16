from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.llm.base import LLMClient
from engine.runtime.guard import NoopGuard, RunGuard
from engine.runtime.recorder import Recorder

if TYPE_CHECKING:  # avoids importing the node layer into the runtime ports
    from engine.nodes.base import TemplateField

RenderFn = Callable[[list["TemplateField"], dict[str, Any]], Awaitable[dict[str, Any]]]


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
    render: RenderFn | None = None
    """Renders a node's template fields off the event loop (Plan 2a worker). None renders inline.

    The implementation must raise TimeoutError when it gives up, and may raise the template errors the
    inline path raises; anything else becomes a non-retryable node error.
    """
