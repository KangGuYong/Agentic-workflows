"""What may be written to the observation tables (MVP design 10.1, 10.3).

Two of the three layers live here: key-name redaction (`redact`) and header-name redaction
(`redact_headers`, 2b design §6 layer 1), both applied whether or not a workflow used a secret. The third,
value-based redaction, cannot live here — it needs the resolved values, which exist only inside one
`http_request.execute()` call, so it sits at that boundary (`engine.secrets.markers.redact_values`).
"""
from __future__ import annotations

import json
import re
from typing import Any

from engine.jsondata import clip

REDACTED = "[REDACTED]"
SECRET_KEY = re.compile(r"(authorization|password|passwd|secret|token|api[_-]?key|cookie)", re.IGNORECASE)
MAX_STORED_BYTES = 256_000  # node_runs.input / node_runs.output
MAX_EVENT_PREVIEW_BYTES = 4_000  # run_events.payload previews
# Exact names, not substrings: `x-api-key-hint` is a different header, and hiding ordinary fields from
# whoever is debugging a run has its own cost. Every name a tenant can set on an outgoing request, plus
# the two a server can send back.
SECRET_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
})


def redact(value: Any) -> Any:
    """A copy of `value` with every value under a secret-looking key replaced.

    Values reaching here are JSON data bounded by engine.jsondata.MAX_JSON_DEPTH, so plain recursion is safe.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if isinstance(key, str) and SECRET_KEY.search(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def clip_json(value: Any, limit: int) -> tuple[Any, bool]:
    """(stored value, truncated). Over the limit the shape changes to {"_truncated": "<start of the JSON>"},
    because a cut-off JSON value is not valid JSON and the editor must be able to tell."""
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(text.encode("utf-8")) <= limit:
        return value, False
    return {"_truncated": clip(text, limit // 8)}, True


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Header values that carry credentials, by name (MVP design 10.3). Applied to every stored request
    and response header set, whether or not the workflow used a secret.

    Names come back lowercased, which also collapses `Authorization` and `authorization` into one entry —
    two spellings of one header, and both are redacted either way.
    """
    return {name.lower(): (REDACTED if name.lower() in SECRET_HEADERS else value)
            for name, value in headers.items()}
