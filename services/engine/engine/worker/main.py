"""`python -m engine.worker.main`: one worker process."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

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


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    if not config.encrypt_checkpoints:
        log.warning("checkpoints are stored unencrypted (ENGINE_DEV_INSECURE=1)")
    await prepare_database(config.database_url)

    pool = make_pool(config.database_url, max_size=config.worker_max_runs + 4)
    await pool.open(wait=True)
    redis = Redis.from_url(config.redis_url, decode_responses=True)
    raw = OllamaRaw(config.ollama_base_url)
    llm = SemaphoreLLM(LLMGateway(raw), ModelSemaphore(redis, limit=config.ollama_num_parallel))
    render = RenderPool(size=config.render_pool_size, timeout=config.render_timeout_sec)
    worker = Worker(config, pool, redis, llm=llm, render=render)  # the worker runs its own reaper

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(AttributeError, NotImplementedError):
            loop.add_signal_handler(getattr(signal, name), stop.set)

    await worker.start()
    log.info("worker %s ready", worker.owner)
    await stop.wait()

    await worker.stop()
    render.close()
    await raw.aclose()
    await redis.aclose()
    await pool.close()


if __name__ == "__main__":
    _windows_selector_loop()  # call before asyncio.run, and pass --loop asyncio to uvicorn on Windows
    asyncio.run(run())
