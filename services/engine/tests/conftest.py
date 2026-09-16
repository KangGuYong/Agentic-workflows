"""Shared fixtures. Tests that ask for `db_url` or `redis_url` start containers; the rest need no Docker."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

APP_TABLES = ("run_events", "node_runs", "runs", "workflow_versions", "workflows")
CHECKPOINT_TABLES = ("checkpoint_blobs", "checkpoint_writes", "checkpoints")  # not checkpoint_migrations


def pytest_collection_modifyitems(items):
    """Mark every test that needs a container, so `-m 'not integration'` stays Docker-free."""
    for item in items:
        if {"db_url", "redis_url", "pool", "redis", "api", "worker"} & set(getattr(item, "fixturenames", ())):
            item.add_marker("integration")


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:17-alpine", driver=None) as container:
        url = container.get_connection_url()
        os.environ["ENGINE_DATABASE_URL"] = url
        os.environ.setdefault("LANGGRAPH_AES_KEY", "0" * 32)
        yield url


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        url = f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"
        os.environ["ENGINE_REDIS_URL"] = url
        yield url


@pytest_asyncio.fixture
async def pool(db_url: str) -> AsyncIterator[AsyncConnectionPool]:
    from engine.db.migrate import prepare_database

    await prepare_database(db_url)
    async with AsyncConnectionPool(db_url, min_size=1, max_size=4, open=False,
                                   kwargs={"row_factory": dict_row, "autocommit": True}) as p:
        await p.open(wait=True)
        async with p.connection() as conn:
            await conn.execute(f"TRUNCATE {', '.join(APP_TABLES)} CASCADE")
            await conn.execute(f"TRUNCATE {', '.join(CHECKPOINT_TABLES)} CASCADE")
        yield p


@pytest_asyncio.fixture
async def redis(redis_url: str):
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def listen_conn(db_url: str) -> AsyncIterator[AsyncConnection]:
    conn = await AsyncConnection.connect(db_url, autocommit=True, row_factory=dict_row)
    yield conn
    await conn.close()
