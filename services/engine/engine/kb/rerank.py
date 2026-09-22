"""text-embeddings-inference `/rerank` client (knowledge-base design §2, §5.2). The model is whatever
the TEI server was started with; the node does not choose one."""
from __future__ import annotations

import httpx

from engine.errors import ErrorCode, NodeError

CONNECT_TIMEOUT = 10.0


class TeiReranker:
    def __init__(self, base_url: str, *, timeout: float = 60.0, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def rerank(self, query: str, texts: list[str]) -> list[tuple[int, float]]:
        try:
            # truncate: True - the node's job is ordering, not summarising; a prefix-scored long text
            # beats a 413 that fails the run.
            response = await self._client.post(f"{self._base}/rerank",
                                                json={"query": query, "texts": texts, "truncate": True})
        except httpx.RequestError as exc:
            raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"리랭커 연결 실패: {exc}", retryable=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"리랭커 오류 HTTP {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise NodeError(ErrorCode.NODE_FAILED, f"리랭커 요청 오류 HTTP {response.status_code}: {response.text[:200]}",
                            retryable=False)
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, list) or not all(
            isinstance(item, dict) and isinstance(item.get("index"), int) and not isinstance(item.get("index"), bool)
            and isinstance(item.get("score"), (int, float)) and 0 <= item["index"] < len(texts)
            for item in data
        ) or len(data) != len(texts) or len({item["index"] for item in data}) != len(data):
            raise NodeError(ErrorCode.LLM_UNAVAILABLE, "리랭커 응답 형식이 올바르지 않습니다", retryable=True)
        pairs = [(item["index"], float(item["score"])) for item in data]
        # TEI happening to answer best-first is not a contract the port can lean on; the port promises
        # best-first, so sort here.
        pairs.sort(key=lambda pair: pair[1], reverse=True)
        return pairs
