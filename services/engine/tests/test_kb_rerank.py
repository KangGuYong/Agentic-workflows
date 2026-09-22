import json

import httpx
import pytest

from engine.errors import ErrorCode, NodeError
from engine.kb.rerank import TeiReranker


def _reranker(handler) -> TeiReranker:
    return TeiReranker("http://tei:8080/", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_posts_query_and_texts_and_returns_index_score_pairs():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json=[{"index": 1, "score": 0.9}, {"index": 0, "score": 0.2}])

    pairs = await _reranker(handler).rerank("질문", ["a", "b"])

    assert seen["url"] == "http://tei:8080/rerank"
    assert json.loads(seen["body"]) == {"query": "질문", "texts": ["a", "b"]}
    assert pairs == [(1, 0.9), (0, 0.2)]


async def test_a_malformed_response_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": 1})

    with pytest.raises(NodeError) as caught:
        await _reranker(handler).rerank("q", ["a"])
    assert caught.value.code == ErrorCode.LLM_UNAVAILABLE and caught.value.retryable


@pytest.mark.parametrize("status,retryable", [(503, True), (422, False)])
async def test_status_codes_decide_retryability(status, retryable):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="x")

    with pytest.raises(NodeError) as caught:
        await _reranker(handler).rerank("q", ["a"])
    assert caught.value.retryable is retryable
