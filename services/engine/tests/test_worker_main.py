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
        self.worker_kwargs: dict = {}
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
    # `**_` on the Worker double so it keeps matching as the entrypoint hands the worker more
    # collaborators (http and secrets arrived with the http_request node).
    def fake_worker(config, pool, redis, *, llm, render, **collaborators):
        fakes.worker_kwargs = collaborators
        return FakeWorker(fakes)

    monkeypatch.setattr(main, "Worker", fake_worker)
    monkeypatch.setattr(main, "GuardedClient", lambda *args, **kwargs: FakeHttpClient(fakes))


class FakeHttpClient:
    """The guarded egress client. It holds an httpx connection pool, so a shutdown that forgets it leaks
    sockets for the life of the process -- and nothing else in the suite would notice."""

    def __init__(self, fakes: Fakes) -> None:
        self._fakes = fakes

    async def aclose(self) -> None:
        self._fakes.events.append("http.aclose")


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


async def test_clean_start_stop_closes_all_six_resources(fakes: Fakes):
    stop = asyncio.Event()
    stop.set()  # already-set: run() must return as soon as it reaches `await stop.wait()`

    await asyncio.wait_for(main.run(stop), timeout=5)

    # Setup happened...
    assert "prepare_database" in fakes.events
    assert "pool.open" in fakes.events
    assert "worker.start" in fakes.events
    # ...and every one of the six resources run() opened was closed on the way out.
    for closed in ("worker.stop", "render.close", "http.aclose", "ollama.aclose", "redis.aclose",
                   "pool.close"):
        assert closed in fakes.events, f"{closed} was never called"


async def test_a_failing_worker_start_still_closes_what_was_already_opened(fakes: Fakes):
    fakes.fail_worker_start = True
    stop = asyncio.Event()

    with pytest.raises(RuntimeError, match="worker.start boom"):
        await asyncio.wait_for(main.run(stop), timeout=5)

    # worker.start() raised, so the run never reached `await stop.wait()` - but every resource opened
    # before that point (including the worker itself, so its own partial state is cleaned up too) must
    # still be closed rather than leaked.
    for closed in ("worker.stop", "render.close", "http.aclose", "ollama.aclose", "redis.aclose",
                   "pool.close"):
        assert closed in fakes.events, f"{closed} was never called"


async def test_the_worker_is_built_with_an_http_client_and_a_secret_resolver(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch,
):
    """`http_request` is useless without both, and the failure is silent in the worst way: every call
    raises `EngineFault` ("http_request needs an HTTP client") at run time, on a deployment that started
    cleanly. The acceptance tests cannot catch this — they construct their own client and hand it to
    `worker_factory`, bypassing this entrypoint entirely.
    """
    # A configured key, because the resolver is deliberately absent without one: `ENGINE_DEV_INSECURE`
    # means "no secret storage", and building a resolver around a null key would only fail later.
    monkeypatch.setenv("ENGINE_SECRET_KEY", "1" * 32)
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(main.run(stop), timeout=5)

    assert fakes.worker_kwargs.get("http") is not None, "the worker was built with no HTTP client"
    assert fakes.worker_kwargs.get("secrets") is not None, "the worker was built with no secret resolver"
