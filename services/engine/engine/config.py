"""Process configuration from the environment (design 10)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from engine.http.policy import AllowEntry, PolicyError, parse_allowlist

# Long enough that guessing is not a strategy; short enough to type into a compose file. A token below
# this is the same as no token, and "we will set a real one later" is how it reaches production.
MIN_API_TOKEN_LEN = 16


class ConfigError(Exception):
    """A required setting is missing or unusable. Raised at startup, never per request."""


@dataclass(frozen=True)
class EngineConfig:
    database_url: str
    redis_url: str
    api_token: str | None
    secret_key: bytes | None
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
    http_allowlist: tuple[AllowEntry, ...]
    http_max_redirects: int
    http_max_request_bytes: int
    http_max_response_bytes: int
    db_pool_max: int
    retention_days: int
    purge_batch: int
    # Knowledge base (knowledge-base design §2). All optional: an engine without MinerU still ingests
    # .md/.txt, and one without a reranker runs every workflow that has no rerank node.
    mineru_base_url: str | None
    mineru_timeout_sec: float
    rerank_base_url: str | None
    kb_max_file_bytes: int
    ingest_max_jobs: int
    kb_embed_model: str


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Like _int, but refuses a value outside [minimum, maximum]. For the three HTTP egress limits
    below, an unvalidated env var would load fine and only misbehave per-request: -1 redirects or a
    0-byte response cap either breaks every http_request call or (read the other way) disables the
    limit outright, and Task 2's client would inherit whichever happened silently."""
    value = _int(name, default)
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def load_config() -> EngineConfig:
    database_url = os.getenv("ENGINE_DATABASE_URL")
    if not database_url:
        raise ConfigError("ENGINE_DATABASE_URL is required")
    dev_insecure = os.getenv("ENGINE_DEV_INSECURE") == "1"
    if not os.getenv("LANGGRAPH_AES_KEY") and not dev_insecure:
        # Checkpoints hold every node output. Adding encryption later cannot read existing checkpoints,
        # so refuse to start rather than write them in the clear.
        raise ConfigError("LANGGRAPH_AES_KEY is required (set ENGINE_DEV_INSECURE=1 only for development)")
    secret_key_text = os.getenv("ENGINE_SECRET_KEY")
    if not secret_key_text and not dev_insecure:
        # Without it no secret can be sealed or opened, so an http_request node that needs one would fail
        # per request instead of at startup where an operator would see it.
        raise ConfigError("ENGINE_SECRET_KEY is required (set ENGINE_DEV_INSECURE=1 only for development)")
    secret_key = secret_key_text.encode("utf-8") if secret_key_text else None
    if secret_key is not None and len(secret_key) not in (16, 24, 32):
        raise ConfigError("ENGINE_SECRET_KEY must be 16, 24 or 32 bytes")
    api_token = os.getenv("ENGINE_API_TOKEN") or None  # an empty value means "unset", not a zero-length token
    if api_token is None and not dev_insecure:
        # Without it the engine serves every workflow, run and secret name to anyone who can reach the
        # port (2b design §9). It used to be a warning, which is a thing people scroll past.
        raise ConfigError("ENGINE_API_TOKEN is required (set ENGINE_DEV_INSECURE=1 only for development)")
    if api_token is not None and len(api_token) < MIN_API_TOKEN_LEN:
        # Checked even in development: dev mode is permission to run *without* a token, not permission to
        # run with a guessable one.
        raise ConfigError(f"ENGINE_API_TOKEN must be at least {MIN_API_TOKEN_LEN} characters")
    try:
        allowlist = parse_allowlist(os.getenv("HTTP_ALLOWLIST") or "")
    except PolicyError as exc:
        # A typo in the allowlist must not start a server that silently blocks everything, or worse,
        # silently allows something the operator did not mean to open.
        raise ConfigError(f"HTTP_ALLOWLIST: {exc}") from None
    return EngineConfig(
        database_url=database_url,
        redis_url=os.getenv("ENGINE_REDIS_URL") or "redis://localhost:6379/0",
        api_token=api_token,
        secret_key=secret_key,
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
        http_allowlist=allowlist,
        # 10 redirects is generous for any legitimate API and still short enough to bound a redirect
        # loop; 0 is a valid choice (follow none). 1 byte..100 MB keeps a request/response cap from
        # being configured into "unusable" (0) or "unbounded" (unset-equivalent) by a typo.
        http_max_redirects=_bounded_int("HTTP_MAX_REDIRECTS", 3, minimum=0, maximum=10),
        http_max_request_bytes=_bounded_int(
            "HTTP_MAX_REQUEST_BYTES", 1_000_000, minimum=1, maximum=100_000_000
        ),
        http_max_response_bytes=_bounded_int(
            "HTTP_MAX_RESPONSE_BYTES", 5_000_000, minimum=1, maximum=100_000_000
        ),
        db_pool_max=_bounded_int("DB_POOL_MAX", 10, minimum=1, maximum=1000),
        # 1 day .. 10 years: a 0-day retention would purge every run the moment it finished, and a
        # negative one would purge runs that have not finished yet.
        retention_days=_bounded_int("RUN_DATA_RETENTION_DAYS", 30, minimum=1, maximum=3650),
        purge_batch=_bounded_int("RUN_PURGE_BATCH", 100, minimum=1, maximum=10_000),
        mineru_base_url=os.getenv("MINERU_BASE_URL") or None,
        mineru_timeout_sec=float(_bounded_int("MINERU_TIMEOUT_SEC", 600, minimum=1, maximum=86_400)),
        rerank_base_url=os.getenv("RERANK_BASE_URL") or None,
        # 1 byte .. 1 GB: 0 would refuse every upload, unbounded would let one upload fill the database.
        kb_max_file_bytes=_bounded_int("KB_MAX_FILE_BYTES", 50_000_000, minimum=1, maximum=1_000_000_000),
        ingest_max_jobs=_bounded_int("INGEST_MAX_JOBS", 1, minimum=1, maximum=64),
        kb_embed_model=os.getenv("KB_EMBED_MODEL") or "bge-m3",
    )
