"""Request bodies are untrusted input: bounded in size and parsed strictly (design 8.1)."""
from __future__ import annotations

from typing import Any

from fastapi import Request

from engine.api.errors import ApiError
from engine.jsondata import check_storable, parse_json


def _too_large(limit: int) -> ApiError:
    # Below 1024 bytes "limit // 1024" rounds down to 0KB for every limit, which is nonsensical (and
    # is exactly the size tests use, since a tiny limit keeps them fast) -- show bytes in that case.
    text = f"{limit}B" if limit < 1024 else f"{limit // 1024}KB"
    return ApiError(413, "PAYLOAD_TOO_LARGE", f"요청이 너무 큽니다 (최대 {text})")


async def read_json(request: Request) -> Any:
    limit = request.app.state.config.max_body_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    # A client can omit Content-Length entirely (chunked transfer encoding) or under-report it, so the
    # only bound that actually holds is on the bytes as they arrive: read the body incrementally and
    # stop the instant the running total goes over the limit, instead of buffering it all first and
    # only then checking -- an unbounded `await request.body()` lets a chunked request of any size
    # through to that point.
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _too_large(limit)
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        # json.loads accepts NaN and duplicate keys; the engine's parser does not.
        value = parse_json(raw.decode("utf-8"))
        # parse_json only rejects NUL/lone surrogates (max_depth=None); bound nesting too, so a
        # pathologically deep body is a 400 here rather than a bare ValueError out of some route's
        # own check_storable() call later.
        check_storable(value)
        return value
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(400, "INVALID_JSON", f"JSON을 읽을 수 없습니다: {exc}") from None


def require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(400, "INVALID_JSON", "요청 본문은 객체여야 합니다")
    return value


def field(body: dict[str, Any], name: str, kind: type, *, required: bool = True, default: Any = None) -> Any:
    if name not in body:
        if required:
            raise ApiError(422, "REQUEST_ERROR", f"{name}이(가) 필요합니다")
        return default
    value = body[name]
    if value is None and not required:  # explicit null on an optional field means "absent", not "wrong type"
        return default
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)  # a JSON number like `3` is a valid float value, just written without a fraction
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ApiError(422, "REQUEST_ERROR", f"{name}의 형식이 올바르지 않습니다")
    return value
