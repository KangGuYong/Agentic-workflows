"""The http_request node (MVP design 4.7, 2b design §3).

This is the only place a secret value exists in plaintext, so it is also where value-based redaction
happens (2b design §6): markers become values immediately before sending, and the values are scrubbed out
of everything the node returns — including the message of any error it raises. Nothing downstream, and
nothing stored, ever sees them.

The order inside `execute` is the security property: resolve → substitute → send → redact. Substituting
before the client is called is what puts the real header value in front of the client's RFC 7230 checks,
so a secret carrying CRLF is refused as a blocked request rather than smuggled onto the wire.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.dsl.models import Policy, RetrySpec
from engine.errors import EngineFault, ErrorCode, NodeError
from engine.events.redact import redact_headers
from engine.http.client import EgressBlocked, ResponseTooLarge, TransportFailed, UnsupportedMedia
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField, Usage
from engine.secrets.markers import find_names, redact_values, substitute

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
IDEMPOTENT = {"GET", "PUT", "DELETE", "HEAD"}
# A header name is an RFC 7230 token, further narrowed: no dot, because `template_fields` addresses each
# header as `headers.{name}` and a dot in the name would collide with that path.
HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
FRAMING_HEADERS = frozenset({"host", "content-length"})
MAX_HEADERS = 20
TIMEOUT_SEC = 30
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "integer"},
        "headers": {"type": "object"},
        "body": {},
    },
    "required": ["status", "headers", "body"],
}


class HttpRequestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal[METHODS] = "GET"  # type: ignore[valid-type]
    url: str = Field(min_length=1, max_length=4096, json_schema_extra=TEMPLATE)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(None, json_schema_extra=TEMPLATE)
    bodyFormat: Literal["json", "text"] = "json"
    sendIdempotencyKey: bool = False

    @field_validator("headers")
    @classmethod
    def _header_names(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_HEADERS:
            raise ValueError(f"헤더는 최대 {MAX_HEADERS}개입니다")
        for name in value:
            if not HEADER_NAME.match(name):
                raise ValueError(f"헤더 이름 형식이 올바르지 않습니다: {name}")
        # Host is set from the URL by the client (2b design §5.4); letting a template set it would be a
        # way to point the request at one host while presenting another. Content-Length is how a body
        # gets desynced from the framing httpx computes for it.
        if any(name.lower() in FRAMING_HEADERS for name in value):
            raise ValueError("Host와 Content-Length 헤더는 지정할 수 없습니다")
        return value


class HttpRequestNode(NodeSpec):
    type = "http_request"
    label = "HTTP 요청"
    category = "Action"
    Config = HttpRequestConfig
    default_policy = Policy(timeoutSec=TIMEOUT_SEC, retry=RetrySpec(maxAttempts=3))
    side_effects = True

    def policy_for(self, config: HttpRequestConfig) -> Policy:
        """POST and PATCH are not idempotent, so a retry can double a payment (MVP design 5.4)."""
        if config.method in IDEMPOTENT:
            return self.default_policy
        return Policy(timeoutSec=TIMEOUT_SEC, retry=RetrySpec(maxAttempts=1))

    def template_fields(self, config: HttpRequestConfig) -> list[TemplateField]:
        fields = [TemplateField("url", config.url, "string")]
        fields += [TemplateField(f"headers.{name}", value, "string")
                   for name, value in config.headers.items()]
        if config.body is not None:
            fields.append(TemplateField("body", config.body,
                                        "json" if config.bodyFormat == "json" else "string"))
        return fields

    def output_schema(self, config: HttpRequestConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return OUTPUT_SCHEMA

    async def execute(self, ctx: NodeContext, config: HttpRequestConfig,
                      rendered: dict[str, Any]) -> NodeResult:
        if ctx.http is None:
            raise EngineFault("http_request needs an HTTP client")
        url = rendered["url"]
        headers = {name: rendered[f"headers.{name}"] for name in config.headers}
        body = _body(config, rendered)
        values = await self._secrets(ctx, url, headers, body)
        if values:
            nonce = ctx.secret_nonce or ""
            url = substitute(url, values, nonce)
            headers = {name: substitute(value, values, nonce) for name, value in headers.items()}
            body = substitute(body, values, nonce) if body is not None else None
        used = list(values.values())
        if config.sendIdempotencyKey:
            headers["Idempotency-Key"] = _idempotency_key(ctx)
        response = await self._send(ctx, config, url, headers, body, used)
        output = redact_values(
            {"status": response.status, "headers": redact_headers(response.headers),
             "body": response.body},
            used,
        )
        if not 200 <= response.status < 300:
            # Only the status: the response body is the tenant's, but it is also where a server most
            # often quotes back the credential it just rejected.
            raise NodeError(ErrorCode.HTTP_ERROR, f"HTTP {response.status} 응답을 받았습니다",
                            retryable=_retryable(response.status))
        return NodeResult(output, Usage())

    async def _secrets(self, ctx: NodeContext, url: str, headers: dict[str, str],
                       body: str | None) -> dict[str, str]:
        nonce = ctx.secret_nonce
        if not nonce:
            return {}
        names: set[str] = set()
        for text in (url, body, *headers.values()):
            if text is not None:
                names |= find_names(text, nonce)
        if not names:
            return {}
        if ctx.secrets is None:
            raise EngineFault("http_request needs a secret resolver")
        values = await ctx.secrets.resolve(names)
        missing = sorted(names - set(values))
        if missing:
            # Before sending, deliberately: an unresolved marker left in the request would put the
            # workflow's secret names on the wire.
            raise NodeError(ErrorCode.SECRET_NOT_FOUND,
                            f"시크릿을 찾을 수 없습니다: {', '.join(missing)}", retryable=False)
        return values

    async def _send(self, ctx: NodeContext, config: HttpRequestConfig, url: str,
                    headers: dict[str, str], body: str | None, used: list[str]):
        try:
            return await ctx.http.request(method=config.method, url=url, headers=headers, body=body,
                                          timeout_sec=ctx.timeout_sec or TIMEOUT_SEC)
        except EgressBlocked as exc:
            # Only the category: the host and address are what an attacker is probing for.
            raise NodeError(ErrorCode.HTTP_BLOCKED, f"차단된 요청입니다 ({exc.category})",
                            retryable=False) from None
        except ResponseTooLarge as exc:
            raise NodeError(ErrorCode.HTTP_RESPONSE_TOO_LARGE,
                            f"응답이 너무 큽니다 (최대 {exc} bytes)", retryable=False) from None
        except UnsupportedMedia as exc:
            raise NodeError(ErrorCode.HTTP_UNSUPPORTED_MEDIA_TYPE,
                            f"처리할 수 없는 응답 형식입니다: {redact_values(str(exc), used)}",
                            retryable=False) from None
        except TransportFailed as exc:
            # httpx puts the whole URL in its message, query string and all.
            raise NodeError(ErrorCode.HTTP_ERROR,
                            f"요청에 실패했습니다: {redact_values(str(exc), used)}",
                            retryable=True) from None


def _body(config: HttpRequestConfig, rendered: dict[str, Any]) -> str | None:
    if config.body is None:
        return None
    value = rendered["body"]
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _idempotency_key(ctx: NodeContext) -> str:
    """Stable for one execution point, across every attempt of it — that is what lets the far side
    deduplicate a retry (MVP design 5.4). `attempt` is deliberately not part of it."""
    seed = f"{ctx.run_id}:{ctx.node_id}:{ctx.exec_index}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status < 600
