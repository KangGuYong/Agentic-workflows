from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.llm.base import LLMClient, TokenSink
from engine.llm.scripted import ScriptedLLM
from engine.nodes.base import NodeContext

GOLDEN = Path(__file__).parent / "golden"


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
