"""Shared fixtures. Tests that ask for `db_url` or `redis_url` start containers; the rest need no Docker."""
from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

APP_TABLES = ("run_events", "node_runs", "runs", "workflow_versions", "workflows", "secrets")
CHECKPOINT_TABLES = ("checkpoint_blobs", "checkpoint_writes", "checkpoints")  # not checkpoint_migrations


if sys.platform == "win32":
    # psycopg's async connections refuse Windows' default ProactorEventLoop, so development on Windows
    # runs tests on the selector loop. The hook is only defined there: pytest-asyncio demands a non-empty
    # mapping from every registered implementation, so a version that returns None to mean "no opinion"
    # fails collection everywhere else. Not registering it at all is what leaves the default alone.
    def pytest_asyncio_loop_factories(config, item):
        return {"selector": asyncio.SelectorEventLoop}


def pytest_collection_modifyitems(items):
    """Mark every test that needs a container, so `-m 'not integration'` stays Docker-free."""
    for item in items:
        if {"db_url", "redis_url", "pool", "redis", "api", "worker"} & set(getattr(item, "fixturenames", ())):
            item.add_marker("integration")


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    # An already-running server, when one is named, so the suite can be run where Docker is not available
    # (CI without a socket, a remote sandbox). The database it points at is truncated between tests like
    # any other, so it must be a throwaway.
    existing = os.getenv("ENGINE_TEST_DATABASE_URL")
    if existing:
        os.environ["ENGINE_DATABASE_URL"] = existing
        os.environ.setdefault("LANGGRAPH_AES_KEY", "0" * 32)
        os.environ.setdefault("ENGINE_SECRET_KEY", "1" * 32)
        yield existing
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:17-alpine", driver=None) as container:
        url = container.get_connection_url()
        os.environ["ENGINE_DATABASE_URL"] = url
        os.environ.setdefault("LANGGRAPH_AES_KEY", "0" * 32)
        os.environ.setdefault("ENGINE_SECRET_KEY", "1" * 32)
        yield url


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    existing = os.getenv("ENGINE_TEST_REDIS_URL")  # as above
    if existing:
        os.environ["ENGINE_REDIS_URL"] = existing
        yield existing
        return
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


async def until(check, *, timeout: float = 15.0, interval: float = 0.05):
    """Wait for `check()` (async) to return something truthy, then return it."""
    import asyncio

    async with asyncio.timeout(timeout):
        while True:
            value = await check()
            if value:
                return value
            await asyncio.sleep(interval)


@pytest_asyncio.fixture
async def api(pool, redis):
    """An httpx client bound to the app, sharing the test's pool and Redis."""
    import dataclasses

    from httpx import ASGITransport, AsyncClient

    from engine.api.app import create_app
    from engine.config import load_config

    config = dataclasses.replace(load_config(), api_token="test-token")
    app = create_app(config, pool, redis)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://api",
                           headers={"Authorization": "Bearer test-token"}) as client:
        client.config = config  # tests that need the token or limits
        yield client


@pytest_asyncio.fixture
async def worker_factory(pool, redis, db_url):
    """Builds workers sharing the test's pool and Redis; every worker is stopped at teardown."""
    from engine.config import load_config
    from engine.worker.worker import Worker

    started: list = []

    async def make(llm, *, owner: str = "worker-1", **overrides):
        import dataclasses

        config = dataclasses.replace(load_config(), claim_poll_sec=0.2, heartbeat_sec=0.2, **overrides)
        worker = Worker(config, pool, redis, owner=owner, llm=llm)
        await worker.start()
        started.append(worker)
        return worker

    yield make
    for worker in started:
        await worker.stop()
