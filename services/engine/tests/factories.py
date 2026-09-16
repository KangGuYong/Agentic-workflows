"""Row builders for tests that need a run to hang observations off."""
from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb

from engine.db.workflows import WORKSPACE


async def make_run(pool, *, dsl: dict[str, Any] | None = None, status: str = "running",
                   store_run_data: bool = True, inputs: dict[str, Any] | None = None) -> str:
    workflow_id, version_id, run_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    dsl = dsl if dsl is not None else {"version": "1", "nodes": [], "edges": []}
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO workflows (id, workspace_id, name, draft_dsl) VALUES (%s, %s, %s, %s)",
            (workflow_id, WORKSPACE, "w", Jsonb(dsl)),
        )
        await conn.execute(
            "INSERT INTO workflow_versions (id, workflow_id, workspace_id, version_no, dsl, dsl_hash)"
            " VALUES (%s, %s, %s, 1, %s, %s)",
            (version_id, workflow_id, WORKSPACE, Jsonb(dsl), f"hash-{version_id}"),
        )
        await conn.execute(
            "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status, inputs,"
            " store_run_data) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (run_id, WORKSPACE, workflow_id, version_id, status, Jsonb(inputs or {}), store_run_data),
        )
    return run_id
