import httpx
import pytest

from engine.kb import IngestError
from engine.kb.mineru import MineruParser


def _parser(handler) -> MineruParser:
    return MineruParser("http://mineru:8000/", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_sends_the_file_as_multipart_and_returns_markdown():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, json={"results": {"report": {"md_content": "# 보고서\n\n본문"}}})

    markdown = await _parser(handler).to_markdown("report.pdf", "application/pdf", b"%PDF-1.4")

    assert seen["url"] == "http://mineru:8000/file_parse"
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'filename="report.pdf"' in seen["body"] and b"%PDF-1.4" in seen["body"]
    assert b'name="files"' in seen["body"]
    assert markdown == "# 보고서\n\n본문"


async def test_a_4xx_is_not_retryable_and_names_the_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="unsupported")

    with pytest.raises(IngestError) as caught:
        await _parser(handler).to_markdown("a.xyz", "application/octet-stream", b"?")
    assert not caught.value.retryable and "422" in caught.value.message


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down")


@pytest.mark.parametrize("handler", [lambda r: httpx.Response(503), _down])
async def test_5xx_and_transport_failures_are_retryable(handler):
    with pytest.raises(IngestError) as caught:
        await _parser(handler).to_markdown("a.pdf", "application/pdf", b"x")
    assert caught.value.retryable


async def test_a_response_without_markdown_is_not_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {}})

    with pytest.raises(IngestError) as caught:
        await _parser(handler).to_markdown("a.pdf", "application/pdf", b"x")
    assert not caught.value.retryable


async def test_a_non_json_response_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    with pytest.raises(IngestError) as caught:
        await _parser(handler).to_markdown("a.pdf", "application/pdf", b"x")
    assert caught.value.retryable
