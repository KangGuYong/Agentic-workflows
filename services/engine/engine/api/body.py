"""Request bodies are untrusted input: bounded in size and parsed strictly (design 8.1)."""
from __future__ import annotations

from typing import Any

from fastapi import Request

from engine.api.errors import ApiError
from engine.jsondata import parse_json


async def read_json(request: Request) -> Any:
    limit = request.app.state.config.max_body_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"요청이 너무 큽니다 (최대 {limit // 1024}KB)")
    raw = await request.body()
    if len(raw) > limit:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"요청이 너무 큽니다 (최대 {limit // 1024}KB)")
    try:
        # json.loads accepts NaN and duplicate keys; the engine's parser does not.
        return parse_json(raw.decode("utf-8"))
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
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ApiError(422, "REQUEST_ERROR", f"{name}의 형식이 올바르지 않습니다")
    return value
