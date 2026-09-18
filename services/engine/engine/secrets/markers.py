"""Secret markers (2b design §4.3).

The renderer binds `secret` to a mapping that answers with a marker instead of a value, so a secret never
enters the render subprocess, the run state, the checkpoint or node_runs. `http_request.execute()` swaps
markers for real values immediately before sending, and scrubs the values back out of what it returns.

The nonce is derived from the run id and ENGINE_SECRET_KEY rather than drawn at random: a replayed render
after a worker restart must produce exactly what the first one produced, or the recorded input and the
checkpoint disagree. It also means tenant text cannot forge a marker.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

# The same name rule the API enforces (engine/api/routers/secrets.py): a name that cannot be stored must
# not render into a marker either.
NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MARKER = re.compile(r"\[\[secret:([A-Z][A-Z0-9_]{0,63}):([0-9a-f]{16})\]\]")
REDACTED = "[REDACTED]"


def nonce_for(run_id: str, key: bytes) -> str:
    return hmac.new(key, run_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def marker_for(name: str, nonce: str) -> str:
    return f"[[secret:{name}:{nonce}]]"


class SecretMarkers(dict):
    """Bound as `secret` in the render context. The template environment reads mappings by key, so
    `{{ secret.API_TOKEN }}` lands in __getitem__; an invalid name raises KeyError, which the environment
    turns into an undefined value and therefore a template error."""

    def __init__(self, nonce: str) -> None:
        super().__init__()
        self._nonce = nonce

    def __getitem__(self, name: str) -> str:
        if not isinstance(name, str) or not NAME.match(name):
            raise KeyError(name)
        return marker_for(name, self._nonce)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and bool(NAME.match(name))


def find_names(text: str, nonce: str) -> set[str]:
    return {found.group(1) for found in MARKER.finditer(text) if found.group(2) == nonce}


def substitute(text: str, values: dict[str, str], nonce: str) -> str:
    """Markers of this run whose name resolved become the value; anything else is left untouched.

    `re.sub` with a function makes this one pass over the text, so a value that itself contains a marker
    is inserted literally rather than expanded — one secret cannot pull in another.
    """

    def replace(found: re.Match[str]) -> str:
        if found.group(2) != nonce:
            return found.group(0)
        return values.get(found.group(1), found.group(0))

    return MARKER.sub(replace, text)


def redact_values(value: Any, secrets: list[str]) -> Any:
    """Replace every secret value wherever it appears in a JSON value.

    Longest first: a short secret that is a prefix of a longer one would otherwise cut the longer one in
    half and leave the rest of it in the output.
    """
    ordered = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    return _walk(value, ordered) if ordered else value


def _walk(value: Any, ordered: list[str]) -> Any:
    if isinstance(value, str):
        for secret in ordered:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {_walk(key, ordered): _walk(item, ordered) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk(item, ordered) for item in value]
    return value
