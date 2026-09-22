"""The ingester: claim a job, parse → chunk → embed → store, and the ways that goes wrong (§4)."""
import asyncio

import pytest_asyncio

from engine.ingest.worker import Ingester
from engine.kb import IngestError, store
from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.helpers import make_config

MD = "# 안내\n\n첫 문단.\n\n## 세부\n\n둘째 문단.\n".encode("utf-8")


class FakeParser:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls = 0
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()

    async def to_markdown(self, filename, media_type, content):
        self.calls += 1
        self.started.set()
        await self.proceed.wait()
        response = self.responses.pop(0) if self.responses else "# 파싱됨\n\n본문"
        if isinstance(response, Exception):
            raise response
        return response


@pytest_asyncio.fixture
async def ingester_factory(pool):
    started = []

    async def make(*, parser=None, llm=None, owner="ing-1", retry_delays=(0.1, 0.1), **overrides):
        overrides.setdefault("lease_sec", 2)
        config = make_config(claim_poll_sec=0.1, heartbeat_sec=0.2, **overrides)
        worker = Ingester(config, pool, llm=llm or ScriptedLLM([]), parser=parser, owner=owner,
                          retry_delays=retry_delays)
        await worker.start()
        started.append(worker)
        return worker

    yield make
    for worker in started:
        await worker.stop()


async def _upload(pool, content=MD, filename="a.md", media_type="text/markdown") -> tuple[str, str]:
    async with pool.connection() as conn, conn.transaction():
        kb = await store.create_kb(conn, name=f"kb-{filename}", embed_model="bge-m3", dim=1024)
        row = await store.add_file(conn, kb_id=str(kb["id"]), filename=filename, media_type=media_type, content=content)
    return str(kb["id"]), str(row["id"])


async def _file(pool, file_id):
    async with pool.connection() as conn:
        return await store.get_file(conn, file_id)


async def _status_is(pool, file_id, status):
    row = await _file(pool, file_id)
    return row if row is not None and row["status"] == status else None


async def _chunks(pool, file_id):
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT heading, text FROM kb_chunks WHERE file_id=%s ORDER BY ordinal", (file_id,))).fetchall()


async def test_a_markdown_file_is_chunked_embedded_and_marked_ready_without_the_parser(pool, ingester_factory):
    kb_id, file_id = await _upload(pool)
    parser = FakeParser()
    llm = ScriptedLLM([])
    await ingester_factory(parser=parser, llm=llm)

    await until(lambda: _status_is(pool, file_id, "ready"))

    assert parser.calls == 0
    assert [c["text"] for c in await _chunks(pool, file_id)] == ["첫 문단.", "둘째 문단."]
    assert llm.embed_calls[0][0] == "bge-m3" and llm.embed_calls[0][1] == ["안내\n\n첫 문단.", "세부\n\n둘째 문단."]


async def test_other_files_go_through_the_parser(pool, ingester_factory):
    _, file_id = await _upload(pool, b"%PDF", "a.pdf", "application/pdf")
    parser = FakeParser(["# 파싱됨\n\n본문"])
    await ingester_factory(parser=parser)

    await until(lambda: _status_is(pool, file_id, "ready"))
    assert parser.calls == 1
    assert [c["text"] for c in await _chunks(pool, file_id)] == ["본문"]


async def test_a_retryable_failure_is_retried_and_then_succeeds(pool, ingester_factory):
    _, file_id = await _upload(pool, b"%PDF", "a.pdf", "application/pdf")
    parser = FakeParser([IngestError("MinerU 오류 HTTP 503", retryable=True), "# ok\n\n본문"])
    await ingester_factory(parser=parser)

    await until(lambda: _status_is(pool, file_id, "ready"))
    assert parser.calls == 2


async def test_three_retryable_failures_mark_the_file_failed_with_the_reason(pool, ingester_factory):
    _, file_id = await _upload(pool, b"%PDF", "a.pdf", "application/pdf")
    parser = FakeParser([IngestError("MinerU 오류 HTTP 503", retryable=True)] * 3)
    await ingester_factory(parser=parser)

    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert parser.calls == 3 and row["error"] == "MinerU 오류 HTTP 503"
    async with pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM ingest_jobs")).fetchone())["n"] == 0


async def test_a_non_retryable_failure_fails_at_once(pool, ingester_factory):
    _, file_id = await _upload(pool, b"?", "a.xyz", "application/octet-stream")
    parser = FakeParser([IngestError("문서를 읽지 못했습니다 (MinerU 422)", retryable=False)])
    await ingester_factory(parser=parser)

    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert parser.calls == 1 and "422" in row["error"]


async def test_a_text_file_that_is_not_utf8_fails_at_once(pool, ingester_factory):
    _, file_id = await _upload(pool, b"\xff\xfe\x00", "a.txt", "text/plain")
    await ingester_factory(parser=FakeParser())

    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert "UTF-8" in row["error"]


async def test_a_file_with_no_text_fails_at_once(pool, ingester_factory):
    _, file_id = await _upload(pool, b"   \n", "empty.md")
    await ingester_factory(parser=FakeParser())

    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert row["error"] == "문서에서 텍스트를 찾지 못했습니다"


async def test_a_wrong_embedding_dimension_fails_at_once(pool, ingester_factory):
    _, file_id = await _upload(pool)

    class ShortLLM(ScriptedLLM):
        EMBED_DIM = 8

    await ingester_factory(llm=ShortLLM([]))
    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert "1024" in row["error"]


async def test_a_file_deleted_while_processing_is_dropped_quietly(pool, ingester_factory, caplog):
    _, file_id = await _upload(pool, b"%PDF", "a.pdf", "application/pdf")
    parser = FakeParser()
    parser.proceed.clear()
    worker = await ingester_factory(parser=parser)
    await asyncio.wait_for(parser.started.wait(), 10)

    async with pool.connection() as conn:
        await store.delete_file(conn, file_id)
    parser.proceed.set()

    async def settled():
        async with pool.connection() as conn:
            jobs = (await (await conn.execute("SELECT count(*) AS n FROM ingest_jobs")).fetchone())["n"]
            chunks = (await (await conn.execute("SELECT count(*) AS n FROM kb_chunks")).fetchone())["n"]
        return parser.calls == 1 and jobs == 0 and chunks == 0 and len(worker._tasks) == 1

    await until(settled)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_an_ingester_whose_lease_was_taken_writes_no_chunks(pool, ingester_factory):
    _, file_id = await _upload(pool, b"%PDF", "a.pdf", "application/pdf")
    parser = FakeParser()
    parser.proceed.clear()
    first = await ingester_factory(parser=parser, owner="first", lease_sec=1)
    await asyncio.wait_for(parser.started.wait(), 10)

    # The lease lapses while the parser is still working; another ingester takes the job. One
    # transaction so "first"'s heartbeat cannot renew the lease between the two statements.
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("UPDATE ingest_jobs SET lease_until = now() - interval '1 second'")
        stolen = await store.claim_job(conn, owner="second", lease_sec=30)
    assert stolen is not None
    parser.proceed.set()
    await asyncio.sleep(1.0)

    async with pool.connection() as conn:
        chunks = (await (await conn.execute("SELECT count(*) AS n FROM kb_chunks")).fetchone())["n"]
        job = await (await conn.execute("SELECT lease_owner FROM ingest_jobs")).fetchone()
    assert chunks == 0 and job["lease_owner"] == "second"
    await first.stop()


async def test_a_job_that_keeps_dying_is_given_up_after_the_attempt_ceiling(pool, ingester_factory):
    _, file_id = await _upload(pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE ingest_jobs SET attempt = 3")  # three claims already came and went

    await ingester_factory()
    row = await until(lambda: _status_is(pool, file_id, "failed"))
    assert "반복해서 중단" in row["error"]


async def test_a_job_whose_owner_died_is_picked_up_by_the_next_ingester(pool, ingester_factory):
    _, file_id = await _upload(pool)
    async with pool.connection() as conn:
        job = await store.claim_job(conn, owner="dead", lease_sec=30)
        await conn.execute("UPDATE ingest_jobs SET lease_until = now() - interval '1 second' WHERE id=%s", (job["id"],))

    await ingester_factory(owner="alive")
    await until(lambda: _status_is(pool, file_id, "ready"))
