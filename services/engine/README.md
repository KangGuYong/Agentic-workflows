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
uv run python -m engine.ingest.main              # ingester (one or more)
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
| `ENGINE_API_TOKEN`     | —                                     | Shared bearer token, at least 16 characters (required unless `ENGINE_DEV_INSECURE=1`) |
| `PORT`                 | `8000`                                 | Port the API listens on under `python -m engine.api.main` (Windows development; see above) |
| `LANGGRAPH_AES_KEY`    | —                                     | 16/24/32-byte key for checkpoint encryption (required unless `ENGINE_DEV_INSECURE=1`) |
| `ENGINE_SECRET_KEY`    | —                                     | 16/24/32-byte key for stored secrets and run payloads (required unless `ENGINE_DEV_INSECURE=1`) |
| `ENGINE_DEV_INSECURE`  | `0`                                    | Start without the keys or the token (development only)           |
| `OLLAMA_BASE_URL`      | `http://localhost:11434`               | Base URL of the Ollama server the LLM gateway calls               |
| `MINERU_BASE_URL`      | (unset)                                | MinerU API server for document parsing; unset limits ingestion to .md/.txt |
| `MINERU_TIMEOUT_SEC`   | `600`                                  | One document's parse timeout                                      |
| `RERANK_BASE_URL`      | (unset)                                | text-embeddings-inference `/rerank` endpoint for the rerank node  |
| `KB_EMBED_MODEL`       | `bge-m3`                               | Ollama embedding model for knowledge bases (1024 dims)            |
| `KB_MAX_FILE_BYTES`    | `50000000`                             | Upload size limit per file                                        |
| `INGEST_MAX_JOBS`      | `1`                                    | Files one ingester processes at once                              |
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
| `HTTP_ALLOWLIST`       | empty                                  | Hosts `http_request` may reach. **Empty blocks every call** (see Operations) |
| `HTTP_MAX_REDIRECTS`   | `3`                                    | Redirect hops an `http_request` may follow (0–10); every hop is re-checked |
| `HTTP_MAX_REQUEST_BYTES` | `1000000`                            | Largest request body `http_request` will send                    |
| `HTTP_MAX_RESPONSE_BYTES` | `5000000`                           | Response bytes read before the node fails                        |
| `RUN_DATA_RETENTION_DAYS` | `30`                                | Days a finished run keeps its payloads (see Operations)          |
| `RUN_PURGE_BATCH`      | `100`                                  | Runs purged per reaper sweep                                      |
| `DB_POOL_MAX`          | `10`                                   | Maximum pooled Postgres connections (the worker also takes `WORKER_MAX_RUNS + 4`) |

`/healthz` sits behind `ENGINE_API_TOKEN` like every other route (design 8.1's shared-token middleware
covers the whole app, docs routes included, with no per-route opt-out -- see `engine/api/security.py`), so
a container healthcheck must send the same bearer token, e.g.
`curl -H "Authorization: Bearer $ENGINE_API_TOKEN" http://localhost:8000/healthz`.

## Deployment

`deploy/docker-compose.yml` runs the whole stack: Postgres, Redis, the API, one or more workers and the
web editor (`apps/web`, published on `WEB_PORT`). Ollama is **not** bundled — it already runs on the
on-prem GPU host, and `OLLAMA_BASE_URL` points at it.
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
docker compose ps             # postgres/redis/api/web healthy, worker running
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

## What a workflow can do

Nine node types: `start`, `end`, `llm`, `classifier`, `condition`, `merge`, `template`, `human_approval`
and `http_request`.

- **`http_request`** — URL, headers and body are templates. `GET`/`PUT`/`DELETE`/`HEAD` retry three times;
  `POST`/`PATCH` run **once**, because a retry can double a side effect. Output is
  `{status, headers, body}`; a non-2xx status fails the node, and `429`/`5xx` are the retryable ones. Only
  hosts in `HTTP_ALLOWLIST` can be reached, and only over the exact scheme and port the entry names.
- **Secrets** — `PUT /secrets/{NAME}` stores a value, `GET /secrets` lists names only,
  `DELETE /secrets/{NAME}` removes one. A value is never readable through the API after it is written.
  Reference one as `{{ secret.NAME }}`, and only in an `http_request` node's URL, headers or body.
- **What a secret looks like everywhere else** — the stored workflow keeps the `{{ secret.NAME }}`
  reference; the recorded node input keeps an opaque per-run marker, or `[REDACTED]` where the field is a
  credential header; anything the server echoes back comes out `[REDACTED]`. The value itself exists in
  plaintext only inside one `http_request` call.

## Node policy

Every node carries a policy — a timeout, a retry spec, and what to do when the attempts run out. A node
may override its type's default with a partial `policy` in the DSL.

`onError` decides the last part:

- **`"fail"`** (default) — the node's failure fails the run.
- **`"default"`** — the node produces `defaultOutput` instead (or, with none given, the node type's own
  fallback) and the run carries on. Downstream nodes see that value like any other, so a `condition`
  reading `{{ http_1.status }}` can route to a manual-handling branch.

A defaulted node leaves **two rows** in `node_runs`: the attempt that failed, and a further attempt
recorded as `defaulted` carrying the stand-in value. Both are kept on purpose — collapsing them into one
row would erase the failure the default was standing in for, and rewriting an attempt that is already
closed is what the recorder's own fence refuses (see `engine/events/recorder.py::_close`).

## Templates

`{{ }}` in a node's config is Jinja-shaped but **not Jinja**: `engine/templates/parser.py` rejects most of
the language and `engine/templates/env.py` allows a short list of filters. The rules a workflow author
actually hits:

- Join values by writing them side by side — `{{ a }}{{ b }}`. `~` and string `+` are rejected.
- Arithmetic is numbers only.
- Loops cannot nest, and nesting depth is capped at 50.
- One rendered field is at most 1,000,000 characters.
- `default(x)` replaces a **missing** value, not `null`. Pass `default(x, true)` when null should count
  as missing too.
- In a `template` node with `format: "json"`, each `{{ }}` inserts one JSON **value**: write
  `{"name": {{ start.name }}}` — no quotes around it, and no `| tojson` (that would double-encode).
  Build strings in a text template node first.
- Values — inputs, node outputs, edited approval values, default outputs — nest at most 100 levels.

The editor shows the same list in the node panel under `템플릿 작성 규칙`
(`apps/web/components/panel/TemplateHelp.tsx`). **That is a copy for readers, not a second source of
truth**: this module is the rule, and changing it here means changing the editor's help text too.

## Operations

**`HTTP_ALLOWLIST`** is a comma-separated list, and **an empty list blocks every `http_request`** — a host
reaches the network only once it is listed. Three forms:

| Entry | Matches |
|---|---|
| `https://api.example.com` | that host, https, port 443 (the scheme's default) |
| `https://*.internal.example.com` | any sub-label — `a.internal.example.com`, `a.b.internal.example.com` — but **not** the bare domain |
| `http://10.0.0.7:8080;allowPrivate` | that host and port, and only with `;allowPrivate` may it resolve to a private, loopback or CGNAT address |

`;allowPrivate` does **not** open link-local addresses: `169.254.169.254` is the cloud metadata endpoint,
and opening one internal API is not consent to reach it. A malformed entry refuses to start rather than
silently blocking or opening something. Every hop of a redirect is re-checked against the same list.

**`RUN_DATA_RETENTION_DAYS`** (default 30). After that many days a finished run keeps its metadata —
status, timings, errors, which nodes ran — and loses its inputs, outputs, node payloads, event payloads
and checkpoints. Retrying a purged run returns `409 RUN_DATA_EXPIRED`, because the checkpoint it would
resume from is gone.

**The two keys and the token.** `LANGGRAPH_AES_KEY`, `ENGINE_SECRET_KEY` and `ENGINE_API_TOKEN` are all
required; `ENGINE_DEV_INSECURE=1` is the only way to start without them, and it means unencrypted
checkpoints, no secret storage and an API that accepts every request. **Rotating either key makes the data
encrypted under the old one unreadable** — checkpoints stop loading, stored secrets read as missing
(`SECRET_NOT_FOUND`) and run payloads come back empty. There is no re-encryption path in this release.

## Not here yet

Multi-tenant workspaces, user accounts, the AI copilot and the RAG knowledge base are all out of scope for
this release.
