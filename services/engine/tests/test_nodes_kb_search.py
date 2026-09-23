"""The kb_search node (knowledge-base design §5.1): embed the question, search, return text only."""
import pytest

from engine.config import load_config
from engine.db import runs as run_db
from engine.errors import EngineFault, ErrorCode, NodeError
from engine.kb import store
from engine.kb.store import PostgresKnowledgeBases
from engine.llm.scripted import ScriptedLLM
from engine.nodes.kb_search import KbSearchNode
from tests.conftest import until
from tests.factories import make_run
from tests.helpers import make_ctx


def _one_hot(text: str) -> list[float]:
    vector = [0.0] * ScriptedLLM.EMBED_DIM
    vector[sum(map(ord, text)) % ScriptedLLM.EMBED_DIM] = 1.0
    return vector


async def _seed(pool, texts: list[str]) -> str:
    async with pool.connection() as conn, conn.transaction():
        kb = await store.create_kb(conn, name="kb", embed_model="bge-m3", dim=1024)
        row = await store.add_file(conn, kb_id=str(kb["id"]), filename="doc.md", media_type="text/markdown", content=b"x")
        await store.replace_chunks(conn, file_id=str(row["id"]), kb_id=str(kb["id"]),
                                   chunks=[("절", t, _one_hot(t)) for t in texts])
    return str(kb["id"])


def _config(kb_id: str, **overrides):
    raw = {"knowledgeBase": kb_id, "query": "{{ start.q }}", **overrides}
    return KbSearchNode().parse_config(raw)


async def test_returns_hits_in_score_order_and_a_joined_context(pool):
    kb_id = await _seed(pool, ["정답 문장", "다른 문장"])
    llm = ScriptedLLM([])
    ctx = make_ctx(llm=llm, kb=PostgresKnowledgeBases(pool))

    result = await KbSearchNode().execute(ctx, _config(kb_id, topK=2), {"query": "정답 문장"})

    assert [h["text"] for h in result.output["hits"]] == ["정답 문장", "다른 문장"]
    assert result.output["hits"][0] == {"text": "정답 문장", "score": pytest.approx(1.0), "heading": "절", "file": "doc.md"}
    assert result.output["context"] == "정답 문장\n\n---\n\n다른 문장"
    assert llm.embed_calls == [("bge-m3", ["정답 문장"])]


async def test_min_score_drops_weak_hits(pool):
    kb_id = await _seed(pool, ["정답 문장", "다른 문장"])
    ctx = make_ctx(llm=ScriptedLLM([]), kb=PostgresKnowledgeBases(pool))

    result = await KbSearchNode().execute(ctx, _config(kb_id, minScore=0.5), {"query": "정답 문장"})

    assert [h["text"] for h in result.output["hits"]] == ["정답 문장"]

    # >= semantics: the exact best hit (score 1.0) survives a minScore of exactly 1.0.
    result = await KbSearchNode().execute(ctx, _config(kb_id, minScore=1.0), {"query": "정답 문장"})
    assert [h["text"] for h in result.output["hits"]] == ["정답 문장"]

    result = await KbSearchNode().execute(ctx, _config(kb_id, topK=1), {"query": "정답 문장"})
    assert len(result.output["hits"]) == 1


async def test_an_empty_knowledge_base_is_an_empty_result_not_an_error(pool):
    kb_id = await _seed(pool, [])
    ctx = make_ctx(llm=ScriptedLLM([]), kb=PostgresKnowledgeBases(pool))

    result = await KbSearchNode().execute(ctx, _config(kb_id), {"query": "무엇"})

    assert result.output == {"hits": [], "context": ""}


async def test_an_unknown_knowledge_base_fails_without_retry(pool):
    ctx = make_ctx(llm=ScriptedLLM([]), kb=PostgresKnowledgeBases(pool))

    with pytest.raises(NodeError) as caught:
        await KbSearchNode().execute(ctx, _config("00000000-0000-0000-0000-00000000dead"), {"query": "무엇"})
    assert caught.value.code == ErrorCode.NODE_FAILED and not caught.value.retryable
    assert "지식베이스를 찾을 수 없습니다" in caught.value.message


async def test_an_empty_query_is_a_template_error():
    with pytest.raises(NodeError) as caught:
        await KbSearchNode().execute(make_ctx(), _config("x"), {"query": "   "})
    assert caught.value.code == ErrorCode.TEMPLATE_ERROR


def test_the_config_schema_marks_the_knowledge_base_field_for_the_editor():
    schema = KbSearchNode.Config.model_json_schema()
    assert schema["properties"]["knowledgeBase"]["x-knowledge-base"] is True
    assert schema["properties"]["query"]["x-template"] is True


def test_the_output_schema_describes_hits_and_context():
    schema = KbSearchNode().output_schema(_config("x"), {})
    assert set(schema["properties"]) == {"hits", "context"}
    assert schema["properties"]["hits"]["items"]["required"] == ["text", "score", "heading", "file"]


async def test_a_missing_port_is_an_engine_fault():
    ctx = make_ctx(llm=ScriptedLLM([]))  # kb=None: worker misconfiguration, not a node error

    with pytest.raises(EngineFault):
        await KbSearchNode().execute(ctx, _config("x"), {"query": "q"})


async def test_a_database_failure_is_a_retryable_node_error(db_url):
    from psycopg_pool import AsyncConnectionPool

    # A pool that can never connect stands in for a dropped connection or a pool timeout.
    dead = AsyncConnectionPool("postgresql://nobody@127.0.0.1:1/none", open=False, timeout=0.5)
    await dead.open(wait=False)
    try:
        with pytest.raises(NodeError) as caught:
            await PostgresKnowledgeBases(dead).get("00000000-0000-0000-0000-000000000000")
        assert caught.value.retryable
    finally:
        await dead.close()


async def test_the_worker_wires_the_kb_port_through_to_the_node(pool, worker_factory):
    kb_id = await _seed(pool, ["정답 문장", "다른 문장"])
    dsl = {
        "version": "1",
        "nodes": [
            {"id": "start", "type": "start", "position": {"x": 0, "y": 0},
             "config": {"inputs": {"type": "object", "properties": {"q": {"type": "string"}},
                                   "required": ["q"]}}},
            {"id": "kb_search_1", "type": "kb_search", "position": {"x": 1, "y": 0},
             "config": {"knowledgeBase": kb_id, "query": "{{ start.q }}", "topK": 1}},
            {"id": "end", "type": "end", "position": {"x": 2, "y": 0},
             "config": {"outputs": {"answer": "{{ kb_search_1.context }}"}}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "kb_search_1"},
            {"id": "e2", "source": "kb_search_1", "target": "end"},
        ],
    }
    run_id = await make_run(pool, dsl=dsl, status="queued", inputs={"q": "정답 문장"})
    await worker_factory(ScriptedLLM([]), kb=PostgresKnowledgeBases(pool))

    async def check():
        async with pool.connection() as conn:
            row = await run_db.get_run(conn, run_id)
        row = run_db.decode_run(row, load_config().secret_key)
        return row if row["status"] in ("succeeded", "failed", "cancelled", "waiting") else None

    row = await until(check)

    assert row["status"] == "succeeded"
    assert row["outputs"]["answer"] == "정답 문장"
