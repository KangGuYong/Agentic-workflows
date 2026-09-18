"""The two encrypted run payload columns (2b design §9).

Same AES-GCM construction as engine/secrets/crypto.py, with the column name as AAD so an `inputs` value
cannot be moved into `outputs`. With no key configured (ENGINE_DEV_INSECURE=1) the value is stored as
plain UTF-8 JSON, which is what a development database already was.
"""
from __future__ import annotations

import json
from typing import Any

from engine.secrets.crypto import SecretCryptoError, open_secret, seal_secret


def seal_payload(key: bytes | None, column: str, value: Any) -> bytes | None:
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if key is None:
        return text.encode("utf-8")
    return seal_secret(key, column, text)


def open_payload(key: bytes | None, column: str, stored: bytes | memoryview | None) -> Any:
    if stored is None:
        return None
    raw = bytes(stored)
    if key is None:
        return json.loads(raw.decode("utf-8"))
    try:
        return json.loads(open_secret(key, column, raw))
    except (SecretCryptoError, ValueError):
        # A row written under a different key -- or before a key was configured at all -- is unreadable,
        # not a reason to fail the request: the run's metadata still describes what happened.
        return None
