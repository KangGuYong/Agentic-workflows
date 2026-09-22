import pytest

from engine.errors import ErrorCode, NodeError
from engine.nodes.rerank import RerankNode
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


async def test_a_missing_reranker_is_a_configuration_error():
    with pytest.raises(NodeError) as caught:
        await RerankNode().execute(make_ctx(rerank=None), _config(), {"query": "q", "hits": HITS})
    assert not caught.value.retryable and "리랭커가 설정되지 않았습니다" in caught.value.message


def test_the_hits_field_is_an_array_template():
    fields = RerankNode().template_fields(_config())
    assert [(f.path, f.target) for f in fields] == [("query", "string"), ("hits", "array")]
