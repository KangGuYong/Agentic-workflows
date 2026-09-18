"""`uvicorn engine.api.main:app` (Linux/deployment), or `python -m engine.api.main` (Windows development
-- see the note below)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator

from fastapi import FastAPI
from redis.asyncio import Redis

from engine.api.app import create_app
from engine.config import load_config
from engine.db.migrate import prepare_database
from engine.db.pool import make_pool

log = logging.getLogger(__name__)


def _windows_selector_loop() -> None:
    """psycopg's async connections cannot use Windows' default ProactorEventLoop (development only; see
    the same-named helper in engine/worker/main.py). Unlike the worker -- whose only entrypoint is
    `python -m engine.worker.main`, so confining this call to `__main__` loses nothing -- this module is
    also imported directly by the plain `uvicorn engine.api.main:app` CLI (the Linux/deployment
    invocation), so it is called here at import scope rather than from `__main__` below.

    That placement is deliberate, not an oversight: verified against this repo's own dev containers,
    `uvicorn engine.api.main:app --loop none` and `--reload` both start cleanly on Windows *because* this
    runs on import, and disabling that import-time call reproduces the exact `psycopg.InterfaceError:
    ... cannot use the 'ProactorEventLoop'` failure `--loop asyncio` always gives -- nothing else in that
    invocation would set a compatible loop policy for them. `--loop asyncio` (uvicorn's default) is
    unaffected either way: uvicorn's own `loop_factory` (uvicorn/loops/asyncio.py) forces
    ProactorEventLoop itself after import, regardless of any policy already in effect, which is why that
    flag -- and the bare `uvicorn engine.api.main:app` invocation, which defaults to it -- still fails on
    Windows and `python -m engine.api.main` remains the documented way to run this here (see the module
    docstring and README). `_run` below also relies on this: it drives its own `asyncio.run()`, which
    honours whatever policy is already in effect at that point, same as before this was pulled out into
    a function.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


_windows_selector_loop()


def build() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    pool = make_pool(config.database_url, max_size=config.db_pool_max)
    redis = Redis.from_url(config.redis_url, decode_responses=True)
    application = create_app(config, pool, redis)

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await prepare_database(config.database_url)
            await pool.open(wait=True)
            yield
        finally:
            # Mirrors the worker's shutdown discipline (engine/worker/main.py): prepare_database and
            # pool.open are inside this try specifically so a failure partway through startup -- either
            # one raising -- still reaches this finally instead of skipping it, same as a failure after
            # yield would. redis.aclose() and pool.close() are both safe to call on a client/pool that was
            # never opened (redis.asyncio.Redis is lazy; AsyncConnectionPool.close() on an unopened pool
            # is a no-op), and each shutdown is independently guarded so one failure never skips the
            # other, and neither replaces an exception already propagating from the block above.
            for name, shutdown in (("redis client", redis.aclose), ("db pool", pool.close)):
                try:
                    await shutdown()
                except Exception:
                    log.warning("api shutdown: closing the %s failed", name, exc_info=True)

    # FastAPI's own `lifespan=` constructor argument is the usual way to wire this up, but create_app
    # builds the FastAPI() instance itself (tests construct the app the same way, without going through
    # this module at all -- see the note above create_app), so the context manager is attached to the
    # already-built app's router instead. This is what `@app.on_event(...)` does internally on every
    # FastAPI version; `on_event` itself is deprecated on the FastAPI version pinned here.
    application.router.lifespan_context = _lifespan
    return application


app = build()


def _run() -> None:
    """`python -m engine.api.main`: the Windows-development path (see the module docstring above). Driving
    uvicorn's own `Server` through this process's `asyncio.run()` -- instead of uvicorn's CLI, which picks
    its own loop -- makes it honour the event loop policy set at import time."""
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port))
    asyncio.run(server.serve())


if __name__ == "__main__":
    _run()
