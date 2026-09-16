"""Template rendering off the event loop, with a deadline (design 4).

Rendering is the one piece of tenant-controlled CPU work the engine cannot bound: a single loop over a big
value costs O(data squared) with almost no output. A thread would keep the loop responsive but could not be
stopped, so rendering runs in a process that is killed when it overruns.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from pebble import ProcessPool

from engine.compiler.wrapper import render_fields
from engine.nodes.base import TemplateField

log = logging.getLogger(__name__)


def _init(memory_limit_mb: int | None) -> None:
    if not memory_limit_mb:
        return
    with contextlib.suppress(Exception):  # POSIX only: containers get the limit, Windows development does not
        import resource

        limit = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _job(fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
    return render_fields(fields, outputs)


class RenderPool:
    """Matches engine.runtime.deps.RenderFn, so it can be handed to RunDeps.render."""

    def __init__(self, *, size: int = 2, timeout: float = 5.0, memory_limit_mb: int | None = 1024) -> None:
        self._timeout = timeout
        self._pool = ProcessPool(max_workers=max(1, size), initializer=_init, initargs=(memory_limit_mb,))
        self._closed = False

    async def __call__(self, fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
        future = self._pool.schedule(_job, args=(list(fields), outputs), timeout=self._timeout)
        wrapped = asyncio.wrap_future(future)
        try:
            return await wrapped
        except asyncio.CancelledError:
            future.cancel()  # the node was cancelled: stop the render too
            raise

    def close(self) -> None:
        """Stop the pool, killing whatever is running in it. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._pool.stop()
        self._pool.join(timeout=5)
