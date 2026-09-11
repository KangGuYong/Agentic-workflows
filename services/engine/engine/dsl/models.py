from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NODE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Position(_Strict):
    x: float = 0
    y: float = 0


class RetrySpec(_Strict):
    maxAttempts: int = Field(3, ge=1, le=5)
    backoff: Literal["fixed", "exponential"] = "exponential"
    initialDelaySec: float = Field(2.0, ge=0.5, le=30)


class Policy(_Strict):
    timeoutSec: float = Field(120, ge=1, le=600)
    retry: RetrySpec = Field(default_factory=RetrySpec)
    onError: Literal["fail", "default"] = "fail"
    defaultOutput: dict[str, Any] | None = None


class Node(_Strict):
    id: str = Field(pattern=NODE_ID_PATTERN)
    type: str = Field(min_length=1)
    label: str = ""
    position: Position = Field(default_factory=Position)
    config: dict[str, Any] = Field(default_factory=dict)
    # Kept raw: a partial override merged onto the node type's default policy (see merge_policy).
    policy: dict[str, Any] | None = None


class Edge(_Strict):
    id: str = Field(min_length=1, max_length=64)
    source: str
    sourceHandle: str = "out"
    target: str
    maxIterations: int | None = Field(None, ge=1, le=20)


class Settings(_Strict):
    storeRunData: bool = True


class WorkflowDSL(_Strict):
    version: Literal["1"] = "1"
    settings: Settings = Field(default_factory=Settings)
    nodes: list[Node]
    edges: list[Edge]


def dsl_hash(dsl: WorkflowDSL) -> str:
    """SHA-256 of the canonical DSL, ignoring editor-only fields (position, label)."""
    data = dsl.model_dump(mode="json", exclude={"nodes": {"__all__": {"position", "label"}}})
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def merge_policy(default: Policy, override: dict[str, Any] | None) -> Policy:
    """Apply a partial policy override on top of a node type's default policy."""
    if not override:
        return default
    merged = default.model_dump()
    for key, value in override.items():
        if key == "retry" and isinstance(value, dict):
            merged["retry"] = {**merged["retry"], **value}
        else:
            merged[key] = value
    return Policy.model_validate(merged)
