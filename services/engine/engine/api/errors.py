"""One error shape for the whole API (MVP design 8.2)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


def api_error_response(exc: ApiError) -> JSONResponse:
    """Build the JSON response for an ApiError. Shared by the exception handler below and by
    TokenAuthMiddleware, which cannot raise into `app.exception_handler` (middleware runs outside it)
    and must build the response itself."""
    return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return api_error_response(exc)

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "REQUEST_ERROR"
        # e.g. a 405 carries an Allow header -- dropping exc.headers here would silently discard it.
        return JSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(_body("REQUEST_ERROR", "요청 형식이 올바르지 않습니다"), status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", type(exc).__name__)
        # Never leak internals to a tenant: the details are in the service log.
        return JSONResponse(_body("INTERNAL", "서버 오류가 발생했습니다"), status_code=500)
