"""`python -m engine.ingest.main`: one ingester process."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from typing import Any

from engine.config import load_config
from engine.db.migrate import prepare_database
from engine.db.pool import make_pool
from engine.ingest.worker import Ingester
from engine.kb.mineru import MineruParser
from engine.llm.gateway import LLMGateway
from engine.llm.ollama import OllamaRaw

log = logging.getLogger(__name__)


async def run(stop: asyncio.Event | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    if stop is None:
        stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(AttributeError, NotImplementedError):
            loop.add_signal_handler(getattr(signal, name), stop.set)

    await prepare_database(config.database_url)
    # Each job holds a connection while its heartbeat checks out another, plus the claim loop.
    pool = make_pool(config.database_url, max_size=max(4, config.ingest_max_jobs * 2 + 2))
    raw: OllamaRaw | None = None
    parser: MineruParser | None = None
    worker: Ingester | None = None
    try:
        await pool.open(wait=True)
        raw = OllamaRaw(config.ollama_base_url)
        if config.mineru_base_url:
            parser = MineruParser(config.mineru_base_url, timeout=config.mineru_timeout_sec)
        else:
            log.warning("MINERU_BASE_URL is not set: only .md/.txt files can be ingested")
        worker = Ingester(config, pool, llm=LLMGateway(raw), parser=parser)
        await worker.start()
        log.info("ingester %s ready", worker.owner)
        await stop.wait()
    finally:
        shutdowns: list[tuple[str, Any]] = [
            ("ingester", worker.stop if worker is not None else None),
            ("mineru client", parser.aclose if parser is not None else None),
            ("ollama client", raw.aclose if raw is not None else None),
            ("db pool", pool.close),
        ]
        for name, shutdown in shutdowns:
            if shutdown is None:
                continue
            try:
                await shutdown()
            except Exception:
                log.warning("ingester shutdown: closing the %s failed", name, exc_info=True)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
