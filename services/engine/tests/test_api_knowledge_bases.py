"""The knowledge base API (knowledge-base design §6)."""
import contextlib
import dataclasses

from httpx import ASGITransport, AsyncClient

from engine.api.app import create_app


async def _kb(api, name="문서") -> str:
    created = await api.post("/knowledge-bases", json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["id"]


@contextlib.asynccontextmanager
async def _small_limit_client(pool, redis, api, *, max_file_bytes=10):
    small = dataclasses.replace(api.config, kb_max_file_bytes=max_file_bytes)
    async with AsyncClient(transport=ASGITransport(app=create_app(small, pool, redis)), base_url="http://api",
                           headers={"Authorization": "Bearer test-token"}) as client:
        yield client


async def test_create_list_and_delete(api):
    kb_id = await _kb(api)
    listed = (await api.get("/knowledge-bases")).json()["knowledgeBases"]
    assert listed[0] == {"id": kb_id, "name": "문서", "embedModel": "bge-m3", "fileCount": 0,
                         "createdAt": listed[0]["createdAt"]}
    assert (await api.delete(f"/knowledge-bases/{kb_id}")).status_code == 204
    assert (await api.delete(f"/knowledge-bases/{kb_id}")).status_code == 404
    assert (await api.get("/knowledge-bases")).json()["knowledgeBases"] == []


async def test_a_duplicate_name_is_a_409(api):
    await _kb(api, "같은")
    response = await api.post("/knowledge-bases", json={"name": "같은"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "NAME_TAKEN"


async def test_a_bad_name_is_a_422(api):
    assert (await api.post("/knowledge-bases", json={"name": ""})).status_code == 422
    assert (await api.post("/knowledge-bases", json={"name": "x" * 101})).status_code == 422


async def test_upload_queues_the_file_and_lists_it_without_content(api):
    kb_id = await _kb(api)
    uploaded = await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "보고서.md"},
                             content="# 제목\n\n본문".encode("utf-8"), headers={"content-type": "text/markdown"})
    assert uploaded.status_code == 202, uploaded.text
    body = uploaded.json()
    assert body["filename"] == "보고서.md" and body["status"] == "pending"

    files = (await api.get(f"/knowledge-bases/{kb_id}/files")).json()["files"]
    assert files[0]["id"] == body["id"] and files[0]["size"] == len("# 제목\n\n본문".encode("utf-8"))
    assert "content" not in files[0] and "본문" not in (await api.get(f"/knowledge-bases/{kb_id}/files")).text
    assert (await api.get("/knowledge-bases")).json()["knowledgeBases"][0]["fileCount"] == 1


async def test_upload_needs_a_name_and_an_existing_knowledge_base(api):
    kb_id = await _kb(api)
    assert (await api.put(f"/knowledge-bases/{kb_id}/files", content=b"x")).status_code == 422
    assert (await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a/b.md"}, content=b"x")).status_code == 422
    assert (await api.put("/knowledge-bases/00000000-0000-0000-0000-00000000dead/files", params={"name": "a.md"},
                          content=b"x")).status_code == 404
    assert (await api.put("/knowledge-bases/not-a-uuid/files", params={"name": "a.md"}, content=b"x")).status_code == 404


async def test_an_upload_over_the_file_limit_is_a_413(pool, redis, api):
    kb_id = await _kb(api)
    async with _small_limit_client(pool, redis, api) as client:
        response = await client.put(f"/knowledge-bases/{kb_id}/files", params={"name": "big.md"}, content=b"x" * 11)
    assert response.status_code == 413 and response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert (await api.get(f"/knowledge-bases/{kb_id}/files")).json()["files"] == []


async def test_an_upload_over_the_limit_is_caught_while_streaming_without_a_declared_length(pool, redis, api):
    kb_id = await _kb(api)

    async def _chunks():
        yield b"x" * 6
        yield b"x" * 6

    async with _small_limit_client(pool, redis, api) as client:
        response = await client.put(f"/knowledge-bases/{kb_id}/files", params={"name": "big.md"},
                                    content=_chunks())
    assert response.status_code == 413 and response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert (await api.get(f"/knowledge-bases/{kb_id}/files")).json()["files"] == []


async def test_deleting_a_file_removes_it_from_the_list(api):
    kb_id = await _kb(api)
    file_id = (await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a.md"}, content=b"# a")).json()["id"]
    assert (await api.delete(f"/knowledge-bases/{kb_id}/files/{file_id}")).status_code == 204
    assert (await api.delete(f"/knowledge-bases/{kb_id}/files/{file_id}")).status_code == 404
    assert (await api.get(f"/knowledge-bases/{kb_id}/files")).json()["files"] == []


async def test_deleting_a_file_under_the_wrong_kb_is_a_404(api):
    kb_a = await _kb(api, "A")
    kb_b = await _kb(api, "B")
    file_id = (await api.put(f"/knowledge-bases/{kb_a}/files", params={"name": "a.md"}, content=b"# a")).json()["id"]
    assert (await api.delete(f"/knowledge-bases/{kb_b}/files/{file_id}")).status_code == 404
    assert (await api.delete(f"/knowledge-bases/{kb_a}/files/not-a-uuid")).status_code == 404
    files = (await api.get(f"/knowledge-bases/{kb_a}/files")).json()["files"]
    assert files[0]["id"] == file_id


async def test_upload_rejects_bad_metadata(api):
    kb_id = await _kb(api)
    assert (await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a.md"}, content=b"")).status_code == 422
    assert (await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a\\b.md"},
                          content=b"x")).status_code == 422
    assert (await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a\x1b.md"},
                          content=b"x")).status_code == 422
    long_type = await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a.md"}, content=b"x",
                              headers={"content-type": "text/plain" + "x" * 300})
    assert long_type.status_code == 422


async def test_deleting_a_knowledge_base_cascades_its_files(pool, api):
    kb_id = await _kb(api)
    await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a.md"}, content=b"# a")
    assert (await api.delete(f"/knowledge-bases/{kb_id}")).status_code == 204
    assert (await api.get(f"/knowledge-bases/{kb_id}/files")).status_code == 404
    async with pool.connection() as conn:
        rows = await (await conn.execute("SELECT id FROM kb_files WHERE kb_id=%s", (kb_id,))).fetchall()
    assert rows == []
