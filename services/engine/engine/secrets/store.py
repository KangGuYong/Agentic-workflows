"""The secrets table and the resolver http_request uses (2b design §4).

Values are fetched and decrypted just in time, inside one node execution. Nothing here caches them: a
worker must not hold plaintext across a run.
"""
from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from engine.db.workflows import WORKSPACE
from engine.secrets.crypto import SecretCryptoError, open_secret, seal_secret


async def put_secret(conn: AsyncConnection, *, key: bytes, name: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO secrets (workspace_id, name, ciphertext) VALUES (%s, %s, %s)"
        " ON CONFLICT (workspace_id, name)"
        " DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = now()",
        (WORKSPACE, name, seal_secret(key, name, value)),
    )


async def delete_secret(conn: AsyncConnection, name: str) -> bool:
    row = await (await conn.execute(
        "DELETE FROM secrets WHERE workspace_id=%s AND name=%s RETURNING name", (WORKSPACE, name)
    )).fetchone()
    return row is not None


async def list_secrets(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Names and times only. No query anywhere returns `ciphertext` to a caller."""
    return await (await conn.execute(
        "SELECT name, created_at, updated_at FROM secrets WHERE workspace_id=%s ORDER BY name",
        (WORKSPACE,),
    )).fetchall()


async def secret_names(conn: AsyncConnection) -> set[str]:
    """Used by run creation to reject a workflow that references a secret nobody stored."""
    rows = await (await conn.execute(
        "SELECT name FROM secrets WHERE workspace_id=%s", (WORKSPACE,))).fetchall()
    return {row["name"] for row in rows}


class PostgresSecretResolver:
    """Matches engine.nodes.base.SecretResolver."""

    def __init__(self, pool: AsyncConnectionPool, key: bytes) -> None:
        self._pool = pool
        self._key = key

    async def resolve(self, names: set[str]) -> dict[str, str]:
        """Decrypted values for the names that exist. A missing name is simply absent from the result —
        the caller decides what that means (the node fails the attempt with SECRET_NOT_FOUND)."""
        if not names:
            # Most requests carry no secret at all; taking a pooled connection for them would make the
            # pool the ceiling on concurrent http_request nodes for no reason.
            return {}
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT name, ciphertext FROM secrets WHERE workspace_id=%s AND name = ANY(%s)",
                (WORKSPACE, sorted(names)),
            )).fetchall()
        resolved: dict[str, str] = {}
        for row in rows:
            try:
                resolved[row["name"]] = open_secret(self._key, row["name"], bytes(row["ciphertext"]))
            except SecretCryptoError:
                # A row sealed with a key that has since been rotated reads as missing, so the node fails
                # the tenant's attempt instead of raising an infrastructure error at the worker.
                continue
        return resolved
