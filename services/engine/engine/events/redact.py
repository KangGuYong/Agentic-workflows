"""What may be written to the observation tables (MVP design 10.1, 10.3).

Plan 2a does key-name redaction, which is all that can be done before secrets and `http_request` exist
(Plan 2b adds header-name and secret-value redaction).
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
    text = json.dumps(value, ensure_ascii=False)
    if len(text.encode("utf-8")) <= limit:
        return value, False
    return {"_truncated": clip(text, limit // 8)}, True
