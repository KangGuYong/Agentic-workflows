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
from engine.http.client import GuardedClient, SystemResolver
from engine.kb.rerank import TeiReranker
from engine.kb.store import PostgresKnowledgeBases
from engine.llm.gateway import LLMGateway
from engine.llm.ollama import OllamaRaw
from engine.llm.semaphore import ModelSemaphore, SemaphoreLLM
from engine.secrets.store import PostgresSecretResolver
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
    http: GuardedClient | None = None
    worker: Worker | None = None
    rerank: TeiReranker | None = None
    try:
        await pool.open(wait=True)
        redis = Redis.from_url(config.redis_url, decode_responses=True)
        raw = OllamaRaw(config.ollama_base_url)
        llm = SemaphoreLLM(LLMGateway(raw), ModelSemaphore(redis, limit=config.ollama_num_parallel))
        render = RenderPool(size=config.render_pool_size, timeout=config.render_timeout_sec)
        # The egress client and the secret resolver, built once for the process. An empty allowlist still
        # builds a client: every http_request is then blocked, which is the documented default.
        http = GuardedClient(config.http_allowlist, resolver=SystemResolver(limit=config.worker_max_runs),
                             max_redirects=config.http_max_redirects,
                             max_request_bytes=config.http_max_request_bytes,
                             max_response_bytes=config.http_max_response_bytes)
        secrets = PostgresSecretResolver(pool, config.secret_key) if config.secret_key else None
        kb = PostgresKnowledgeBases(pool)
        rerank = TeiReranker(config.rerank_base_url) if config.rerank_base_url else None
        if rerank is None:
            log.warning("RERANK_BASE_URL is not set: rerank nodes will fail")
        # Hosts only, never a full URL (MVP design 10.1).
        log.info("egress allowlist: %s",
                 ", ".join(f"{e.scheme}://{e.host}:{e.port}" for e in config.http_allowlist)
                 or "(empty: all http_request calls are blocked)")
        worker = Worker(config, pool, redis, llm=llm, render=render, http=http,
                        secrets=secrets, kb=kb, rerank=rerank)  # the worker runs its own reaper

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
            ("http client", http.aclose if http is not None else None),
            ("rerank client", rerank.aclose if rerank is not None else None),
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
