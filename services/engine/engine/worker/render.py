"""Template rendering off the event loop, with a deadline (design 4).

Rendering is the one piece of tenant-controlled CPU work the engine cannot bound: a single loop over a big
value costs O(data squared) with almost no output. A thread would keep the loop responsive but could not be
stopped, so rendering runs in a process that is killed when it overruns.

`RenderPool.close()` blocks the calling thread for as long as pebble takes to terminate every worker (see
its docstring) - call it through `asyncio.to_thread`, as `engine.worker.main` does, never directly from a
coroutine.

Windows development gets no memory limit: `resource` (used by `_init`) is POSIX-only, so the pool runs
unbounded there. Deployment is Linux, where the limit applies.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
from concurrent.futures.process import BrokenProcessPool
from types import ModuleType
from typing import Any

from pebble import ProcessFuture, ProcessPool

from engine.compiler.wrapper import render_fields
from engine.errors import EngineFault
from engine.nodes.base import TemplateField

log = logging.getLogger(__name__)

# Not wired to EngineConfig: nothing reads a config/env value for it today (main.py never passes
# memory_limit_mb), so the default below is the only value that can ever apply. Add a config field and
# thread it through main.py before this needs to be anything but a local constant.
_DEFAULT_MEMORY_LIMIT_MB = 1024


def _init(memory_limit_mb: int | None) -> None:
    """Pool worker initializer: cap this worker's own data-segment growth (POSIX only).

    Runs once per worker, right after it starts. Uses RLIMIT_DATA rather than RLIMIT_AS: by the time a
    forked worker gets here it has already inherited the parent's entire virtual address space (every
    thread stack, every shared library, every mmap'd file the DB pool / Redis client / httpx client had
    open) - on a threaded CPython process that alone can exceed a 1024 MB RLIMIT_AS, so setrlimit would
    succeed (it never checks current usage) but the *next* allocation, however small, would fail with
    MemoryError. RLIMIT_DATA only counts what this worker itself allocates from here on, which is what we
    actually want bounded.
    """
    if not memory_limit_mb:
        log.info("render pool worker: no memory limit configured")
        return
    try:
        import resource

        limit = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    except Exception as exc:  # noqa: BLE001 - Windows has no `resource`; a container may refuse the limit
        log.warning("render pool worker: memory limit not applied (%s: %s)", type(exc).__name__, exc)
    else:
        log.info("render pool worker: memory limit set to %s MB", memory_limit_mb)


def _job(fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
    return render_fields(fields, outputs)


def _fork_safe_context() -> ModuleType | None:
    """A `forkserver` multiprocessing context where the platform offers one, else None (pebble's default).

    pebble gives its `ProcessPool` no context by default, so on Linux it forks workers lazily on the first
    render - by then the worker process is already running the DB pool, the LISTEN connection, the Redis
    client and the httpx client, each with its own thread. Forking a multithreaded asyncio process is the
    classic deadlock hazard (only the forking thread survives in the child; a lock held by another thread
    at fork time never unlocks) and duplicates the parent's live Postgres/Redis file descriptors into a
    worker that has no business holding them. `forkserver` starts a clean helper process up front, before
    any of that exists, so later workers fork from it instead of from the live app process. `spawn` would
    dodge the hazard too but re-imports the whole application in every worker and requires every scheduled
    function to be picklable by reference - `forkserver` keeps pebble's current by-value pickling.

    Windows only offers `spawn` (confirmed via `multiprocessing.get_all_start_methods()`), so this returns
    None there and `ProcessPool` falls back to its own default.
    """
    if "forkserver" not in multiprocessing.get_all_start_methods():
        return None
    return multiprocessing.get_context("forkserver")


class RenderPool:
    """Matches engine.runtime.deps.RenderFn, so it can be handed to RunDeps.render."""

    def __init__(self, *, size: int = 2, timeout: float = 5.0,
                 memory_limit_mb: int | None = _DEFAULT_MEMORY_LIMIT_MB) -> None:
        self._timeout = timeout
        context = _fork_safe_context()
        pool_kwargs: dict[str, Any] = {"context": context} if context is not None else {}
        self._pool = ProcessPool(max_workers=max(1, size), initializer=_init, initargs=(memory_limit_mb,),
                                 **pool_kwargs)
        self._closed = False
        self._futures: set[ProcessFuture] = set()

    async def __call__(self, fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
        try:
            future = self._pool.schedule(_job, args=(fields, outputs), timeout=self._timeout)
        except RuntimeError as exc:
            # The pool is CLOSED/STOPPED/ERROR (e.g. its manager threads died, or it raced close()):
            # this is our infrastructure failing, never the tenant's template, so it must not become a
            # NodeError - a worker with a dead render pool would otherwise claim runs and permanently fail
            # them as fast as it could claim them.
            log.error("render pool schedule failed: %s", exc)
            raise EngineFault(f"render pool is not active: {exc}") from exc
        self._futures.add(future)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()  # the node was cancelled: stop the render too
            raise
        except (RuntimeError, BrokenProcessPool) as exc:
            log.error("render pool failed: %s", exc)
            raise EngineFault(f"render pool failed: {exc}") from exc
        finally:
            self._futures.discard(future)

    def close(self) -> None:
        """Stop the pool, killing whatever is running in it. Safe to call more than once.

        Every outstanding future is cancelled *before* `self._pool.stop()` runs: `ProcessFuture.cancel()`
        resolves the future (and, through it, the asyncio future `wrap_future` chained to it in `__call__`)
        by itself, synchronously, so it still works once `stop()` has killed the pool's manager threads and
        workers. pebble's own `stop()`/`join()` never touches pending futures - skipping this step would
        leave every `await pool(...)` call that was in flight at close time pending forever.

        Blocks the calling thread. pebble's `join(timeout=...)` only honours the timeout when the pool was
        first `close()`d to let pending work finish; `stop()` (used here, to kill in-flight work rather than
        wait for it) puts `join()` on its unbounded path instead, and `force_stop_workers()` then SIGTERMs
        each worker in turn, giving each up to pebble's `term_timeout` (3s) before SIGKILL - a worker inside
        a C-level call (e.g. `tojson`) won't run its SIGTERM handler, so a large pool can cost tens of
        seconds. Call this off the event loop (`asyncio.to_thread`), as `engine.worker.main` does.
        """
        if self._closed:
            return
        self._closed = True
        for future in list(self._futures):
            future.cancel()
        self._pool.stop()
        self._pool.join(timeout=5)
