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

if sys.platform == "win32":
    # psycopg's async connections cannot use Windows' default ProactorEventLoop (development only; see the
    # same note in engine/worker/main.py). Setting the policy here is necessary but not sufficient: it is
    # what makes `python -m engine.api.main` below work, since that path drives its own asyncio.run() and
    # so honours the current policy -- but the plain `uvicorn` CLI does not. uvicorn picks its loop via an
    # explicit `loop_factory` (uvicorn/loops/asyncio.py) that hardcodes ProactorEventLoop on win32
    # regardless of the policy in effect, `--loop asyncio` included, so `uvicorn engine.api.main:app`
    # fails immediately on startup here. Windows development therefore needs `python -m engine.api.main`.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def build() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    pool = make_pool(config.database_url)
    redis = Redis.from_url(config.redis_url, decode_responses=True)
    application = create_app(config, pool, redis)

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        await prepare_database(config.database_url)
        await pool.open(wait=True)
        try:
            yield
        finally:
            # Mirrors the worker's shutdown discipline (engine/worker/main.py): each resource is closed
            # independently so one failure never skips the other, and neither replaces an exception
            # already propagating from the block above.
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
