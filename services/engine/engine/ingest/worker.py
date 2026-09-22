"""The ingester: one process that turns uploaded files into chunk vectors (knowledge-base design §4).

Same shape as engine/worker/worker.py -- claim under a lease, heartbeat while working, hand back on
failure -- but for `ingest_jobs`, and with nothing to checkpoint: a job either finishes or runs again
from the start.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
import uuid
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from engine.config import EngineConfig
from engine.errors import NodeError
from engine.kb import IngestError, store
from engine.kb.chunk import chunk_markdown
from engine.llm.base import LLMClient

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAYS_SEC: tuple[float, ...] = (30.0, 120.0)  # after attempt 1, after attempt 2; attempt 3 is the last
EMBED_BATCH = 32
STOP_TIMEOUT_SEC = 10.0


class Parser(Protocol):
    async def to_markdown(self, filename: str, media_type: str, content: bytes) -> str: ...


class Ingester:
    def __init__(self, config: EngineConfig, pool: AsyncConnectionPool, *, llm: LLMClient,
                 parser: Parser | None, owner: str | None = None,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS_SEC) -> None:
        self._config = config
        self._pool = pool
        self._llm = llm
        self._parser = parser
        self._retry_delays = retry_delays
        self.owner = owner or f"ingester-{uuid.uuid4()}"
        self._tasks: set[asyncio.Task] = set()
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._spawn(self._claim_loop())

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=STOP_TIMEOUT_SEC)
            if pending:
                log.warning("ingester %s: %s task(s) did not stop within %ss; abandoning them",
                           self.owner, len(pending), STOP_TIMEOUT_SEC)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _claim_loop(self) -> None:
        in_flight: set[asyncio.Task] = set()
        while self._running:
            try:
                claimed = False
                while self._running and len(in_flight) < self._config.ingest_max_jobs:
                    async with self._pool.connection() as conn:
                        job = await store.claim_job(conn, owner=self.owner, lease_sec=self._config.lease_sec)
                    if job is None:
                        break
                    claimed = True
                    task = self._spawn(self._process(job))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                if not claimed:
                    await asyncio.sleep(self._config.claim_poll_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ingest claim loop failed; retrying after the poll interval")
                await asyncio.sleep(self._config.claim_poll_sec)

    async def _heartbeat(self, job_id: str, task: asyncio.Task) -> None:
        last_ok = time.monotonic()
        while True:
            await asyncio.sleep(self._config.heartbeat_sec)
            try:
                async with self._pool.connection() as conn:
                    owned = await store.heartbeat_job(conn, job_id=job_id, owner=self.owner,
                                                      lease_sec=self._config.lease_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("ingest heartbeat failed for job %s; retrying", job_id, exc_info=True)
                if time.monotonic() - last_ok > self._config.lease_sec:
                    # The lease has certainly expired by now and another ingester may hold the job;
                    # keep going and we would be the second writer.
                    log.error("no heartbeat for job %s in %.0fs; giving up the job",
                             job_id, time.monotonic() - last_ok)
                    task.cancel()
                    return
                continue
            if not owned:  # the lease is gone: someone else owns the job, write nothing more
                task.cancel()
                return
            last_ok = time.monotonic()

    async def _process(self, job: dict[str, Any]) -> None:
        job_id, file_id, attempt = str(job["id"]), str(job["file_id"]), int(job["attempt"])
        beat = self._spawn(self._heartbeat(job_id, asyncio.current_task()))
        try:
            async with self._pool.connection() as conn:
                row = await store.get_file(conn, file_id)
            if row is None:
                return  # deleted between claim and read; the job went with it
            if attempt > MAX_ATTEMPTS:
                # Every earlier attempt died without reaching finish_job (a crash, a kill mid-parse): the
                # lease expired and the claim counted it. Without this the job would be re-leased forever.
                async with self._pool.connection() as conn:
                    await store.finish_job(conn, job_id=job_id, owner=self.owner,
                                           error="처리가 반복해서 중단되어 포기했습니다")
                return
            try:
                chunks = await self._ingest(row)
            except (IngestError, NodeError) as exc:
                if exc.retryable and attempt < MAX_ATTEMPTS:
                    delay = self._retry_delays[min(attempt, len(self._retry_delays)) - 1]
                    log.info("ingest of %s failed (attempt %s/%s), retrying in %ss: %s",
                             file_id, attempt, MAX_ATTEMPTS, delay, exc.message)
                    async with self._pool.connection() as conn:
                        await store.release_for_retry(conn, job_id=job_id, owner=self.owner, delay_sec=delay)
                    return
                async with self._pool.connection() as conn:
                    await store.finish_job(conn, job_id=job_id, owner=self.owner, error=exc.message)
                return
            async with self._pool.connection() as conn:
                if not await store.replace_chunks(conn, file_id=file_id, kb_id=str(row["kb_id"]), chunks=chunks,
                                                  lease=(job_id, self.owner)):
                    return  # the file was deleted, or the lease is no longer ours
                await store.finish_job(conn, job_id=job_id, owner=self.owner, error=None)
        except asyncio.CancelledError:
            # stop() cancelled us mid-job; the attempt is spent either way, but the next ingester need
            # not wait out the lease. Fenced, so a cancel caused by a lost lease writes nothing.
            with contextlib.suppress(Exception):
                async with self._pool.connection() as conn:
                    await asyncio.shield(store.release_for_retry(conn, job_id=job_id, owner=self.owner, delay_sec=0))
            raise
        except Exception:
            # Infrastructure, not the document: leave the lease to expire so the job is retried.
            log.exception("ingest of %s aborted", file_id)
        finally:
            beat.cancel()

    async def _ingest(self, row: dict[str, Any]) -> list[tuple[str | None, str, list[float]]]:
        filename, media_type, content = row["filename"], row["media_type"], bytes(row["content"])
        if store.is_text_file(filename, media_type):
            try:
                markdown = content.decode("utf-8")
            except UnicodeDecodeError:
                raise IngestError("텍스트 파일이 UTF-8이 아닙니다", retryable=False) from None
        else:
            if self._parser is None:
                raise IngestError("MinerU가 설정되지 않아 이 형식은 처리할 수 없습니다", retryable=False)
            markdown = await self._parser.to_markdown(filename, media_type, content)
        chunks = chunk_markdown(markdown)
        if not chunks:
            raise IngestError("문서에서 텍스트를 찾지 못했습니다", retryable=False)
        async with self._pool.connection() as conn:
            kb = await store.get_kb(conn, str(row["kb_id"]))
        if kb is None:
            raise IngestError("지식베이스를 찾을 수 없습니다", retryable=False)
        # The heading is part of what gets embedded -- "세부" alone says little, "안내 > 세부 ..." finds
        # the section -- but it is stored separately so the hit can show it.
        texts = [f"{c.heading}\n\n{c.text}" if c.heading else c.text for c in chunks]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            vectors += await self._llm.embed(model=kb["embed_model"], texts=texts[start:start + EMBED_BATCH])
        if any(len(v) != kb["dim"] or not all(map(math.isfinite, v)) for v in vectors):
            raise IngestError(f"임베딩이 올바르지 않습니다 (차원 {kb['dim']} 또는 유한값이 아님)", retryable=False)
        return [(c.heading, c.text, v) for c, v in zip(chunks, vectors, strict=True)]
