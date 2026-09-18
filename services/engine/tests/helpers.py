from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from engine.config import EngineConfig
from engine.llm.base import LLMClient, TokenSink
from engine.llm.scripted import ScriptedLLM
from engine.nodes.base import NodeContext

GOLDEN = Path(__file__).parent / "golden"

_BASE_CONFIG = EngineConfig(
    database_url="postgresql://u:p@h/db", redis_url="redis://h:6379/0", api_token=None,
    # The same key the `db_url` fixture puts in the environment, so a test that seals a secret through
    # the config and one that seals it through the fixtures agree.
    secret_key=b"1" * 32, encrypt_checkpoints=False, ollama_base_url="http://localhost:11434", ollama_num_parallel=1,
    worker_max_runs=1, lease_sec=1, heartbeat_sec=1, claim_poll_sec=1.0, reaper_interval_sec=1.0,
    run_max_active_ms=1000, render_timeout_sec=1.0, render_pool_size=1, max_body_bytes=1000,
    http_allowlist=(), http_max_redirects=3, http_max_request_bytes=1000, http_max_response_bytes=1000,
    db_pool_max=10, retention_days=30, purge_batch=100,
)


def make_config(**overrides: Any) -> EngineConfig:
    """An EngineConfig with test-friendly defaults (short timeouts, no token). Every field is spelled
    out once here rather than at each call site, so a new required field breaks one place, not every
    test that hand-built a config."""
    return dataclasses.replace(_BASE_CONFIG, **overrides)


def make_ctx(
    *,
    llm: LLMClient | None = None,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    pred_ids: list[str] | None = None,
    node_id: str = "n",
    interrupt=None,
    on_token: TokenSink | None = None,
) -> NodeContext:
    return NodeContext(
        run_id="run-test",
        node_id=node_id,
        exec_index=1,
        attempt=1,
        inputs=inputs or {},
        outputs=outputs or {},
        pred_ids=pred_ids or [],
        llm=llm or ScriptedLLM([]),
        on_token=on_token,
        interrupt=interrupt,
    )


def load_golden(name: str) -> dict[str, Any]:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
