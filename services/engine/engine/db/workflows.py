"""Workflow drafts and immutable versions (MVP design 3.3, 8.3)."""
from __future__ import annotations

import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

WORKSPACE = "00000000-0000-0000-0000-000000000001"  # MVP runs as one workspace (design 2.2)
EMPTY_DSL: dict[str, Any] = {"version": "1", "nodes": [], "edges": []}
ACTIVE = ("queued", "running", "waiting")


async def create(conn: AsyncConnection, *, name: str) -> dict[str, Any]:
    return await (await conn.execute(
        "INSERT INTO workflows (id, workspace_id, name, draft_dsl) VALUES (%s, %s, %s, %s)"
        " RETURNING id, name, revision, updated_at",
        (str(uuid.uuid4()), WORKSPACE, name, Jsonb(EMPTY_DSL)),
    )).fetchone()


async def get(conn: AsyncConnection, workflow_id: str) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM workflows WHERE id=%s", (workflow_id,))).fetchone()


async def lock(conn: AsyncConnection, workflow_id: str) -> dict[str, Any] | None:
    """Row-lock the workflow for the rest of the caller's transaction. Two callers: run creation (Task 13)
    uses this so a concurrent save cannot change the draft mid-transaction, and delete_workflow uses it so
    a run insert racing the delete cannot slip past `has_active_runs` -- Postgres takes a FOR KEY SHARE
    lock on this row for any INSERT into `runs` (its workflow_id FK), which blocks behind this FOR UPDATE
    until the delete's transaction ends; if it then committed the delete, the blocked insert re-checks the
    FK against a now-missing row and fails instead of quietly creating a run for a deleted workflow."""
    return await (await conn.execute(
        "SELECT * FROM workflows WHERE id=%s FOR UPDATE", (workflow_id,)
    )).fetchone()


async def list_all(conn: AsyncConnection, *, limit: int = 50) -> list[dict[str, Any]]:
    return await (await conn.execute(
        "SELECT id, name, revision, updated_at FROM workflows WHERE workspace_id=%s"
        " ORDER BY updated_at DESC LIMIT %s", (WORKSPACE, limit),
    )).fetchall()


async def save_draft(conn: AsyncConnection, *, workflow_id: str, draft: dict[str, Any], revision: int,
                     name: str | None) -> dict[str, Any] | None:
    """Optimistic locking: None means the caller's revision is stale (design 8.3)."""
    return await (await conn.execute(
        "UPDATE workflows SET draft_dsl=%(draft)s, name=COALESCE(%(name)s, name),"
        "   revision=revision + 1, updated_at=now()"
        " WHERE id=%(id)s AND revision=%(revision)s RETURNING revision",
        {"id": workflow_id, "draft": Jsonb(draft), "revision": revision, "name": name},
    )).fetchone()


async def has_active_runs(conn: AsyncConnection, workflow_id: str) -> bool:
    row = await (await conn.execute(
        "SELECT 1 FROM runs WHERE workflow_id=%s AND status = ANY(%s) LIMIT 1", (workflow_id, list(ACTIVE))
    )).fetchone()
    return row is not None


async def delete(conn: AsyncConnection, workflow_id: str) -> bool:
    row = await (await conn.execute(
        "DELETE FROM workflows WHERE id=%s RETURNING id", (workflow_id,)
    )).fetchone()
    return row is not None


async def pin_version(conn: AsyncConnection, *, workflow_id: str, dsl: dict[str, Any],
                      dsl_hash: str) -> dict[str, Any]:
    """Reuse the version with this hash, or create the next one (design 8.3 step 4)."""
    existing = await (await conn.execute(
        "SELECT id, version_no FROM workflow_versions WHERE workflow_id=%s AND dsl_hash=%s",
        (workflow_id, dsl_hash),
    )).fetchone()
    if existing is not None:
        return existing
    return await (await conn.execute(
        "INSERT INTO workflow_versions (id, workflow_id, workspace_id, version_no, dsl, dsl_hash)"
        " VALUES (%s, %s, %s,"
        "   (SELECT coalesce(max(version_no), 0) + 1 FROM workflow_versions WHERE workflow_id=%s),"
        "   %s, %s) RETURNING id, version_no",
        (str(uuid.uuid4()), workflow_id, WORKSPACE, workflow_id, Jsonb(dsl), dsl_hash),
    )).fetchone()
