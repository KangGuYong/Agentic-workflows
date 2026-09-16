# Runtime Core (Plan 2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the Plan 1 engine as a service — Postgres-backed state, a worker that claims and recovers runs, and an HTTP API with live events — so a workflow can be saved, run, watched, approved and retried without an editor.

**Architecture:** One Python package (`services/engine`) with two entrypoints. The API writes `runs` rows and wakes workers with `NOTIFY`; a worker claims a run with `UPDATE … FOR UPDATE SKIP LOCKED`, holds a lease, and drives `engine.runtime.runner.execute_run` against a Postgres checkpointer. Postgres is the source of truth; Redis only carries live events, cancel signals and the per-model LLM semaphore.

**Tech Stack:** Python 3.11+, psycopg3 (async) + Alembic, FastAPI + uvicorn, redis-py asyncio, LangGraph `AsyncPostgresSaver` with an encrypted serializer, pebble process pool, pytest + testcontainers.

**Design:** `docs/superpowers/specs/2026-09-16-runtime-core-design.md` (this plan implements it; MVP contract in `docs/superpowers/specs/2026-09-11-agentic-workflow-builder-mvp-design.md`).

---

## Conventions for every task

- Work in `services/engine` unless a path says otherwise. Run tests with `uv run pytest -q`, lint with `uv run ruff check .`.
- Tests that need Postgres or Redis request the `db`/`redis_url` fixtures, which start containers. They are marked `integration` automatically by `tests/conftest.py`; unit tests must not need Docker.
- User-facing strings (API error messages, node errors) are Korean. Comments and docstrings are English, like Plan 1.
- Commit messages end with the line `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` and nothing else after it.
- Never widen a Plan 1 public contract silently: if a task changes `engine/runtime/*` or `engine/compiler/*`, the existing suite must still pass unchanged except where the task says otherwise.

## File structure

```
services/engine/
  engine/
    config.py                 env-driven settings (new)
    compiler/state.py         per-node channels (Task 1)
    runtime/deps.py           render hook (Task 2)
    db/
      pool.py                 connection pool helpers
      migrate.py              alembic runner + checkpointer setup
      alembic.ini
      migrations/env.py, migrations/versions/0001_initial.py
      workflows.py            workflow + version queries
      runs.py                 run queries: create, claim, lease, transitions
      node_runs.py            node_runs reads for the API
    events/
      redact.py               key-name redaction, size clipping
      writer.py               run_events seq allocation
      recorder.py             PostgresRecorder (Plan 1 Recorder protocol)
      publish.py              Redis publish + control channel
      stream.py               SSE event stream
    llm/semaphore.py          Redis per-model semaphore
    worker/
      main.py                 entrypoint
      worker.py               claim loop, lease, run task, cancel
      reaper.py               lease recovery, waiting expiry
      render.py               pebble render pool
    api/
      app.py                  create_app
      security.py             shared-token dependency
      body.py                 strict JSON + size limits
      errors.py               error format and handlers
      routers/node_types.py, routers/workflows.py, routers/runs.py
  tests/
    conftest.py               container fixtures, pool/redis fixtures, api client, worker harness
    test_db_schema.py, test_events_recorder.py, test_events_stream.py, test_db_runs.py,
    test_worker_*.py, test_api_*.py, test_runtime_e2e.py
deploy/docker-compose.dev.yml postgres + redis for development
```

---

## Task 0: Dependencies, dev services, container fixtures

**Files:**

- Modify: `services/engine/pyproject.toml`
- Create: `deploy/docker-compose.dev.yml`
- Create: `services/engine/engine/config.py`
- Create: `services/engine/tests/conftest.py`
- Test: `services/engine/tests/test_config.py`

- [ ]  **Step 1: Add dependencies**

In `services/engine/pyproject.toml` replace the `dependencies` and `dev` lists:

```toml
dependencies = [
    "pydantic>=2.13,<3",
    "jinja2>=3.1.6,<4",
    "jsonschema>=4.26,<5",
    "httpx>=0.28,<1",
    "langgraph>=1.2.11,<1.3",
    "langgraph-checkpoint-postgres>=3.1,<4",  # 3.1 is the line that needs langgraph-checkpoint 4.x
    "psycopg[binary,pool]>=3.2,<4",
    "pycryptodome>=3.21,<4",
    "redis>=5.2,<6",
    "fastapi>=0.115,<1",
    "uvicorn[standard]>=0.34,<1",
    "alembic>=1.14,<2",
    "pebble>=5.1,<6",
]

[dependency-groups]
dev = [
    "pytest>=9,<10",
    "pytest-asyncio>=1.4,<2",
    "ruff>=0.8,<1",
    "testcontainers[postgres,redis]>=4.9,<5",
]
```

Run: `uv sync`
Expected: resolves and installs. `langgraph-checkpoint-postgres` 3.1 is the release that requires `langgraph-checkpoint>=4.1`, which is what `langgraph` 1.2 pulls in; older lines (2.x) need checkpoint 2.x and cannot resolve. If it still conflicts, report it instead of loosening the `langgraph` pin.

- [ ]  **Step 2: Verify the two libraries we depend on behaving a certain way**

Write `services/engine/tests/test_smoke_runtime_deps.py`:

```python
import os

from langgraph.checkpoint.serde.encrypted import EncryptedSerializer


def test_encrypted_serializer_round_trips_with_an_env_key(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    serde = EncryptedSerializer.from_pycryptodome_aes()

    typed = serde.dumps_typed({"a": [1, "x"]})

    assert serde.loads_typed(typed) == {"a": [1, "x"]}
    assert b"x" not in typed[1]  # the payload is not stored in the clear


def test_pebble_kills_a_task_that_overruns_its_deadline():
    from concurrent.futures import TimeoutError as FuturesTimeout

    from pebble import ProcessPool

    with ProcessPool(max_workers=1) as pool:
        future = pool.schedule(_spin, args=(30,), timeout=0.5)
        try:
            future.result()
            raise AssertionError("expected a timeout")
        except FuturesTimeout:
            pass
        assert pool.schedule(_spin, args=(0,)).result() == "done"  # the pool still works


def _spin(seconds: float) -> str:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass
    return "done"
```

Run: `uv run pytest tests/test_smoke_runtime_deps.py -q`
Expected: PASS. `_spin` must be a module-level function so the pool can pickle it. If pebble cannot kill the worker on this platform, stop and report — the design's render deadline depends on it.

- [ ]  **Step 3: Development services**

Create `deploy/docker-compose.dev.yml`:

```yaml
services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: engine
      POSTGRES_PASSWORD: engine
      POSTGRES_DB: engine
    ports: ["5433:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U engine"]
      interval: 5s
      timeout: 3s
      retries: 10
  redis:
    image: redis:7-alpine
    ports: ["6380:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10
```

Ports are shifted (5433/6380) so they never collide with a locally installed Postgres or Redis.

- [ ]  **Step 4: Write the failing config test**

Create `services/engine/tests/test_config.py`:

```python
import pytest

from engine.config import ConfigError, load_config


def test_config_reads_the_environment(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("ENGINE_REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    monkeypatch.setenv("ENGINE_API_TOKEN", "t0ken")
    monkeypatch.setenv("WORKER_MAX_RUNS", "3")

    config = load_config()

    assert (config.database_url, config.api_token, config.worker_max_runs) == (
        "postgresql://u:p@h/db", "t0ken", 3
    )
    assert config.lease_sec == 30 and config.heartbeat_sec == 10


def test_missing_encryption_key_is_refused_unless_dev_insecure(monkeypatch):
    monkeypatch.setenv("ENGINE_DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("ENGINE_REDIS_URL", "redis://h:6379/0")
    monkeypatch.delenv("LANGGRAPH_AES_KEY", raising=False)

    with pytest.raises(ConfigError):
        load_config()

    monkeypatch.setenv("ENGINE_DEV_INSECURE", "1")
    assert load_config().encrypt_checkpoints is False


def test_a_missing_database_url_is_an_error(monkeypatch):
    monkeypatch.delenv("ENGINE_DATABASE_URL", raising=False)
    monkeypatch.setenv("LANGGRAPH_AES_KEY", "0" * 32)
    with pytest.raises(ConfigError):
        load_config()
```

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL (`No module named 'engine.config'`).

- [ ]  **Step 5: Implement the config**

Create `services/engine/engine/config.py`:

```python
"""Process configuration from the environment (design 10)."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """A required setting is missing or unusable. Raised at startup, never per request."""


@dataclass(frozen=True)
class EngineConfig:
    database_url: str
    redis_url: str
    api_token: str | None
    encrypt_checkpoints: bool
    ollama_base_url: str
    ollama_num_parallel: int
    worker_max_runs: int
    lease_sec: int
    heartbeat_sec: int
    claim_poll_sec: float
    reaper_interval_sec: float
    run_max_active_ms: int
    render_timeout_sec: float
    render_pool_size: int
    max_body_bytes: int


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def load_config() -> EngineConfig:
    database_url = os.getenv("ENGINE_DATABASE_URL")
    if not database_url:
        raise ConfigError("ENGINE_DATABASE_URL is required")
    dev_insecure = os.getenv("ENGINE_DEV_INSECURE") == "1"
    if not os.getenv("LANGGRAPH_AES_KEY") and not dev_insecure:
        # Checkpoints hold every node output. Adding encryption later cannot read existing checkpoints,
        # so refuse to start rather than write them in the clear.
        raise ConfigError("LANGGRAPH_AES_KEY is required (set ENGINE_DEV_INSECURE=1 only for development)")
    return EngineConfig(
        database_url=database_url,
        redis_url=os.getenv("ENGINE_REDIS_URL") or "redis://localhost:6379/0",
        api_token=os.getenv("ENGINE_API_TOKEN") or None,
        encrypt_checkpoints=bool(os.getenv("LANGGRAPH_AES_KEY")),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434",
        ollama_num_parallel=_int("OLLAMA_NUM_PARALLEL", 1),
        worker_max_runs=_int("WORKER_MAX_RUNS", 10),
        lease_sec=_int("WORKER_LEASE_SEC", 30),
        heartbeat_sec=_int("WORKER_HEARTBEAT_SEC", 10),
        claim_poll_sec=float(_int("WORKER_CLAIM_POLL_SEC", 5)),
        reaper_interval_sec=float(_int("WORKER_REAPER_SEC", 15)),
        run_max_active_ms=_int("RUN_MAX_ACTIVE_MS", 3_600_000),
        render_timeout_sec=float(_int("RENDER_TIMEOUT_SEC", 5)),
        render_pool_size=_int("RENDER_POOL_SIZE", os.cpu_count() or 2),
        max_body_bytes=_int("MAX_BODY_BYTES", 1_000_000),
    )
```

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS.

- [ ]  **Step 6: Container fixtures**

Create `services/engine/tests/conftest.py`:

```python
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

APP_TABLES = ("run_events", "node_runs", "runs", "workflow_versions", "workflows")
CHECKPOINT_TABLES = ("checkpoint_blobs", "checkpoint_writes", "checkpoints")  # not checkpoint_migrations


def pytest_asyncio_loop_factories(config, item):
    """psycopg's async connections refuse Windows' default ProactorEventLoop, so development on Windows
    runs tests on the selector loop. Deployment is Linux, where None leaves the default alone."""
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return None


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
```

`prepare_database` arrives in Task 3; until then the fixtures are unused.

Add the marker to `pyproject.toml` so `-m` works and unknown-marker warnings stay off:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
markers = ["integration: needs Postgres/Redis containers"]
```

- [ ]  **Step 7: Run the whole suite and commit**

Run: `uv run pytest -q -m "not integration"` → the Plan 1 suite plus the new tests pass (645+).
Run: `uv run ruff check .` → `All checks passed!`

```bash
git add services/engine/pyproject.toml services/engine/uv.lock services/engine/engine/config.py \
        services/engine/tests/conftest.py services/engine/tests/test_config.py \
        services/engine/tests/test_smoke_runtime_deps.py deploy/docker-compose.dev.yml
git commit -m "chore(engine): add runtime dependencies, dev services and container fixtures"
```

> **Post-review note (Task 0, as implemented):** commits `c0963b1` and `94e1043`.
>
> - `langgraph-checkpoint-postgres` had to be `>=3.1,<4`: the 2.x line needs `langgraph-checkpoint` 2.x, while our pinned `langgraph` pulls 4.2.
> - Both spikes passed on Windows: pebble kills an overrunning task and keeps serving, and `EncryptedSerializer.from_pycryptodome_aes()` round-trips without storing the payload in the clear.
> - The encryption smoke test first asserted a single byte was absent from a 39-byte ciphertext, which collided about 12% of the time; it now checks a 29-character marker (fixed in `f9917f7`).
> - Known: `load_config` accepts a whitespace-only database URL and non-positive integers. The render pool clamps its own size, so nothing downstream breaks today.
>
> Suite: 648.

---

## Task 1: One checkpoint channel per node

The single `outputs` channel is rewritten in full at every superstep, so checkpoint storage grows with (steps × total output size) — 226 MB measured for 40 nodes carrying a 250 KB input. Give each node its own channel.

**Files:**

- Modify: `services/engine/engine/compiler/state.py`
- Modify: `services/engine/engine/compiler/wrapper.py:150-230`
- Modify: `services/engine/engine/compiler/build.py`
- Modify: `services/engine/engine/runtime/runner.py`
- Test: `services/engine/tests/test_compiler_state.py` (new), existing compiler/runner tests

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_compiler_state.py`:

```python
import json

from langgraph.checkpoint.memory import InMemorySaver

from engine.compiler.build import compile_workflow
from engine.compiler.state import build_state_type, initial_state, outputs_of
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import execute_run


def test_state_type_has_one_channel_per_node():
    fields = build_state_type(["start", "llm_1"]).__annotations__

    assert set(fields) == {"inputs", "routes", "loop_counters", "exec_counts", "out_start", "out_llm_1"}


def test_outputs_are_assembled_from_the_node_channels():
    state = {"inputs": {"a": 1}, "out_start": {"a": 1}, "out_llm_1": {"text": "답"}, "routes": {}}

    assert outputs_of(state) == {"start": {"a": 1}, "llm_1": {"text": "답"}}
    assert outputs_of(initial_state({"a": 1})) == {}


class _SizedSaver(InMemorySaver):
    """Counts the bytes a Postgres checkpointer would write: only channels whose version changed."""

    def __init__(self) -> None:
        super().__init__()
        self.written = 0

    async def aput(self, config, checkpoint, metadata, new_versions):
        values = checkpoint["channel_values"]
        self.written += sum(len(json.dumps(values.get(name), default=str)) for name in new_versions)
        return await super().aput(config, checkpoint, metadata, new_versions)


def _chain(length: int) -> dict:
    nodes = [{"id": "start", "type": "start"}]
    edges = []
    previous = "start"
    for index in range(length):
        node_id = f"template_{index}"
        nodes.append({"id": node_id, "type": "template",
                      "config": {"template": "{{ %s.text }}" % previous if index else "{{ start.text }}"}})
        edges.append({"id": f"e{index}", "source": previous, "target": node_id})
        previous = node_id
    nodes.append({"id": "end", "type": "end", "config": {"outputs": {"final": "{{ %s.text }}" % previous}}})
    edges.append({"id": "e_end", "source": previous, "target": "end"})
    return {"nodes": nodes, "edges": edges}


async def test_checkpoint_writes_do_not_grow_with_the_number_of_steps():
    payload = "가" * 20_000
    saver = _SizedSaver()
    compiled = compile_workflow(_chain(8), checkpointer=saver)
    deps = RunDeps(run_id="run-size", llm=ScriptedLLM([]), recorder=InMemoryRecorder())

    outcome = await execute_run(compiled, deps=deps, inputs={"text": payload})

    assert outcome.status == "succeeded"
    # 10 nodes each storing the payload once, plus inputs: quadratic rewriting would be several times this.
    assert saver.written < 14 * len(payload)
```

Run: `uv run pytest tests/test_compiler_state.py -q`
Expected: FAIL — `build_state_type` and `outputs_of` do not exist; the size test fails on the import too.

- [ ]  **Step 2: Rewrite the state module**

Replace `services/engine/engine/compiler/state.py`:

```python
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Any, TypedDict

OUT_PREFIX = "out_"


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    """Reducer: shallow merge, so parallel nodes can write disjoint keys in the same superstep."""
    return {**(left or {}), **(right or {})}


GLOBAL_CHANNELS: dict[str, Any] = {
    "inputs": dict[str, Any],
    "routes": Annotated[dict[str, list[str]], merge_dicts],  # branch node id -> chosen next node ids
    "loop_counters": Annotated[dict[str, int], merge_dicts],  # back-edge id -> traversals so far
    "exec_counts": Annotated[dict[str, int], merge_dicts],  # node id -> completed executions
}

RunState = TypedDict("RunState", GLOBAL_CHANNELS, total=False)  # type: ignore[misc]


def build_state_type(node_ids: Iterable[str]) -> type:
    """The global channels plus one last-value channel per node.

    One shared `outputs` channel would be written in full at every superstep, so checkpoint storage would
    grow with (steps x total output size). A node only ever writes its own channel, so no reducer is needed
    and parallel branches cannot collide.
    """
    channels = {OUT_PREFIX + node_id: dict[str, Any] for node_id in node_ids}
    return TypedDict("WorkflowState", {**GLOBAL_CHANNELS, **channels}, total=False)  # type: ignore[misc]


def initial_state(inputs: dict[str, Any]) -> dict[str, Any]:
    return {"inputs": inputs, "routes": {}, "loop_counters": {}, "exec_counts": {}}


def outputs_of(state: Mapping[str, Any]) -> dict[str, Any]:
    """Latest output of every node that ran, keyed by node id — what templates and routing read."""
    return {
        name[len(OUT_PREFIX):]: value
        for name, value in state.items()
        if name.startswith(OUT_PREFIX) and value is not None
    }
```

- [ ]  **Step 3: Write to the node's own channel**

In `services/engine/engine/compiler/wrapper.py`:

1. Import `OUT_PREFIX`, `RunState`, `outputs_of` from `engine.compiler.state`.
2. In `_succeed`, replace the first line of `write`:

```python
    write: dict[str, Any] = {OUT_PREFIX + node_id: result.output, "exec_counts": {node_id: exec_index}}
```

3. In `node_fn`, replace `outputs = state.get("outputs", {})` with:

```python
        outputs = outputs_of(state)
```

4. In `_context`, replace `outputs=state.get("outputs", {})` with a new parameter: change the signature to
   `_context(plan, deps, state, outputs, exec_index, attempt, resumed)` and pass `outputs=outputs`, so the
   assembly happens once per node call.

- [ ]  **Step 4: Build the graph with the generated type**

In `services/engine/engine/compiler/build.py` replace the builder line:

```python
    builder = StateGraph(build_state_type(graph.nodes), context_schema=RunDeps)
```

and import `build_state_type` instead of `RunState`.

In `services/engine/engine/runtime/runner.py` replace the success line:

```python
    return RunOutcome("succeeded", outputs=outputs_of(snapshot.values).get("end", {}))
```

importing `outputs_of` from `engine.compiler.state`.

- [ ]  **Step 5: Run the tests**

Run: `uv run pytest -q -m "not integration"`
Expected: PASS. Existing tests that assert on `state["outputs"]` directly must be updated to `outputs_of(state)`; tests that only go through `execute_run` need no change. Do not change any assertion about node behaviour to make it pass — if a golden test fails, the wrapper change is wrong.

Run: `uv run ruff check .` → `All checks passed!`

- [ ]  **Step 6: Commit**

```bash
git add services/engine/engine/compiler services/engine/engine/runtime/runner.py services/engine/tests
git commit -m "perf(engine): give every node its own checkpoint channel"
```

> **Post-review note (Task 1, as implemented):** commits `8bd51a2` and `f9917f7`.
>
> **Plan errors found while implementing.**
> - `node_fn`'s `state` parameter must be annotated `dict[str, Any]`, not `RunState`. LangGraph reads the first parameter's hint and, when it is a TypedDict, uses it as the node's input schema — every `out_*` channel would have been hidden from the node. Restoring the old annotation fails 29 tests.
> - The `_chain` helper needs `config.inputs` on the start node, or the validator rejects `{{ start.text }}`.
> - `_SizedSaver` needs `ensure_ascii=False`; escaped Korean inflates the byte count about six times.
>
> **Measured** (payload 20,000 chars, `_SizedSaver` accounting): a 6-node chain went from 23.1x to 8.0x the payload, 10 nodes from 57.1x to 12.1x, 18 nodes from 173.4x to 20.2x. After the change the cost is `(nodes + 2) x payload` — linear.
>
> **Known.** `RunState` is now exported but unused, and `RESERVED_IDS` still reserves `outputs` (harmless: it keeps older workflows valid) with a comment that no longer matches.
>
> Suite: 651.

---

## Task 2: Off-loop rendering hook

Template rendering is unbounded CPU work (measured 7.7 s for one loop over a 20k-item list) and cannot be interrupted in a thread. Add a hook so the worker can run it in a process with a deadline; without the hook the engine renders inline exactly as today.

**Files:**

- Modify: `services/engine/engine/runtime/deps.py`
- Modify: `services/engine/engine/compiler/wrapper.py`
- Test: `services/engine/tests/test_compiler_wrapper.py`

- [ ]  **Step 1: Write the failing tests**

Append to `services/engine/tests/test_compiler_wrapper.py`:

```python
async def test_rendering_goes_through_the_deps_hook_when_one_is_set():
    seen: list[dict] = []

    async def render(fields, outputs):
        seen.append({field.path: field.source for field in fields})
        return {field.path: "치환됨" for field in fields}

    deps, _, _ = _deps(ScriptedLLM(["답"]))
    deps.render = render

    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)

    assert seen and result["outputs"]["n"] == {"text": "답"}
    assert deps.llm.calls[0]["messages"][-1].content == "치환됨"


async def test_a_render_deadline_fails_the_node_without_retrying():
    async def render(fields, outputs):
        raise TimeoutError("render deadline")

    deps, _, recorder = _deps(ScriptedLLM(["답"]))
    deps.render = render
    plan = _plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy)

    with pytest.raises(NodeFailedError) as exc:
        await _run(plan, deps)

    assert exc.value.error.code == ErrorCode.TEMPLATE_ERROR
    assert len(recorder.for_node("n")) == 1  # a deadline is not retryable: it would just happen again
```

Adjust the helper names (`_deps`, `_run`, `_plan`, `LLM_CONFIG`) to the ones already in the file, and import
`NodeFailedError`/`ErrorCode` if they are not imported yet.

Run: `uv run pytest tests/test_compiler_wrapper.py -q`
Expected: FAIL (`RunDeps` has no attribute `render`).

- [ ]  **Step 2: Add the hook to RunDeps**

In `services/engine/engine/runtime/deps.py`:

```python
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoids importing the node layer into the runtime ports
    from engine.nodes.base import TemplateField

RenderFn = Callable[[list["TemplateField"], dict[str, Any]], Awaitable[dict[str, Any]]]
```

and add the field to `RunDeps`:

```python
    render: RenderFn | None = None
    """Renders a node's template fields off the event loop (Plan 2a worker). None renders inline.

    The implementation must raise TimeoutError when it gives up, and may raise the template errors the
    inline path raises; anything else becomes a non-retryable node error.
    """
```

- [ ]  **Step 3: Use the hook**

In `services/engine/engine/compiler/wrapper.py` split rendering from checking:

```python
def _render(fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
    """Pure rendering: runs inline or, through RunDeps.render, in the worker's render pool."""
    return {f.path: render_template(f.source, outputs, f.target) for f in fields}


async def _rendered(deps: RunDeps, fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
    if deps.render is None:
        rendered = _render(fields, outputs)
    else:
        try:
            rendered = await deps.render(fields, outputs)
        except TimeoutError as exc:
            raise NodeError(
                ErrorCode.TEMPLATE_ERROR, "템플릿 렌더링이 제한 시간을 초과했습니다", retryable=False
            ) from exc
    try:
        check_storable(rendered)  # recorded as the attempt's input (jsonb) and passed on to outputs
    except ValueError as exc:
        raise NodeError(ErrorCode.TEMPLATE_ERROR, f"템플릿 결과를 사용할 수 없습니다: {exc}", retryable=False) from exc
    return rendered
```

and in `node_fn` replace `rendered = _render(fields, outputs)` with `rendered = await _rendered(deps, fields, outputs)`.

- [ ]  **Step 4: Run the tests and commit**

Run: `uv run pytest -q -m "not integration"` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/runtime/deps.py services/engine/engine/compiler/wrapper.py services/engine/tests/test_compiler_wrapper.py
git commit -m "feat(engine): let a run render template fields off the event loop"
```

---

## Task 3: Schema, migrations and the checkpointer

**Files:**

- Create: `services/engine/engine/db/__init__.py`, `pool.py`, `migrate.py`, `alembic.ini`
- Create: `services/engine/engine/db/migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_initial.py`
- Test: `services/engine/tests/test_db_schema.py`

- [ ]  **Step 1: Write the failing test**

Create `services/engine/tests/test_db_schema.py`:

```python
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
```

Run: `uv run pytest tests/test_db_schema.py -q`
Expected: FAIL — `engine.db.migrate` does not exist (the `pool` fixture imports it).

- [ ]  **Step 2: The pool helper**

Create `services/engine/engine/db/__init__.py` (empty) and `services/engine/engine/db/pool.py`:

```python
"""Postgres access. Every query in this package is written by hand: the important ones are CAS updates."""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def make_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    """A pool of autocommit connections with dict rows.

    Autocommit is what LangGraph's Postgres saver needs, and it keeps single statements out of an implicit
    transaction; multi-statement work opens `conn.transaction()` explicitly.
    """
    return AsyncConnectionPool(
        url, min_size=min_size, max_size=max_size, open=False,
        kwargs={"row_factory": dict_row, "autocommit": True},
    )
```

- [ ]  **Step 3: Alembic wiring**

Create `services/engine/engine/db/alembic.ini`:

```ini
[alembic]
script_location = migrations
prepend_sys_path = .
path_separator = os
```

Create `services/engine/engine/db/migrations/script.py.mako`:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

Create `services/engine/engine/db/migrations/env.py`:

```python
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
connectable = engine_from_config(
    config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
)
with connectable.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
```

Offline mode is not supported on purpose: migrations always run against a live database.

- [ ]  **Step 4: The initial migration**

Create `services/engine/engine/db/migrations/versions/0001_initial.py`:

```python
"""Application tables (MVP design 3.3).

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = """
CREATE TABLE workflows (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    name text NOT NULL,
    draft_dsl jsonb NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX workflows_workspace_idx ON workflows (workspace_id, updated_at DESC);

CREATE TABLE workflow_versions (
    id uuid PRIMARY KEY,
    workflow_id uuid NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    workspace_id uuid NOT NULL,
    version_no integer NOT NULL,
    dsl jsonb NOT NULL,
    dsl_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, dsl_hash),
    UNIQUE (workflow_id, version_no)
);

CREATE TABLE runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    workflow_id uuid NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    workflow_version_id uuid NOT NULL REFERENCES workflow_versions (id),
    status text NOT NULL CHECK (status IN ('queued','running','waiting','succeeded','failed','cancelled')),
    inputs jsonb,
    outputs jsonb,
    error jsonb,
    idempotency_key text,
    lease_owner text,
    lease_expires_at timestamptz,
    cancel_requested_at timestamptz,
    waiting_node_id text,
    waiting_exec_index integer,
    resume_payload jsonb,
    retry_count integer NOT NULL DEFAULT 0,
    recovery_count integer NOT NULL DEFAULT 0,
    event_seq bigint NOT NULL DEFAULT 0,
    active_ms bigint NOT NULL DEFAULT 0,
    store_run_data boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE UNIQUE INDEX runs_idempotency_idx ON runs (workflow_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX runs_queue_idx ON runs (status, created_at);
CREATE INDEX runs_workflow_idx ON runs (workflow_id, created_at DESC);
CREATE INDEX runs_lease_idx ON runs (lease_expires_at) WHERE status = 'running';
CREATE INDEX runs_waiting_idx ON runs (updated_at) WHERE status = 'waiting';

CREATE TABLE node_runs (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    node_id text NOT NULL,
    exec_index integer NOT NULL,
    attempt integer NOT NULL,
    status text NOT NULL,
    input jsonb,
    output jsonb,
    error jsonb,
    meta jsonb,
    tokens_in integer NOT NULL DEFAULT 0,
    tokens_out integer NOT NULL DEFAULT 0,
    truncated boolean NOT NULL DEFAULT false,
    waited boolean NOT NULL DEFAULT false,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (run_id, node_id, exec_index, attempt)
);
CREATE INDEX node_runs_run_idx ON node_runs (run_id, started_at);

CREATE TABLE run_events (
    run_id uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq bigint NOT NULL,
    type text NOT NULL,
    node_id text,
    exec_index integer,
    attempt integer,
    payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);
"""


def upgrade() -> None:
    op.execute(TABLES)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS run_events, node_runs, runs, workflow_versions, workflows CASCADE")
```

`runs.store_run_data` is a copy of the workflow version's `settings.storeRunData`, taken when the run is
created: the worker and the API decide what to store without parsing the DSL again, and a later edit of the
workflow cannot change how an in-flight run stores data.

- [ ]  **Step 5: The migration runner and the checkpointer**

Create `services/engine/engine/db/migrate.py`:

```python
"""Bring a database up to date: application tables (Alembic), then LangGraph's checkpoint tables."""
from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

HERE = Path(__file__).parent


def _alembic_config(url: str) -> Config:
    config = Config(str(HERE / "alembic.ini"))
    config.set_main_option("script_location", str(HERE / "migrations"))
    # SQLAlchemy (used only by Alembic) needs the driver spelled out, and treats % as interpolation.
    sqlalchemy_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    config.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))
    return config


async def prepare_database(url: str) -> None:
    """Idempotent: safe to call at every startup and from tests."""
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
```

- [ ]  **Step 6: The checkpointer factory**

`setup()` only creates tables; the serializer is chosen wherever the saver is constructed. Design 5.3 wants
encryption from the first checkpoint a deployment writes, so the factory lives here and every caller (the
worker in Task 7) uses it. Create `services/engine/engine/db/checkpointer.py`:

```python
"""The run checkpointer (design 5.3).

Checkpoints hold every node output, so they are encrypted at rest with `LANGGRAPH_AES_KEY`. The serializer
has to be in place from the first checkpoint a deployment writes: turning it on later leaves the existing
rows unreadable, which is why `load_config` refuses to start without a key unless ENGINE_DEV_INSECURE is set.
"""
from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from psycopg_pool import AsyncConnectionPool

from engine.config import EngineConfig


def make_checkpointer(pool: AsyncConnectionPool, config: EngineConfig) -> AsyncPostgresSaver:
    """A saver over `pool`, encrypted unless the process was started in development-insecure mode.

    The pool must hand out autocommit connections (see `engine.db.pool.make_pool`).
    """
    if not config.encrypt_checkpoints:
        return AsyncPostgresSaver(pool)
    return AsyncPostgresSaver(pool, serde=EncryptedSerializer.from_pycryptodome_aes())
```

Add `services/engine/tests/test_db_checkpointer.py` with three tests, each running a tiny workflow through
`compile_workflow(..., checkpointer=make_checkpointer(pool, config))` and `execute_run`: a node output must
not appear in `checkpoint_blobs.blob` when a key is set; a second `execute_run` with an empty `ScriptedLLM`
must resume from those encrypted checkpoints; and with `encrypt_checkpoints=False` the output *is* readable
in the blob (which is what the key requirement protects against).

- [ ]  **Step 7: Run the tests**

Run: `uv run pytest tests/test_db_schema.py -q`
Expected: PASS (the first run pulls the Postgres image, which can take a minute).

Run: `uv run pytest -q -m "not integration"` → the Docker-free suite still passes.
Run: `uv run ruff check .` → clean.

- [ ]  **Step 8: Commit**

```bash
git add services/engine/engine/db services/engine/tests/test_db_schema.py services/engine/tests/test_db_checkpointer.py
git commit -m "feat(engine): add the Postgres schema, migrations and checkpoint setup"
```

> **Post-review note (Task 3, as implemented):** commits `5b2ae54`, `3b7583c` and `0fe4c1a`.
>
> - psycopg's async connections refuse Windows' default ProactorEventLoop, so `tests/conftest.py` selects the selector loop there (`pytest_asyncio_loop_factories`), and the entrypoints do the same for development. Without it every container test fails with `InterfaceError`.
> - `alembic.ini` needs `path_separator = os` or Alembic warns on every run.
> - The review found that nothing wired `EncryptedSerializer`: `setup()` only creates tables, and the worker built a plain saver, so a real `LANGGRAPH_AES_KEY` would have had no effect. `engine/db/checkpointer.py` now owns that choice and Task 7 uses it.
> - Verified against a real database: `prepare_database` is idempotent, deleting a workflow cascades to versions, runs, node_runs and events, a version referenced by a run cannot be deleted, the status check rejects unknown values, and the partial unique index dedupes idempotency keys while allowing many rows without one.
> - Known: `node_runs.meta`, `runs.updated_at` and `runs_waiting_idx` are used by later tasks but are not listed in the design's table; add them there when the design is next touched.
>
> Suite: 661.

---

## Task 4: PostgresRecorder and redaction

**Files:**

- Create: `services/engine/engine/events/__init__.py`, `redact.py`, `writer.py`, `recorder.py`
- Test: `services/engine/tests/test_events_redact.py`, `services/engine/tests/test_events_recorder.py`

- [ ]  **Step 1: Write the failing redaction test**

Create `services/engine/tests/test_events_redact.py`:

```python
from engine.events.redact import MAX_EVENT_PREVIEW_BYTES, MAX_STORED_BYTES, clip_json, redact


def test_values_under_secret_looking_keys_are_replaced():
    value = {"Authorization": "Bearer x", "api_key": "k", "nested": {"password": "p", "keep": 1},
             "list": [{"token": "t"}], "apikey": "a", "keep": "v"}

    assert redact(value) == {
        "Authorization": "[REDACTED]", "api_key": "[REDACTED]",
        "nested": {"password": "[REDACTED]", "keep": 1},
        "list": [{"token": "[REDACTED]"}], "apikey": "[REDACTED]", "keep": "v",
    }


def test_redaction_copies_and_never_shares_containers():
    value = {"a": {"b": [1]}}
    copied = redact(value)
    copied["a"]["b"].append(2)
    assert value == {"a": {"b": [1]}}


def test_clip_json_keeps_small_values_and_replaces_large_ones():
    small, truncated = clip_json({"text": "짧음"}, MAX_STORED_BYTES)
    assert (small, truncated) == ({"text": "짧음"}, False)

    big, truncated = clip_json({"text": "가" * 200_000}, MAX_EVENT_PREVIEW_BYTES)
    assert truncated and set(big) == {"_truncated"} and len(big["_truncated"]) < 2000
```

Run: `uv run pytest tests/test_events_redact.py -q` → FAIL (no module).

- [ ]  **Step 2: Implement redaction**

Create `services/engine/engine/events/__init__.py` (empty) and `services/engine/engine/events/redact.py`:

```python
"""What may be written to the observation tables (MVP design 10.1, 10.3).

Plan 2a does key-name redaction, which is all that can be done before secrets and `http_request` exist
(Plan 2b adds header-name and secret-value redaction).
"""
from __future__ import annotations

import json
import re
from typing import Any

from engine.jsondata import clip

REDACTED = "[REDACTED]"
SECRET_KEY = re.compile(r"(authorization|password|passwd|secret|token|api[_-]?key|cookie)", re.IGNORECASE)
MAX_STORED_BYTES = 256_000  # node_runs.input / node_runs.output
MAX_EVENT_PREVIEW_BYTES = 4_000  # run_events.payload previews


def redact(value: Any) -> Any:
    """A copy of `value` with every value under a secret-looking key replaced.

    Values reaching here are JSON data bounded by engine.jsondata.MAX_JSON_DEPTH, so plain recursion is safe.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if isinstance(key, str) and SECRET_KEY.search(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def clip_json(value: Any, limit: int) -> tuple[Any, bool]:
    """(stored value, truncated). Over the limit the shape changes to {"_truncated": "<start of the JSON>"},
    because a cut-off JSON value is not valid JSON and the editor must be able to tell."""
    text = json.dumps(value, ensure_ascii=False)
    if len(text.encode("utf-8")) <= limit:
        return value, False
    return {"_truncated": clip(text, limit // 8)}, True
```

Run: `uv run pytest tests/test_events_redact.py -q` → PASS.

- [ ]  **Step 3: Write the failing recorder tests**

Create `services/engine/tests/test_events_recorder.py`:

```python
import asyncio

import pytest

from engine.events.recorder import PostgresRecorder
from engine.nodes.base import Usage
from engine.runtime.recorder import DuplicateAttempt
from tests.factories import make_run  # added in this task


async def _records(pool, run_id: str) -> list[dict]:
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT node_id, exec_index, attempt, status, input, output, error, meta, waited, truncated,"
            " tokens_out FROM node_runs WHERE run_id=%s ORDER BY started_at, attempt", (run_id,)
        )).fetchall()


async def _events(pool, run_id: str) -> list[dict]:
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT seq, type, node_id, exec_index, attempt, payload FROM run_events"
            " WHERE run_id=%s ORDER BY seq", (run_id,)
        )).fetchall()


async def test_an_attempt_is_opened_closed_and_evented(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)

    await recorder.node_started("llm_1", 1, 1, {"prompt": "안녕"})
    await recorder.node_succeeded("llm_1", 1, 1, {"text": "답"}, Usage(3, 4), defaulted=False, meta={})

    [record] = await _records(pool, run_id)
    assert (record["status"], record["input"], record["output"]) == ("succeeded", {"prompt": "안녕"}, {"text": "답"})
    assert record["tokens_out"] == 4
    assert [(event["seq"], event["type"]) for event in await _events(pool, run_id)] == [
        (1, "node_started"), (2, "node_finished")
    ]


async def test_a_second_worker_opening_the_same_attempt_is_told_to_stop(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)
    await recorder.node_started("llm_1", 1, 1, None)

    with pytest.raises(DuplicateAttempt):
        await PostgresRecorder(pool, run_id).node_started("llm_1", 1, 1, None)


async def test_waiting_is_remembered_after_the_attempt_finishes(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)
    await recorder.node_started("human_approval_1", 1, 1, None)
    await recorder.node_waiting("human_approval_1", 1, 1, {"message": "검토"})

    assert await recorder.find_waiting("human_approval_1", 1) == 1
    await recorder.node_succeeded("human_approval_1", 1, 1, {"decision": "approve"}, Usage(),
                                  defaulted=False, meta={})
    assert await recorder.find_waiting("human_approval_1", 1) == 1  # still "ever waited"
    assert await recorder.attempts_so_far("human_approval_1", 1) == 1


async def test_closing_an_already_closed_attempt_is_allowed(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)
    await recorder.node_started("llm_1", 1, 1, None)
    await recorder.node_succeeded("llm_1", 1, 1, {"text": "답"}, Usage(), defaulted=False, meta={})

    await recorder.node_succeeded("llm_1", 1, 1, {"text": "답"}, Usage(), defaulted=False, meta={})

    assert len(await _records(pool, run_id)) == 1


async def test_event_numbers_are_gapless_when_two_recorders_write_at_once(pool):
    run_id = await make_run(pool)
    one, two = PostgresRecorder(pool, run_id), PostgresRecorder(pool, run_id)

    async def write(recorder, node_id):
        for index in range(1, 11):
            await recorder.node_started(node_id, index, 1, None)

    await asyncio.gather(write(one, "a"), write(two, "b"))

    assert [event["seq"] for event in await _events(pool, run_id)] == list(range(1, 21))


async def test_secrets_are_redacted_and_large_values_truncated(pool):
    run_id = await make_run(pool)
    recorder = PostgresRecorder(pool, run_id)

    await recorder.node_started("llm_1", 1, 1, {"headers": {"Authorization": "Bearer x"}})
    await recorder.node_succeeded("llm_1", 1, 1, {"text": "가" * 300_000}, Usage(), defaulted=False, meta={})

    [record] = await _records(pool, run_id)
    assert record["input"] == {"headers": {"Authorization": "[REDACTED]"}}
    assert record["truncated"] and set(record["output"]) == {"_truncated"}
    finished = (await _events(pool, run_id))[-1]
    assert len(str(finished["payload"]["outputPreview"])) < 2_000


async def test_store_run_data_false_keeps_metadata_only(pool):
    run_id = await make_run(pool, store_run_data=False)
    recorder = PostgresRecorder(pool, run_id, store_run_data=False)

    await recorder.node_started("llm_1", 1, 1, {"prompt": "비밀"})
    await recorder.node_succeeded("llm_1", 1, 1, {"text": "답"}, Usage(1, 2), defaulted=False, meta={})

    [record] = await _records(pool, run_id)
    assert (record["input"], record["output"], record["status"]) == (None, None, "succeeded")
    assert record["tokens_out"] == 2
    assert "outputPreview" not in ((await _events(pool, run_id))[-1]["payload"] or {})
```

Create `services/engine/tests/factories.py`:

```python
"""Row builders for tests that need a run to hang observations off."""
from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb

WORKSPACE = "00000000-0000-0000-0000-000000000001"


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
```

Run: `uv run pytest tests/test_events_recorder.py -q` → FAIL (no `engine.events.recorder`).

- [ ]  **Step 4: Event writing**

Create `services/engine/engine/events/writer.py`:

```python
"""Run-scoped event numbering (MVP design 7.2).

`seq` is allocated by incrementing `runs.event_seq` in the same transaction as the row that is being
written, so the API and the worker can both write events without colliding, and no number is skipped.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncCursor
from psycopg.types.json import Jsonb


class RunGone(Exception):
    """The run row disappeared under us (deleted workflow). Nothing more can be recorded for it."""


async def append_event(
    cursor: AsyncCursor,
    run_id: str,
    event_type: str,
    *,
    node_id: str | None = None,
    exec_index: int | None = None,
    attempt: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert one event and return it in wire form (spec 7.1) for publishing after the commit."""
    await cursor.execute("UPDATE runs SET event_seq = event_seq + 1 WHERE id = %s RETURNING event_seq", (run_id,))
    row = await cursor.fetchone()
    if row is None:
        raise RunGone(run_id)
    seq = row["event_seq"]
    await cursor.execute(
        "INSERT INTO run_events (run_id, seq, type, node_id, exec_index, attempt, payload)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (run_id, seq, event_type, node_id, exec_index, attempt, Jsonb(payload) if payload is not None else None),
    )
    return {
        "seq": seq,
        "runId": run_id,
        "type": event_type,
        "nodeId": node_id,
        "execIndex": exec_index,
        "attempt": attempt,
        "ts": datetime.now(UTC).isoformat(),
        "payload": payload or {},
    }
```

- [ ]  **Step 5: Implement the recorder**

Create `services/engine/engine/events/recorder.py`:

```python
"""Postgres implementation of the Plan 1 Recorder protocol (node_runs + run_events)."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from engine.events.redact import MAX_EVENT_PREVIEW_BYTES, MAX_STORED_BYTES, clip_json, redact
from engine.events.writer import append_event
from engine.nodes.base import Usage
from engine.runtime.recorder import DuplicateAttempt

log = logging.getLogger(__name__)


class RecorderInconsistent(RuntimeError):
    """A close arrived for an attempt that was never opened. The wrapper turns this into an EngineFault."""


class PostgresRecorder:
    """One instance per run. Writes are single transactions; publishing happens after the commit."""

    def __init__(self, pool: AsyncConnectionPool, run_id: str, *, publisher: Any = None,
                 store_run_data: bool = True) -> None:
        self._pool = pool
        self._run_id = run_id
        self._publisher = publisher
        self._store = store_run_data

    # ------------------------------------------------------------------ reads

    async def attempts_so_far(self, node_id: str, exec_index: int) -> int:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT count(*) AS n FROM node_runs WHERE run_id=%s AND node_id=%s AND exec_index=%s",
                (self._run_id, node_id, exec_index),
            )).fetchone()
        return int(row["n"])

    async def find_waiting(self, node_id: str, exec_index: int) -> int | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT max(attempt) AS attempt FROM node_runs"
                " WHERE run_id=%s AND node_id=%s AND exec_index=%s AND waited",
                (self._run_id, node_id, exec_index),
            )).fetchone()
        return row["attempt"]

    # ------------------------------------------------------------------ writes

    async def node_started(self, node_id: str, exec_index: int, attempt: int, input: dict[str, Any] | None) -> None:
        stored = self._value(input)
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            await cursor.execute(
                "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status, input)"
                " VALUES (%s, %s, %s, %s, %s, 'running', %s) ON CONFLICT DO NOTHING RETURNING id",
                (str(uuid.uuid4()), self._run_id, node_id, exec_index, attempt,
                 Jsonb(stored[0]) if stored[0] is not None else None),
            )
            if await cursor.fetchone() is None:
                # The unique key already holds this attempt: another worker is running the same run.
                raise DuplicateAttempt((node_id, exec_index, attempt))
            event = await append_event(cursor, self._run_id, "node_started", node_id=node_id,
                                       exec_index=exec_index, attempt=attempt)
        await self._publish(event)

    async def node_succeeded(self, node_id: str, exec_index: int, attempt: int, output: dict[str, Any],
                             usage: Usage, *, defaulted: bool, meta: dict[str, Any]) -> None:
        value, truncated = self._value(output)
        preview = self._preview(output)
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            await self._close(cursor, node_id, exec_index, attempt,
                              "defaulted" if defaulted else "succeeded",
                              output=value, truncated=truncated, usage=usage, meta=meta)
            payload = {"defaulted": defaulted, "tokensOut": usage.tokens_out, **meta}
            if preview is not None:
                payload["outputPreview"] = preview
            event = await append_event(cursor, self._run_id, "node_finished", node_id=node_id,
                                       exec_index=exec_index, attempt=attempt, payload=payload)
        await self._publish(event)

    async def node_failed(self, node_id: str, exec_index: int, attempt: int, error: dict[str, Any],
                          *, will_retry: bool) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            await self._close(cursor, node_id, exec_index, attempt, "failed", error=error)
            event = await append_event(cursor, self._run_id, "node_failed", node_id=node_id,
                                       exec_index=exec_index, attempt=attempt,
                                       payload={"error": error, "willRetry": will_retry})
        await self._publish(event)

    async def node_waiting(self, node_id: str, exec_index: int, attempt: int, payload: dict[str, Any]) -> None:
        stored, _ = self._value(payload)
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            await cursor.execute(
                "UPDATE node_runs SET status='waiting', waited=true, meta=%s"
                " WHERE run_id=%s AND node_id=%s AND exec_index=%s AND attempt=%s",
                (Jsonb({"waiting": stored}), self._run_id, node_id, exec_index, attempt),
            )
            if cursor.rowcount == 0:
                raise RecorderInconsistent((node_id, exec_index, attempt))
            event = await append_event(cursor, self._run_id, "node_waiting", node_id=node_id,
                                       exec_index=exec_index, attempt=attempt, payload=payload)
        await self._publish(event)

    async def node_token(self, node_id: str, exec_index: int, text: str) -> None:
        """Transient (spec 7.1): published for the live preview, never stored and never numbered."""
        await self._publish({"runId": self._run_id, "type": "node_token", "nodeId": node_id,
                             "execIndex": exec_index, "payload": {"text": text}})

    # ------------------------------------------------------------------ helpers

    async def _close(self, cursor, node_id: str, exec_index: int, attempt: int, status: str, *,
                     output: Any = None, truncated: bool = False, usage: Usage | None = None,
                     meta: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
        """Close one attempt. A replayed node closes a row that is already closed; that is not an error."""
        usage = usage or Usage()
        await cursor.execute(
            "UPDATE node_runs SET status=%s, output=%s, error=%s, meta=%s, tokens_in=%s, tokens_out=%s,"
            " truncated=%s, finished_at=now()"
            " WHERE run_id=%s AND node_id=%s AND exec_index=%s AND attempt=%s",
            (status, Jsonb(output) if output is not None else None,
             Jsonb(redact(error)) if error is not None else None,
             Jsonb(meta) if meta else None, usage.tokens_in, usage.tokens_out, truncated,
             self._run_id, node_id, exec_index, attempt),
        )
        if cursor.rowcount == 0:
            raise RecorderInconsistent((node_id, exec_index, attempt))

    def _value(self, value: Any) -> tuple[Any, bool]:
        if value is None or not self._store:
            return None, False
        return clip_json(redact(value), MAX_STORED_BYTES)

    def _preview(self, value: Any) -> Any:
        if not self._store:
            return None
        clipped, _ = clip_json(redact(value), MAX_EVENT_PREVIEW_BYTES)
        return clipped

    async def _publish(self, event: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        try:  # the event is already committed; losing the live copy only delays the editor (SSE gap fill)
            await self._publisher.publish(self._run_id, event)
        except Exception:  # noqa: BLE001
            log.warning("publishing event failed for run %s", self._run_id, exc_info=True)
```

Note `node_started` calls `self._value(input)` and unpacks it as `stored[0]`; keep one style — use
`stored, _ = self._value(input)` and pass `stored`.

- [ ]  **Step 6: Run the tests and commit**

Run: `uv run pytest tests/test_events_recorder.py tests/test_events_redact.py -q` → PASS.
Run: `uv run pytest -q -m "not integration"` → unchanged.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/events services/engine/tests/test_events_recorder.py \
        services/engine/tests/test_events_redact.py services/engine/tests/factories.py
git commit -m "feat(engine): record node runs and events in Postgres"
```

> **Post-review note (Task 4, as implemented):** commits `648071a` and `3bb8a49`.
>
> The review found that only `node_succeeded` put its body through redaction and the size cap. `node_failed` and `node_waiting` passed the caller's dict straight into `run_events.payload`, so a secret inside an error (Plan 2b's `http_request` will put response headers there) would have been stored unredacted and streamed over SSE, and a 1 MB approval payload would have been written whole. Fixed in `PostgresRecorder`:
>
> - One `_safe(value, limit=None)` helper: redact, `check_text`, then clip. Every stored value and every event body goes through it, so a value jsonb refuses raises `ValueError` here as it does in `InMemoryRecorder` instead of surfacing as an opaque psycopg error. `clip_json` also measures with `allow_nan=False`.
> - `node_failed`'s event keeps the error (it is the failure reason, and metadata even when run data is not stored) but redacted and clipped to the preview size.
> - `node_waiting` stores its payload in `node_runs.meta` regardless of `storeRunData` — without it the approval cannot be answered or displayed — while the event body follows the normal preview rule and is omitted when run data is not kept. That makes Task 15's Step 4 unnecessary.
> - A truncated input now sets `node_runs.truncated`, and closing an attempt never clears the flag.
>
> Conformance was checked by running the same call sequences and three real engine runs (success, retry, approval + resume) through both recorders and diffing every row and event: identical apart from the intended differences (redaction, `node_token` not persisted, event payload shape).
>
> Suite: 676.

---

## Task 5: Redis publishing and the per-model semaphore

**Files:**

- Create: `services/engine/engine/events/publish.py`
- Create: `services/engine/engine/llm/semaphore.py`
- Test: `services/engine/tests/test_events_publish.py`, `services/engine/tests/test_llm_semaphore.py`

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_events_publish.py`:

```python
import asyncio
import json

from engine.events.publish import RedisPublisher, control_messages


async def test_published_events_reach_a_subscriber(redis):
    publisher = RedisPublisher(redis)
    pubsub = redis.pubsub()
    await pubsub.subscribe("run:r1")

    await publisher.publish("r1", {"seq": 1, "type": "run_started"})

    message = await _next(pubsub)
    assert json.loads(message["data"]) == {"seq": 1, "type": "run_started"}
    await pubsub.aclose()


async def test_cancel_requests_go_to_one_control_channel(redis):
    publisher = RedisPublisher(redis)
    received: list[dict] = []

    async def listen():
        async for message in control_messages(redis):
            received.append(message)
            return

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.2)
    await publisher.request_cancel("r2")
    await asyncio.wait_for(task, 5)

    assert received == [{"runId": "r2", "action": "cancel"}]


async def _next(pubsub, timeout: float = 5.0) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message is not None:
                return message
```

Create `services/engine/tests/test_llm_semaphore.py`:

```python
import asyncio
import time

import pytest

from engine.llm.base import ChatMessage, ChatResult
from engine.llm.semaphore import ModelSemaphore, SemaphoreLLM


async def test_only_the_limit_may_hold_a_model_at_once(redis):
    semaphore = ModelSemaphore(redis, limit=2, ttl_sec=60)
    held = []

    async def hold(index: int):
        async with semaphore.slot("m"):
            held.append(index)
            await asyncio.sleep(0.3)

    task = asyncio.gather(hold(1), hold(2), hold(3))
    await asyncio.sleep(0.1)
    assert len(held) == 2  # the third waits
    await task
    assert len(held) == 3
    assert await redis.zcard("llm:sem:m") == 0  # every slot was returned


async def test_a_slot_held_by_a_dead_worker_is_reclaimed_after_the_ttl(redis):
    semaphore = ModelSemaphore(redis, limit=1, ttl_sec=1)
    await redis.zadd("llm:sem:m", {"dead-worker": time.time() - 10})

    async with asyncio.timeout(5):
        async with semaphore.slot("m"):
            assert await redis.zcard("llm:sem:m") == 1  # the stale entry was dropped


async def test_the_slot_is_returned_when_the_call_fails(redis):
    semaphore = ModelSemaphore(redis, limit=1, ttl_sec=60)

    class _Boom:
        async def chat(self, **kwargs):
            raise RuntimeError("model down")

    with pytest.raises(RuntimeError):
        await SemaphoreLLM(_Boom(), semaphore).chat(model="m", messages=[ChatMessage("user", "안녕")])

    assert await redis.zcard("llm:sem:m") == 0


async def test_the_wrapper_passes_the_call_through(redis):
    class _Echo:
        async def chat(self, *, model, messages, schema=None, temperature=0.7, on_token=None):
            return ChatResult(text=f"{model}:{messages[-1].content}")

    result = await SemaphoreLLM(_Echo(), ModelSemaphore(redis, limit=1)).chat(
        model="m", messages=[ChatMessage("user", "안녕")]
    )

    assert result.text == "m:안녕"
```

Run both files → FAIL (no modules).

- [ ]  **Step 2: Implement publishing**

Create `services/engine/engine/events/publish.py`:

```python
"""Redis transport for live events and run control (MVP design 3, 5.9).

Redis is never the source of truth: a lost event is filled in from `run_events` by the SSE endpoint, and a
lost cancel is noticed by the worker's next heartbeat.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

CONTROL_CHANNEL = "runs:control"


def run_channel(run_id: str) -> str:
    return f"run:{run_id}"


class RedisPublisher:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        await self._redis.publish(run_channel(run_id), json.dumps(event, ensure_ascii=False))

    async def request_cancel(self, run_id: str) -> None:
        await self._redis.publish(CONTROL_CHANNEL, json.dumps({"runId": run_id, "action": "cancel"}))


async def control_messages(redis: Any) -> AsyncIterator[dict[str, Any]]:
    """Every worker subscribes once and routes messages to the runs it holds."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(CONTROL_CHANNEL)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                yield json.loads(message["data"])
            except ValueError:  # someone else publishing on our channel
                continue
    finally:
        await pubsub.aclose()


async def run_events(redis: Any, run_id: str) -> AsyncIterator[dict[str, Any]]:
    """Live events for one run, for the SSE endpoint."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(run_channel(run_id))
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                yield json.loads(message["data"])
            except ValueError:
                continue
    finally:
        await pubsub.aclose()
```

- [ ]  **Step 3: Implement the semaphore**

Create `services/engine/engine/llm/semaphore.py`:

```python
"""Per-model concurrency limit shared by every worker (MVP design 6.4).

A sorted set per model holds one member per in-flight call, scored with the time it was taken. Entries older
than the TTL are dropped on every acquire, so a worker that dies never leaks a slot.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from engine.llm.base import ChatMessage, ChatResult, LLMClient, TokenSink

ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
  redis.call('PEXPIRE', KEYS[1], ARGV[5])
  return 1
end
return 0
"""


class ModelSemaphore:
    def __init__(self, redis: Any, *, limit: int = 1, ttl_sec: float = 120.0, poll_sec: float = 0.05) -> None:
        self._redis = redis
        self._limit = max(1, limit)
        self._ttl = ttl_sec
        self._poll = poll_sec
        self._acquire = redis.register_script(ACQUIRE)

    def key(self, model: str) -> str:
        return f"llm:sem:{model}"

    @contextlib.asynccontextmanager
    async def slot(self, model: str) -> AsyncIterator[None]:
        key, token = self.key(model), str(uuid.uuid4())
        delay = self._poll
        while not await self._try(key, token):
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1.0)  # back off, but keep waiting: the node's timeout bounds this
        refresher = asyncio.create_task(self._refresh(key, token))
        try:
            yield
        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
            with contextlib.suppress(Exception):
                await self._redis.zrem(key, token)

    async def _try(self, key: str, token: str) -> bool:
        now = time.time()
        result = await self._acquire(
            keys=[key], args=[now - self._ttl, self._limit, now, token, int(self._ttl * 2 * 1000)]
        )
        return bool(result)

    async def _refresh(self, key: str, token: str) -> None:
        """Keep a long call's slot alive; a dead worker stops refreshing and its slot expires."""
        while True:
            await asyncio.sleep(self._ttl / 4)
            with contextlib.suppress(Exception):
                await self._redis.zadd(key, {token: time.time()}, xx=True)


class SemaphoreLLM:
    """Wraps an LLMClient so every call holds a slot for its model."""

    def __init__(self, inner: LLMClient, semaphore: ModelSemaphore) -> None:
        self._inner = inner
        self._semaphore = semaphore

    async def chat(self, *, model: str, messages: list[ChatMessage], schema: dict[str, Any] | None = None,
                   temperature: float = 0.7, on_token: TokenSink | None = None) -> ChatResult:
        async with self._semaphore.slot(model):
            return await self._inner.chat(model=model, messages=messages, schema=schema,
                                          temperature=temperature, on_token=on_token)
```

- [ ]  **Step 4: Run the tests and commit**

Run: `uv run pytest tests/test_events_publish.py tests/test_llm_semaphore.py -q` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/events/publish.py services/engine/engine/llm/semaphore.py services/engine/tests
git commit -m "feat(engine): publish run events and limit LLM calls per model"
```

> **Post-review note (Task 5, as implemented):** commits `a70953f` and `82d13ea`.
>
> - The consumers accepted any JSON, so a stray `"42"` or `["cancel"]` published on `runs:control` or `run:{id}` was handed to the caller and would have crashed the worker's cancel routing and the SSE handler on the first `.get()`. `_decode` now drops anything that is not an object, and a test publishes junk on the channel before the real message.
> - Verified against a real Redis: the Lua acquire is atomic (20 acquirers, limit 3, never more than 3 in flight), the limit holds across two independent `ModelSemaphore` instances, and the slot comes back when the call raises, when the awaiting task is cancelled, and — for a worker that dies without releasing — when the TTL expires. The score refresher keeps a call longer than the TTL alive, and leaves no task behind.
>
> Suite: 684.

---

## Task 6: Run queries — claim, lease and transitions

Every statement here carries its own guard: the claim only touches a `queued` row, and every worker write
carries `AND lease_owner = :me` (MVP design 5.2 fencing).

**Files:**

- Create: `services/engine/engine/db/runs.py`
- Test: `services/engine/tests/test_db_runs.py`

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_db_runs.py`:

```python
import asyncio

from engine.db import runs as run_db
from tests.factories import make_run


async def _row(pool, run_id: str) -> dict:
    async with pool.connection() as conn:
        return await (await conn.execute("SELECT * FROM runs WHERE id=%s", (run_id,))).fetchone()


async def test_claiming_moves_one_queued_run_to_running(pool):
    run_id = await make_run(pool, status="queued")

    async with pool.connection() as conn:
        claimed = await run_db.claim_next(conn, owner="worker-1", lease_sec=30)

    assert claimed["id"] == run_id and claimed["status"] == "running"
    row = await _row(pool, run_id)
    assert row["lease_owner"] == "worker-1" and row["started_at"] is not None


async def test_two_workers_never_claim_the_same_run(pool):
    await make_run(pool, status="queued")

    async def claim(owner: str):
        async with pool.connection() as conn:
            return await run_db.claim_next(conn, owner=owner, lease_sec=30)

    first, second = await asyncio.gather(claim("worker-1"), claim("worker-2"))

    assert len([row for row in (first, second) if row is not None]) == 1


async def test_claiming_an_empty_queue_returns_nothing(pool):
    await make_run(pool, status="succeeded")
    async with pool.connection() as conn:
        assert await run_db.claim_next(conn, owner="worker-1", lease_sec=30) is None


async def test_the_heartbeat_extends_the_lease_and_reports_a_cancel(pool):
    run_id = await make_run(pool, status="queued")
    async with pool.connection() as conn:
        await run_db.claim_next(conn, owner="worker-1", lease_sec=1)
        await conn.execute("UPDATE runs SET cancel_requested_at=now() WHERE id=%s", (run_id,))

        beat = await run_db.heartbeat(conn, run_id=run_id, owner="worker-1", lease_sec=30, delta_ms=1000)
        stolen = await run_db.heartbeat(conn, run_id=run_id, owner="worker-2", lease_sec=30, delta_ms=1000)

    assert beat["cancel_requested_at"] is not None and beat["active_ms"] == 1000
    assert stolen is None  # a worker that lost the lease must find out


async def test_only_the_lease_owner_can_finish_a_run(pool):
    run_id = await make_run(pool, status="queued")
    async with pool.connection() as conn:
        await run_db.claim_next(conn, owner="worker-1", lease_sec=30)

        assert await run_db.finish(conn, run_id=run_id, owner="worker-2", status="succeeded") is False
        assert await run_db.finish(conn, run_id=run_id, owner="worker-1", status="succeeded",
                                   outputs={"final": "x"}) is True

    row = await _row(pool, run_id)
    assert (row["status"], row["outputs"], row["lease_owner"]) == ("succeeded", {"final": "x"}, None)
    assert row["finished_at"] is not None


async def test_finishing_can_drop_the_inputs_when_run_data_is_not_stored(pool):
    run_id = await make_run(pool, status="queued", store_run_data=False, inputs={"topic": "비밀"})
    async with pool.connection() as conn:
        await run_db.claim_next(conn, owner="worker-1", lease_sec=30)
        await run_db.finish(conn, run_id=run_id, owner="worker-1", status="succeeded", clear_inputs=True)

    assert (await _row(pool, run_id))["inputs"] is None


async def test_waiting_records_the_target_and_releases_the_lease(pool):
    run_id = await make_run(pool, status="queued")
    async with pool.connection() as conn:
        await run_db.claim_next(conn, owner="worker-1", lease_sec=30)
        assert await run_db.set_waiting(conn, run_id=run_id, owner="worker-1",
                                        node_id="human_approval_1", exec_index=1) is True

    row = await _row(pool, run_id)
    assert (row["status"], row["waiting_node_id"], row["waiting_exec_index"]) == ("waiting", "human_approval_1", 1)
    assert row["lease_owner"] is None and row["resume_payload"] is None


async def test_open_node_runs_are_closed_when_a_run_stops(pool):
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status)"
            " VALUES (gen_random_uuid(), %s, 'llm_1', 1, 1, 'running')", (run_id,))

        closed = await run_db.close_open_node_runs(conn, run_id, "cancelled")

        rows = await (await conn.execute("SELECT status, finished_at FROM node_runs WHERE run_id=%s",
                                         (run_id,))).fetchall()
    assert closed == 1 and rows[0]["status"] == "cancelled" and rows[0]["finished_at"] is not None


async def test_notify_wakes_a_listener(pool, listen_conn):
    await listen_conn.execute("LISTEN runs_queued")

    async with pool.connection() as conn:
        await run_db.notify_queued(conn)

    async with asyncio.timeout(5):
        async for _ in listen_conn.notifies(stop_after=1):
            pass
```

Run: `uv run pytest tests/test_db_runs.py -q` → FAIL (no module).

- [ ]  **Step 2: Implement the queries**

Create `services/engine/engine/db/runs.py`:

```python
"""Run rows: claiming, the lease, and the transitions a worker owns (MVP design 5.1, 5.2).

Every worker write carries `AND lease_owner = %(owner)s` so a worker that lost its lease writes nothing.
Callers manage transactions; these helpers never commit on their own.
"""
from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

QUEUE_CHANNEL = "runs_queued"


async def notify_queued(conn: AsyncConnection, run_id: str | None = None) -> None:
    """Wake a worker. Sent inside the transaction that queued the run, so it cannot fire for a rolled-back run."""
    await conn.execute("SELECT pg_notify(%s, %s)", (QUEUE_CHANNEL, run_id or ""))


async def claim_next(conn: AsyncConnection, *, owner: str, lease_sec: int) -> dict[str, Any] | None:
    """Take the oldest queued run, or None. This single statement is both the queue and the CAS claim."""
    row = await (await conn.execute(
        "UPDATE runs SET status='running', lease_owner=%(owner)s,"
        "   lease_expires_at=now() + make_interval(secs => %(lease)s),"
        "   started_at=coalesce(started_at, now()), updated_at=now()"
        " WHERE id = (SELECT id FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1"
        "             FOR UPDATE SKIP LOCKED)"
        " RETURNING *",
        {"owner": owner, "lease": lease_sec},
    )).fetchone()
    return row


async def heartbeat(conn: AsyncConnection, *, run_id: str, owner: str, lease_sec: int,
                    delta_ms: int) -> dict[str, Any] | None:
    """Extend the lease and add to the active time. None means the lease is gone: stop writing."""
    return await (await conn.execute(
        "UPDATE runs SET lease_expires_at=now() + make_interval(secs => %(lease)s),"
        "   active_ms = active_ms + %(delta)s, updated_at=now()"
        " WHERE id=%(id)s AND lease_owner=%(owner)s AND status='running'"
        " RETURNING cancel_requested_at, active_ms",
        {"id": run_id, "owner": owner, "lease": lease_sec, "delta": delta_ms},
    )).fetchone()


async def finish(conn: AsyncConnection, *, run_id: str, owner: str, status: str,
                 outputs: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
                 clear_inputs: bool = False) -> bool:
    """Terminal transition. False means this worker no longer owns the run and wrote nothing."""
    row = await (await conn.execute(
        "UPDATE runs SET status=%(status)s, outputs=%(outputs)s, error=%(error)s,"
        "   inputs = CASE WHEN %(clear)s THEN NULL ELSE inputs END,"
        "   lease_owner=NULL, lease_expires_at=NULL, resume_payload=NULL,"
        "   finished_at=now(), updated_at=now()"
        " WHERE id=%(id)s AND lease_owner=%(owner)s AND status='running' RETURNING id",
        {"id": run_id, "owner": owner, "status": status, "clear": clear_inputs,
         "outputs": Jsonb(outputs) if outputs is not None else None,
         "error": Jsonb(error) if error is not None else None},
    )).fetchone()
    return row is not None


async def set_waiting(conn: AsyncConnection, *, run_id: str, owner: str, node_id: str, exec_index: int) -> bool:
    """Park the run on an approval and release the lease; waiting costs no worker resources."""
    row = await (await conn.execute(
        "UPDATE runs SET status='waiting', waiting_node_id=%(node)s, waiting_exec_index=%(index)s,"
        "   resume_payload=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=now()"
        " WHERE id=%(id)s AND lease_owner=%(owner)s AND status='running' RETURNING id",
        {"id": run_id, "owner": owner, "node": node_id, "index": exec_index},
    )).fetchone()
    return row is not None


async def expire_lease(conn: AsyncConnection, *, run_id: str, owner: str) -> None:
    """Hand the run back for recovery after an engine fault: the reaper picks it up on its next pass."""
    await conn.execute(
        "UPDATE runs SET lease_expires_at=now(), updated_at=now() WHERE id=%s AND lease_owner=%s",
        (run_id, owner),
    )


async def clear_resume_payload(conn: AsyncConnection, run_id: str) -> None:
    """A stored answer must not survive the invocation that used it, whatever its outcome."""
    await conn.execute("UPDATE runs SET resume_payload=NULL WHERE id=%s", (run_id,))


async def close_open_node_runs(conn: AsyncConnection, run_id: str, status: str) -> int:
    """Close attempts left `running` by a crash, a cancel or a timeout, so the trace has no open rows."""
    cursor = await conn.execute(
        "UPDATE node_runs SET status=%s, finished_at=now() WHERE run_id=%s AND status='running'",
        (status, run_id),
    )
    return cursor.rowcount


async def get_run(conn: AsyncConnection, run_id: str) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM runs WHERE id=%s", (run_id,))).fetchone()


async def get_version_dsl(conn: AsyncConnection, version_id: str) -> dict[str, Any] | None:
    row = await (await conn.execute(
        "SELECT dsl, dsl_hash FROM workflow_versions WHERE id=%s", (version_id,)
    )).fetchone()
    return row
```

- [ ]  **Step 3: Run the tests and commit**

Run: `uv run pytest tests/test_db_runs.py -q` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/db/runs.py services/engine/tests/test_db_runs.py
git commit -m "feat(engine): add run claiming, lease and transition queries"
```

> **Post-review note (Task 6, as implemented):** commits `1f51dd5` and `d280909`.
>
> - The plan's first claim test compared psycopg's `uuid.UUID` to a `str` and could never pass; ids come back as UUID objects from `RETURNING *`, so every caller that sends one to a client must wrap it in `str()`.
> - The review showed the suite did not actually test the claim: deleting `FOR UPDATE SKIP LOCKED` left all 693 tests green while 8 claimers took 20 runs 67 times, because two coroutines on one pool are serialised by asyncio. A contention test now runs 8 claimers on independent connections and asserts each run is claimed exactly once; it fails without the clause.
> - `clear_resume_payload` was the one unfenced write to `runs`. A stalled worker could have cleared the answer a new owner was about to use, so the reviewer's approval would have been asked for twice. It now takes the owner and returns whether it applied, like its siblings. `expire_lease` reports the same way, and `finish` only accepts the three terminal statuses.
> - Verified: `NOTIFY` inside a rolled-back transaction delivers nothing and the same connection's committed one arrives; a non-owner changes no column in any helper; `close_open_node_runs` leaves finished attempts alone.
>
> Suite: 696.

---

## Task 7: The worker — claim, execute, finish

**Files:**

- Create: `services/engine/engine/worker/__init__.py`, `services/engine/engine/worker/worker.py`
- Modify: `services/engine/tests/conftest.py` (worker fixture, `until` helper)
- Test: `services/engine/tests/test_worker_run.py`

- [ ]  **Step 1: Test helpers**

Append to `services/engine/tests/conftest.py`:

```python
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
```

- [ ]  **Step 2: Write the failing test**

Create `services/engine/tests/test_worker_run.py`:

```python
from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.factories import make_run

CHAIN = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}},
                               "required": ["topic"]}}},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "{{ start.topic }} 요약"}},
        {"id": "end", "type": "end", "config": {"outputs": {"result": "{{ llm_1.text }}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"},
              {"id": "e2", "source": "llm_1", "target": "end"}],
}

BROKEN = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "안녕"},
         "policy": {"retry": {"maxAttempts": 1}}},
        {"id": "end", "type": "end"},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"},
              {"id": "e2", "source": "llm_1", "target": "end"}],
}


async def _status(pool, run_id: str):
    from engine.db import runs as run_db

    async def check():
        async with pool.connection() as conn:
            row = await run_db.get_run(conn, run_id)
        return row if row["status"] in ("succeeded", "failed", "cancelled", "waiting") else None

    return await until(check)


async def test_a_queued_run_is_picked_up_and_finished(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    await worker_factory(ScriptedLLM(["요약본"]))

    row = await _status(pool, run_id)

    assert (row["status"], row["outputs"]) == ("succeeded", {"result": "요약본"})
    assert row["lease_owner"] is None and row["finished_at"] is not None
    async with pool.connection() as conn:
        events = await (await conn.execute(
            "SELECT type FROM run_events WHERE run_id=%s ORDER BY seq", (run_id,))).fetchall()
    types = [event["type"] for event in events]
    assert types[0] == "run_started" and types[-1] == "run_succeeded"
    assert "node_finished" in types


async def test_a_node_failure_fails_the_run_with_its_error(pool, worker_factory):
    from engine.errors import ErrorCode, NodeError

    run_id = await make_run(pool, dsl=BROKEN, status="queued")
    await worker_factory(ScriptedLLM([NodeError(ErrorCode.LLM_UNAVAILABLE, "모델 없음", retryable=False)]))

    row = await _status(pool, run_id)

    assert row["status"] == "failed"
    assert (row["error"]["code"], row["error"]["nodeId"]) == ("LLM_UNAVAILABLE", "llm_1")


async def test_a_run_waiting_for_approval_releases_the_lease(pool, worker_factory):
    hitl = {
        "version": "1",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "human_approval_1", "type": "human_approval", "config": {"message": "승인할까요?"}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "human_approval_1"},
            {"id": "e2", "source": "human_approval_1", "sourceHandle": "approve", "target": "end"},
            {"id": "e3", "source": "human_approval_1", "sourceHandle": "reject", "target": "end"},
        ],
    }
    run_id = await make_run(pool, dsl=hitl, status="queued")
    await worker_factory(ScriptedLLM([]))

    row = await _status(pool, run_id)

    assert (row["status"], row["waiting_node_id"], row["waiting_exec_index"]) == ("waiting", "human_approval_1", 1)
    assert row["lease_owner"] is None


async def test_only_one_of_two_workers_runs_a_given_run(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    await worker_factory(ScriptedLLM(["요약본"]), owner="worker-1")
    await worker_factory(ScriptedLLM(["요약본"]), owner="worker-2")

    await _status(pool, run_id)

    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT node_id, attempt FROM node_runs WHERE run_id=%s AND node_id='llm_1'", (run_id,))).fetchall()
    assert len(rows) == 1  # the loser never opened an attempt
```

Run: `uv run pytest tests/test_worker_run.py -q` → FAIL (no `engine.worker.worker`).

- [ ]  **Step 3: Implement the worker**

Create `services/engine/engine/worker/__init__.py` (empty) and `services/engine/engine/worker/worker.py`:

```python
"""The worker: claim a run, drive the engine, write the terminal state (MVP design 5.2, 6.3)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from engine.compiler.build import CompiledWorkflow, WorkflowInvalid, compile_workflow
from engine.config import EngineConfig
from engine.db import runs as run_db
from engine.db.checkpointer import make_checkpointer
from engine.errors import EngineFault, ErrorCode, LeaseLost
from engine.events.publish import RedisPublisher
from engine.events.recorder import PostgresRecorder
from engine.events.writer import append_event
from engine.llm.base import LLMClient
from engine.nodes.registry import NodeRegistry, default_registry
from engine.runtime.deps import RunDeps
from engine.runtime.guard import FlagGuard
from engine.runtime.runner import ResumeRejected, RunOutcome, execute_run

log = logging.getLogger(__name__)


class Worker:
    """One process worth of execution. `owner` identifies this worker in `runs.lease_owner`."""

    def __init__(self, config: EngineConfig, pool: AsyncConnectionPool, redis: Any, *, llm: LLMClient,
                 owner: str | None = None, registry: NodeRegistry | None = None,
                 render: Any = None) -> None:
        self._config = config
        self._pool = pool
        self._redis = redis
        self._llm = llm
        self.owner = owner or f"worker-{uuid.uuid4()}"
        # One registry and one checkpointer per process: CompiledWorkflow is cached by dsl_hash alone.
        self._registry = registry or default_registry()
        self._render = render
        self._publisher = RedisPublisher(redis)
        self._checkpointer = make_checkpointer(pool, config)  # encrypted unless dev-insecure (design 5.3)
        self._compiled: dict[str, CompiledWorkflow] = {}
        self._guards: dict[str, FlagGuard] = {}
        self._tasks: set[asyncio.Task] = set()
        self._listen: AsyncConnection | None = None
        self._running = False
        self._idle = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._running = True
        self._listen = await AsyncConnection.connect(self._config.database_url, autocommit=True)
        await self._listen.execute(f"LISTEN {run_db.QUEUE_CHANNEL}")
        self._spawn(self._claim_loop())

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._listen is not None:
            await self._listen.close()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ claiming

    async def _claim_loop(self) -> None:
        in_flight: set[asyncio.Task] = set()
        while self._running:
            while self._running and len(in_flight) < self._config.worker_max_runs:
                async with self._pool.connection() as conn:
                    row = await run_db.claim_next(conn, owner=self.owner, lease_sec=self._config.lease_sec)
                if row is None:
                    break
                task = self._spawn(self._execute(row))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            await self._wait_for_work()

    async def _wait_for_work(self) -> None:
        """Sleep until something is queued or the poll interval elapses.

        The notification is an optimisation: it is sent inside the transaction that queued the run, and the
        poll covers the window where no worker was listening.
        """
        assert self._listen is not None
        try:
            async with asyncio.timeout(self._config.claim_poll_sec):
                async for _ in self._listen.notifies(stop_after=1):
                    return
        except TimeoutError:
            return
        except Exception:  # noqa: BLE001 - a dropped listener must not stop the worker
            log.warning("listen connection failed; reconnecting", exc_info=True)
            with contextlib.suppress(Exception):
                await self._listen.close()
            self._listen = await AsyncConnection.connect(self._config.database_url, autocommit=True)
            await self._listen.execute(f"LISTEN {run_db.QUEUE_CHANNEL}")

    # ------------------------------------------------------------------ one run

    async def _execute(self, row: dict[str, Any]) -> None:
        run_id = str(row["id"])
        guard = FlagGuard()
        self._guards[run_id] = guard
        try:
            compiled = await self._compile(row)
        except WorkflowInvalid as exc:  # a stored version must already be valid; treat it as a run failure
            await self._terminal(run_id, "failed", error={
                "code": str(ErrorCode.NODE_FAILED),
                "message": "저장된 워크플로를 실행할 수 없습니다",
                "details": [issue.to_dict() for issue in exc.issues[:5]],
            })
            return

        await self._event(run_id, "run_recovered" if row["recovery_count"] else "run_started")
        recorder = PostgresRecorder(self._pool, run_id, publisher=self._publisher,
                                    store_run_data=row["store_run_data"])
        deps = RunDeps(run_id=run_id, llm=self._llm, recorder=recorder, guard=guard, render=self._render)
        try:
            outcome = await execute_run(compiled, deps=deps, inputs=row["inputs"] or {},
                                        resume=row["resume_payload"])
        except LeaseLost:
            log.info("lease lost for run %s; leaving it to the new owner", run_id)
            return
        except ResumeRejected as exc:  # the API checks first, so this is a stale payload
            log.warning("resume rejected for run %s: %s", run_id, exc)
            async with self._pool.connection() as conn:
                await run_db.clear_resume_payload(conn, run_id=run_id, owner=self.owner)
            return
        except (EngineFault, Exception) as exc:  # noqa: BLE001 - infrastructure problem: let recovery retry
            log.error("run %s aborted: %s", run_id, type(exc).__name__, exc_info=True)
            async with self._pool.connection() as conn:
                await run_db.expire_lease(conn, run_id=run_id, owner=self.owner)
            return
        finally:
            self._guards.pop(run_id, None)

        await self._write_outcome(run_id, row, outcome)

    async def _write_outcome(self, run_id: str, row: dict[str, Any], outcome: RunOutcome) -> None:
        store = row["store_run_data"]
        if outcome.status == "waiting":
            waiting = outcome.waiting or {}
            async with self._pool.connection() as conn, conn.transaction():
                if await run_db.set_waiting(conn, run_id=run_id, owner=self.owner,
                                            node_id=waiting.get("nodeId", ""),
                                            exec_index=int(waiting.get("execIndex", 0))):
                    event = await append_event(conn.cursor(), run_id, "run_waiting", payload=waiting)
                else:
                    event = None
            if event:
                await self._publish(run_id, event)
            return
        await self._terminal(run_id, outcome.status, outputs=outcome.outputs, error=outcome.error,
                             clear_inputs=not store)

    async def _terminal(self, run_id: str, status: str, *, outputs: dict[str, Any] | None = None,
                        error: dict[str, Any] | None = None, clear_inputs: bool = False) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            owned = await run_db.finish(conn, run_id=run_id, owner=self.owner, status=status,
                                        outputs=outputs, error=error, clear_inputs=clear_inputs)
            if not owned:  # someone else owns the run now: write nothing at all
                return
            await run_db.close_open_node_runs(conn, run_id, "cancelled" if status == "cancelled" else "failed")
            event = await append_event(conn.cursor(), run_id, f"run_{status}",
                                       payload={"error": error} if error else {})
        await self._publish(run_id, event)

    async def _event(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            event = await append_event(conn.cursor(), run_id, event_type, payload=payload)
        await self._publish(run_id, event)

    async def _publish(self, run_id: str, event: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self._publisher.publish(run_id, event)

    async def _compile(self, row: dict[str, Any]) -> CompiledWorkflow:
        async with self._pool.connection() as conn:
            version = await run_db.get_version_dsl(conn, str(row["workflow_version_id"]))
        cached = self._compiled.get(version["dsl_hash"])
        if cached is None:
            cached = compile_workflow(version["dsl"], checkpointer=self._checkpointer, registry=self._registry)
            self._compiled[version["dsl_hash"]] = cached
        return cached
```

Note the `except (EngineFault, Exception)` line: write it as `except Exception` with a comment, since
`EngineFault` is an `Exception` — the implementer must not leave a redundant tuple.

- [ ]  **Step 4: Run the tests**

Run: `uv run pytest tests/test_worker_run.py -q`
Expected: PASS. If a run hangs, check that `start` awaited `LISTEN` on a separate connection — a pooled
connection cannot be used for notifications.

Run: `uv run ruff check .` → clean.

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/worker services/engine/tests/test_worker_run.py services/engine/tests/conftest.py
git commit -m "feat(engine): claim and execute runs in a worker"
```

---

## Task 8: Lease heartbeat, cancellation and the run time limit

**Files:**

- Modify: `services/engine/engine/worker/worker.py`
- Test: `services/engine/tests/test_worker_lease.py`

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_worker_lease.py`:

```python
import asyncio

from engine.db import runs as run_db
from engine.events.publish import RedisPublisher
from tests.conftest import until
from tests.factories import make_run
from tests.test_worker_run import CHAIN


async def _run_row(pool, run_id):
    async with pool.connection() as conn:
        return await run_db.get_run(conn, run_id)


class _SlowLLM:
    """Blocks inside the node so the test can interfere while the run is in flight."""

    def __init__(self, seconds: float = 30.0) -> None:
        self.started = asyncio.Event()
        self._seconds = seconds

    async def chat(self, **kwargs):
        self.started.set()
        await asyncio.sleep(self._seconds)
        raise AssertionError("should have been cancelled")


async def test_a_cancel_request_stops_a_running_run(pool, redis, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM()
    await worker_factory(llm)
    await asyncio.wait_for(llm.started.wait(), 10)

    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET cancel_requested_at=now() WHERE id=%s", (run_id,))
    await RedisPublisher(redis).request_cancel(run_id)

    row = await until(lambda: _finished(pool, run_id))
    assert row["status"] == "cancelled"
    async with pool.connection() as conn:
        open_rows = await (await conn.execute(
            "SELECT count(*) AS n FROM node_runs WHERE run_id=%s AND status='running'", (run_id,))).fetchone()
    assert open_rows["n"] == 0


async def test_a_worker_that_lost_its_lease_writes_nothing(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM(seconds=1.0)
    await worker_factory(llm, lease_sec=1)
    await asyncio.wait_for(llm.started.wait(), 10)

    async with pool.connection() as conn:  # another worker steals the run
        await conn.execute("UPDATE runs SET lease_owner='thief', lease_expires_at=now() + interval '1 hour'"
                           " WHERE id=%s", (run_id,))

    await asyncio.sleep(1.5)
    row = await _run_row(pool, run_id)
    assert (row["status"], row["lease_owner"]) == ("running", "thief")  # untouched by the old owner


async def test_a_run_over_its_active_time_limit_fails(pool, worker_factory):
    run_id = await make_run(pool, dsl=CHAIN, status="queued", inputs={"topic": "AI"})
    llm = _SlowLLM()
    await worker_factory(llm, run_max_active_ms=300)
    await asyncio.wait_for(llm.started.wait(), 10)

    row = await until(lambda: _finished(pool, run_id))

    assert (row["status"], row["error"]["code"]) == ("failed", "RUN_TIMEOUT")


async def _finished(pool, run_id):
    row = await _run_row(pool, run_id)
    return row if row["status"] in ("succeeded", "failed", "cancelled") else None
```

Add `RUN_TIMEOUT` to `engine/errors.py`'s `ErrorCode` if it is not there yet (MVP design 8.2 lists it).

Run: `uv run pytest tests/test_worker_lease.py -q` → FAIL.

- [ ]  **Step 2: Heartbeat and control subscription**

In `services/engine/engine/worker/worker.py` add to `start()`:

```python
        self._spawn(self._control_loop())
```

and implement:

```python
    async def _control_loop(self) -> None:
        """One subscription per worker; messages are routed to the guard of a run we hold."""
        from engine.events.publish import control_messages

        while self._running:
            try:
                async for message in control_messages(self._redis):
                    guard = self._guards.get(str(message.get("runId")))
                    if guard is not None and message.get("action") == "cancel":
                        guard.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - Redis is a convenience; the heartbeat also sees cancels
                log.warning("control channel dropped; resubscribing", exc_info=True)
                await asyncio.sleep(1)

    async def _heartbeat(self, run_id: str, guard: FlagGuard, task: asyncio.Task) -> None:
        """Extend the lease, notice cancels, and stop a run that used up its active time."""
        interval = self._config.heartbeat_sec
        while True:
            await asyncio.sleep(interval)
            async with self._pool.connection() as conn:
                beat = await run_db.heartbeat(conn, run_id=run_id, owner=self.owner,
                                              lease_sec=self._config.lease_sec,
                                              delta_ms=int(interval * 1000))
            if beat is None:  # the lease is gone: stop the run without writing anything
                guard.lose_lease()
                return
            if beat["cancel_requested_at"] is not None:
                guard.cancel()
            if beat["active_ms"] > self._config.run_max_active_ms:
                self._timed_out.add(run_id)
                task.cancel()
                return
```

Track timeouts with `self._timed_out: set[str] = set()` in `__init__`.

- [ ]  **Step 3: Wire the heartbeat into `_execute`**

Wrap the `execute_run` call:

```python
        task = asyncio.current_task()
        beat = self._spawn(self._heartbeat(run_id, guard, task))
        try:
            outcome = await execute_run(...)
        except asyncio.CancelledError:
            if run_id not in self._timed_out:
                raise  # a real shutdown
            self._timed_out.discard(run_id)
            await self._terminal(run_id, "failed", error={
                "code": str(ErrorCode.RUN_TIMEOUT), "message": "실행 시간이 한도를 넘었습니다"
            })
            return
        ...
        finally:
            beat.cancel()
            self._guards.pop(run_id, None)
```

`RunCancelled` raised by the guard reaches `execute_run`, which returns `RunOutcome("cancelled")`, so the
cancel path needs no special handling here beyond `_terminal` closing open `node_runs` rows.

- [ ]  **Step 4: Run the tests and commit**

Run: `uv run pytest tests/test_worker_lease.py tests/test_worker_run.py -q` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/worker/worker.py services/engine/engine/errors.py services/engine/tests/test_worker_lease.py
git commit -m "feat(engine): hold the run lease, honour cancels and bound active time"
```

---

## Task 9: The reaper

**Files:**

- Create: `services/engine/engine/worker/reaper.py`
- Modify: `services/engine/engine/errors.py` (add `ENGINE_RECOVERY_EXHAUSTED`)
- Modify: `services/engine/engine/worker/worker.py` (always write `run_started`; the reaper owns `run_recovered`)
- Test: `services/engine/tests/test_worker_reaper.py`

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_worker_reaper.py`:

```python
import asyncio

from engine.db import runs as run_db
from engine.worker.reaper import Reaper
from tests.factories import make_run


async def _claimed(pool, *, recovery_count: int = 0, cancel: bool = False) -> str:
    """A run that a dead worker left behind: running, lease already expired."""
    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET lease_owner='dead', lease_expires_at=now() - interval '1 minute',"
            " recovery_count=%s, cancel_requested_at = CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s",
            (recovery_count, cancel, run_id),
        )
        await conn.execute(
            "INSERT INTO node_runs (id, run_id, node_id, exec_index, attempt, status)"
            " VALUES (gen_random_uuid(), %s, 'llm_1', 1, 1, 'running')", (run_id,))
    return run_id


async def _row(pool, run_id):
    async with pool.connection() as conn:
        return await run_db.get_run(conn, run_id)


async def test_an_expired_lease_is_requeued_and_counted(pool, redis):
    run_id = await _claimed(pool)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert (row["status"], row["recovery_count"], row["lease_owner"]) == ("queued", 1, None)
    async with pool.connection() as conn:
        events = await (await conn.execute("SELECT type FROM run_events WHERE run_id=%s", (run_id,))).fetchall()
    assert [event["type"] for event in events] == ["run_recovered"]


async def test_too_many_recoveries_fail_the_run(pool, redis):
    run_id = await _claimed(pool, recovery_count=3)

    await Reaper(_config(), pool, redis).sweep()

    row = await _row(pool, run_id)
    assert (row["status"], row["error"]["code"]) == ("failed", "ENGINE_RECOVERY_EXHAUSTED")
    async with pool.connection() as conn:
        node_run = await (await conn.execute(
            "SELECT status FROM node_runs WHERE run_id=%s", (run_id,))).fetchone()
    assert node_run["status"] == "failed"  # no attempt is left open


async def test_an_expired_lease_with_a_cancel_request_ends_cancelled(pool, redis):
    run_id = await _claimed(pool, cancel=True)

    await Reaper(_config(), pool, redis).sweep()

    assert (await _row(pool, run_id))["status"] == "cancelled"


async def test_an_approval_waiting_too_long_is_cancelled(pool, redis):
    run_id = await make_run(pool, status="waiting")
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET updated_at = now() - interval '31 days' WHERE id=%s", (run_id,))

    await Reaper(_config(), pool, redis).sweep()

    assert (await _row(pool, run_id))["status"] == "cancelled"


async def test_only_one_reaper_sweeps_at_a_time(pool, redis):
    await _claimed(pool)
    first, second = Reaper(_config(), pool, redis), Reaper(_config(), pool, redis)

    async def hold():
        async with first.exclusive() as acquired:
            assert acquired
            await asyncio.sleep(0.5)

    task = asyncio.create_task(hold())
    await asyncio.sleep(0.1)
    async with second.exclusive() as acquired:
        assert acquired is False
    await task


def _config():
    import dataclasses

    from engine.config import load_config

    return dataclasses.replace(load_config(), reaper_interval_sec=0.1)
```

Run: `uv run pytest tests/test_worker_reaper.py -q` → FAIL.

- [ ]  **Step 2: Implement the reaper**

Add `ENGINE_RECOVERY_EXHAUSTED = "ENGINE_RECOVERY_EXHAUSTED"` to `ErrorCode` in `engine/errors.py`.

Create `services/engine/engine/worker/reaper.py`:

```python
"""Recovers runs whose worker died, and expires approvals nobody answered (MVP design 5.2).

Runs inside every worker, but a Postgres advisory lock lets only one of them sweep at a time.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from psycopg_pool import AsyncConnectionPool

from engine.config import EngineConfig
from engine.db import runs as run_db
from engine.errors import ErrorCode
from engine.events.publish import RedisPublisher
from engine.events.writer import append_event

log = logging.getLogger(__name__)

LOCK_KEY = 8_273_441_002  # arbitrary, stable: identifies "the reaper" among advisory locks
MAX_RECOVERIES = 3
WAITING_MAX_DAYS = 30
BATCH = 50


class Reaper:
    def __init__(self, config: EngineConfig, pool: AsyncConnectionPool, redis: Any) -> None:
        self._config = config
        self._pool = pool
        self._publisher = RedisPublisher(redis)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.reaper_interval_sec)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed sweep must not stop the worker
                log.warning("reaper sweep failed", exc_info=True)

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[bool]:
        """Session-level advisory lock so several workers do not recover the same runs."""
        async with self._pool.connection() as conn:
            row = await (await conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,))).fetchone()
            acquired = bool(row["ok"])
            try:
                yield acquired
            finally:
                if acquired:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))

    async def sweep(self) -> None:
        async with self.exclusive() as acquired:
            if not acquired:
                return
            await self._recover_expired()
            await self._expire_waiting()

    async def _recover_expired(self) -> None:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id, recovery_count, cancel_requested_at FROM runs"
                " WHERE status='running' AND lease_expires_at < now() ORDER BY lease_expires_at LIMIT %s",
                (BATCH,),
            )).fetchall()
        for row in rows:
            await self._recover_one(str(row["id"]), row)

    async def _recover_one(self, run_id: str, row: dict[str, Any]) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cursor = conn.cursor()
            if row["cancel_requested_at"] is not None:
                changed = await self._take(cursor, run_id, "cancelled", finished=True)
                event_type, payload = "run_cancelled", {}
            elif row["recovery_count"] >= MAX_RECOVERIES:
                error = {"code": str(ErrorCode.ENGINE_RECOVERY_EXHAUSTED),
                         "message": "실행을 여러 번 복구했지만 끝내지 못했습니다"}
                changed = await self._take(cursor, run_id, "failed", finished=True, error=error)
                event_type, payload = "run_failed", {"error": error}
            else:
                changed = await self._take(cursor, run_id, "queued", finished=False)
                event_type = "run_recovered"
                payload = {"recoveryCount": row["recovery_count"] + 1}
            if not changed:  # a worker renewed the lease between our select and update
                return
            await run_db.close_open_node_runs(
                conn, run_id, "cancelled" if event_type == "run_cancelled" else "failed"
            )
            if event_type == "run_recovered":
                await run_db.notify_queued(conn, run_id)
            event = await append_event(cursor, run_id, event_type, payload=payload)
        with contextlib.suppress(Exception):
            await self._publisher.publish(run_id, event)

    async def _take(self, cursor, run_id: str, status: str, *, finished: bool,
                    error: dict[str, Any] | None = None) -> bool:
        from psycopg.types.json import Jsonb

        await cursor.execute(
            "UPDATE runs SET status=%(status)s, lease_owner=NULL, lease_expires_at=NULL,"
            "   recovery_count = recovery_count + CASE WHEN %(status)s='queued' THEN 1 ELSE 0 END,"
            "   error = COALESCE(%(error)s, error),"
            "   finished_at = CASE WHEN %(finished)s THEN now() ELSE finished_at END, updated_at=now()"
            " WHERE id=%(id)s AND status='running' AND lease_expires_at < now() RETURNING id",
            {"id": run_id, "status": status, "finished": finished,
             "error": Jsonb(error) if error else None},
        )
        return await cursor.fetchone() is not None

    async def _expire_waiting(self) -> None:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id FROM runs WHERE status='waiting'"
                " AND updated_at < now() - make_interval(days => %s) LIMIT %s",
                (WAITING_MAX_DAYS, BATCH),
            )).fetchall()
        for row in rows:
            run_id = str(row["id"])
            async with self._pool.connection() as conn, conn.transaction():
                cursor = conn.cursor()
                await cursor.execute(
                    "UPDATE runs SET status='cancelled', finished_at=now(), updated_at=now()"
                    " WHERE id=%s AND status='waiting' RETURNING id", (run_id,))
                if await cursor.fetchone() is None:
                    continue
                await run_db.close_open_node_runs(conn, run_id, "cancelled")
                event = await append_event(cursor, run_id, "run_cancelled",
                                           payload={"reason": "waiting_expired"})
            with contextlib.suppress(Exception):
                await self._publisher.publish(run_id, event)
```

- [ ]  **Step 3: Run the reaper inside the worker, and always write `run_started`**

The design puts the reaper in the worker process, so `Worker` owns one. In `engine/worker/worker.py`:

```python
        self._reaper = Reaper(config, pool, redis)          # in __init__
        ...
        await self._reaper.start()                          # at the end of start()
        ...
        await self._reaper.stop()                           # at the beginning of stop()
```

Every worker has a reaper; the advisory lock decides which one actually sweeps.

In `engine/worker/worker.py` replace

```python
        await self._event(run_id, "run_recovered" if row["recovery_count"] else "run_started")
```

with

```python
        await self._event(run_id, "run_started", payload={"recoveryCount": row["recovery_count"]})
```

The reaper owns `run_recovered`: it is the event for the act of recovering, and every attempt to run still
starts with `run_started`.

- [ ]  **Step 4: Run the tests and commit**

Run: `uv run pytest tests/test_worker_reaper.py tests/test_worker_run.py -q` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/worker services/engine/engine/errors.py services/engine/tests/test_worker_reaper.py
git commit -m "feat(engine): recover abandoned runs and expire stale approvals"
```

---

## Task 10: Render pool and the worker entrypoint

**Files:**

- Modify: `services/engine/engine/compiler/wrapper.py` (rename `_render` → `render_fields`)
- Create: `services/engine/engine/worker/render.py`, `services/engine/engine/worker/main.py`
- Test: `services/engine/tests/test_worker_render.py`

- [ ]  **Step 1: Make the pure renderer public**

In `engine/compiler/wrapper.py` rename `_render` to `render_fields` (it is now called from another process)
and update the two call sites. No behaviour change.

- [ ]  **Step 2: Write the failing test**

Create `services/engine/tests/test_worker_render.py`:

```python
import asyncio
import time

import pytest

from engine.dsl.types import Target
from engine.nodes.base import TemplateField
from engine.worker.render import RenderPool

FIELDS = [TemplateField("prompt", "{{ start.topic }} 요약", "string")]
SLOW = [TemplateField("prompt", "{% for row in start.rows %}{{ start.rows | tojson | length }}{% endfor %}",
                      "string")]


async def test_rendering_happens_in_the_pool():
    pool = RenderPool(size=1, timeout=10)
    try:
        assert await pool(FIELDS, {"start": {"topic": "AI"}}) == {"prompt": "AI 요약"}
    finally:
        pool.close()


async def test_a_slow_template_hits_the_deadline_and_the_pool_survives():
    pool = RenderPool(size=1, timeout=0.5)
    data = {"start": {"rows": list(range(20_000))}}
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await pool(SLOW, data)
        assert time.monotonic() - started < 5  # killed, not waited out

        assert await pool(FIELDS, {"start": {"topic": "AI"}}) == {"prompt": "AI 요약"}
    finally:
        pool.close()


async def test_the_event_loop_keeps_running_during_a_render():
    pool = RenderPool(size=2, timeout=5)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        await pool(SLOW, {"start": {"rows": list(range(3_000))}})
    except TimeoutError:
        pass
    finally:
        ticker.cancel()
        pool.close()

    assert ticks > 5  # the loop was not blocked by the render


async def test_template_errors_come_back_as_errors():
    from engine.templates.render import TemplateRenderError

    pool = RenderPool(size=1, timeout=5)
    try:
        with pytest.raises(TemplateRenderError):
            await pool([TemplateField("prompt", "{{ start.missing }}", "string")], {"start": {}})
    finally:
        pool.close()
```

Run: `uv run pytest tests/test_worker_render.py -q` → FAIL.

- [ ]  **Step 3: Implement the pool**

Create `services/engine/engine/worker/render.py`:

```python
"""Template rendering off the event loop, with a deadline (design 4).

Rendering is the one piece of tenant-controlled CPU work the engine cannot bound: a single loop over a big
value costs O(data squared) with almost no output. A thread would keep the loop responsive but could not be
stopped, so rendering runs in a process that is killed when it overruns.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pebble import ProcessPool

from engine.compiler.wrapper import render_fields
from engine.nodes.base import TemplateField

log = logging.getLogger(__name__)


def _init(memory_limit_mb: int | None) -> None:
    if not memory_limit_mb:
        return
    try:  # POSIX only: containers get the limit, Windows development does not
        import resource

        limit = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:  # noqa: BLE001
        pass


def _job(fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
    return render_fields(fields, outputs)


class RenderPool:
    """Matches engine.runtime.deps.RenderFn, so it can be handed to RunDeps.render."""

    def __init__(self, *, size: int = 2, timeout: float = 5.0, memory_limit_mb: int | None = 1024) -> None:
        self._timeout = timeout
        self._pool = ProcessPool(max_workers=max(1, size), initializer=_init, initargs=(memory_limit_mb,))

    async def __call__(self, fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
        future = self._pool.schedule(_job, args=(list(fields), outputs), timeout=self._timeout)
        wrapped = asyncio.wrap_future(future)
        try:
            return await wrapped
        except asyncio.CancelledError:
            future.cancel()  # the node was cancelled: stop the render too
            raise

    def close(self) -> None:
        self._pool.stop()
        self._pool.join(timeout=5)
```

`pebble` raises `concurrent.futures.TimeoutError`, which is `TimeoutError` on Python 3.11+, so the node
wrapper's deadline mapping (Task 2) applies unchanged. Template errors pickle back as themselves because
they carry only a message.

- [ ]  **Step 4: The worker entrypoint**

Create `services/engine/engine/worker/main.py`:

```python
"""`python -m engine.worker.main`: one worker process."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from redis.asyncio import Redis

from engine.config import load_config
from engine.db.migrate import prepare_database
from engine.db.pool import make_pool
from engine.llm.gateway import LLMGateway
from engine.llm.ollama import OllamaRaw
from engine.llm.semaphore import ModelSemaphore, SemaphoreLLM
from engine.worker.render import RenderPool
from engine.worker.worker import Worker

log = logging.getLogger(__name__)


def _windows_selector_loop() -> None:
    """psycopg's async connections cannot use Windows' default ProactorEventLoop (development only)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config()
    if not config.encrypt_checkpoints:
        log.warning("checkpoints are stored unencrypted (ENGINE_DEV_INSECURE=1)")
    await prepare_database(config.database_url)

    pool = make_pool(config.database_url, max_size=config.worker_max_runs + 4)
    await pool.open(wait=True)
    redis = Redis.from_url(config.redis_url, decode_responses=True)
    raw = OllamaRaw(config.ollama_base_url)
    llm = SemaphoreLLM(LLMGateway(raw), ModelSemaphore(redis, limit=config.ollama_num_parallel))
    render = RenderPool(size=config.render_pool_size, timeout=config.render_timeout_sec)
    worker = Worker(config, pool, redis, llm=llm, render=render)  # the worker runs its own reaper

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(AttributeError, NotImplementedError):
            loop.add_signal_handler(getattr(signal, name), stop.set)

    await worker.start()
    log.info("worker %s ready", worker.owner)
    await stop.wait()

    await worker.stop()
    render.close()
    await raw.aclose()
    await redis.aclose()
    await pool.close()


if __name__ == "__main__":
    _windows_selector_loop()  # call before asyncio.run, and pass --loop asyncio to uvicorn on Windows
    asyncio.run(run())
```

The checkpointer comes from `make_checkpointer(pool, config)` inside `Worker`; it must see an autocommit
`make_pool` provides.

- [ ]  **Step 5: Run the tests and commit**

Run: `uv run pytest tests/test_worker_render.py -q` → PASS (no containers needed).
Run: `uv run pytest -q` → the whole suite, including integration, passes.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/worker services/engine/engine/compiler/wrapper.py services/engine/tests/test_worker_render.py
git commit -m "feat(engine): render templates in a process pool and add the worker entrypoint"
```

---

## Task 11: API skeleton — app, auth, strict bodies, errors, node types

**Files:**

- Create: `services/engine/engine/api/__init__.py`, `errors.py`, `security.py`, `body.py`, `app.py`
- Create: `services/engine/engine/api/routers/__init__.py`, `routers/node_types.py`
- Modify: `services/engine/tests/conftest.py` (api client fixture)
- Test: `services/engine/tests/test_api_basics.py`

- [ ]  **Step 1: The API client fixture**

Append to `services/engine/tests/conftest.py`:

```python
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
```

- [ ]  **Step 2: Write the failing tests**

Create `services/engine/tests/test_api_basics.py`:

```python
async def test_healthz_reports_both_backends(api):
    response = await api.get("/healthz")

    assert response.status_code == 200 and response.json() == {"status": "ok", "db": True, "redis": True}


async def test_a_request_without_the_token_is_rejected(api):
    response = await api.get("/node-types", headers={"Authorization": ""})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_node_types_describe_the_registry(api):
    response = await api.get("/node-types")

    types = {item["type"]: item for item in response.json()["nodeTypes"]}
    assert set(types) == {"start", "end", "template", "llm", "classifier", "condition", "merge",
                          "human_approval"}
    assert types["llm"]["configSchema"]["properties"]["prompt"]["x-template"] is True
    assert types["llm"]["defaultPolicy"]["retry"]["maxAttempts"] == 3
    assert types["condition"]["isBranch"] is True and types["start"]["defaultPolicy"] is None


async def test_an_unknown_path_uses_the_error_format(api):
    response = await api.get("/nope")

    assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"
```

The body rules (strict JSON, size limit) are tested in Task 12, where the first POST route exists.

Run: `uv run pytest tests/test_api_basics.py -q` → FAIL.

- [ ]  **Step 3: Errors**

Create `services/engine/engine/api/__init__.py` (empty) and `services/engine/engine/api/errors.py`:

```python
"""One error shape for the whole API (MVP design 8.2)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "REQUEST_ERROR"
        return JSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(_body("REQUEST_ERROR", "요청 형식이 올바르지 않습니다"), status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", type(exc).__name__)
        # Never leak internals to a tenant: the details are in the service log.
        return JSONResponse(_body("INTERNAL", "서버 오류가 발생했습니다"), status_code=500)
```

- [ ]  **Step 4: Auth and bodies**

Create `services/engine/engine/api/security.py`:

```python
"""Shared-token access control. MVP has no accounts (design 8.1); this keeps an exposed port from being open."""
from __future__ import annotations

import hmac

from fastapi import Request

from engine.api.errors import ApiError


def require_token(request: Request) -> None:
    expected = request.app.state.config.api_token
    if not expected:
        return  # development: no token configured
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(value, expected):
        raise ApiError(401, "UNAUTHORIZED", "인증 토큰이 필요합니다")
```

Create `services/engine/engine/api/body.py`:

```python
"""Request bodies are untrusted input: bounded in size and parsed strictly (design 8.1)."""
from __future__ import annotations

from typing import Any

from fastapi import Request

from engine.api.errors import ApiError
from engine.jsondata import parse_json


async def read_json(request: Request) -> Any:
    limit = request.app.state.config.max_body_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"요청이 너무 큽니다 (최대 {limit // 1024}KB)")
    raw = await request.body()
    if len(raw) > limit:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"요청이 너무 큽니다 (최대 {limit // 1024}KB)")
    try:
        # json.loads accepts NaN and duplicate keys; the engine's parser does not.
        return parse_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(400, "INVALID_JSON", f"JSON을 읽을 수 없습니다: {exc}") from None


def require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(400, "INVALID_JSON", "요청 본문은 객체여야 합니다")
    return value


def field(body: dict[str, Any], name: str, kind: type, *, required: bool = True, default: Any = None) -> Any:
    if name not in body:
        if required:
            raise ApiError(422, "REQUEST_ERROR", f"{name}이(가) 필요합니다")
        return default
    value = body[name]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ApiError(422, "REQUEST_ERROR", f"{name}의 형식이 올바르지 않습니다")
    return value
```

- [ ]  **Step 5: The app and `/node-types`**

Create `services/engine/engine/api/routers/__init__.py` (empty) and `routers/node_types.py`:

```python
"""Node palette for the editor (MVP design 4.6): the Python spec is the single source of truth."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/node-types")
async def node_types(request: Request) -> dict:
    registry = request.app.state.registry
    return {"nodeTypes": [
        {
            "type": spec.type,
            "label": spec.label,
            "category": spec.category,
            "isBranch": spec.is_branch,
            "sideEffects": spec.side_effects,
            "configSchema": spec.Config.model_json_schema(),
            "defaultPolicy": spec.default_policy.model_dump() if spec.default_policy else None,
        }
        for spec in registry.all()
    ]}
```

Create `services/engine/engine/api/app.py`:

```python
"""FastAPI application. API and worker share one process-wide node registry (design 8.3)."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI
from psycopg_pool import AsyncConnectionPool

from engine.api import errors
from engine.api.routers import node_types
from engine.api.security import require_token
from engine.config import EngineConfig
from engine.nodes.registry import NodeRegistry, default_registry


def create_app(config: EngineConfig, pool: AsyncConnectionPool, redis: Any,
               registry: NodeRegistry | None = None) -> FastAPI:
    app = FastAPI(title="Agentic Workflow Engine", dependencies=[Depends(require_token)])
    app.state.config = config
    app.state.pool = pool
    app.state.redis = redis
    app.state.registry = registry or default_registry()
    errors.install(app)
    app.include_router(node_types.router)

    @app.get("/healthz", dependencies=[])
    async def healthz() -> dict:
        db = redis_ok = False
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
            db = True
        except Exception:  # noqa: BLE001
            db = False
        try:
            redis_ok = bool(await redis.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False
        return {"status": "ok" if db and redis_ok else "degraded", "db": db, "redis": redis_ok}

    return app
```

`/healthz` keeps the app-wide token dependency (it is cheap and the editor never calls it anonymously); if a
probe needs it open, the implementer may drop it from the dependency list — the test above sends the token.

- [ ]  **Step 6: Run the tests and commit**

Run: `uv run pytest tests/test_api_basics.py -q` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/api services/engine/tests/test_api_basics.py services/engine/tests/conftest.py
git commit -m "feat(engine): add the API skeleton with token auth and strict bodies"
```

---

## Task 12: Workflows — CRUD, optimistic locking, validation

**Files:**

- Create: `services/engine/engine/db/workflows.py`
- Create: `services/engine/engine/api/routers/workflows.py`
- Modify: `services/engine/engine/api/app.py` (include the router)
- Test: `services/engine/tests/test_api_workflows.py`

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_api_workflows.py`:

```python
CHAIN = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "{{ start.topic }}"}},
        {"id": "end", "type": "end", "config": {"outputs": {"result": "{{ llm_1.text }}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"},
              {"id": "e2", "source": "llm_1", "target": "end"}],
}


async def _create(api, name: str = "내 워크플로") -> dict:
    response = await api.post("/workflows", json={"name": name})
    assert response.status_code == 201
    return response.json()


async def test_create_read_and_list(api):
    created = await _create(api)

    fetched = (await api.get(f"/workflows/{created['id']}")).json()
    listed = (await api.get("/workflows")).json()

    assert (created["revision"], fetched["name"]) == (1, "내 워크플로")
    assert fetched["draftDsl"] == {"version": "1", "nodes": [], "edges": []}
    assert [item["id"] for item in listed["workflows"]] == [created["id"]]


async def test_saving_a_draft_bumps_the_revision(api):
    created = await _create(api)

    saved = await api.put(f"/workflows/{created['id']}",
                          json={"draftDsl": CHAIN, "revision": 1, "name": "이름 변경"})

    assert saved.status_code == 200 and saved.json()["revision"] == 2
    fetched = (await api.get(f"/workflows/{created['id']}")).json()
    assert fetched["draftDsl"] == CHAIN and fetched["name"] == "이름 변경"


async def test_a_stale_revision_conflicts_and_returns_the_current_draft(api):
    created = await _create(api)
    await api.put(f"/workflows/{created['id']}", json={"draftDsl": CHAIN, "revision": 1})

    conflict = await api.put(f"/workflows/{created['id']}", json={"draftDsl": {"nodes": [], "edges": []},
                                                                  "revision": 1})

    assert conflict.status_code == 409
    error = conflict.json()["error"]
    assert error["code"] == "REVISION_CONFLICT" and error["details"]["currentRevision"] == 2
    assert error["details"]["draftDsl"] == CHAIN


async def test_a_draft_may_be_invalid_but_not_oversized(api):
    created = await _create(api)
    broken = {"version": "1", "nodes": [{"id": "start", "type": "start"}], "edges": []}

    assert (await api.put(f"/workflows/{created['id']}", json={"draftDsl": broken, "revision": 1})).status_code == 200

    huge = {"version": "1", "nodes": [{"id": "start", "type": "start", "label": "가" * 300_000}], "edges": []}
    oversized = await api.put(f"/workflows/{created['id']}", json={"draftDsl": huge, "revision": 2})
    assert oversized.status_code == 422 and oversized.json()["error"]["code"] == "LIMIT_EXCEEDED"


async def test_validate_reports_issues_without_saving(api):
    created = await _create(api)
    broken = {"version": "1", "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}],
              "edges": []}

    response = await api.post(f"/workflows/{created['id']}/validate", json={"draftDsl": broken})

    codes = {issue["code"] for issue in response.json()["issues"]}
    assert response.status_code == 200 and codes
    assert (await api.get(f"/workflows/{created['id']}")).json()["revision"] == 1


async def test_a_body_that_is_not_standard_json_is_rejected(api):
    response = await api.post("/workflows", content='{"name": NaN}',
                              headers={"content-type": "application/json"})

    assert response.status_code == 400 and response.json()["error"]["code"] == "INVALID_JSON"


async def test_a_body_over_the_limit_is_rejected(api):
    import json

    big = json.dumps({"name": "x" * (api.config.max_body_bytes + 10)})

    response = await api.post("/workflows", content=big, headers={"content-type": "application/json"})

    assert response.status_code == 413 and response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_a_workflow_with_an_active_run_cannot_be_deleted(api, pool):
    from tests.factories import WORKSPACE

    created = await _create(api)
    async with pool.connection() as conn:
        from psycopg.types.json import Jsonb
        await conn.execute(
            "INSERT INTO workflow_versions (id, workflow_id, workspace_id, version_no, dsl, dsl_hash)"
            " VALUES (gen_random_uuid(), %s, %s, 1, %s, 'h') RETURNING id",
            (created["id"], WORKSPACE, Jsonb(CHAIN)))
        version = await (await conn.execute(
            "SELECT id FROM workflow_versions WHERE workflow_id=%s", (created["id"],))).fetchone()
        await conn.execute(
            "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status)"
            " VALUES (gen_random_uuid(), %s, %s, %s, 'waiting')",
            (WORKSPACE, created["id"], version["id"]))

    blocked = await api.delete(f"/workflows/{created['id']}")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "WORKFLOW_HAS_ACTIVE_RUNS"

    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET status='cancelled' WHERE workflow_id=%s", (created["id"],))
    assert (await api.delete(f"/workflows/{created['id']}")).status_code == 204
    assert (await api.get(f"/workflows/{created['id']}")).status_code == 404
```

Run: `uv run pytest tests/test_api_workflows.py -q` → FAIL.

- [ ]  **Step 2: Workflow queries**

Create `services/engine/engine/db/workflows.py`:

```python
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
    """Used by run creation so a concurrent save cannot change the draft mid-transaction."""
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
```

- [ ]  **Step 3: The router**

Create `services/engine/engine/api/routers/workflows.py`:

```python
"""Workflow CRUD and validation (MVP design 8.1)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request, Response

from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.db import workflows as workflow_db
from engine.validator import MAX_DSL_BYTES, analyze

router = APIRouter()


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(row["id"]), "name": row["name"], "revision": row["revision"],
            "updatedAt": row["updated_at"].isoformat()}


async def _analysis(request: Request, dsl: dict[str, Any]):
    """Validation is CPU work on untrusted input: keep it off the event loop (design 8.1)."""
    return await asyncio.to_thread(analyze, dsl, request.app.state.registry)


def _check_size(dsl: dict[str, Any]) -> None:
    if len(json.dumps(dsl, ensure_ascii=False).encode("utf-8")) > MAX_DSL_BYTES:
        raise ApiError(422, "LIMIT_EXCEEDED", f"워크플로가 너무 큽니다 (최대 {MAX_DSL_BYTES // 1024}KB)")


async def _require(conn, workflow_id: str) -> dict[str, Any]:
    row = await workflow_db.get(conn, workflow_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "워크플로를 찾을 수 없습니다")
    return row


@router.post("/workflows", status_code=201)
async def create_workflow(request: Request) -> dict[str, Any]:
    body = require_object(await read_json(request))
    name = field(body, "name", str)
    if not name.strip() or len(name) > 200:
        raise ApiError(422, "REQUEST_ERROR", "이름은 1~200자여야 합니다")
    async with request.app.state.pool.connection() as conn:
        return _view(await workflow_db.create(conn, name=name))


@router.get("/workflows")
async def list_workflows(request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        rows = await workflow_db.list_all(conn)
    return {"workflows": [_view(row) for row in rows]}


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        row = await _require(conn, workflow_id)
    return {**_view(row), "draftDsl": row["draft_dsl"]}


@router.put("/workflows/{workflow_id}")
async def save_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    body = require_object(await read_json(request))
    draft = field(body, "draftDsl", dict)
    revision = field(body, "revision", int)
    name = field(body, "name", str, required=False)
    _check_size(draft)  # a draft may be invalid (the editor saves work in progress) but not oversized
    async with request.app.state.pool.connection() as conn, conn.transaction():
        current = await _require(conn, workflow_id)
        saved = await workflow_db.save_draft(conn, workflow_id=workflow_id, draft=draft,
                                             revision=revision, name=name)
        if saved is None:
            raise ApiError(409, "REVISION_CONFLICT", "다른 곳에서 먼저 저장했습니다",
                           {"currentRevision": current["revision"], "draftDsl": current["draft_dsl"]})
    return {"revision": saved["revision"]}


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, request: Request) -> Response:
    async with request.app.state.pool.connection() as conn, conn.transaction():
        await _require(conn, workflow_id)
        if await workflow_db.has_active_runs(conn, workflow_id):
            raise ApiError(409, "WORKFLOW_HAS_ACTIVE_RUNS", "실행 중인 워크플로는 삭제할 수 없습니다")
        await workflow_db.delete(conn, workflow_id)
    return Response(status_code=204)


@router.post("/workflows/{workflow_id}/validate")
async def validate_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    body = require_object(await read_json(request))
    draft = field(body, "draftDsl", dict)
    async with request.app.state.pool.connection() as conn:
        await _require(conn, workflow_id)
    _check_size(draft)
    analysis = await _analysis(request, draft)
    return {"issues": [issue.to_dict() for issue in analysis.issues]}
```

Check that `MAX_DSL_BYTES` is exported from `engine.validator`; if it lives elsewhere, import it from there
rather than duplicating the number.

Include the router in `create_app`: `app.include_router(workflows.router)`.

- [ ]  **Step 4: Run the tests and commit**

Run: `uv run pytest tests/test_api_workflows.py tests/test_api_basics.py -q` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/db/workflows.py services/engine/engine/api services/engine/tests/test_api_workflows.py
git commit -m "feat(engine): serve workflow CRUD and validation"
```

---

## Task 13: Creating and reading runs

**Files:**

- Modify: `services/engine/engine/db/runs.py` (insert, lookups, lists)
- Create: `services/engine/engine/api/routers/runs.py`
- Test: `services/engine/tests/test_api_runs.py`

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_api_runs.py`:

```python
from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.test_api_workflows import CHAIN, _create

INPUT_SCHEMA = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
TYPED = {**CHAIN, "nodes": [{**CHAIN["nodes"][0], "config": {"inputs": INPUT_SCHEMA}}, *CHAIN["nodes"][1:]]}


async def _saved(api, dsl=TYPED) -> str:
    workflow = await _create(api)
    await api.put(f"/workflows/{workflow['id']}", json={"draftDsl": dsl, "revision": 1})
    return workflow["id"]


async def test_creating_a_run_pins_a_version_and_queues_it(api, pool):
    workflow_id = await _saved(api)

    response = await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})

    assert response.status_code == 202
    body = response.json()
    run = (await api.get(f"/runs/{body['runId']}")).json()
    assert (run["status"], run["inputs"]) == ("queued", {"topic": "AI"})
    assert run["versionId"] == body["versionId"]
    async with pool.connection() as conn:
        events = await (await conn.execute("SELECT type, seq FROM run_events WHERE run_id=%s",
                                           (body["runId"],))).fetchall()
    assert [(event["type"], event["seq"]) for event in events] == [("run_queued", 1)]


async def test_the_same_idempotency_key_returns_the_same_run(api):
    workflow_id = await _saved(api)
    headers = {"Idempotency-Key": "click-1"}

    first = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "A"}, "revision": 2},
                           headers=headers)
    second = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "B"}, "revision": 2},
                            headers=headers)

    assert first.json()["runId"] == second.json()["runId"]


async def test_the_same_dsl_reuses_its_version(api):
    workflow_id = await _saved(api)

    first = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "A"}, "revision": 2})
    second = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {"topic": "B"}, "revision": 2})

    assert first.json()["versionId"] == second.json()["versionId"]


async def test_running_a_stale_revision_conflicts(api):
    workflow_id = await _saved(api)

    response = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {}, "revision": 1})

    assert response.status_code == 409 and response.json()["error"]["code"] == "REVISION_CONFLICT"


async def test_an_invalid_workflow_is_not_run(api):
    workflow_id = await _saved(api, dsl={"version": "1", "nodes": [{"id": "start", "type": "start"}],
                                         "edges": []})

    response = await api.post(f"/workflows/{workflow_id}/runs", json={"inputs": {}, "revision": 2})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["details"]["issues"]


async def test_oversized_inputs_are_rejected(api):
    workflow_id = await _saved(api)

    response = await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "가" * 300_000}, "revision": 2})

    assert response.status_code == 413 and response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_a_run_is_picked_up_and_its_nodes_readable(api, pool, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    async def done():
        run = (await api.get(f"/runs/{created['runId']}")).json()
        return run if run["status"] == "succeeded" else None

    run = await until(done)
    assert run["outputs"] == {"result": "요약본"}
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()["nodeRuns"]
    assert [(node["nodeId"], node["status"], node["attempt"]) for node in nodes] == [
        ("start", "succeeded", 1), ("llm_1", "succeeded", 1), ("end", "succeeded", 1)
    ]
    listed = (await api.get(f"/workflows/{workflow_id}/runs")).json()["runs"]
    assert [item["id"] for item in listed] == [created["runId"]]
```

Run: `uv run pytest tests/test_api_runs.py -q` → FAIL.

- [ ]  **Step 2: Extend the run queries**

Append to `services/engine/engine/db/runs.py`:

```python
MAX_INPUT_BYTES = 256_000


async def insert_queued(conn: AsyncConnection, *, workflow_id: str, version_id: str, workspace_id: str,
                        inputs: dict[str, Any], idempotency_key: str | None,
                        store_run_data: bool) -> dict[str, Any]:
    return await (await conn.execute(
        "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status, inputs,"
        " idempotency_key, store_run_data) VALUES (gen_random_uuid(), %s, %s, %s, 'queued', %s, %s, %s)"
        " RETURNING *",
        (workspace_id, workflow_id, version_id, Jsonb(inputs), idempotency_key, store_run_data),
    )).fetchone()


async def find_by_idempotency_key(conn: AsyncConnection, workflow_id: str, key: str) -> dict[str, Any] | None:
    return await (await conn.execute(
        "SELECT * FROM runs WHERE workflow_id=%s AND idempotency_key=%s", (workflow_id, key)
    )).fetchone()


async def lock_run(conn: AsyncConnection, run_id: str) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM runs WHERE id=%s FOR UPDATE", (run_id,))).fetchone()


async def list_for_workflow(conn: AsyncConnection, workflow_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    return await (await conn.execute(
        "SELECT id, status, created_at, started_at, finished_at, retry_count, workflow_version_id"
        " FROM runs WHERE workflow_id=%s ORDER BY created_at DESC LIMIT %s", (workflow_id, limit)
    )).fetchall()


async def list_node_runs(conn: AsyncConnection, run_id: str) -> list[dict[str, Any]]:
    return await (await conn.execute(
        "SELECT node_id, exec_index, attempt, status, input, output, error, meta, tokens_in, tokens_out,"
        " truncated, started_at, finished_at FROM node_runs WHERE run_id=%s"
        " ORDER BY started_at, exec_index, attempt", (run_id,)
    )).fetchall()


async def waiting_payload(conn: AsyncConnection, run_id: str, node_id: str, exec_index: int) -> dict[str, Any] | None:
    """The interrupt payload of the approval this run is parked on (stored by the recorder)."""
    row = await (await conn.execute(
        "SELECT meta->'waiting' AS waiting FROM node_runs"
        " WHERE run_id=%s AND node_id=%s AND exec_index=%s AND waited ORDER BY attempt DESC LIMIT 1",
        (run_id, node_id, exec_index),
    )).fetchone()
    return row["waiting"] if row else None


async def has_checkpoint(conn: AsyncConnection, run_id: str) -> bool:
    row = await (await conn.execute(
        "SELECT 1 FROM checkpoints WHERE thread_id=%s LIMIT 1", (str(run_id),)
    )).fetchone()
    return row is not None
```

- [ ]  **Step 3: The runs router**

Create `services/engine/engine/api/routers/runs.py`:

```python
"""Creating and reading runs (MVP design 8.1, 8.3, 8.4)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request
from psycopg.errors import UniqueViolation

from engine.api.body import field, read_json, require_object
from engine.api.errors import ApiError
from engine.db import runs as run_db
from engine.db import workflows as workflow_db
from engine.dsl.models import dsl_hash
from engine.events.publish import RedisPublisher
from engine.events.writer import append_event
from engine.validator import analyze
from engine.validator.issues import has_errors

router = APIRouter()


def _run_view(row: dict[str, Any], waiting: dict[str, Any] | None = None) -> dict[str, Any]:
    view = {
        "id": str(row["id"]),
        "status": row["status"],
        "versionId": str(row["workflow_version_id"]),
        "inputs": row["inputs"],
        "outputs": row["outputs"],
        "error": row["error"],
        "cancelRequested": row["cancel_requested_at"] is not None,
        "retryCount": row["retry_count"],
        "recoveryCount": row["recovery_count"],
        "createdAt": row["created_at"].isoformat(),
        "startedAt": row["started_at"].isoformat() if row["started_at"] else None,
        "finishedAt": row["finished_at"].isoformat() if row["finished_at"] else None,
    }
    if waiting is not None:
        view["waitingFor"] = waiting
    return view


async def _run_or_404(conn, run_id: str) -> dict[str, Any]:
    row = await run_db.get_run(conn, run_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "실행을 찾을 수 없습니다")
    return row


@router.post("/workflows/{workflow_id}/runs", status_code=202)
async def create_run(workflow_id: str, request: Request) -> dict[str, Any]:
    body = require_object(await read_json(request))
    inputs = field(body, "inputs", dict, required=False, default={}) or {}
    revision = field(body, "revision", int)
    key = request.headers.get("idempotency-key")
    if len(json.dumps(inputs, ensure_ascii=False).encode("utf-8")) > run_db.MAX_INPUT_BYTES:
        raise ApiError(413, "PAYLOAD_TOO_LARGE",
                       f"실행 입력이 너무 큽니다 (최대 {run_db.MAX_INPUT_BYTES // 1024}KB)")

    pool = request.app.state.pool
    try:
        async with pool.connection() as conn, conn.transaction():
            if key:
                existing = await run_db.find_by_idempotency_key(conn, workflow_id, key)
                if existing is not None:  # a double click or a retried request (design 8.4)
                    return {"runId": str(existing["id"]),
                            "versionId": str(existing["workflow_version_id"])}
            workflow = await workflow_db.lock(conn, workflow_id)
            if workflow is None:
                raise ApiError(404, "NOT_FOUND", "워크플로를 찾을 수 없습니다")
            if workflow["revision"] != revision:
                raise ApiError(409, "REVISION_CONFLICT", "다른 곳에서 먼저 저장했습니다",
                               {"currentRevision": workflow["revision"]})
            draft = workflow["draft_dsl"]
            analysis = await asyncio.to_thread(analyze, draft, request.app.state.registry)
            if has_errors(analysis.issues) or analysis.dsl is None:
                raise ApiError(422, "VALIDATION_FAILED", "워크플로에 오류가 있습니다",
                               {"issues": [issue.to_dict() for issue in analysis.issues]})
            version = await workflow_db.pin_version(conn, workflow_id=workflow_id, dsl=draft,
                                                    dsl_hash=dsl_hash(analysis.dsl))
            run = await run_db.insert_queued(
                conn, workflow_id=workflow_id, version_id=str(version["id"]),
                workspace_id=str(workflow["workspace_id"]), inputs=inputs, idempotency_key=key,
                store_run_data=analysis.dsl.settings.storeRunData,
            )
            event = await append_event(conn.cursor(), str(run["id"]), "run_queued",
                                       payload={"versionNo": version["version_no"]})
            await run_db.notify_queued(conn, str(run["id"]))  # inside the transaction: never fires early
    except UniqueViolation:  # two requests with the same key raced; the winner's row is the answer
        async with pool.connection() as conn:
            existing = await run_db.find_by_idempotency_key(conn, workflow_id, key or "")
        if existing is None:
            raise
        return {"runId": str(existing["id"]), "versionId": str(existing["workflow_version_id"])}

    await _publish(request, str(run["id"]), event)
    return {"runId": str(run["id"]), "versionId": str(version["id"])}


@router.get("/workflows/{workflow_id}/runs")
async def list_runs(workflow_id: str, request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        rows = await run_db.list_for_workflow(conn, workflow_id)
    return {"runs": [{"id": str(row["id"]), "status": row["status"],
                      "versionId": str(row["workflow_version_id"]),
                      "createdAt": row["created_at"].isoformat(),
                      "finishedAt": row["finished_at"].isoformat() if row["finished_at"] else None,
                      "retryCount": row["retry_count"]} for row in rows]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        row = await _run_or_404(conn, run_id)
        waiting = None
        if row["status"] == "waiting" and row["waiting_node_id"]:
            waiting = await run_db.waiting_payload(conn, run_id, row["waiting_node_id"],
                                                   row["waiting_exec_index"])
    return _run_view(row, waiting)


@router.get("/runs/{run_id}/nodes")
async def get_node_runs(run_id: str, request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn:
        await _run_or_404(conn, run_id)
        rows = await run_db.list_node_runs(conn, run_id)
    return {"nodeRuns": [{"nodeId": row["node_id"], "execIndex": row["exec_index"], "attempt": row["attempt"],
                          "status": row["status"], "input": row["input"], "output": row["output"],
                          "error": row["error"], "meta": row["meta"], "truncated": row["truncated"],
                          "tokensIn": row["tokens_in"], "tokensOut": row["tokens_out"],
                          "startedAt": row["started_at"].isoformat(),
                          "finishedAt": row["finished_at"].isoformat() if row["finished_at"] else None}
                         for row in rows]}


async def _publish(request: Request, run_id: str, event: dict[str, Any]) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        await RedisPublisher(request.app.state.redis).publish(run_id, event)
```

Include the router in `create_app`.

- [ ]  **Step 4: Run the tests and commit**

Run: `uv run pytest tests/test_api_runs.py -q` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/db/runs.py services/engine/engine/api services/engine/tests/test_api_runs.py
git commit -m "feat(engine): create runs against a pinned version and read them back"
```

---

## Task 14: Live events over SSE

**Files:**

- Create: `services/engine/engine/events/stream.py`
- Modify: `services/engine/engine/api/routers/runs.py` (the `/events` route)
- Test: `services/engine/tests/test_api_events.py`

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_api_events.py`:

```python
import asyncio
import json

from engine.events.publish import RedisPublisher
from engine.llm.scripted import ScriptedLLM
from tests.test_api_runs import _saved


async def _collect(api, url: str, *, headers: dict | None = None, limit: int = 50) -> list[dict]:
    """Read an SSE stream until it closes or `limit` events arrive."""
    events: list[dict] = []
    async with api.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        current: dict = {}
        async for line in response.aiter_lines():
            if line.startswith("id:"):
                current["id"] = int(line[3:].strip())
            elif line.startswith("data:"):
                current["data"] = json.loads(line[5:].strip())
            elif line == "":
                if current.get("data"):
                    events.append(current)
                    if len(events) >= limit or current["data"]["type"] in (
                            "run_succeeded", "run_failed", "run_cancelled"):
                        break
                current = {}
    return events


async def test_a_finished_run_replays_from_the_start_and_closes(api, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    events = await asyncio.wait_for(_collect(api, f"/runs/{run['runId']}/events"), 30)

    types = [event["data"]["type"] for event in events]
    assert types[0] == "run_queued" and types[-1] == "run_succeeded"
    assert [event["id"] for event in events] == list(range(1, len(events) + 1))  # gapless ids


async def test_reconnecting_with_last_event_id_skips_what_was_seen(api, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    first = await asyncio.wait_for(_collect(api, f"/runs/{run['runId']}/events"), 30)

    again = await asyncio.wait_for(
        _collect(api, f"/runs/{run['runId']}/events", headers={"Last-Event-ID": "2"}), 30)

    assert [event["id"] for event in again] == [event["id"] for event in first if event["id"] > 2]


async def test_a_dropped_publish_is_filled_in_from_postgres(api, pool, redis):
    """The stream must not depend on Redis delivering every message."""
    from tests.factories import make_run

    run_id = await make_run(pool, status="running")
    async with pool.connection() as conn, conn.transaction():
        from engine.events.writer import append_event
        for index in range(1, 4):
            await append_event(conn.cursor(), run_id, "node_started", node_id=f"n{index}",
                               exec_index=1, attempt=1)
        last = await append_event(conn.cursor(), run_id, "run_succeeded")

    async def publish_last_only():
        await asyncio.sleep(0.3)
        await RedisPublisher(redis).publish(run_id, last)  # events 1-3 were never published

    task = asyncio.create_task(publish_last_only())
    events = await asyncio.wait_for(_collect(api, f"/runs/{run_id}/events?after=1"), 20)
    await task

    assert [event["id"] for event in events] == [2, 3, 4]


async def test_token_events_carry_no_id(api, pool, redis):
    from tests.factories import make_run

    run_id = await make_run(pool, status="running")

    async def publish():
        await asyncio.sleep(0.3)
        publisher = RedisPublisher(redis)
        await publisher.publish(run_id, {"runId": run_id, "type": "node_token", "nodeId": "llm_1",
                                         "payload": {"text": "안"}})
        async with pool.connection() as conn, conn.transaction():
            from engine.events.writer import append_event
            event = await append_event(conn.cursor(), run_id, "run_succeeded")
        await publisher.publish(run_id, event)

    task = asyncio.create_task(publish())
    events = await asyncio.wait_for(_collect(api, f"/runs/{run_id}/events"), 20)
    await task

    token = [event for event in events if event["data"]["type"] == "node_token"]
    assert token and "id" not in token[0]  # Last-Event-ID must not move past a transient event
```

Run: `uv run pytest tests/test_api_events.py -q` → FAIL.

- [ ]  **Step 2: Implement the stream**

Create `services/engine/engine/events/stream.py`:

```python
"""SSE stream for one run (MVP design 7.3).

Order matters: subscribe first, then read what is already stored, then play the live messages, dropping
duplicates and filling gaps from Postgres. That way an event produced between the read and the subscribe
cannot be lost.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from psycopg_pool import AsyncConnectionPool

from engine.events.publish import run_events

TERMINAL = {"run_succeeded", "run_failed", "run_cancelled"}
PING = ": ping\n\n"


def format_event(event: dict[str, Any]) -> str:
    lines = []
    if event.get("seq") is not None:  # transient events get no id (design 7.3)
        lines.append(f"id: {event['seq']}")
    lines.append(f"event: {event['type']}")
    lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


async def _stored(pool: AsyncConnectionPool, run_id: str, after: int,
                  before: int | None = None) -> list[dict[str, Any]]:
    query = ("SELECT seq, type, node_id, exec_index, attempt, payload, created_at FROM run_events"
             " WHERE run_id=%s AND seq > %s")
    args: list[Any] = [run_id, after]
    if before is not None:
        query += " AND seq < %s"
        args.append(before)
    async with pool.connection() as conn:
        rows = await (await conn.execute(query + " ORDER BY seq", args)).fetchall()
    return [{"seq": row["seq"], "runId": run_id, "type": row["type"], "nodeId": row["node_id"],
             "execIndex": row["exec_index"], "attempt": row["attempt"],
             "ts": row["created_at"].isoformat(), "payload": row["payload"] or {}} for row in rows]


async def event_stream(pool: AsyncConnectionPool, redis: Any, run_id: str, *, after: int = 0,
                       ping_sec: float = 15.0) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def pump() -> None:
        async for event in run_events(redis, run_id):
            await queue.put(event)

    subscriber = asyncio.create_task(pump())
    await asyncio.sleep(0)  # let the subscription start before we read the stored events
    try:
        last = after
        for event in await _stored(pool, run_id, after):
            yield format_event(event)
            last = event["seq"]
            if event["type"] in TERMINAL:
                return
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), ping_sec)
            except TimeoutError:
                yield PING
                continue
            seq = event.get("seq")
            if seq is None:
                yield format_event(event)
                continue
            if seq <= last:
                continue
            if seq > last + 1:  # a publish was lost: read the missing range
                for missed in await _stored(pool, run_id, last, before=seq):
                    yield format_event(missed)
                    last = missed["seq"]
            yield format_event(event)
            last = seq
            if event["type"] in TERMINAL:
                return
    finally:
        subscriber.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscriber
```

- [ ]  **Step 3: The route**

Add to `services/engine/engine/api/routers/runs.py`:

```python
@router.get("/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    async with request.app.state.pool.connection() as conn:
        await _run_or_404(conn, run_id)
    header = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    after = int(header) if header.isdigit() else 0
    stream = event_stream(request.app.state.pool, request.app.state.redis, run_id, after=after)
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

with `from fastapi.responses import StreamingResponse` and `from engine.events.stream import event_stream`.

- [ ]  **Step 4: Run the tests and commit**

Run: `uv run pytest tests/test_api_events.py -q` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/events/stream.py services/engine/engine/api/routers/runs.py services/engine/tests/test_api_events.py
git commit -m "feat(engine): stream run events over SSE with gap filling"
```

---

## Task 15: Resume, retry and cancel

**Files:**

- Modify: `services/engine/engine/db/runs.py` (queue_resume, queue_retry, cancel)
- Modify: `services/engine/engine/api/routers/runs.py`
- Test: `services/engine/tests/test_api_control.py`

- [ ]  **Step 1: Write the failing tests**

Create `services/engine/tests/test_api_control.py`:

```python
import asyncio

from engine.llm.scripted import ScriptedLLM
from tests.conftest import until
from tests.test_api_runs import _saved

HITL = {
    "version": "1",
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"draft": {"type": "string"}},
                               "required": ["draft"]}}},
        {"id": "human_approval_1", "type": "human_approval",
         "config": {"message": "검토해 주세요", "review": "{{ start.draft }}", "allowEdit": True}},
        {"id": "end", "type": "end",
         "config": {"outputs": {"final": "{{ human_approval_1.editedValue }}"}}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "human_approval_1"},
        {"id": "e2", "source": "human_approval_1", "sourceHandle": "approve", "target": "end"},
        {"id": "e3", "source": "human_approval_1", "sourceHandle": "reject", "target": "end"},
    ],
}


async def _wait_status(api, run_id: str, status: str) -> dict:
    async def check():
        run = (await api.get(f"/runs/{run_id}")).json()
        return run if run["status"] == status else None

    return await until(check)


async def test_an_approval_can_be_answered_and_the_run_finishes(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    waiting = await _wait_status(api, run["runId"], "waiting")

    assert waiting["waitingFor"]["review"] == "원고" and waiting["waitingFor"]["allowEdit"] is True
    answer = {"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve",
              "editedValue": "수정된 원고"}
    accepted = await api.post(f"/runs/{run['runId']}/resume", json=answer)

    assert accepted.status_code == 202
    finished = await _wait_status(api, run["runId"], "succeeded")
    assert finished["outputs"] == {"final": "수정된 원고"}


async def test_a_wrong_target_or_a_bad_answer_is_refused(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "waiting")

    mismatch = await api.post(f"/runs/{run['runId']}/resume",
                              json={"nodeId": "human_approval_1", "execIndex": 7, "decision": "approve"})
    bad = await api.post(f"/runs/{run['runId']}/resume",
                         json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "maybe"})
    typed = await api.post(f"/runs/{run['runId']}/resume",
                           json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve",
                                 "editedValue": 42})

    assert (mismatch.status_code, mismatch.json()["error"]["code"]) == (409, "RESUME_TARGET_MISMATCH")
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "VALIDATION_FAILED")
    assert typed.status_code == 422  # editedValue must keep the review's type
    assert (await api.get(f"/runs/{run['runId']}")).json()["status"] == "waiting"  # still answerable


async def test_the_server_sets_reviewed_at(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "waiting")

    await api.post(f"/runs/{run['runId']}/resume",
                   json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve",
                         "reviewedAt": "1999-01-01T00:00:00+00:00"})

    await _wait_status(api, run["runId"], "succeeded")
    nodes = (await api.get(f"/runs/{run['runId']}/nodes")).json()["nodeRuns"]
    approval = [node for node in nodes if node["nodeId"] == "human_approval_1"][-1]
    assert not approval["output"]["reviewedAt"].startswith("1999")


async def test_resuming_a_run_that_is_not_waiting_is_a_conflict(api):
    workflow_id = await _saved(api)
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    response = await api.post(f"/runs/{run['runId']}/resume",
                              json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"})

    assert (response.status_code, response.json()["error"]["code"]) == (409, "INVALID_STATE_TRANSITION")


async def test_a_failed_run_can_be_retried_and_keeps_finished_nodes(api, pool, worker_factory):
    from engine.errors import ErrorCode, NodeError

    workflow_id = await _saved(api)
    worker = await worker_factory(ScriptedLLM([NodeError(ErrorCode.LLM_UNAVAILABLE, "모델 없음", retryable=False)]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "failed")
    worker._llm = ScriptedLLM(["요약본"])  # the model is back

    retried = await api.post(f"/runs/{run['runId']}/retry")

    assert retried.status_code == 202
    finished = await _wait_status(api, run["runId"], "succeeded")
    assert finished["retryCount"] == 1 and finished["outputs"] == {"result": "요약본"}
    nodes = (await api.get(f"/runs/{run['runId']}/nodes")).json()["nodeRuns"]
    assert len([node for node in nodes if node["nodeId"] == "start"]) == 1  # not re-run


async def test_retry_is_refused_without_a_checkpoint(api, pool):
    from tests.factories import make_run

    run_id = await make_run(pool, status="failed")

    response = await api.post(f"/runs/{run_id}/retry")

    assert (response.status_code, response.json()["error"]["code"]) == (409, "RUN_DATA_EXPIRED")


async def test_cancelling_a_queued_run_is_immediate(api):
    workflow_id = await _saved(api)
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    response = await api.post(f"/runs/{run['runId']}/cancel")

    assert response.json()["status"] == "cancelled"
    assert (await api.get(f"/runs/{run['runId']}")).json()["status"] == "cancelled"


async def test_cancelling_a_waiting_run_is_immediate_and_final(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    await worker_factory(ScriptedLLM([]))
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, run["runId"], "waiting")

    await api.post(f"/runs/{run['runId']}/cancel")

    assert (await api.get(f"/runs/{run['runId']}")).json()["status"] == "cancelled"
    late = await api.post(f"/runs/{run['runId']}/resume",
                          json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"})
    assert late.status_code == 409


async def test_cancelling_a_running_run_only_requests_it(api, pool, worker_factory):
    import asyncio

    class _Slow:
        def __init__(self):
            self.started = asyncio.Event()

        async def chat(self, **kwargs):
            self.started.set()
            await asyncio.sleep(30)
            raise AssertionError("cancelled")

    workflow_id = await _saved(api)
    llm = _Slow()
    await worker_factory(llm)
    run = (await api.post(f"/workflows/{workflow_id}/runs",
                          json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await asyncio.wait_for(llm.started.wait(), 20)

    response = await api.post(f"/runs/{run['runId']}/cancel")

    assert response.json()["status"] in ("running", "cancelled")
    assert (await _wait_status(api, run["runId"], "cancelled"))["cancelRequested"] is True
```

Run: `uv run pytest tests/test_api_control.py -q` → FAIL.

- [ ]  **Step 2: Transition queries**

Append to `services/engine/engine/db/runs.py`:

```python
async def queue_resume(conn: AsyncConnection, *, run_id: str, answer: dict[str, Any]) -> bool:
    row = await (await conn.execute(
        "UPDATE runs SET status='queued', resume_payload=%s, updated_at=now()"
        " WHERE id=%s AND status='waiting' RETURNING id", (Jsonb(answer), run_id),
    )).fetchone()
    return row is not None


async def queue_retry(conn: AsyncConnection, run_id: str) -> bool:
    row = await (await conn.execute(
        "UPDATE runs SET status='queued', retry_count=retry_count + 1, error=NULL, finished_at=NULL,"
        "   updated_at=now() WHERE id=%s AND status='failed' RETURNING id", (run_id,),
    )).fetchone()
    return row is not None


async def cancel_now(conn: AsyncConnection, run_id: str) -> bool:
    """Queued and waiting runs have no worker, so the API ends them itself (design 5.9)."""
    row = await (await conn.execute(
        "UPDATE runs SET status='cancelled', cancel_requested_at=coalesce(cancel_requested_at, now()),"
        "   finished_at=now(), resume_payload=NULL, updated_at=now()"
        " WHERE id=%s AND status IN ('queued','waiting') RETURNING id", (run_id,),
    )).fetchone()
    return row is not None


async def request_cancel(conn: AsyncConnection, run_id: str) -> bool:
    row = await (await conn.execute(
        "UPDATE runs SET cancel_requested_at=coalesce(cancel_requested_at, now()), updated_at=now()"
        " WHERE id=%s AND status='running' RETURNING id", (run_id,),
    )).fetchone()
    return row is not None
```

- [ ]  **Step 3: The routes**

Add to `services/engine/engine/api/routers/runs.py`:

```python
@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_run(run_id: str, request: Request) -> dict[str, Any]:
    body = require_object(await read_json(request))
    pool = request.app.state.pool
    async with pool.connection() as conn, conn.transaction():
        run = await run_db.lock_run(conn, run_id)
        if run is None:
            raise ApiError(404, "NOT_FOUND", "실행을 찾을 수 없습니다")
        if run["status"] != "waiting":
            raise ApiError(409, "INVALID_STATE_TRANSITION", "승인을 기다리는 실행이 아닙니다")
        if (body.get("nodeId") != run["waiting_node_id"]
                or body.get("execIndex") != run["waiting_exec_index"]):
            raise ApiError(409, "RESUME_TARGET_MISMATCH", "대기 중인 승인과 대상이 다릅니다",
                           {"nodeId": run["waiting_node_id"], "execIndex": run["waiting_exec_index"]})
        waiting = await run_db.waiting_payload(conn, run_id, run["waiting_node_id"],
                                               run["waiting_exec_index"])
        if waiting is None:
            raise ApiError(409, "RUN_DATA_EXPIRED", "승인 정보를 찾을 수 없습니다")
        # reviewedAt is always the server's clock: resume_output accepts any timestamp it is given.
        answer = {**body, "reviewedAt": datetime.now(UTC).isoformat()}
        try:
            resume_output(answer, waiting)  # reject a bad answer here, not after the run resumes
        except NodeError as exc:
            raise ApiError(422, "VALIDATION_FAILED", exc.message) from None
        await run_db.queue_resume(conn, run_id=run_id, answer=answer)
        event = await append_event(conn.cursor(), run_id, "run_resumed",
                                   node_id=run["waiting_node_id"], exec_index=run["waiting_exec_index"])
        await run_db.notify_queued(conn, run_id)
    await _publish(request, run_id, event)
    return {"status": "queued"}


@router.post("/runs/{run_id}/retry", status_code=202)
async def retry_run(run_id: str, request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn, conn.transaction():
        run = await run_db.lock_run(conn, run_id)
        if run is None:
            raise ApiError(404, "NOT_FOUND", "실행을 찾을 수 없습니다")
        if run["status"] != "failed":
            raise ApiError(409, "INVALID_STATE_TRANSITION", "실패한 실행만 재시도할 수 있습니다")
        if not await run_db.has_checkpoint(conn, run_id):
            raise ApiError(409, "RUN_DATA_EXPIRED", "보존 기간이 지나 재시도할 수 없습니다")
        await run_db.queue_retry(conn, run_id)
        event = await append_event(conn.cursor(), run_id, "run_queued", payload={"retry": True})
        await run_db.notify_queued(conn, run_id)
    await _publish(request, run_id, event)
    return {"status": "queued"}


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    async with request.app.state.pool.connection() as conn, conn.transaction():
        run = await run_db.lock_run(conn, run_id)
        if run is None:
            raise ApiError(404, "NOT_FOUND", "실행을 찾을 수 없습니다")
        if run["status"] in ("succeeded", "failed", "cancelled"):
            raise ApiError(409, "INVALID_STATE_TRANSITION", "이미 끝난 실행입니다")
        if await run_db.cancel_now(conn, run_id):
            await run_db.close_open_node_runs(conn, run_id, "cancelled")
            event = await append_event(conn.cursor(), run_id, "run_cancelled")
            status = "cancelled"
        else:
            await run_db.request_cancel(conn, run_id)
            event = await append_event(conn.cursor(), run_id, "run_cancel_requested")
            status = "running"  # the worker stops it; the UI shows "취소 중"
    await _publish(request, run_id, event)
    if status == "running":  # tell the worker now; the heartbeat is the fallback
        import contextlib

        with contextlib.suppress(Exception):
            await RedisPublisher(request.app.state.redis).request_cancel(run_id)
    return {"status": status}
```

Add the imports: `from datetime import UTC, datetime`, `from engine.errors import NodeError`,
`from engine.nodes.human_approval import resume_output`.

- [ ]  **Step 4: (already done in Task 4)**

The approval payload is the only way to answer a waiting run, so `PostgresRecorder.node_waiting` stores it
regardless of `storeRunData` — see the Task 4 post-review note. Nothing to do here beyond checking that
`tests/test_events_recorder.py::test_an_approval_payload_survives_without_run_data_but_is_not_evented`
still passes.

- [ ]  **Step 5: Run the tests and commit**

Run: `uv run pytest tests/test_api_control.py tests/test_events_recorder.py -q` → PASS.
Run: `uv run ruff check .` → clean.

```bash
git add services/engine/engine/db/runs.py services/engine/engine/api/routers/runs.py \
        services/engine/engine/events/recorder.py services/engine/tests
git commit -m "feat(engine): resume, retry and cancel runs through the API"
```

---

## Task 16: End-to-end scenarios

These are the tests the design's completion criteria are written against. They use the API and a real worker,
never internal calls.

**Files:**

- Test: `services/engine/tests/test_runtime_e2e.py`

- [ ]  **Step 1: Write the scenarios**

Create `services/engine/tests/test_runtime_e2e.py`:

```python
import asyncio
import json
import pathlib

import pytest

from engine.llm.scripted import ScriptedLLM
from tests.test_api_control import HITL, _wait_status
from tests.test_api_runs import _saved

GOLDEN = pathlib.Path(__file__).parent / "golden"


def _responder(model, messages, schema):
    """Answers any golden workflow: a category when a schema asks for one, otherwise text."""
    if schema is not None:
        options = schema.get("properties", {}).get("category", {}).get("enum")
        return {"category": options[0]} if options else {}
    return "생성된 내용"


@pytest.mark.parametrize("name", ["chaining", "routing", "parallel", "evaluator_loop"])
async def test_a_golden_workflow_runs_through_the_api(api, worker_factory, name):
    dsl = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    workflow_id = await _saved(api, dsl=dsl)
    await worker_factory(ScriptedLLM(_responder))

    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": _inputs_for(dsl), "revision": 2})).json()

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["outputs"]


def _inputs_for(dsl: dict) -> dict:
    """Fill the start node's declared inputs with strings."""
    schema = next((node.get("config", {}).get("inputs") for node in dsl["nodes"] if node["type"] == "start"), None)
    properties = (schema or {}).get("properties", {})
    return {name: "값" for name in properties}


async def test_a_dead_worker_is_recovered_and_finished_nodes_are_not_re_run(api, pool, worker_factory):
    workflow_id = await _saved(api)
    stuck = _Stuck()
    dying = await worker_factory(stuck, owner="dying", lease_sec=1, reaper_interval_sec=0.2)
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()
    await asyncio.wait_for(stuck.started.wait(), 20)

    await dying.stop()  # the process dies mid-node; the lease is left behind
    await worker_factory(ScriptedLLM(["요약본"]), owner="healthy", lease_sec=30, reaper_interval_sec=0.2)

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["recoveryCount"] >= 1
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()["nodeRuns"]
    starts = [node for node in nodes if node["nodeId"] == "start"]
    assert len(starts) == 1  # the checkpointed node was not executed again
    assert [node["status"] for node in nodes if node["nodeId"] == "llm_1"][-1] == "succeeded"


async def test_an_approval_survives_a_worker_restart(api, worker_factory):
    workflow_id = await _saved(api, dsl=HITL)
    first = await worker_factory(ScriptedLLM([]), owner="before")
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"draft": "원고"}, "revision": 2})).json()
    await _wait_status(api, created["runId"], "waiting")

    await first.stop()
    await api.post(f"/runs/{created['runId']}/resume",
                   json={"nodeId": "human_approval_1", "execIndex": 1, "decision": "approve"})
    await worker_factory(ScriptedLLM([]), owner="after")

    run = await _wait_status(api, created["runId"], "succeeded")
    assert run["outputs"] == {"final": "원고"}
    nodes = (await api.get(f"/runs/{created['runId']}/nodes")).json()["nodeRuns"]
    approvals = [node for node in nodes if node["nodeId"] == "human_approval_1"]
    assert len(approvals) == 1  # the waiting attempt was reused, not re-opened


async def test_progress_is_visible_over_sse_while_the_run_is_in_flight(api, worker_factory):
    from tests.test_api_events import _collect

    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(["요약본"], delay=0.2))
    created = (await api.post(f"/workflows/{workflow_id}/runs",
                              json={"inputs": {"topic": "AI"}, "revision": 2})).json()

    events = await asyncio.wait_for(_collect(api, f"/runs/{created['runId']}/events"), 30)

    types = [event["data"]["type"] for event in events]
    assert types.count("node_started") == 3 and types[-1] == "run_succeeded"
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)


async def test_two_workers_share_the_queue_without_double_running(api, worker_factory):
    workflow_id = await _saved(api)
    await worker_factory(ScriptedLLM(_responder), owner="w1")
    await worker_factory(ScriptedLLM(_responder), owner="w2")

    created = [
        (await api.post(f"/workflows/{workflow_id}/runs",
                        json={"inputs": {"topic": f"주제 {index}"}, "revision": 2})).json()["runId"]
        for index in range(4)
    ]

    for run_id in created:
        await _wait_status(api, run_id, "succeeded")
        nodes = (await api.get(f"/runs/{run_id}/nodes")).json()["nodeRuns"]
        assert len([node for node in nodes if node["nodeId"] == "llm_1"]) == 1


class _Stuck:
    """Never returns: the worker holding this run has to be recovered."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def chat(self, **kwargs):
        self.started.set()
        await asyncio.sleep(3600)
```

- [ ]  **Step 2: Run them**

Run: `uv run pytest tests/test_runtime_e2e.py -q`
Expected: PASS. These exercise Tasks 1–15 together; when one fails, fix the component, not the test.

If `test_a_dead_worker_is_recovered...` is flaky, the cause is usually the reaper interval versus the lease:
`lease_sec=1` with `reaper_interval_sec=0.2` leaves a whole second before recovery, and `until` waits 15 s.
Do not paper over a real failure with a longer timeout.

- [ ]  **Step 3: Commit**

```bash
git add services/engine/tests/test_runtime_e2e.py
git commit -m "test(engine): cover recovery, approval restart and live events end to end"
```

---

## Task 17: Documentation and full verification

**Files:**

- Create: `services/engine/README.md`
- Modify: `docs/superpowers/plans/2026-09-16-runtime-core.md` (check off the coverage table below)

- [ ]  **Step 1: Write the README**

Create `services/engine/README.md`:

```markdown
# engine

Workflow engine, API and worker for the agentic workflow builder.

## Development

```bash
docker compose -f ../../deploy/docker-compose.dev.yml up -d
export ENGINE_DATABASE_URL=postgresql://engine:engine@localhost:5433/engine
export ENGINE_REDIS_URL=redis://localhost:6380/0
export LANGGRAPH_AES_KEY=0123456789abcdef0123456789abcdef
export ENGINE_API_TOKEN=dev-token
export OLLAMA_BASE_URL=http://localhost:11434

uv run uvicorn engine.api.main:app --port 8000   # API
uv run python -m engine.worker.main              # worker (one or more)
```

The API and the worker both run migrations at startup, so either can be started first.

## Tests

```bash
uv run pytest -q                    # everything (needs Docker for Postgres and Redis)
uv run pytest -q -m "not integration"   # unit tests only, no Docker
uv run ruff check .
```

## Environment


| Variable              | Default                    | Meaning                                                |
| ----------------------- | ---------------------------- | -------------------------------------------------------- |
| `ENGINE_DATABASE_URL` | —                         | Postgres connection string (required)                  |
| `ENGINE_REDIS_URL`    | `redis://localhost:6379/0` | Redis for events, control and the model semaphore      |
| `ENGINE_API_TOKEN`    | none                       | Shared bearer token; unset means no authentication     |
| `LANGGRAPH_AES_KEY`   | —                         | 16/24/32-byte key for checkpoint encryption (required) |
| `ENGINE_DEV_INSECURE` | `0`                        | Start without an encryption key (development only)     |
| `WORKER_MAX_RUNS`     | `10`                       | Concurrent runs per worker                             |
| `RUN_MAX_ACTIVE_MS`   | `3600000`                  | Active time before a run fails with`RUN_TIMEOUT`       |
| `RENDER_TIMEOUT_SEC`  | `5`                        | Deadline for rendering one node's templates            |

```

- [ ] **Step 2: The API entrypoint**

Create `services/engine/engine/api/main.py` so `uvicorn engine.api.main:app` works:

```python
"""`uvicorn engine.api.main:app`."""
from __future__ import annotations

import contextlib
import logging

from fastapi import FastAPI
from redis.asyncio import Redis

from engine.api.app import create_app
from engine.config import load_config
from engine.db.migrate import prepare_database
from engine.db.pool import make_pool

log = logging.getLogger(__name__)


def build() -> FastAPI:
    config = load_config()
    pool = make_pool(config.database_url)
    redis = Redis.from_url(config.redis_url, decode_responses=True)
    application = create_app(config, pool, redis)

    @application.on_event("startup")
    async def _startup() -> None:
        await prepare_database(config.database_url)
        await pool.open(wait=True)
        if not config.api_token:
            log.warning("ENGINE_API_TOKEN is not set: the API accepts unauthenticated requests")

    @application.on_event("shutdown")
    async def _shutdown() -> None:
        with contextlib.suppress(Exception):
            await redis.aclose()
        await pool.close()

    return application


app = build()
```

If the FastAPI version in use deprecates `on_event`, use a `lifespan` context manager instead — the test
fixture builds the app directly, so either style works there.

- [ ]  **Step 3: Full verification**

Run: `uv run pytest -q`
Expected: every test passes, including integration. Note the count.

Run: `uv run pytest -q -m "not integration"`
Expected: passes without Docker running.

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ]  **Step 4: Confirm the coverage table below still holds, then commit**

```bash
git add services/engine/README.md services/engine/engine/api/main.py docs/superpowers/plans/2026-09-16-runtime-core.md
git commit -m "docs(engine): document running the service and record Plan 2a verification"
```

---

## Design coverage (Plan 2a)


| Design section             | Covered by                                                                            |
| ---------------------------- | --------------------------------------------------------------------------------------- |
| 1 목표와 완료 기준         | Task 16 (all six criteria), Task 17                                                   |
| 2 MVP 문서에서 달라지는 점 | Task 6 (claim/NOTIFY), Task 9 (reaper), Task 1 (channels), Task 3 (psycopg3,`waited`) |
| 3 실행 상태 채널 구조      | Task 1                                                                                |
| 4 CPU 격리                 | Task 2 (hook), Task 10 (pool, deadline, memory)                                       |
| 5 데이터 계층              | Task 3 (schema, migrations, encrypted checkpointer)                                   |
| 6.1 점유                   | Task 6, Task 7                                                                        |
| 6.2 리스와 펜싱            | Task 6, Task 8                                                                        |
| 6.3 취소                   | Task 8, Task 15 (API side)                                                            |
| 6.4 실행 루프              | Task 7, Task 8 (timeout), Task 16 (recovery)                                          |
| 6.5 reaper                 | Task 9                                                                                |
| 7.1 PostgresRecorder       | Task 4                                                                                |
| 7.2 저장 정책              | Task 4 (redaction, truncation,`storeRunData`), Task 15 (waiting payload)              |
| 7.3 SSE                    | Task 14, Task 16                                                                      |
| 8.1 공통 계층              | Task 11                                                                               |
| 8.2 엔드포인트             | Tasks 11–15                                                                          |
| 8.3 레지스트리 하나        | Task 7 (worker), Task 11 (API state)                                                  |
| 8.4 컴파일 캐시            | Task 7                                                                                |
| 9 Redis 사용               | Task 5 (publish, semaphore), Task 8 (control), Task 14 (SSE)                          |
| 10 설정                    | Task 0                                                                                |
| 11 테스트                  | Every task; Task 16 for the integration matrix                                        |
| 12 열린 위험               | Task 0 step 2 (pebble, EncryptedSerializer spikes)                                    |

Deferred to Plan 2b by design: `http_request`, egress policy, secrets and `{{secret.NAME}}`, header/value
redaction, retention purge, the deployment compose file.
