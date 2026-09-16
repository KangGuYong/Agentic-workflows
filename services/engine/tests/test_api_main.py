"""`uvicorn engine.api.main:app` / `python -m engine.api.main` -- the API's ASGI lifespan
(`engine/api/main.py`'s `build()` and its nested `_lifespan`). `prepare_database`, the DB pool and Redis
are faked, so the tests above the integration section exercise only the lifespan's own wiring and shutdown
ordering (A3: startup and shutdown must mirror the worker's discipline in `engine/worker/main.py` --
including when startup itself fails partway through), never real I/O. This mirrors `tests/test_worker_main.py`,
which asserts the same discipline for the worker entrypoint.

Before the A3 fix, `prepare_database(...)` and `pool.open(wait=True)` sat *outside* the lifespan's
`try:`, so a failure in either one skipped the `finally:` entirely and leaked whatever had already been
opened (verified against this repo's own dev containers with Postgres stopped: `lifespan.startup.failed`,
the exception re-raised, and neither `redis.aclose()` nor `pool.close()` ever ran). The two failure tests
below reproduce that with fakes instead.

The migration-lock tests at the bottom are the exception to "no real I/O": they need a real Postgres to
prove two concurrent `prepare_database` callers actually serialize instead of racing (rather than just
not-crashing), and that a caller who cannot get the lock in time raises instead of spinning forever (A2).
They use the `db_url` fixture from `tests/conftest.py`, which `pytest_collection_modifyitems` there already
marks `integration` for.
"""
from __future__ import annotations

import asyncio
import os
from types import ModuleType

import pytest


def _import_main(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """`engine.api.main` runs `app = build()` at import time (module-level -- see its own docstring),
    which calls `load_config()` and so needs `ENGINE_DATABASE_URL` (and an encryption key, or
    `ENGINE_DEV_INSECURE`) set before the *first* import of this module, and every `build()` call after
    that needs them too, since `load_config()` re-reads the environment each time.

    These are set through `monkeypatch`, not a plain `os.environ.setdefault`, specifically so they are
    scoped to the current test and reverted at teardown -- a bare `setdefault` would stick in `os.environ`
    for the rest of the pytest session (module import is a one-time, unreverted side effect) and could
    flip the outcome of an unrelated test; `tests/test_config.py::test_missing_encryption_key_is_refused_
    unless_dev_insecure` does exactly that; it failed the first time this file set `ENGINE_DEV_INSECURE`
    with `setdefault`. Only set a fallback when nothing real is already there (e.g. from the `db_url`
    fixture, which does mutate `os.environ` directly, by design, for the rest of the session), so a real
    value always wins. Importing an already-imported module a second time is a no-op (Python caches it in
    `sys.modules`), so this is safe to call from every test in this file.
    """
    if "ENGINE_DATABASE_URL" not in os.environ:
        monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://fake/db")
    if not os.environ.get("LANGGRAPH_AES_KEY") and os.environ.get("ENGINE_DEV_INSECURE") != "1":
        monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")
    import engine.api.main as main

    return main


class Fakes:
    """Shared state the fakes below record into, so a test can assert what the lifespan opened and
    closed, and in what order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.fail_prepare_database = False
        self.fail_pool_open = False


class FakePool:
    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    async def open(self, wait: bool = True) -> None:
        self._fakes.events.append("pool.open")
        if self._fakes.fail_pool_open:
            raise RuntimeError("pool.open boom")

    async def close(self) -> None:
        self._fakes.events.append("pool.close")


class FakeRedisClient:
    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    async def aclose(self) -> None:
        self._fakes.events.append("redis.aclose")


class FakeRedis:
    """Stands in for the `Redis` class main.py imports: `Redis.from_url(...)` builds the client (same
    substitution `tests/test_worker_main.py` uses for the worker's own `Redis` import)."""

    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    def from_url(self, url: str, decode_responses: bool = True) -> FakeRedisClient:
        return FakeRedisClient(self._fakes)


@pytest.fixture
def main_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return _import_main(monkeypatch)


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, main_module: ModuleType) -> Fakes:
    state = Fakes()

    async def fake_prepare_database(database_url: str) -> None:
        state.events.append("prepare_database")
        if state.fail_prepare_database:
            raise RuntimeError("prepare_database boom")

    monkeypatch.setattr(main_module, "prepare_database", fake_prepare_database)
    monkeypatch.setattr(main_module, "make_pool", lambda database_url: FakePool(state))
    monkeypatch.setattr(main_module, "Redis", FakeRedis(state))
    return state


async def test_clean_startup_shutdown_opens_and_closes_every_resource_in_order(
    main_module: ModuleType, fakes: Fakes,
):
    app = main_module.build()

    async with app.router.lifespan_context(app):
        # Setup happened, in order, before control reaches the running application...
        assert fakes.events == ["prepare_database", "pool.open"]

    # ...and both resources it opened were closed on the way out, in the order the code closes them.
    assert fakes.events == ["prepare_database", "pool.open", "redis.aclose", "pool.close"]


async def test_a_failing_prepare_database_still_closes_everything_and_propagates(
    main_module: ModuleType, fakes: Fakes,
):
    fakes.fail_prepare_database = True
    app = main_module.build()

    with pytest.raises(RuntimeError, match="prepare_database boom"):
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan should have raised before yielding")

    # prepare_database raised before pool.open() was ever reached, but the redis client (never actually
    # connected -- it's lazy) and the pool (never opened) must still both be closed, not leaked, and the
    # original exception must still propagate rather than being swallowed by cleanup.
    assert fakes.events == ["prepare_database", "redis.aclose", "pool.close"]


async def test_a_failing_pool_open_still_closes_everything_and_propagates(
    main_module: ModuleType, fakes: Fakes,
):
    fakes.fail_pool_open = True
    app = main_module.build()

    with pytest.raises(RuntimeError, match="pool.open boom"):
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan should have raised before yielding")

    assert fakes.events == ["prepare_database", "pool.open", "redis.aclose", "pool.close"]


async def test_concurrent_prepare_database_calls_both_succeed_with_correct_schema(db_url: str):
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    from engine.db.migrate import prepare_database

    # Two callers reaching prepare_database at the same moment (the API and a worker starting together
    # against a shared database) must both complete rather than racing to create the same tables.
    await asyncio.gather(prepare_database(db_url), prepare_database(db_url))

    async with await AsyncConnection.connect(db_url, autocommit=True, row_factory=dict_row) as conn:
        for table in ("workflows", "runs", "checkpoints"):  # Alembic's tables and LangGraph's
            row = await (await conn.execute("SELECT to_regclass(%s) AS reg", (table,))).fetchone()
            assert row["reg"] is not None, f"{table!r} is missing after concurrent prepare_database"


async def test_prepare_database_raises_instead_of_spinning_past_its_deadline(db_url: str):
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    from engine.db.migrate import _MIGRATION_LOCK_KEY, prepare_database

    # Hold the migration lock from a separate session, standing in for a lock holder wedged mid-upgrade,
    # and confirm a caller with a short deadline gives up with a clear error rather than polling forever.
    holder = await AsyncConnection.connect(db_url, autocommit=True, row_factory=dict_row)
    try:
        await holder.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(prepare_database(db_url, lock_timeout_sec=0.5), timeout=5)
    finally:
        await holder.close()  # closing the session releases every advisory lock it still holds
