import pytest

from engine.config import load_config
from engine.db import runs as run_db
from engine.errors import ErrorCode, NodeError
from engine.kb import store
from engine.kb.store import PostgresKnowledgeBases
from engine.llm.scripted import ScriptedLLM
from engine.nodes.rerank import RerankNode
from tests.conftest import until
from tests.factories import make_run
from tests.helpers import make_ctx

HITS = [
    {"text": "하나", "score": 0.7, "heading": None, "file": "a.md"},
    {"text": "둘", "score": 0.6, "heading": "h", "file": "a.md"},
    {"text": "셋", "score": 0.5, "heading": None, "file": "b.md"},
]


class FakeReranker:
    def __init__(self, pairs) -> None:
        self.pairs = pairs
        self.calls = []

    async def rerank(self, query, texts):
        self.calls.append((query, list(texts)))
        return self.pairs


def _config(**overrides):
    return RerankNode().parse_config({"query": "{{ start.q }}", "hits": "{{ kb_search_1.hits }}", **overrides})


async def test_reorders_by_rerank_score_and_keeps_top_n():
    reranker = FakeReranker([(2, 0.95), (0, 0.4), (1, 0.1)])
    result = await RerankNode().execute(make_ctx(rerank=reranker), _config(topN=2), {"query": "질문", "hits": HITS})

    assert reranker.calls == [("질문", ["하나", "둘", "셋"])]
    assert [(h["text"], h["score"]) for h in result.output["hits"]] == [("셋", 0.95), ("하나", 0.4)]
    assert result.output["hits"][0]["file"] == "b.md"
    assert result.output["context"] == "셋\n\n---\n\n하나"


async def test_min_score_filters_after_reranking():
    reranker = FakeReranker([(0, 0.9), (1, 0.2)])
    result = await RerankNode().execute(make_ctx(rerank=reranker), _config(minScore=0.5), {"query": "q", "hits": HITS[:2]})
    assert [h["text"] for h in result.output["hits"]] == ["하나"]


async def test_empty_hits_skip_the_reranker():
    reranker = FakeReranker([])
    result = await RerankNode().execute(make_ctx(rerank=reranker), _config(), {"query": "q", "hits": []})
    assert result.output == {"hits": [], "context": ""} and reranker.calls == []


@pytest.mark.parametrize("hits", ["문자열", [{"nope": 1}], [{"text": 3}], [{"text": "x"}] * 101])
async def test_malformed_hits_are_a_template_error(hits):
    with pytest.raises(NodeError) as caught:
        await RerankNode().execute(make_ctx(rerank=FakeReranker([])), _config(), {"query": "q", "hits": hits})
    assert caught.value.code == ErrorCode.TEMPLATE_ERROR and not caught.value.retryable
    assert "{{ 지식검색노드.hits }}" in caught.value.message


async def test_a_missing_reranker_is_a_configuration_error():
    with pytest.raises(NodeError) as caught:
        await RerankNode().execute(make_ctx(rerank=None), _config(), {"query": "q", "hits": HITS})
    assert not caught.value.retryable and "리랭커가 설정되지 않았습니다" in caught.value.message


def test_the_hits_field_is_an_array_template():
    fields = RerankNode().template_fields(_config())
    assert [(f.path, f.target) for f in fields] == [("query", "string"), ("hits", "array")]


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


async def test_the_worker_wires_the_rerank_port_through_to_the_node(pool, worker_factory):
    kb_id = await _seed(pool, ["정답 문장", "다른 문장"])
    dsl = {
        "version": "1",
        "nodes": [
            {"id": "start", "type": "start", "position": {"x": 0, "y": 0},
             "config": {"inputs": {"type": "object", "properties": {"q": {"type": "string"}},
                                   "required": ["q"]}}},
            {"id": "kb_search_1", "type": "kb_search", "position": {"x": 1, "y": 0},
             "config": {"knowledgeBase": kb_id, "query": "{{ start.q }}", "topK": 2}},
            {"id": "rerank_1", "type": "rerank", "position": {"x": 2, "y": 0},
             "config": {"query": "{{ start.q }}", "hits": "{{ kb_search_1.hits }}", "topN": 1}},
            {"id": "end", "type": "end", "position": {"x": 3, "y": 0},
             "config": {"outputs": {"answer": "{{ rerank_1.context }}"}}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "kb_search_1"},
            {"id": "e2", "source": "kb_search_1", "target": "rerank_1"},
            {"id": "e3", "source": "rerank_1", "target": "end"},
        ],
    }
    run_id = await make_run(pool, dsl=dsl, status="queued", inputs={"q": "정답 문장"})
    # kb_search would put "정답 문장" first (it is the exact query); the reranker flips that order, so
    # a final answer of "다른 문장" proves the rerank port produced it, not kb_search.
    await worker_factory(ScriptedLLM([]), kb=PostgresKnowledgeBases(pool), rerank=FakeReranker([(1, 0.9), (0, 0.1)]))

    async def check():
        async with pool.connection() as conn:
            row = await run_db.get_run(conn, run_id)
        row = run_db.decode_run(row, load_config().secret_key)
        return row if row["status"] in ("succeeded", "failed", "cancelled", "waiting") else None

    row = await until(check)

    assert row["status"] == "succeeded"
    assert row["outputs"]["answer"] == "다른 문장"
