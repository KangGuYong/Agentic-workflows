"""Knowledge bases and their files (knowledge-base design §6).

An upload is the raw file body with its Content-Type, not multipart: one fewer dependency, and the
proxy streams it through unchanged. It is the one route whose body limit is KB_MAX_FILE_BYTES rather
than MAX_BODY_BYTES.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from psycopg.errors import UniqueViolation

from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.kb import store

router = APIRouter()

MAX_NAME_CHARS = 100
MAX_FILENAME_CHARS = 255
MAX_MEDIA_TYPE_CHARS = 255
EMBED_DIM = 1024  # what kb_chunks.embedding holds (migration 0004)


def _has_control_chars(name: str) -> bool:
    # The name lands in logs and terminals; an escape sequence there is a nuisance nobody should have
    # to think about.
    return any(ord(ch) < 32 or ch == "\x7f" for ch in name)


def _kb_id(raw: str) -> str:
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "지식베이스를 찾을 수 없습니다") from None


def _kb_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row["id"]), "name": row["name"], "embedModel": row["embed_model"],
            "fileCount": int(row.get("file_count", 0)), "createdAt": row["created_at"].isoformat()}


def _file_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row["id"]), "filename": row["filename"], "size": int(row["size"]), "status": row["status"],
            "error": row.get("error"), "createdAt": row["created_at"].isoformat(),
            "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else row["created_at"].isoformat()}


@router.get("/knowledge-bases")
async def list_knowledge_bases(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        rows = await store.list_kbs(conn)
    return {"knowledgeBases": [_kb_view(row) for row in rows]}


@router.post("/knowledge-bases", status_code=201)
async def create_knowledge_base(request: Request) -> dict[str, Any]:
    body = require_object(await read_json(request))
    name = field(body, "name", str).strip()
    if not 1 <= len(name) <= MAX_NAME_CHARS or _has_control_chars(name):
        raise ApiError(422, "REQUEST_ERROR", f"이름은 1~{MAX_NAME_CHARS}자여야 합니다")
    config = request.app.state.config
    try:
        async with request.app.state.pool.connection() as conn:
            row = await store.create_kb(conn, name=name, embed_model=config.kb_embed_model, dim=EMBED_DIM)
    except UniqueViolation:
        raise ApiError(409, "NAME_TAKEN", "같은 이름의 지식베이스가 이미 있습니다") from None
    return _kb_view(row)


@router.delete("/knowledge-bases/{kb_id}", status_code=204)
async def delete_knowledge_base(kb_id: str, request: Request) -> Response:
    async with request.app.state.pool.connection() as conn:
        if not await store.delete_kb(conn, _kb_id(kb_id)):
            raise ApiError(404, "NOT_FOUND", "지식베이스를 찾을 수 없습니다")
    return Response(status_code=204)


@router.get("/knowledge-bases/{kb_id}/files")
async def list_files(kb_id: str, request: Request) -> dict[str, Any]:
    kb_id = _kb_id(kb_id)
    async with request.app.state.pool.connection() as conn:
        if await store.get_kb(conn, kb_id) is None:
            raise ApiError(404, "NOT_FOUND", "지식베이스를 찾을 수 없습니다")
        rows = await store.list_files(conn, kb_id)
    return {"files": [_file_view(row) for row in rows]}


@router.put("/knowledge-bases/{kb_id}/files", status_code=202)
async def upload_file(kb_id: str, request: Request) -> dict[str, Any]:
    kb_id = _kb_id(kb_id)
    name = (request.query_params.get("name") or "").strip()
    if (not 1 <= len(name) <= MAX_FILENAME_CHARS or "/" in name or "\\" in name or "\x00" in name
            or _has_control_chars(name)):
        raise ApiError(422, "REQUEST_ERROR", "파일 이름이 필요합니다 (경로 구분자 없이 255자 이하)")
    media_type = request.headers.get("content-type") or "application/octet-stream"
    if len(media_type) > MAX_MEDIA_TYPE_CHARS:
        raise ApiError(422, "REQUEST_ERROR", "Content-Type이 너무 깁니다")
    # The body is read before the knowledge base is checked, on purpose: checking first would hold a
    # pool connection open across the whole transfer.
    # ponytail: the body is buffered in memory (about twice the file at peak); stream it to the
    # database if uploads ever run wide.
    content = await _read_bytes(request, request.app.state.config.kb_max_file_bytes)
    if not content:
        raise ApiError(422, "REQUEST_ERROR", "빈 파일은 올릴 수 없습니다")
    async with request.app.state.pool.connection() as conn, conn.transaction():
        if await store.get_kb(conn, kb_id) is None:
            raise ApiError(404, "NOT_FOUND", "지식베이스를 찾을 수 없습니다")
        row = await store.add_file(conn, kb_id=kb_id, filename=name, media_type=media_type, content=content)
    return _file_view({**row, "error": None, "updated_at": None})


@router.delete("/knowledge-bases/{kb_id}/files/{file_id}", status_code=204)
async def delete_file(kb_id: str, file_id: str, request: Request) -> Response:
    kb_id = _kb_id(kb_id)
    async with request.app.state.pool.connection() as conn:
        if not await store.delete_file(conn, file_id, kb_id=kb_id):
            raise ApiError(404, "NOT_FOUND", "파일을 찾을 수 없습니다")
    return Response(status_code=204)


async def _read_bytes(request: Request, limit: int) -> bytes:
    """Like body.read_json's bounded read, for bytes: stop the instant the running total passes `limit`."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise _file_too_large(limit)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _file_too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _file_too_large(limit: int) -> ApiError:
    text = f"{limit}B" if limit < 1_000_000 else f"{limit // 1_000_000}MB"
    return ApiError(413, "PAYLOAD_TOO_LARGE", f"파일이 너무 큽니다 (최대 {text})")
