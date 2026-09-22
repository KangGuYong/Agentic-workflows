"""The knowledge base store: rows, the ingest lease, and cosine search (knowledge-base design §3)."""
import pytest
from psycopg.errors import UniqueViolation

from engine.kb import store
from engine.llm.scripted import ScriptedLLM


def _one_hot(text: str) -> list[float]:
    vector = [0.0] * ScriptedLLM.EMBED_DIM
    vector[sum(map(ord, text)) % ScriptedLLM.EMBED_DIM] = 1.0
    return vector


async def _kb(pool, name="kb") -> str:
    async with pool.connection() as conn:
        return str((await store.create_kb(conn, name=name, embed_model="bge-m3", dim=1024))["id"])


async def test_a_knowledge_base_name_is_unique(pool):
    await _kb(pool, "같은 이름")
    async with pool.connection() as conn:
        with pytest.raises(UniqueViolation):
            await store.create_kb(conn, name="같은 이름", embed_model="bge-m3", dim=1024)


async def test_listing_counts_files_and_never_returns_content(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"# a")
        listed = await store.list_kbs(conn)
        files = await store.list_files(conn, kb_id)
    assert listed[0]["file_count"] == 1
    assert files[0]["status"] == "pending" and "content" not in files[0]


async def test_add_file_queues_exactly_one_job_and_claim_takes_it_once(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        first = await store.claim_job(conn, owner="w1", lease_sec=30)
        second = await store.claim_job(conn, owner="w2", lease_sec=30)
        row = await store.get_file(conn, file_id)
    assert str(first["file_id"]) == file_id and first["attempt"] == 1
    assert second is None
    assert row["status"] == "processing"


async def test_an_expired_lease_is_claimed_again_with_a_higher_attempt(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x")
        job = await store.claim_job(conn, owner="dead", lease_sec=30)
        await conn.execute("UPDATE ingest_jobs SET lease_until = now() - interval '1 second' WHERE id=%s", (job["id"],))
        again = await store.claim_job(conn, owner="alive", lease_sec=30)
    assert again is not None and again["attempt"] == 2


async def test_release_for_retry_delays_the_next_claim(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        job = await store.claim_job(conn, owner="w", lease_sec=30)
        await store.release_for_retry(conn, job_id=str(job["id"]), owner="w", delay_sec=3600)
        assert (await store.get_file(conn, file_id))["status"] == "pending"
        assert await store.claim_job(conn, owner="w", lease_sec=30) is None
        await conn.execute("UPDATE ingest_jobs SET next_attempt_at = now()")
        assert await store.claim_job(conn, owner="w", lease_sec=30) is not None


async def test_replace_chunks_is_idempotent_and_marks_the_file_ready(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        job = await store.claim_job(conn, owner="w", lease_sec=30)
        rows = [("h", "하나", _one_hot("하나")), ("h", "둘", _one_hot("둘"))]
        assert await store.replace_chunks(conn, file_id=file_id, kb_id=kb_id, chunks=rows) is True
        assert await store.replace_chunks(conn, file_id=file_id, kb_id=kb_id, chunks=rows) is True
        count = (await (await conn.execute("SELECT count(*) AS n FROM kb_chunks WHERE file_id=%s", (file_id,))).fetchone())["n"]
        assert await store.finish_job(conn, job_id=str(job["id"]), owner="w", error=None)
        row = await store.get_file(conn, file_id)
        jobs = (await (await conn.execute("SELECT count(*) AS n FROM ingest_jobs")).fetchone())["n"]
    assert count == 2 and row["status"] == "ready" and jobs == 0


async def test_finish_job_with_an_error_marks_the_file_failed(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        job = await store.claim_job(conn, owner="w", lease_sec=30)
        await store.finish_job(conn, job_id=str(job["id"]), owner="w", error="문서를 읽지 못했습니다")
        row = await store.get_file(conn, file_id)
    assert (row["status"], row["error"]) == ("failed", "문서를 읽지 못했습니다")


async def test_replace_chunks_reports_a_file_that_was_deleted_meanwhile(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        await store.delete_file(conn, file_id)
        assert await store.replace_chunks(conn, file_id=file_id, kb_id=kb_id, chunks=[("h", "t", _one_hot("t"))]) is False


async def test_search_orders_by_cosine_similarity_and_returns_text_only(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="doc.md", media_type="text/markdown", content=b"x"))["id"])
        await store.replace_chunks(conn, file_id=file_id, kb_id=kb_id,
                                   chunks=[("절", "정답 문장", _one_hot("정답 문장")), (None, "다른 문장", _one_hot("다른 문장"))])
        hits = await store.search(conn, kb_id=kb_id, embedding=_one_hot("정답 문장"), top_k=5)
    assert [h["text"] for h in hits] == ["정답 문장", "다른 문장"]
    assert hits[0] == {"text": "정답 문장", "score": pytest.approx(1.0), "heading": "절", "file": "doc.md"}
    assert hits[1]["score"] == pytest.approx(0.0)


async def test_get_kb_rejects_a_non_uuid_without_touching_the_database(pool):
    async with pool.connection() as conn:
        assert await store.get_kb(conn, "not-a-uuid") is None


async def test_a_stale_owner_cannot_release_or_finish_a_job_someone_else_holds(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        file_id = str((await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x"))["id"])
        job = await store.claim_job(conn, owner="w1", lease_sec=30)
        await conn.execute("UPDATE ingest_jobs SET lease_until = now() - interval '1 second' WHERE id=%s", (job["id"],))
        assert (await store.claim_job(conn, owner="w2", lease_sec=30))["attempt"] == 2

        assert await store.release_for_retry(conn, job_id=str(job["id"]), owner="w1", delay_sec=1) is False
        assert await store.finish_job(conn, job_id=str(job["id"]), owner="w1", error=None) is False
        assert await store.heartbeat_job(conn, job_id=str(job["id"]), owner="w1", lease_sec=30) is False
        row = await store.get_file(conn, file_id)
        jobs = (await (await conn.execute("SELECT count(*) AS n FROM ingest_jobs")).fetchone())["n"]
    assert row["status"] == "processing" and jobs == 1


async def test_finish_job_on_a_deleted_file_writes_nothing(pool):
    kb_id = await _kb(pool)
    async with pool.connection() as conn:
        await store.add_file(conn, kb_id=kb_id, filename="a.md", media_type="text/markdown", content=b"x")
        job = await store.claim_job(conn, owner="w", lease_sec=30)
        await store.delete_file(conn, str(job["file_id"]))
        assert await store.finish_job(conn, job_id=str(job["id"]), owner="w", error=None) is False
