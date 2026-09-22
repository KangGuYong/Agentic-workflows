"""Knowledge base rows, the ingest lease and cosine search (knowledge-base design §3, §4.1).

Vectors are passed to Postgres as their text form (`'[0.1,0.2,...]'::vector`) and never read back, so
no pgvector client library is needed. Callers manage transactions; nothing here commits on its own.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from engine.db.workflows import WORKSPACE
from engine.errors import ErrorCode, NodeError

TEXT_TYPES = ("text/markdown", "text/plain")
TEXT_SUFFIXES = (".md", ".txt")


def _uuid(raw: str) -> str | None:
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


def vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def is_text_file(filename: str, media_type: str) -> bool:
    """Files the ingester reads as UTF-8 markdown itself, without MinerU."""
    return media_type.split(";")[0].strip().lower() in TEXT_TYPES or filename.lower().endswith(TEXT_SUFFIXES)


# ------------------------------------------------------------------ knowledge bases

async def create_kb(conn: AsyncConnection, *, name: str, embed_model: str, dim: int) -> dict[str, Any]:
    return await (await conn.execute(
        "INSERT INTO knowledge_bases (id, workspace_id, name, embed_model, dim) VALUES (%s, %s, %s, %s, %s)"
        " RETURNING id, name, embed_model, dim, created_at",
        (str(uuid.uuid4()), WORKSPACE, name, embed_model, dim),
    )).fetchone()


async def get_kb(conn: AsyncConnection, kb_id: str) -> dict[str, Any] | None:
    kb_id = _uuid(kb_id)
    if kb_id is None:
        return None
    return await (await conn.execute("SELECT * FROM knowledge_bases WHERE id=%s", (kb_id,))).fetchone()


async def list_kbs(conn: AsyncConnection) -> list[dict[str, Any]]:
    return await (await conn.execute(
        "SELECT k.id, k.name, k.embed_model, k.created_at,"
        "   (SELECT count(*) FROM kb_files f WHERE f.kb_id = k.id) AS file_count"
        " FROM knowledge_bases k WHERE k.workspace_id=%s ORDER BY k.created_at DESC", (WORKSPACE,),
    )).fetchall()


async def delete_kb(conn: AsyncConnection, kb_id: str) -> bool:
    kb_id = _uuid(kb_id)
    if kb_id is None:
        return False
    row = await (await conn.execute("DELETE FROM knowledge_bases WHERE id=%s RETURNING id", (kb_id,))).fetchone()
    return row is not None


# ------------------------------------------------------------------ files and jobs

async def add_file(conn: AsyncConnection, *, kb_id: str, filename: str, media_type: str,
                   content: bytes) -> dict[str, Any]:
    """The file row and its job in one statement: the pool is autocommit, and a file without a job
    would sit in `pending` forever."""
    return await (await conn.execute(
        "WITH f AS (INSERT INTO kb_files (id, kb_id, filename, media_type, size, content, status)"
        "             VALUES (%s, %s, %s, %s, %s, %s, 'pending')"
        "             RETURNING id, filename, size, status, created_at),"
        "     j AS (INSERT INTO ingest_jobs (file_id) SELECT id FROM f)"
        " SELECT * FROM f",
        (str(uuid.uuid4()), kb_id, filename, media_type, len(content), content),
    )).fetchone()


async def get_file(conn: AsyncConnection, file_id: str) -> dict[str, Any] | None:
    file_id = _uuid(file_id)
    if file_id is None:
        return None
    return await (await conn.execute("SELECT * FROM kb_files WHERE id=%s", (file_id,))).fetchone()


async def list_files(conn: AsyncConnection, kb_id: str) -> list[dict[str, Any]]:
    return await (await conn.execute(
        "SELECT id, filename, media_type, size, status, error, created_at, updated_at"
        " FROM kb_files WHERE kb_id=%s ORDER BY created_at DESC", (kb_id,),
    )).fetchall()


async def delete_file(conn: AsyncConnection, file_id: str) -> bool:
    file_id = _uuid(file_id)
    if file_id is None:
        return False
    row = await (await conn.execute("DELETE FROM kb_files WHERE id=%s RETURNING id", (file_id,))).fetchone()
    return row is not None


async def claim_job(conn: AsyncConnection, *, owner: str, lease_sec: int) -> dict[str, Any] | None:
    """Take the oldest due job whose lease is free or expired, like runs.claim_next, and mark its file
    `processing` in the same statement. The attempt counter moves here, so a crash mid-job (lease
    expiry) counts as an attempt too."""
    return await (await conn.execute(
        "WITH j AS (UPDATE ingest_jobs SET lease_owner=%(owner)s, attempt=attempt + 1,"
        "             lease_until=now() + make_interval(secs => %(lease)s)"
        "           WHERE id = (SELECT id FROM ingest_jobs"
        "                       WHERE (lease_until IS NULL OR lease_until < now()) AND next_attempt_at <= now()"
        "                       ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)"
        "           RETURNING id, file_id, attempt),"
        "     f AS (UPDATE kb_files SET status='processing', error=NULL, updated_at=now()"
        "           WHERE id = (SELECT file_id FROM j))"
        " SELECT id, file_id, attempt FROM j",
        {"owner": owner, "lease": lease_sec},
    )).fetchone()


async def heartbeat_job(conn: AsyncConnection, *, job_id: str, owner: str, lease_sec: int) -> bool:
    row = await (await conn.execute(
        "UPDATE ingest_jobs SET lease_until=now() + make_interval(secs => %s)"
        " WHERE id=%s AND lease_owner=%s RETURNING id", (lease_sec, job_id, owner),
    )).fetchone()
    return row is not None


async def release_for_retry(conn: AsyncConnection, *, job_id: str, owner: str, delay_sec: float) -> bool:
    """Hand the job back for a later attempt. False means this owner no longer held the lease and
    nothing was written -- including the file's status, which belongs to whoever holds it now."""
    row = await (await conn.execute(
        "WITH j AS (UPDATE ingest_jobs SET lease_owner=NULL, lease_until=NULL,"
        "             next_attempt_at=now() + make_interval(secs => %(delay)s)"
        "           WHERE id=%(job)s AND lease_owner=%(owner)s RETURNING file_id)"
        " UPDATE kb_files SET status='pending', updated_at=now()"
        " WHERE id = (SELECT file_id FROM j) RETURNING id",
        {"delay": delay_sec, "job": job_id, "owner": owner},
    )).fetchone()
    return row is not None


async def finish_job(conn: AsyncConnection, *, job_id: str, owner: str, error: str | None) -> bool:
    """Close the job and mark its file ready, or failed with `error`, in one statement: two would let a
    crash in between delete the job and leave the file `processing` with nothing left to recover it.
    False means this owner no longer held the lease (or the file is gone) and nothing was written."""
    row = await (await conn.execute(
        "WITH j AS (DELETE FROM ingest_jobs WHERE id=%(job)s AND lease_owner=%(owner)s RETURNING file_id)"
        " UPDATE kb_files SET status=%(status)s, error=%(error)s, updated_at=now()"
        " WHERE id = (SELECT file_id FROM j) RETURNING id",
        {"job": job_id, "owner": owner, "status": "failed" if error else "ready", "error": error},
    )).fetchone()
    return row is not None


async def replace_chunks(conn: AsyncConnection, *, file_id: str, kb_id: str,
                         chunks: list[tuple[str | None, str, list[float]]],
                         lease: tuple[str, str] | None = None) -> bool:
    """Delete this file's chunks and insert `chunks` (heading, text, embedding), so a re-run never
    duplicates. False when the file no longer exists, or when `lease` is given and this owner no
    longer holds the job."""
    async with conn.transaction():
        if lease is None:
            exists = await (await conn.execute("SELECT 1 FROM kb_files WHERE id=%s FOR UPDATE", (file_id,))).fetchone()
        else:
            # The writer must still own the job: a worker whose lease expired mid-embed writes nothing
            # (design §4), the same rule finish_job enforces.
            exists = await (await conn.execute(
                "SELECT 1 FROM kb_files f WHERE f.id=%s"
                "   AND EXISTS (SELECT 1 FROM ingest_jobs j WHERE j.id=%s AND j.lease_owner=%s AND j.file_id=f.id)"
                " FOR UPDATE OF f", (file_id, *lease))).fetchone()
        if exists is None:
            return False
        await conn.execute("DELETE FROM kb_chunks WHERE file_id=%s", (file_id,))
        async with conn.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO kb_chunks (kb_id, file_id, ordinal, heading, text, embedding)"
                " VALUES (%s, %s, %s, %s, %s, %s::vector)",
                [(kb_id, file_id, i, heading, text, vector_literal(vec))
                 for i, (heading, text, vec) in enumerate(chunks)],
            )
    return True


# ------------------------------------------------------------------ search

async def search(conn: AsyncConnection, *, kb_id: str, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
    """Top-k chunks by cosine similarity. Text and metadata only: the vector stays in this table."""
    # ponytail: one HNSW index over every knowledge base, filtered by kb_id after the scan. With several
    # large bases in one table a small base can get fewer than top_k hits back; raise hnsw.ef_search or
    # give each base its own partial index when that shows up.
    rows = await (await conn.execute(
        "SELECT c.text, c.heading, f.filename AS file, 1 - (c.embedding <=> %(q)s::vector) AS score"
        " FROM kb_chunks c JOIN kb_files f ON f.id = c.file_id"
        " WHERE c.kb_id=%(kb)s ORDER BY c.embedding <=> %(q)s::vector LIMIT %(k)s",
        {"q": vector_literal(embedding), "kb": kb_id, "k": top_k},
    )).fetchall()
    return [{"text": r["text"], "score": float(r["score"]), "heading": r["heading"], "file": r["file"]} for r in rows]


class PostgresKnowledgeBases:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get(self, kb_id: str) -> dict[str, Any] | None:
        try:
            async with self._pool.connection() as conn:
                return await get_kb(conn, kb_id)
        except psycopg.Error as exc:
            raise NodeError(ErrorCode.NODE_FAILED, "지식베이스 저장소에 연결하지 못했습니다", retryable=True) from exc

    async def search(self, kb_id: str, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        try:
            async with self._pool.connection() as conn:
                return await search(conn, kb_id=kb_id, embedding=embedding, top_k=top_k)
        except psycopg.Error as exc:
            raise NodeError(ErrorCode.NODE_FAILED, "지식베이스 저장소에 연결하지 못했습니다", retryable=True) from exc
