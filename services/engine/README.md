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

uv run uvicorn engine.api.main:app --port 8000   # API (Linux; see the Windows note below)
uv run python -m engine.worker.main              # worker (one or more)
```

On Windows, run the API with `uv run python -m engine.api.main` instead of the `uvicorn` CLI. psycopg's
async connections cannot use Windows' default `ProactorEventLoop`, and uvicorn's CLI picks that loop
unconditionally on Windows (`--loop asyncio` included) regardless of the event loop policy in effect, so
`uvicorn engine.api.main:app` fails at startup there. `python -m engine.api.main` drives the same app
through `uvicorn.Server` under this process's own `asyncio.run()`, which does honour the compatible-loop
policy `engine/api/main.py` sets at import time. This only matters for local Windows development --
deployment is Linux, where the default loop is already compatible and either form works.

The API and every worker call `prepare_database` at startup, so any of them can be started first,
including at the same moment: `prepare_database` (`engine/db/migrate.py`) takes a session-level Postgres
advisory lock before running Alembic, so concurrent callers serialize instead of racing to create the same
tables. (Alembic's own `CREATE TABLE` statements have no `IF NOT EXISTS` guard; without the lock, two
processes migrating a brand-new database at once were observed to stall indefinitely rather than fail
cleanly, so this is not just a hypothetical race.)

## Tests

```bash
uv run pytest -q                        # everything (needs Docker for Postgres and Redis)
uv run pytest -q -m "not integration"   # unit tests only, no Docker
uv run ruff check .
```

## Environment

| Variable                | Default                                | Meaning                                                          |
| ------------------------ | ---------------------------------------- | ------------------------------------------------------------------ |
| `ENGINE_DATABASE_URL`  | —                                     | Postgres connection string (required)                            |
| `ENGINE_REDIS_URL`     | `redis://localhost:6379/0`             | Redis for events, control and the model semaphore                |
| `ENGINE_API_TOKEN`     | none                                   | Shared bearer token; unset means no authentication                |
| `LANGGRAPH_AES_KEY`    | —                                     | 16/24/32-byte key for checkpoint encryption (required unless `ENGINE_DEV_INSECURE=1`) |
| `ENGINE_DEV_INSECURE`  | `0`                                    | Start without an encryption key (development only)               |
| `OLLAMA_BASE_URL`      | `http://localhost:11434`               | Base URL of the Ollama server the LLM gateway calls               |
| `OLLAMA_NUM_PARALLEL`  | `1`                                    | Per-model concurrency limit, enforced via the Redis semaphore     |
| `WORKER_MAX_RUNS`      | `10`                                   | Concurrent runs per worker                                        |
| `WORKER_LEASE_SEC`     | `30`                                   | Seconds a worker's claim on a run stays valid before the reaper may recover it |
| `WORKER_HEARTBEAT_SEC` | `10`                                   | How often a worker renews the lease on each run it holds          |
| `WORKER_CLAIM_POLL_SEC`| `5`                                    | Fallback poll interval for claiming runs when `NOTIFY` is missed  |
| `WORKER_REAPER_SEC`    | `15`                                   | How often the reaper sweeps for expired leases and stale waiting runs |
| `RUN_MAX_ACTIVE_MS`    | `3600000`                              | Active time before a run fails with `RUN_TIMEOUT`                |
| `RENDER_TIMEOUT_SEC`   | `5`                                    | Deadline for rendering one node's templates                      |
| `RENDER_POOL_SIZE`     | number of CPUs (`os.cpu_count()`), or `2` if that can't be determined | Worker processes in the template-render pool |
| `MAX_BODY_BYTES`       | `1000000`                              | Maximum accepted HTTP request body size, in bytes                |

`/healthz` sits behind `ENGINE_API_TOKEN` like every other route (design 8.1's shared-token middleware
covers the whole app, docs routes included, with no per-route opt-out -- see `engine/api/security.py`), so
a container healthcheck must send the same bearer token, e.g.
`curl -H "Authorization: Bearer $ENGINE_API_TOKEN" http://localhost:8000/healthz`.

## Not here yet

Deferred to Plan 2b by design: the `http_request` node, egress policy, secrets and `{{secret.NAME}}`,
header/value redaction, retention purge, and the production deployment compose file. `deploy/docker-compose.dev.yml`
is development-only (plaintext credentials, ports published to the host).
