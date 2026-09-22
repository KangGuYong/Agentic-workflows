"""The knowledge base API (knowledge-base design §6)."""
import dataclasses

from httpx import ASGITransport, AsyncClient

from engine.api.app import create_app


async def _kb(api, name="문서") -> str:
    created = await api.post("/knowledge-bases", json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["id"]


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
    small = dataclasses.replace(api.config, kb_max_file_bytes=10)
    async with AsyncClient(transport=ASGITransport(app=create_app(small, pool, redis)), base_url="http://api",
                           headers={"Authorization": "Bearer test-token"}) as client:
        response = await client.put(f"/knowledge-bases/{kb_id}/files", params={"name": "big.md"}, content=b"x" * 11)
    assert response.status_code == 413 and response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert (await api.get(f"/knowledge-bases/{kb_id}/files")).json()["files"] == []


async def test_deleting_a_file_removes_it_from_the_list(api):
    kb_id = await _kb(api)
    file_id = (await api.put(f"/knowledge-bases/{kb_id}/files", params={"name": "a.md"}, content=b"# a")).json()["id"]
    assert (await api.delete(f"/knowledge-bases/{kb_id}/files/{file_id}")).status_code == 204
    assert (await api.delete(f"/knowledge-bases/{kb_id}/files/{file_id}")).status_code == 404
    assert (await api.get(f"/knowledge-bases/{kb_id}/files")).json()["files"] == []
