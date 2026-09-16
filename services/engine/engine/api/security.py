"""Shared-token access control. MVP has no accounts (design 8.1); this keeps an exposed port from being open."""
from __future__ import annotations

import hmac

from fastapi import Request

from engine.api.errors import ApiError


def require_token(request: Request) -> None:
    expected = request.app.state.config.api_token
    if not expected:
        return  # development: no token configured
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(value, expected):
        raise ApiError(401, "UNAUTHORIZED", "인증 토큰이 필요합니다")
