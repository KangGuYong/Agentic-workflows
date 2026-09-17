"""Process configuration from the environment (design 10)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from engine.http.policy import AllowEntry, PolicyError, parse_allowlist


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
    http_allowlist: tuple[AllowEntry, ...]
    http_max_redirects: int
    http_max_request_bytes: int
    http_max_response_bytes: int


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
    try:
        allowlist = parse_allowlist(os.getenv("HTTP_ALLOWLIST") or "")
    except PolicyError as exc:
        # A typo in the allowlist must not start a server that silently blocks everything, or worse,
        # silently allows something the operator did not mean to open.
        raise ConfigError(f"HTTP_ALLOWLIST: {exc}") from None
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
        http_allowlist=allowlist,
        http_max_redirects=_int("HTTP_MAX_REDIRECTS", 3),
        http_max_request_bytes=_int("HTTP_MAX_REQUEST_BYTES", 1_000_000),
        http_max_response_bytes=_int("HTTP_MAX_RESPONSE_BYTES", 5_000_000),
    )
