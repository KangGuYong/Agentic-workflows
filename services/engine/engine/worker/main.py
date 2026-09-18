"""`python -m engine.worker.main`: one worker process."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from typing import Any

from redis.asyncio import Redis

from engine.config import load_config
from engine.db.migrate import prepare_database
from engine.db.pool import make_pool
from engine.llm.gateway import LLMGateway
from engine.llm.ollama import OllamaRaw
from engine.llm.semaphore import ModelSemaphore, SemaphoreLLM
from engine.worker.render import RenderPool
from engine.worker.worker import Worker

log = logging.getLogger(__name__)


def _windows_selector_loop() -> None:
    """psycopg's async connections cannot use Windows' default ProactorEventLoop (development only)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def run(stop: asyncio.Event | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    if not config.encrypt_checkpoints:
        log.warning("checkpoints are stored unencrypted (ENGINE_DEV_INSECURE=1)")

    if stop is None:
        stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Registered before prepare_database/pool.open: an alembic upgrade can run long, and until now nothing
    # could interrupt it - Ctrl-C during migration just sat there. add_signal_handler is POSIX-only (raises
    # NotImplementedError on the Windows ProactorEventLoop, which _windows_selector_loop below switches
    # away from anyway); Windows development therefore gets no signal handling at all and Ctrl-C there
    # skips the orderly shutdown below, same as before this change.
    for name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(AttributeError, NotImplementedError):
            loop.add_signal_handler(getattr(signal, name), stop.set)

    await prepare_database(config.database_url)

    # The worker needs a connection per in-flight run plus the recorder/reaper traffic around them; the
    # heartbeat has its own connection (2b design §8.1) and is deliberately not counted here.
    pool = make_pool(config.database_url, max_size=max(config.db_pool_max, config.worker_max_runs + 4))
    log.info("render pool size %s bounds concurrent template renders", config.render_pool_size)
    redis: Redis | None = None
    raw: OllamaRaw | None = None
    render: RenderPool | None = None
    worker: Worker | None = None
    try:
        await pool.open(wait=True)
        redis = Redis.from_url(config.redis_url, decode_responses=True)
        raw = OllamaRaw(config.ollama_base_url)
        llm = SemaphoreLLM(LLMGateway(raw), ModelSemaphore(redis, limit=config.ollama_num_parallel))
        render = RenderPool(size=config.render_pool_size, timeout=config.render_timeout_sec)
        worker = Worker(config, pool, redis, llm=llm, render=render)  # the worker runs its own reaper

        await worker.start()
        log.info("worker %s ready", worker.owner)
        await stop.wait()
    finally:
        # pool.open(), worker.start() or the wait above can raise (or be cancelled); every resource opened
        # since must still be released, or the DB pool, Redis client, httpx client and render pool are all
        # left open and any run this worker held keeps its lease for the full lease_sec instead of being
        # handed back for recovery now. Each shutdown is independently guarded so one failure never skips
        # the rest, and none of them replaces an exception already propagating from the block above.
        shutdowns: list[tuple[str, Any]] = [
            ("worker", worker.stop if worker is not None else None),
            # join() blocks the thread for as long as pebble takes to SIGTERM/SIGKILL every worker (see
            # RenderPool.close's docstring) - tens of seconds for a large pool, well past a typical SIGTERM
            # grace period, so it must not run directly on the event loop.
            ("render pool", (lambda r=render: asyncio.to_thread(r.close)) if render is not None else None),
            ("ollama client", raw.aclose if raw is not None else None),
            ("redis client", redis.aclose if redis is not None else None),
            ("db pool", pool.close),
        ]
        for name, shutdown in shutdowns:
            if shutdown is None:
                continue
            try:
                await shutdown()
            except Exception:
                log.warning("worker shutdown: closing the %s failed", name, exc_info=True)


if __name__ == "__main__":
    _windows_selector_loop()  # call before asyncio.run, and pass --loop asyncio to uvicorn on Windows
    asyncio.run(run())
