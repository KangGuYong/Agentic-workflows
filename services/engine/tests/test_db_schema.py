import uuid

import pytest
from psycopg.errors import CheckViolation, UniqueViolation
from psycopg.types.json import Jsonb


async def _workflow(pool) -> str:
    workflow_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO workflows (id, workspace_id, name, draft_dsl) VALUES (%s, %s, %s, %s)",
            (workflow_id, "00000000-0000-0000-0000-000000000001", "w", Jsonb({"nodes": [], "edges": []})),
        )
    return workflow_id


async def test_tables_exist_with_the_checkpoint_tables(pool):
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        )).fetchall()

    names = {row["table_name"] for row in rows}
    assert {"workflows", "workflow_versions", "runs", "node_runs", "run_events"} <= names
    assert {"checkpoints", "checkpoint_blobs", "checkpoint_writes"} <= names


async def test_one_version_per_dsl_hash(pool):
    workflow_id = await _workflow(pool)
    async with pool.connection() as conn:
        for version_no in (1, 2):
            statement = ("INSERT INTO workflow_versions (id, workflow_id, workspace_id, version_no, dsl, dsl_hash)"
                         " VALUES (%s, %s, %s, %s, %s, %s)")
            args = (str(uuid.uuid4()), workflow_id, "00000000-0000-0000-0000-000000000001",
                    version_no, Jsonb({}), "same-hash")
            if version_no == 1:
                await conn.execute(statement, args)
            else:
                with pytest.raises(UniqueViolation):
                    await conn.execute(statement, args)


async def test_run_status_is_constrained(pool):
    workflow_id = await _workflow(pool)
    async with pool.connection() as conn:
        version_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO workflow_versions (id, workflow_id, workspace_id, version_no, dsl, dsl_hash)"
            " VALUES (%s, %s, %s, 1, %s, %s)",
            (version_id, workflow_id, "00000000-0000-0000-0000-000000000001", Jsonb({}), "h"),
        )
        with pytest.raises(CheckViolation):
            await conn.execute(
                "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status)"
                " VALUES (%s, %s, %s, %s, 'sleeping')",
                (str(uuid.uuid4()), "00000000-0000-0000-0000-000000000001", workflow_id, version_id),
            )


async def test_one_row_per_attempt(pool):
    workflow_id = await _workflow(pool)
    async with pool.connection() as conn:
        version_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO workflow_versions (id, workflow_id, workspace_id, version_no, dsl, dsl_hash)"
            " VALUES (%s, %s, %s, 1, %s, %s)",
            (version_id, workflow_id, "00000000-0000-0000-0000-000000000001", Jsonb({}), "h"),
        )
        await conn.execute(
            "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status)"
            " VALUES (%s, %s, %s, %s, 'queued')",
            (run_id, "00000000-0000-0000-0000-000000000001", workflow_id, version_id),
        )
        insert = ("INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status)"
                  " VALUES (%s, %s, 'llm_1', 1, 1, 'running')")
        await conn.execute(insert, (str(uuid.uuid4()), run_id))
        with pytest.raises(UniqueViolation):
            await conn.execute(insert, (str(uuid.uuid4()), run_id))


async def test_the_secrets_table_and_retention_columns_exist(pool):
    async with pool.connection() as conn:
        columns = await (await conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='secrets'")).fetchall()
        purged = await (await conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name='runs' AND column_name='purged_at'")).fetchall()
        key = await (await conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a"
            "   ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = 'secrets'::regclass AND i.indisprimary ORDER BY a.attname")).fetchall()

    assert {row["column_name"] for row in columns} == {
        "workspace_id", "name", "ciphertext", "created_at", "updated_at"}
    assert [row["column_name"] for row in purged] == ["purged_at"]
    assert [row["attname"] for row in key] == ["name", "workspace_id"]
