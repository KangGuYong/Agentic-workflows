"""MinerU API client (knowledge-base design §2): a document in, markdown out.

The server address is an operator setting, not tenant data, so it is outside the http_request egress
policy on purpose -- the same standing as OLLAMA_BASE_URL.
"""
from __future__ import annotations

from typing import Any

import httpx

from engine.kb import IngestError

PARSE_PATH = "/file_parse"
CONNECT_TIMEOUT = 10.0


class MineruParser:
    def __init__(self, base_url: str, *, timeout: float = 600.0, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def to_markdown(self, filename: str, media_type: str, content: bytes) -> str:
        try:
            response = await self._client.post(
                f"{self._base}{PARSE_PATH}", files={"files": (filename, content, media_type)}
            )
        except httpx.RequestError as exc:
            raise IngestError(f"MinerU 연결 실패: {exc}", retryable=True) from exc
        if response.status_code >= 500:
            raise IngestError(f"MinerU 오류 HTTP {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise IngestError(f"문서를 읽지 못했습니다 (MinerU {response.status_code})", retryable=False)
        try:
            data = response.json()
        except ValueError:
            raise IngestError("MinerU 응답을 해석할 수 없습니다", retryable=True) from None
        markdown = _markdown_of(data)
        if not markdown or not markdown.strip():
            raise IngestError("문서에서 텍스트를 찾지 못했습니다", retryable=False)
        return markdown


def _markdown_of(data: Any) -> str | None:
    """mineru-api answers {"results": {"<stem>": {"md_content": ...}}}; one file in, one result out."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, dict) or not results:
        return None
    first = next(iter(results.values()))
    text = first.get("md_content") if isinstance(first, dict) else None
    return text if isinstance(text, str) else None
