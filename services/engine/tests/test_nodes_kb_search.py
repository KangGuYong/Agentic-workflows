"""The kb_search node (knowledge-base design §5.1): embed the question, search, return text only."""
import pytest

from engine.errors import ErrorCode, NodeError
from engine.kb import store
from engine.kb.store import PostgresKnowledgeBases
from engine.llm.scripted import ScriptedLLM
from engine.nodes.kb_search import KbSearchNode
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
