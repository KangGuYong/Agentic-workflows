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

On Windows, run the API with `uv run python -m engine.api.main` instead of the plain `uvicorn` CLI's
default. psycopg's async connections cannot use Windows' default `ProactorEventLoop`, and uvicorn's
`--loop asyncio` (its default, so a bare `uvicorn engine.api.main:app` fails the same way) picks that loop
itself after importing the app, regardless of any event loop policy already in effect -- so that one form
fails at startup there no matter what. `--loop none` and `--reload` are not affected the same way: both
start cleanly on Windows too, because `engine/api/main.py` sets a compatible policy at import time and
neither of those flags overrides it afterward the way `--loop asyncio` does. `python -m engine.api.main`
sidesteps the question entirely by driving the same app through `uvicorn.Server` under this process's own
`asyncio.run()`, which always honours that same import-time policy. This only matters for local Windows
development -- deployment is Linux, where the default loop is already compatible and every form works.

The API and every worker call `prepare_database` at startup, so any of them can be started first,
including at the same moment: `prepare_database` (`engine/db/migrate.py`) takes a session-level Postgres
advisory lock before running Alembic, so concurrent callers serialize instead of racing to create the same
tables. (Alembic's own `CREATE TABLE` statements have no `IF NOT EXISTS` guard; without the lock, racing
two unguarded upgrades against the same brand-new database six times over failed in 0.08-0.11s every time
-- not a hang -- with every caller but one dying on a `UniqueViolation` against a system catalog index or
one of Alembic's own tables, or on a `KeyError` from Alembic's non-thread-safe globals when raced in the
same process. That is a real race with a fast, unclean failure, not just a hypothetical one, which is why
it is worth serializing even though nothing here hangs.) The lock adds a small, constant cost to every
`prepare_database` call, including the common case where the schema is already current: measured against
this repo's own dev database, an already-migrated `prepare_database` went from about 53 ms to about 87 ms
once the lock was added -- worth knowing so it isn't a surprise when it shows up in the test suite.

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
| `PORT`                 | `8000`                                 | Port the API listens on under `python -m engine.api.main` (Windows development; see above) |
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

## Deployment

`deploy/docker-compose.yml` runs the whole engine: Postgres, Redis, the API and one or more workers.
Ollama is **not** bundled — it already runs on the on-prem GPU host, and `OLLAMA_BASE_URL` points at it.
(`deploy/docker-compose.dev.yml` is the development stack instead: plaintext credentials, ports published
to the host, no engine containers.)

```bash
cd deploy
cp .env.example .env          # then fill it in; .env is git-ignored

# the two keys (32 random ASCII characters each; 16 and 24 also work)
python -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(32)))"
# the API token (at least 16 characters)
python -c "import secrets; print(secrets.token_urlsafe(32))"

docker compose up -d --build
docker compose ps             # postgres/redis/api healthy, worker running
```

Every route, `/healthz` included, requires `Authorization: Bearer $ENGINE_API_TOKEN`; the API container's
own healthcheck carries it for that reason. Workers scale with `docker compose up -d --scale worker=3` —
leases, fencing and the reaper's advisory lock were built for more than one, and one worker wins the
reaper's lock each sweep while the others skip it.

Both processes run `prepare_database` at startup, so migrations apply on the first `up` with no separate
step; concurrent starts serialise on an advisory lock.

**What you must set, and what happens if you get it wrong:**

| Variable | Consequence |
|---|---|
| `LANGGRAPH_AES_KEY` | Encrypts checkpoints. **Rotating it makes every existing checkpoint unreadable** — there is no re-encryption path in this release. |
| `ENGINE_SECRET_KEY` | Encrypts stored secrets and run inputs/outputs. **Rotating it makes them unreadable too**: secrets read as missing (`SECRET_NOT_FOUND`) and run payloads come back empty. Deliberately a different key from the one above, so one leaking does not open the other. |
| `ENGINE_API_TOKEN` | Required, at least 16 characters. Without it the engine refuses to start unless `ENGINE_DEV_INSECURE=1`, which accepts every request unauthenticated. |
| `HTTP_ALLOWLIST` | **Empty blocks every `http_request` node.** That is the default on purpose: a host reaches the network only once it is listed. |
| `RUN_DATA_RETENTION_DAYS` | After this many days a finished run keeps its metadata and loses its payloads and checkpoints. |

## Not here yet

Multi-tenant workspaces, user accounts, the AI copilot and the RAG knowledge base are all out of scope for
this release.
