"""`python -m engine.worker.main` (Task 10, design 4). No containers needed: every dependency `run()`
touches - the DB pool, Redis, the Ollama client, the render pool and the worker itself - is faked, so these
tests exercise only `run()`'s own wiring and shutdown ordering (A4/A3), never real I/O.
"""
from __future__ import annotations

import asyncio

import pytest

import engine.worker.main as main


class Fakes:
    """Shared state the fakes below record into, so a test can assert what `run()` opened and closed."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.fail_worker_start = False


def install(monkeypatch: pytest.MonkeyPatch, fakes: Fakes) -> None:
    async def fake_prepare_database(database_url: str) -> None:
        fakes.events.append("prepare_database")

    def fake_make_pool(database_url: str, *, max_size: int) -> object:
        return FakePool(fakes)

    monkeypatch.setattr(main, "prepare_database", fake_prepare_database)
    monkeypatch.setattr(main, "make_pool", fake_make_pool)
    monkeypatch.setattr(main, "Redis", FakeRedis(fakes))
    monkeypatch.setattr(main, "OllamaRaw", lambda base_url: FakeOllama(fakes))
    monkeypatch.setattr(main, "RenderPool", lambda *, size, timeout: FakeRenderPool(fakes))
    monkeypatch.setattr(main, "Worker", lambda config, pool, redis, *, llm, render: FakeWorker(fakes))


class FakePool:
    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    async def open(self, wait: bool = True) -> None:
        self._fakes.events.append("pool.open")

    async def close(self) -> None:
        self._fakes.events.append("pool.close")


class FakeRedisClient:
    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    def register_script(self, script: str):  # used by engine.llm.semaphore.ModelSemaphore's constructor
        async def _run(*args, **kwargs) -> int:
            return 0

        return _run

    async def aclose(self) -> None:
        self._fakes.events.append("redis.aclose")


class FakeRedis:
    """Stands in for the `Redis` class main.py imports: `Redis.from_url(...)` builds the client."""

    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    def from_url(self, url: str, decode_responses: bool = True) -> FakeRedisClient:
        return FakeRedisClient(self._fakes)


class FakeOllama:
    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    async def aclose(self) -> None:
        self._fakes.events.append("ollama.aclose")


class FakeRenderPool:
    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    def close(self) -> None:  # sync, like the real RenderPool.close - run() must call it off the loop
        self._fakes.events.append("render.close")


class FakeWorker:
    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes
        self.owner = "fake-worker"

    async def start(self) -> None:
        self._fakes.events.append("worker.start")
        if self._fakes.fail_worker_start:
            raise RuntimeError("worker.start boom")

    async def stop(self) -> None:
        self._fakes.events.append("worker.stop")


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")
    state = Fakes()
    install(monkeypatch, state)
    return state


async def test_clean_start_stop_closes_all_five_resources(fakes: Fakes):
    stop = asyncio.Event()
    stop.set()  # already-set: run() must return as soon as it reaches `await stop.wait()`

    await asyncio.wait_for(main.run(stop), timeout=5)

    # Setup happened...
    assert "prepare_database" in fakes.events
    assert "pool.open" in fakes.events
    assert "worker.start" in fakes.events
    # ...and every one of the five resources run() opened was closed on the way out.
    for closed in ("worker.stop", "render.close", "ollama.aclose", "redis.aclose", "pool.close"):
        assert closed in fakes.events, f"{closed} was never called"


async def test_a_failing_worker_start_still_closes_what_was_already_opened(fakes: Fakes):
    fakes.fail_worker_start = True
    stop = asyncio.Event()

    with pytest.raises(RuntimeError, match="worker.start boom"):
        await asyncio.wait_for(main.run(stop), timeout=5)

    # worker.start() raised, so the run never reached `await stop.wait()` - but every resource opened
    # before that point (including the worker itself, so its own partial state is cleaned up too) must
    # still be closed rather than leaked.
    for closed in ("worker.stop", "render.close", "ollama.aclose", "redis.aclose", "pool.close"):
        assert closed in fakes.events, f"{closed} was never called"
