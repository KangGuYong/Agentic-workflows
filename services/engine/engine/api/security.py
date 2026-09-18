"""Shared-token access control. MVP has no accounts (design 8.1); this keeps an exposed port from being open.

Enforced by TokenAuthMiddleware below -- a plain ASGI middleware, not `@app.middleware("http")`
(Starlette's BaseHTTPMiddleware), which relays the response through an in-memory stream and is known to
break long-lived/streaming responses. Task 14 puts an SSE stream behind this same path, so the middleware
here just forwards `send` straight through on the success path and never touches the body.

It is also the *only* place the token is checked: FastAPI's app-level `dependencies=[...]` is attached by
`add_api_route`, but FastAPI registers its own docs routes (`/openapi.json`, `/docs`, `/redoc`,
`/docs/oauth2-redirect`) with Starlette's `add_route`, which never sees that list -- so an app-level
dependency alone leaves those routes open. Plain ASGI middleware sits in front of routing entirely and
covers every path unconditionally, docs included, so there is no separate app-level
`Depends(require_token)` as well -- that would just re-run the same check a second time for routes that
already go through the middleware, for no extra coverage.
"""
from __future__ import annotations

import hmac

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from engine.api.errors import ApiError, api_error_response


async def require_token(request: Request) -> None:
    expected = request.app.state.config.api_token
    if not expected:
        return  # development: no token configured (design 8.1); create_app logs a warning about this
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    # h11 permits obs-text (non-ASCII) bytes in a header value, and Starlette decodes header values as
    # latin-1 rather than rejecting them, so `value` can be an arbitrary non-ASCII str here. Comparing
    # two `str` with hmac.compare_digest raises TypeError unless both are ASCII-only; comparing bytes
    # has no such restriction, so encode both sides first.
    if scheme.lower() != "bearer" or not hmac.compare_digest(value.encode(), expected.encode()):
        raise ApiError(401, "UNAUTHORIZED", "인증 토큰이 필요합니다")


class TokenAuthMiddleware:
    """Rejects any HTTP request that fails `require_token` before it reaches routing, for every path --
    including FastAPI's own docs routes and anything mounted later. On success it awaits `self.app`
    directly, so it never buffers, decodes, or delays a streaming response body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan/websocket scopes: nothing to authenticate here
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        try:
            await require_token(request)
        except ApiError as exc:
            # An exception raised from middleware does not reach `app.exception_handler` (that only
            # wraps routing, which is inside this middleware), so build and send the response directly.
            response = api_error_response(exc)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
