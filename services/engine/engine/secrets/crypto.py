"""Secret values at rest (2b design §4.1).

AES-GCM under ENGINE_SECRET_KEY, deliberately a different key from LANGGRAPH_AES_KEY: if one leaks the
other still holds. The secret's name is the AAD, so a ciphertext copied onto another row fails to open
instead of silently becoming that other secret.
"""
from __future__ import annotations

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

NONCE_BYTES = 12
TAG_BYTES = 16


class SecretCryptoError(Exception):
    """The ciphertext does not open: wrong key, wrong name, or tampering."""


def seal_secret(key: bytes, name: str, value: str) -> bytes:
    cipher = AES.new(key, AES.MODE_GCM, nonce=get_random_bytes(NONCE_BYTES))
    cipher.update(name.encode("utf-8"))
    body, tag = cipher.encrypt_and_digest(value.encode("utf-8"))
    return cipher.nonce + tag + body


def open_secret(key: bytes, name: str, sealed: bytes) -> str:
    if len(sealed) < NONCE_BYTES + TAG_BYTES:
        # Checked here so a truncated row raises the same error as a wrong key, rather than reaching the
        # cipher with a short nonce and failing as something else.
        raise SecretCryptoError("secret could not be decrypted")
    nonce = sealed[:NONCE_BYTES]
    tag = sealed[NONCE_BYTES:NONCE_BYTES + TAG_BYTES]
    body = sealed[NONCE_BYTES + TAG_BYTES:]
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        cipher.update(name.encode("utf-8"))
        return cipher.decrypt_and_verify(body, tag).decode("utf-8")
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        # Deliberately no detail: which of the three went wrong is information an attacker would use.
        raise SecretCryptoError("secret could not be decrypted") from exc
