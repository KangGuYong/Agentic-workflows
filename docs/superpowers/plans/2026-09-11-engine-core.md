# Engine Core (Plan 1 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure-Python workflow engine core — DSL models, type system, sandboxed templates, node library, validator, DSL→LangGraph compiler and an in-process runner — fully testable without Postgres/Redis/Ollama.

**Architecture:** A `WorkflowDSL` (Pydantic) is validated in three phases (structure → graph → references/types). A validated DSL compiles to a LangGraph `StateGraph` whose every node is wrapped by one generic wrapper that owns template rendering, retries, timeouts, `onError`, exec_index/attempt bookkeeping and branch routing. Per-run dependencies (LLM client, recorder, cancellation guard) are injected through LangGraph's `context=` so the compiled graph is reusable across runs. Recording is behind a `Recorder` protocol (in-memory here; Postgres in Plan 2).

**Tech Stack:** Python 3.11, uv, Pydantic 2.13, Jinja2 3.1 (SandboxedEnvironment), jsonschema 4.26, httpx 0.28, LangGraph 1.2.11, pytest 9 + pytest-asyncio 1.4.

**Spec:** `docs/superpowers/specs/2026-09-11-agentic-workflow-builder-mvp-design.md` (sections 4, 5.3–5.10, 6.1, 6.2, 6.4). Plans 2 (runtime service) and 3 (web editor) are scoped in `docs/superpowers/plans/2026-09-11-roadmap.md`.

**Verified library behaviors** (spike on 2026-09-11, pinned by Task 1 tests): join edge `add_edge([a, b], m)` waits for all sources; completed parallel siblings are not re-run after a failure (pending writes, `durability="sync"`); an interrupted node re-executes from its start on resume; `Runtime[Deps]` + `ainvoke(..., context=deps)` gives per-invocation deps; `GraphInterrupt` subclasses `Exception`; Jinja `ChainableUndefined`+`StrictUndefined` makes `{{ a.b | default('') }}` work while bare missing refs still fail.

**Conventions**

- All commands run from `services/engine/` unless stated otherwise.
- DSL field names are camelCase in Python too (they mirror the JSON contract 1:1).
- Every code block is preceded by `**File:** \`path\`` and contains the **complete** file content (later tasks re-show whole files they change).
- User-facing messages (validation issues, node errors) are Korean; code identifiers and log text are English.

---

## File Structure

```
services/engine/
  pyproject.toml
  engine/
    __init__.py
    errors.py                 # ErrorCode, NodeError, NodeFailedError, RunCancelled
    dsl/
      __init__.py
      models.py               # WorkflowDSL, Node, Edge, Policy, RetrySpec, dsl_hash, merge_policy
      types.py                # kinds_of, resolve_path, compat, coerce_runtime, to_text
    templates/
      __init__.py
      env.py                  # sandboxed Jinja env, ALLOWED_FILTERS, ChainableStrictUndefined
      parser.py               # parse_template -> ParsedTemplate(refs, whole_value, problems)
      render.py               # render_template, TemplateRenderError, TemplateTypeError
    llm/
      __init__.py
      base.py                 # ChatMessage, ChatResult, LLMClient protocol
      scripted.py             # ScriptedLLM test double (shipped: Plan 2 tests reuse it)
      gateway.py              # LLMGateway: structured output + repair loop over a RawLLM
      ollama.py               # OllamaRaw: /api/chat transport (non-stream + NDJSON stream)
    nodes/
      __init__.py
      base.py                 # NodeSpec, NodeContext, NodeResult, TemplateField, Usage
      io.py                   # start, end
      template.py             # template
      llm.py                  # llm
      classifier.py           # classifier
      condition.py            # condition
      merge.py                # merge
      human_approval.py       # human_approval
      registry.py             # NodeRegistry, default_registry()
    validator/
      __init__.py             # validate(), analyze()
      issues.py               # Issue, has_errors
      structure.py            # phase 1: ids, types, config, policy, edges, limits
      graph.py                # phase 2: Graph, back-edges, reachability, loops, parallel regions
      refs.py                 # phase 3: before-sets, output schemas, template refs & types
    compiler/
      __init__.py
      state.py                # RunState, merge_dicts, initial_state
      routing.py              # resolve_route (branch handle -> targets, loop counters)
      wrapper.py              # make_node_fn: render/retry/timeout/onError/record/route
      build.py                # compile_workflow -> CompiledWorkflow
    runtime/
      __init__.py
      recorder.py             # Recorder protocol, InMemoryRecorder, NodeRunRecord
      guard.py                # RunGuard protocol, NoopGuard, FlagGuard
      deps.py                 # RunDeps (context passed to LangGraph)
      runner.py               # execute_run -> RunOutcome
  tests/
    __init__.py
    helpers.py                # make_ctx, load_golden
    characterization/test_langgraph_contract.py
    golden/*.json             # chaining, routing, parallel, evaluator_loop, hitl
    test_*.py
```

---

## Task 0: Project scaffold

**Files:**

- Create: `.gitignore`
- Create: `services/engine/pyproject.toml`
- Create: `services/engine/engine/__init__.py`
- Create: `services/engine/tests/__init__.py`
- Test: `services/engine/tests/test_smoke.py`

- [ ]  **Step 1: Create the repo `.gitignore`**

**File:** `.gitignore`

```gitignore
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
.ruff_cache/
node_modules/
.env
```

- [ ]  **Step 2: Create the engine project**

**File:** `services/engine/pyproject.toml`

```toml
[project]
name = "engine"
version = "0.1.0"
description = "Agentic workflow engine core: DSL, validator, LangGraph compiler"
requires-python = ">=3.11"
dependencies = [
    "pydantic>=2.13,<3",
    "jinja2>=3.1.6,<4",
    "jsonschema>=4.26,<5",
    "httpx>=0.28,<1",
    "langgraph>=1.2.11,<1.3",
]

[dependency-groups]
dev = ["pytest>=9,<10", "pytest-asyncio>=1.4,<2", "ruff>=0.8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["engine"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]

[tool.ruff]
line-length = 110
target-version = "py311"

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "SIM"]
```

**File:** `services/engine/engine/__init__.py`

```python
__version__ = "0.1.0"
```

**File:** `services/engine/tests/__init__.py`

```python

```

- [ ]  **Step 3: Write the smoke test**

**File:** `services/engine/tests/test_smoke.py`

```python
import engine


def test_package_imports():
    assert engine.__version__ == "0.1.0"
```

- [ ]  **Step 4: Install and run**

Run: `uv sync && uv run pytest -q`
Expected: `1 passed`

- [ ]  **Step 5: Commit**

```bash
git add .gitignore services/engine/pyproject.toml services/engine/uv.lock services/engine/engine services/engine/tests
git commit -m "chore(engine): scaffold python engine project"
```

---

## Task 1: Pin the LangGraph behaviors we depend on

These are characterization tests: they pass immediately against LangGraph 1.2.11. Their job is to fail loudly if an upgrade changes semantics the engine is built on.

**Files:**

- Create: `services/engine/tests/characterization/__init__.py`
- Test: `services/engine/tests/characterization/test_langgraph_contract.py`

- [ ]  **Step 1: Write the tests**

**File:** `services/engine/tests/characterization/__init__.py`

```python

```

**File:** `services/engine/tests/characterization/test_langgraph_contract.py`

```python
"""Pin the LangGraph behaviors the engine relies on (spec 5.3, 5.6, 5.7).

If a LangGraph upgrade breaks one of these, the engine's execution semantics break too.
"""
import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt


def _merge(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class S(TypedDict, total=False):
    outputs: Annotated[dict[str, Any], _merge]
    log: Annotated[list[str], operator.add]


def _node(name: str, calls: dict[str, int], fail_times: int = 0):
    async def fn(state: S) -> dict:
        calls[name] = calls.get(name, 0) + 1
        if calls[name] <= fail_times:
            raise RuntimeError(f"{name} failed")
        return {"outputs": {name: calls[name]}, "log": [name]}

    return fn


def _cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


async def test_join_edge_waits_for_all_branches_of_unequal_length():
    calls: dict[str, int] = {}
    g = StateGraph(S)
    for name in ["s", "a1", "a2", "b", "m"]:
        g.add_node(name, _node(name, calls))
    g.add_edge(START, "s")
    g.add_edge("s", "a1")
    g.add_edge("s", "b")
    g.add_edge("a1", "a2")
    g.add_edge(["a2", "b"], "m")
    g.add_edge("m", END)
    app = g.compile(checkpointer=InMemorySaver())

    result = await app.ainvoke({}, _cfg("join"))

    assert calls["m"] == 1
    assert result["log"][-1] == "m"


async def test_completed_parallel_sibling_is_not_rerun_after_failure():
    calls: dict[str, int] = {}
    g = StateGraph(S)
    g.add_node("s", _node("s", calls))
    g.add_node("ok", _node("ok", calls))
    g.add_node("bad", _node("bad", calls, fail_times=1))
    g.add_node("m", _node("m", calls))
    g.add_edge(START, "s")
    g.add_edge("s", "ok")
    g.add_edge("s", "bad")
    g.add_edge(["ok", "bad"], "m")
    g.add_edge("m", END)
    app = g.compile(checkpointer=InMemorySaver())

    with pytest.raises(RuntimeError, match="bad failed"):
        await app.ainvoke({}, _cfg("pw"), durability="sync")
    assert (await app.aget_state(_cfg("pw"))).next == ("bad",)

    await app.ainvoke(None, _cfg("pw"), durability="sync")

    assert calls == {"s": 1, "ok": 1, "bad": 2, "m": 1}


async def test_interrupted_node_reexecutes_from_start_on_resume():
    calls: dict[str, int] = {}

    async def approve(state: S) -> dict:
        calls["approve"] = calls.get("approve", 0) + 1
        answer = interrupt({"ask": "ok?"})
        return {"outputs": {"approve": answer}}

    g = StateGraph(S)
    g.add_node("approve", approve)
    g.add_edge(START, "approve")
    g.add_edge("approve", END)
    app = g.compile(checkpointer=InMemorySaver())

    await app.ainvoke({}, _cfg("hitl"))
    snapshot = await app.aget_state(_cfg("hitl"))
    assert snapshot.tasks[0].interrupts[0].value == {"ask": "ok?"}

    result = await app.ainvoke(Command(resume="yes"), _cfg("hitl"))

    assert result["outputs"]["approve"] == "yes"
    assert calls["approve"] == 2


@dataclass
class Deps:
    tag: str


async def test_runtime_context_is_per_invocation_and_fresh_thread_is_empty():
    seen: list[str] = []

    async def node(state: S, runtime: Runtime[Deps]) -> dict:
        seen.append(runtime.context.tag)
        return {"outputs": {"n": runtime.context.tag}}

    g = StateGraph(S, context_schema=Deps)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    app = g.compile(checkpointer=InMemorySaver())

    assert (await app.aget_state(_cfg("never-run"))).values == {}
    await app.ainvoke({}, _cfg("c1"), context=Deps("a"))
    await app.ainvoke({}, _cfg("c2"), context=Deps("b"))

    assert seen == ["a", "b"]
```

- [ ]  **Step 2: Run**

Run: `uv run pytest tests/characterization -v`
Expected: `4 passed`

- [ ]  **Step 3: Commit**

```bash
git add services/engine/tests/characterization
git commit -m "test(engine): pin langgraph behaviors the engine relies on"
```

---

## Task 2: Errors and DSL models

**Files:**

- Create: `services/engine/engine/errors.py`
- Create: `services/engine/engine/dsl/__init__.py`
- Create: `services/engine/engine/dsl/models.py`
- Test: `services/engine/tests/test_dsl_models.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_dsl_models.py`

```python
import pytest
from pydantic import ValidationError

from engine.dsl.models import Policy, RetrySpec, WorkflowDSL, dsl_hash, merge_policy
from engine.errors import ErrorCode, NodeError


def _dsl(**overrides) -> dict:
    base = {
        "nodes": [
            {"id": "start", "type": "start", "label": "시작", "position": {"x": 0, "y": 0}},
            {"id": "end", "type": "end", "config": {"outputs": {"r": "{{start.x}}"}}},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "end"}],
    }
    base.update(overrides)
    return base


def test_parses_minimal_dsl_with_defaults():
    dsl = WorkflowDSL.model_validate(_dsl())
    assert dsl.version == "1"
    assert dsl.settings.storeRunData is True
    assert dsl.edges[0].sourceHandle == "out"
    assert dsl.nodes[1].policy is None


@pytest.mark.parametrize("bad_id", ["Start", "1abc", "a-b", "x" * 41, ""])
def test_rejects_invalid_node_ids(bad_id):
    raw = _dsl()
    raw["nodes"][0]["id"] = bad_id
    with pytest.raises(ValidationError):
        WorkflowDSL.model_validate(raw)


def test_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        WorkflowDSL.model_validate(_dsl(extra=1))


def test_rejects_max_iterations_out_of_range():
    raw = _dsl()
    raw["edges"][0]["maxIterations"] = 21
    with pytest.raises(ValidationError):
        WorkflowDSL.model_validate(raw)


def test_hash_ignores_position_and_label_but_not_config():
    base = WorkflowDSL.model_validate(_dsl())
    moved = _dsl()
    moved["nodes"][0]["position"] = {"x": 500, "y": 1}
    moved["nodes"][0]["label"] = "다른 이름"
    changed = _dsl()
    changed["nodes"][1]["config"] = {"outputs": {"r": "{{start.y}}"}}
    assert dsl_hash(base) == dsl_hash(WorkflowDSL.model_validate(moved))
    assert dsl_hash(base) != dsl_hash(WorkflowDSL.model_validate(changed))


def test_merge_policy_overrides_only_given_fields():
    default = Policy(timeoutSec=120, retry=RetrySpec(maxAttempts=3))
    merged = merge_policy(default, {"timeoutSec": 30, "retry": {"maxAttempts": 1}})
    assert merged.timeoutSec == 30
    assert merged.retry.maxAttempts == 1
    assert merged.retry.backoff == "exponential"
    assert merged.onError == "fail"


def test_merge_policy_without_override_returns_default():
    default = Policy()
    assert merge_policy(default, None) is default


def test_merge_policy_rejects_invalid_values():
    with pytest.raises(ValidationError):
        merge_policy(Policy(), {"retry": {"maxAttempts": 9}})


def test_node_error_serializes_code_and_message():
    err = NodeError(ErrorCode.NODE_TIMEOUT, "시간 초과", retryable=True)
    assert err.to_dict() == {"code": "NODE_TIMEOUT", "message": "시간 초과"}
    assert err.retryable is True
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_dsl_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.dsl'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/errors.py`

```python
from enum import StrEnum


class ErrorCode(StrEnum):
    NODE_TIMEOUT = "NODE_TIMEOUT"
    NODE_FAILED = "NODE_FAILED"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    TEMPLATE_ERROR = "TEMPLATE_ERROR"
    STRUCTURED_OUTPUT_FAILED = "STRUCTURED_OUTPUT_FAILED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    ENGINE_RECURSION_LIMIT = "ENGINE_RECURSION_LIMIT"


class NodeError(Exception):
    """Failure of a single node attempt. `retryable` decides whether the policy may retry it."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict[str, str]:
        return {"code": str(self.code), "message": self.message}


class NodeFailedError(Exception):
    """Raised by the node wrapper when a node exhausted its attempts with onError=fail."""

    def __init__(self, node_id: str, error: NodeError) -> None:
        super().__init__(f"node {node_id} failed: {error.message}")
        self.node_id = node_id
        self.error = error


class RunCancelled(Exception):
    """Raised when the run guard observes a cancellation request or a lost lease."""
```

**File:** `services/engine/engine/dsl/__init__.py`

```python

```

**File:** `services/engine/engine/dsl/models.py`

```python
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
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_dsl_models.py -v`
Expected: all PASS (13 tests)

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/errors.py services/engine/engine/dsl services/engine/tests/test_dsl_models.py
git commit -m "feat(engine): add DSL models, policy merge and error types"
```

---

## Task 3: Type system

Spec 4.5. Types are derived from JSON Schema; `compat` drives validation severities, `coerce_runtime` drives the runtime check.

**Files:**

- Create: `services/engine/engine/dsl/types.py`
- Test: `services/engine/tests/test_types.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_types.py`

```python
import pytest

from engine.dsl.types import coerce_runtime, compat, kinds_of, resolve_path, to_text

OBJ = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "score": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "meta": {"type": "object"},
        "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["text"],
}


def test_kinds_of_basic_and_unions():
    assert kinds_of({"type": "integer"}) == {"number"}
    assert kinds_of({"type": ["string", "null"]}) == {"string", "null"}
    assert kinds_of(OBJ["properties"]["maybe"]) == {"string", "null"}
    assert kinds_of({"enum": ["a", "b"]}) == {"string"}
    assert kinds_of({"properties": {}}) == {"object"}
    assert kinds_of({}) == {"unknown"}
    assert kinds_of(None) == {"unknown"}


def test_resolve_path():
    assert resolve_path(OBJ, ()) == OBJ
    assert resolve_path(OBJ, ("text",)) == {"type": "string"}
    assert resolve_path(OBJ, ("tags", "0", "name")) == {"type": "string"}
    assert resolve_path(OBJ, ("nope",)) is None
    assert resolve_path(OBJ, ("text", "deeper")) is None
    assert resolve_path(OBJ, ("meta", "anything")) == {}
    assert resolve_path({}, ("a", "b")) == {}


def test_resolve_path_through_nullable_object():
    schema = {"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}}}, {"type": "null"}]}
    assert resolve_path(schema, ("a",)) == {"type": "string"}


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ({"string"}, "string", "ok"),
        ({"number"}, "string", "ok"),
        ({"object"}, "string", "warning"),
        ({"unknown"}, "string", "ok"),
        ({"string"}, "number", "error"),
        ({"number"}, "number", "ok"),
        ({"unknown"}, "number", "warning"),
        ({"number", "null"}, "number", "warning"),
        ({"array"}, "string|array", "ok"),
        ({"unknown"}, "string|array", "warning"),
        ({"number"}, "string|array", "error"),
        ({"object"}, "any", "ok"),
        ({"string", "object"}, "number", "error"),
    ],
)
def test_compat(source, target, expected):
    assert compat(source, target) == expected


def test_to_text():
    assert to_text("a") == "a"
    assert to_text(3) == "3"
    assert to_text(2.5) == "2.5"
    assert to_text(True) == "true"
    assert to_text(None) == ""
    assert to_text({"k": "값"}) == '{"k": "값"}'
    assert to_text([1, 2]) == "[1, 2]"


def test_coerce_runtime_accepts_compatible_values():
    assert coerce_runtime({"a": 1}, "any") == {"a": 1}
    assert coerce_runtime(5, "string") == "5"
    assert coerce_runtime("7.5", "number") == 7.5
    assert coerce_runtime(3, "number") == 3
    assert coerce_runtime([1], "string|array") == [1]


@pytest.mark.parametrize(("value", "target"), [("abc", "number"), (True, "number"), (None, "number"), (5, "string|array")])
def test_coerce_runtime_rejects_incompatible_values(value, target):
    with pytest.raises(TypeError):
        coerce_runtime(value, target)
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.dsl.types'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/dsl/types.py`

```python
"""Type system for template references (spec 4.5), derived from JSON Schema."""
from __future__ import annotations

import json
from typing import Any, Literal

Target = Literal["string", "number", "boolean", "object", "array", "any", "string|array"]
Severity = Literal["ok", "warning", "error"]

_JSON_TO_KIND = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
    "null": "null",
}
_RANK = {"ok": 0, "warning": 1, "error": 2}


def kinds_of(schema: dict[str, Any] | None) -> set[str]:
    """Value kinds a JSON Schema admits. {"unknown"} when the schema says nothing."""
    if not schema:
        return {"unknown"}
    branches = (schema.get("anyOf") or []) + (schema.get("oneOf") or [])
    if branches:
        kinds: set[str] = set()
        for branch in branches:
            kinds |= kinds_of(branch)
        return kinds
    declared = schema.get("type")
    if declared is None:
        if "enum" in schema:
            return {_kind_of_value(v) for v in schema["enum"]}
        if "properties" in schema:
            return {"object"}
        if "items" in schema:
            return {"array"}
        return {"unknown"}
    names = declared if isinstance(declared, list) else [declared]
    return {_JSON_TO_KIND.get(name, "unknown") for name in names}


def _kind_of_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _strip_null(schema: dict[str, Any]) -> dict[str, Any]:
    branches = schema.get("anyOf") or schema.get("oneOf")
    if branches:
        non_null = [b for b in branches if kinds_of(b) != {"null"}]
        if len(non_null) == 1:
            return non_null[0]
    return schema


def _is_unknown(schema: dict[str, Any]) -> bool:
    return kinds_of(schema) == {"unknown"} and "properties" not in schema and "items" not in schema


def resolve_path(schema: dict[str, Any] | None, path: tuple[str, ...]) -> dict[str, Any] | None:
    """Sub-schema at `path`. `{}` means unknown (not checkable); `None` means the field does not exist."""
    current: dict[str, Any] = schema or {}
    for key in path:
        current = _strip_null(current)
        if _is_unknown(current):
            return {}
        if key.isdigit() and "items" in current:
            current = current["items"] or {}
            continue
        props = current.get("properties")
        if props is not None:
            if key in props:
                current = props[key]
                continue
            extra = current.get("additionalProperties")
            if isinstance(extra, dict):
                current = extra
                continue
            return None
        if "object" in kinds_of(current):
            return {}
        return None
    return current


def compat(source: set[str], target: Target) -> Severity:
    """Severity of passing a value of `source` kinds into a field expecting `target` (spec 4.5 table)."""
    if target == "any":
        return "ok"
    worst: Severity = "ok"
    for kind in source or {"unknown"}:
        severity = _compat_one(kind, target)
        if _RANK[severity] > _RANK[worst]:
            worst = severity
    return worst


def _compat_one(kind: str, target: str) -> Severity:
    if kind == "null":
        return "warning"
    if kind == "unknown":
        return "ok" if target == "string" else "warning"
    if target == "string":
        return "warning" if kind in ("object", "array") else "ok"
    if target == "string|array":
        return "ok" if kind in ("string", "array") else "error"
    return "ok" if kind == target else "error"


def to_text(value: Any) -> str:
    """How a value looks when interpolated into a string."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def coerce_runtime(value: Any, target: Target) -> Any:
    """Runtime type check for a rendered field. Raises TypeError with a Korean message on mismatch."""
    if target == "any":
        return value
    if target == "string":
        return to_text(value)
    if target == "number":
        if isinstance(value, bool) or value is None:
            raise TypeError(f"숫자가 필요하지만 {_kind_of_value(value)} 값을 받았습니다")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                raise TypeError(f"숫자가 필요하지만 {value!r} 문자열을 받았습니다") from None
        raise TypeError(f"숫자가 필요하지만 {_kind_of_value(value)} 값을 받았습니다")
    if target == "string|array":
        if isinstance(value, (str, list)):
            return value
        raise TypeError(f"문자열 또는 배열이 필요하지만 {_kind_of_value(value)} 값을 받았습니다")
    expected = {"boolean": bool, "object": dict, "array": list}[target]
    if isinstance(value, expected):
        return value
    raise TypeError(f"{target} 값이 필요하지만 {_kind_of_value(value)} 값을 받았습니다")
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_types.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/dsl/types.py services/engine/tests/test_types.py
git commit -m "feat(engine): add template reference type system"
```

---

> **Post-review fix (applied during execution, separate commit):** `resolve_path` returns `{}` (unknown) for `additionalProperties: true` and for digit keys into arrays without a single `items` schema (non-digit keys on arrays still return `None`); `coerce_runtime` rejects non-finite numeric strings (`nan`, `inf`). Four tests were appended to `tests/test_types.py` (28 cases; suite 46).

---

## Task 4: Sandboxed template environment and parser

Spec 4.4. The parser is used by the validator (static checks) and the renderer (whole-value detection).

**Files:**

- Create: `services/engine/engine/templates/__init__.py`
- Create: `services/engine/engine/templates/env.py`
- Create: `services/engine/engine/templates/parser.py`
- Test: `services/engine/tests/test_template_parser.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_template_parser.py`

```python
import pytest

from engine.templates.parser import Ref, TemplateParseError, parse_template


def test_extracts_refs_with_paths_in_source_order():
    parsed = parse_template("안녕 {{ start.name }}, {{ llm_1.data.items[0].title }}")
    assert [(r.root, r.path) for r in parsed.refs] == [
        ("start", ("name",)),
        ("llm_1", ("data", "items", "0", "title")),
    ]
    assert parsed.whole_value is None
    assert parsed.problems == ()


def test_whole_value_reference():
    parsed = parse_template("{{ llm_1.text }}")
    assert parsed.whole_value == Ref("llm_1", ("text",), has_default=False, direct=True)
    assert parsed.expression == "llm_1.text"


def test_whole_value_with_default():
    parsed = parse_template("{{ llm_2.text | default('') }}")
    assert parsed.whole_value == Ref("llm_2", ("text",), has_default=True, direct=True)
    assert parsed.expression == "llm_2.text | default('')"


def test_dynamic_index_is_not_a_whole_value():
    parsed = parse_template("{{ m.branches[i] }}")
    assert parsed.whole_value is None
    assert [(r.root, r.path) for r in parsed.refs] == [("m", ("branches",)), ("i", ())]


def test_loop_variables_are_not_refs():
    parsed = parse_template("{% for x in merge_1.branches %}{{ x.text }}{{ loop.index }}{% endfor %}")
    assert parsed.refs == (Ref("merge_1", ("branches",), has_default=False, direct=False),)


def test_direct_flag_distinguishes_printed_refs_from_filtered_ones():
    parsed = parse_template("{{ a.obj }} {{ a.obj | tojson }}")
    assert [r.direct for r in parsed.refs] == [True, False]


@pytest.mark.parametrize(
    "source",
    ["{{ x.__class__ }}", "{{ a | safe }}", "{{ range(3) }}", "{% set y = 1 %}", "{% if a is defined %}{% endif %}"],
)
def test_reports_forbidden_constructs(source):
    assert parse_template(source).problems != ()


def test_syntax_error_raises():
    with pytest.raises(TemplateParseError):
        parse_template("{{ a. }}")


def test_plain_text():
    parsed = parse_template("그냥 텍스트")
    assert parsed.refs == ()
    assert parsed.whole_value is None
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_template_parser.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.templates'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/templates/__init__.py`

```python

```

**File:** `services/engine/engine/templates/env.py`

```python
"""Sandboxed Jinja2 environment shared by the parser (validation) and the renderer (execution)."""
from __future__ import annotations

import json
from typing import Any

from jinja2 import ChainableUndefined, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from engine.dsl.types import to_text

ALLOWED_FILTERS = frozenset({"default", "tojson", "length", "upper", "lower", "trim", "join", "round"})


class ChainableStrictUndefined(ChainableUndefined, StrictUndefined):
    """`a.b` on a missing `a` stays undefined (so `| default()` works); printing/iterating/testing it fails."""

    __slots__ = ()


def _finalize(value: Any) -> Any:
    return value if isinstance(value, str) else to_text(value)


def _tojson(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent)


def make_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        undefined=ChainableStrictUndefined,
        autoescape=False,
        finalize=_finalize,
        keep_trailing_newline=True,
    )
    builtin = env.filters
    env.filters = {name: builtin[name] for name in ALLOWED_FILTERS if name != "tojson"}
    env.filters["tojson"] = _tojson
    env.tests = {}
    env.globals = {}
    return env


ENV = make_env()
```

**File:** `services/engine/engine/templates/parser.py`

```python
"""Static analysis of template fields: references, whole-value detection, forbidden constructs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from jinja2 import nodes
from jinja2.exceptions import TemplateSyntaxError

from engine.templates.env import ALLOWED_FILTERS, ENV

_FORBIDDEN = (
    nodes.Call,
    nodes.Assign,
    nodes.AssignBlock,
    nodes.Macro,
    nodes.CallBlock,
    nodes.FilterBlock,
    nodes.Include,
    nodes.Import,
    nodes.FromImport,
    nodes.Extends,
    nodes.Block,
    nodes.With,
    nodes.Scope,
    nodes.Test,
)
_WHOLE = re.compile(r"\s*\{\{-?(.*?)-?\}\}\s*", re.DOTALL)


class TemplateParseError(ValueError):
    """The template is not valid Jinja syntax."""


@dataclass(frozen=True)
class Ref:
    root: str  # node id, "start", or "secret"
    path: tuple[str, ...]  # field path below the root (a dynamic index cuts the path short)
    has_default: bool  # wrapped directly in `| default(...)`
    direct: bool  # printed directly by {{ }} (not a loop/if operand, not filtered)


@dataclass(frozen=True)
class ParsedTemplate:
    refs: tuple[Ref, ...]
    whole_value: Ref | None  # the template is exactly `{{ ref }}` or `{{ ref | default(x) }}`
    expression: str | None  # expression source of a whole-value template
    problems: tuple[str, ...]  # forbidden constructs or filters (Korean messages)


def _chain(expr: nodes.Node) -> tuple[str, tuple[str, ...], bool] | None:
    """(root, path, exact) of a Name/Getattr/Getitem chain; exact=False once a dynamic index cut the path."""
    if isinstance(expr, nodes.Name):
        return expr.name, (), True
    if isinstance(expr, nodes.Getattr):
        base = _chain(expr.node)
        if base is None:
            return None
        root, path, exact = base
        return (root, (*path, expr.attr), True) if exact else base
    if isinstance(expr, nodes.Getitem):
        base = _chain(expr.node)
        if base is None:
            return None
        root, path, exact = base
        key = expr.arg.value if isinstance(expr.arg, nodes.Const) else None
        if exact and isinstance(key, (str, int)) and not isinstance(key, bool):
            return root, (*path, str(key)), True
        return root, path, False
    return None


@lru_cache(maxsize=4096)
def parse_template(source: str) -> ParsedTemplate:
    try:
        ast = ENV.parse(source)
    except TemplateSyntaxError as exc:
        raise TemplateParseError(f"{exc.lineno}행: {exc.message}") from exc

    problems: list[str] = []
    for node in ast.find_all(_FORBIDDEN):
        problems.append(f"허용되지 않은 구문입니다: {type(node).__name__}")
    for node in ast.find_all(nodes.Filter):
        if node.name not in ALLOWED_FILTERS:
            problems.append(f"허용되지 않은 필터입니다: {node.name}")
    for node in ast.find_all(nodes.Getattr):
        if node.attr.startswith("_"):
            problems.append(f"밑줄로 시작하는 속성은 사용할 수 없습니다: {node.attr}")

    local_names = {n.name for n in ast.find_all(nodes.Name) if n.ctx != "load"} | {"loop"}
    inner = {id(n.node) for n in ast.find_all((nodes.Getattr, nodes.Getitem))}
    defaulted = {id(f.node) for f in ast.find_all(nodes.Filter) if f.name == "default"}
    direct: set[int] = set()
    for output in ast.find_all(nodes.Output):
        for expr in output.nodes:
            direct.add(id(expr))
            if isinstance(expr, nodes.Filter) and expr.name == "default":
                direct.add(id(expr.node))

    refs: list[Ref] = []
    for node in ast.find_all((nodes.Getattr, nodes.Getitem, nodes.Name)):
        if id(node) in inner or (isinstance(node, nodes.Name) and node.ctx != "load"):
            continue
        chain = _chain(node)
        if chain is None or chain[0] in local_names:
            continue
        refs.append(Ref(chain[0], chain[1], id(node) in defaulted, id(node) in direct))

    whole: Ref | None = None
    expression: str | None = None
    body = ast.body
    if len(body) == 1 and isinstance(body[0], nodes.Output) and len(body[0].nodes) == 1:
        expr = body[0].nodes[0]
        is_default = isinstance(expr, nodes.Filter) and expr.name == "default"
        target = expr.node if is_default else expr
        chain = _chain(target)
        match = _WHOLE.fullmatch(source)
        if chain is not None and chain[2] and chain[0] not in local_names and match:
            whole = Ref(chain[0], chain[1], is_default, True)
            expression = match.group(1).strip()
    return ParsedTemplate(tuple(refs), whole, expression, tuple(problems))
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_template_parser.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/templates services/engine/tests/test_template_parser.py
git commit -m "feat(engine): add sandboxed template env and reference parser"
```

---

> **Post-review fixes (applied during execution, two separate commits):** `env.py` now uses a `TemplateEnvironment(ImmutableSandboxedEnvironment)` whose `getattr`/`getitem` read mapping keys before attributes (so `{{ x.items }}` reads the `items` key; missing keys are undefined, dict methods are never exposed). Resource rule — a template can never build data larger than O(template length + data size): intercepted `*`, `**`, `%` work on numbers only, integer powers are bounded by `MAX_POWER_BITS` (4096), `join` is replaced by a size-checked version (≤ `MAX_OUTPUT_CHARS` = 1,000,000, no `attribute` parameter), `round` is replaced by a version that clamps precision to 0–15 (Jinja's builtin computes `10**precision` unbounded), and `tojson` takes no `indent` and fails on undefined. CPU is not fully bounded (a single loop scanning the data is O(data²)) — that is the Plan 2 render deadline. `parser.py` enforces `MAX_TEMPLATE_LENGTH` (20,000 chars) and `MAX_LOOP_DEPTH` (1 — loops cannot nest), maps `RecursionError` to `TemplateParseError`, rejects `_`-prefixed string keys, and extracts references scope-aware (loop variables only shadow inside their loop body). New `tests/test_template_env.py`; suite 85.
>
> **Carried into later tasks:** Task 5's renderer also maps `TypeError`/`ValueError`/`ArithmeticError` from filters to `TemplateRenderError` and caps rendered output size (streamed via `Template.generate`, aborted past `MAX_OUTPUT_CHARS`); Task 12 adds `self` to `RESERVED_IDS`.
>
> Pre-dispatch probes of the plan code (run against the extracted plan tree) added two more:
>
> - Task 6 gateway: `json.loads` also raises `RecursionError` (deeply nested output) and plain `ValueError` (integers over 4300 digits), and accepts `NaN`/`Infinity`. The gateway parses with `parse_constant` rejecting non-standard constants and treats `(ValueError, RecursionError)` as a repairable "not JSON" failure.
> - Task 7 Ollama transport: a malformed body or NDJSON line leaked `JSONDecodeError`, a body without `message` (or with an `error` field) leaked `KeyError`, and a stream cut before `done: true` returned partial text as success. All three become retryable `LLM_UNAVAILABLE`.

---

## Task 5: Template renderer

Spec 4.4: whole-value templates keep the value's type, everything else is string interpolation; objects interpolate as JSON.

**Files:**

- Create: `services/engine/engine/templates/render.py`
- Test: `services/engine/tests/test_template_render.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_template_render.py`

```python
import pytest

from engine.templates.render import TemplateRenderError, TemplateTypeError, render_template

CTX = {
    "start": {"topic": "AI", "n": "3"},
    "llm_1": {"text": "본문", "data": {"k": [1, 2]}},
    "cond": {"result": True},
}


def test_string_interpolation():
    assert render_template("주제: {{ start.topic }}", CTX, "string") == "주제: AI"


def test_objects_interpolate_as_json():
    assert render_template("v={{ llm_1.data }}", CTX, "string") == 'v={"k": [1, 2]}'


def test_whole_value_bool_into_string_field():
    assert render_template("{{ cond.result }}", CTX, "string") == "true"


def test_whole_value_keeps_type():
    assert render_template("{{ llm_1.data }}", CTX, "any") == {"k": [1, 2]}


def test_whole_value_number_coercion():
    assert render_template("{{ start.n }}", CTX, "number") == 3.0


def test_default_for_node_that_did_not_run():
    assert render_template("{{ llm_9.text | default('없음') }}", CTX, "string") == "없음"
    assert render_template("x{{ llm_9.text | default('') }}y", CTX, "string") == "xy"


@pytest.mark.parametrize("source", ["{{ llm_9.text }}", "a {{ llm_9.text }}", "{{ start.nope }}"])
def test_missing_reference_fails(source):
    with pytest.raises(TemplateRenderError):
        render_template(source, CTX, "any")


def test_type_mismatch_is_a_distinct_error():
    with pytest.raises(TemplateTypeError):
        render_template("{{ start.topic }}", CTX, "number")


def test_forbidden_construct_fails():
    with pytest.raises(TemplateRenderError):
        render_template("{{ start.__class__ }}", CTX, "string")


def test_tojson_keeps_unicode():
    assert render_template("{{ llm_1 | tojson }}", CTX, "string") == '{"text": "본문", "data": {"k": [1, 2]}}'


def test_for_loop():
    assert render_template("{% for x in llm_1.data.k %}{{ x }},{% endfor %}", CTX, "string") == "1,2,"
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_template_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.templates.render'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/templates/render.py`

```python
from __future__ import annotations

from functools import lru_cache
from typing import Any

from jinja2 import Template, Undefined
from jinja2.exceptions import TemplateError

from engine.dsl.types import Target, coerce_runtime
from engine.errors import ErrorCode
from engine.templates.env import ENV
from engine.templates.parser import TemplateParseError, parse_template


class TemplateRenderError(Exception):
    code = ErrorCode.TEMPLATE_ERROR


class TemplateTypeError(TemplateRenderError):
    code = ErrorCode.TYPE_MISMATCH


@lru_cache(maxsize=4096)
def _template(source: str) -> Template:
    return ENV.from_string(source)


@lru_cache(maxsize=4096)
def _expression(expression: str):
    return ENV.compile_expression(expression, undefined_to_none=False)


def render_template(source: str, context: dict[str, Any], target: Target) -> Any:
    """Render one template field. Whole-value templates keep the referenced value's type."""
    try:
        parsed = parse_template(source)
    except TemplateParseError as exc:
        raise TemplateRenderError(str(exc)) from exc
    if parsed.problems:
        raise TemplateRenderError("; ".join(parsed.problems))
    try:
        if parsed.whole_value is not None:
            value = _expression(parsed.expression)(**context)
            if isinstance(value, Undefined):
                raise TemplateRenderError(f"참조한 값이 없습니다: {parsed.expression}")
        else:
            value = _template(source).render(**context)
    except TemplateError as exc:  # includes UndefinedError and SecurityError
        raise TemplateRenderError(str(exc)) from exc
    try:
        return coerce_runtime(value, target)
    except TypeError as exc:
        raise TemplateTypeError(str(exc)) from exc
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_template_render.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/templates/render.py services/engine/tests/test_template_render.py
git commit -m "feat(engine): add typed template renderer"
```

---

> **Post-review note (Task 5, as implemented):** the code quality review found raw exceptions escaping the renderer (compile-time SyntaxError/RecursionError from deep nesting, >4300-digit ints), size amplification past the cap (whole values, chained `tojson`, `~`, list literals), non-JSON results (bound methods, nested Undefined, inf) and aliasing of context objects. The fix (commit `01f021b`) makes the rule "templates operate on JSON data only; every value a template builds is JSON data of serialized size ≤ `MAX_OUTPUT_CHARS`, checked while building":
>
> - `env.py`: all arithmetic operators are numbers-only; `MAX_POWER_BITS` became `MAX_INT_BITS`, which also bounds `*`. `getattr` and `getitem` read mapping keys and sequence indexes only (`LoopContext` excepted), never Python attributes. New `json_value()` gives a validated, size-budgeted deep copy, used by printing, `tojson` and `join`.
> - `parser.py`: rejects `~` and whole-AST nesting deeper than `MAX_NESTING_DEPTH` = 50. Python fails to compile Jinja's output at about 200 (expressions) or 102 (nested `if`).
> - `render.py`: passes the context positionally. Whole values go through `json_value`. Any failure becomes `TemplateRenderError`, and string results are capped.
>
> Known limit: a whole value with a non-string target is budgeted by `json_value`'s lower bound, not an exact serialized size. That is pass-through, not amplification, and Task 16's 1 MB node-output cap applies. Jinja binds the name `self` to its own template reference, so Task 12 reserving `self` is required, not cosmetic. Suite: 154.

## Task 6: LLM client contract, test double and structured-output gateway

Spec 6.4. `LLMClient` is what nodes call. `LLMGateway` implements it over a `RawLLM` transport and owns the JSON-Schema validation + repair loop (max 2 repairs inside one attempt → `STRUCTURED_OUTPUT_FAILED`, retryable). `ScriptedLLM` is a shipped test double (Plan 2 tests reuse it).

**Files:**

- Create: `services/engine/engine/llm/__init__.py`
- Create: `services/engine/engine/llm/base.py`
- Create: `services/engine/engine/llm/scripted.py`
- Create: `services/engine/engine/llm/gateway.py`
- Test: `services/engine/tests/test_llm_scripted.py`
- Test: `services/engine/tests/test_llm_gateway.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_llm_scripted.py`

```python
import pytest

from engine.llm.base import ChatMessage, ChatResult
from engine.llm.scripted import ScriptedLLM

MSG = [ChatMessage("user", "질문")]


async def test_list_script_is_consumed_in_order():
    llm = ScriptedLLM(["첫째", {"score": 1}, ChatResult(text="셋째", tokens_out=3)])
    assert (await llm.chat(model="m", messages=MSG)).text == "첫째"
    assert (await llm.chat(model="m", messages=MSG)).data == {"score": 1}
    assert (await llm.chat(model="m", messages=MSG)).tokens_out == 3
    with pytest.raises(AssertionError):
        await llm.chat(model="m", messages=MSG)


async def test_exceptions_are_raised():
    llm = ScriptedLLM([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        await llm.chat(model="m", messages=MSG)


async def test_responder_function_and_call_log():
    llm = ScriptedLLM(lambda model, messages, schema: f"{model}:{messages[-1].content}")
    result = await llm.chat(model="qwen", messages=MSG, temperature=0.1)
    assert result.text == "qwen:질문"
    assert llm.calls[0]["temperature"] == 0.1
    assert llm.prompts() == ["질문"]


async def test_text_responses_are_streamed_to_token_sink():
    tokens: list[str] = []

    async def sink(text: str) -> None:
        tokens.append(text)

    await ScriptedLLM(["토큰"]).chat(model="m", messages=MSG, on_token=sink)
    assert tokens == ["토큰"]
```

**File:** `services/engine/tests/test_llm_gateway.py`

```python
import pytest

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage, ChatResult
from engine.llm.gateway import LLMGateway

SCHEMA = {"type": "object", "properties": {"score": {"type": "number"}}, "required": ["score"]}
MSG = [ChatMessage("user", "평가해줘")]


class FakeRaw:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[dict] = []

    async def complete(self, *, model, messages, format, temperature, on_token):
        self.calls.append({"messages": list(messages), "format": format, "on_token": on_token})
        return ChatResult(text=self.texts.pop(0), tokens_in=10, tokens_out=5)


async def test_plain_chat_passes_through_with_token_sink():
    raw = FakeRaw(["안녕"])

    async def sink(text: str) -> None:
        return None

    result = await LLMGateway(raw).chat(model="m", messages=MSG, on_token=sink)

    assert result.text == "안녕"
    assert result.data is None
    assert raw.calls[0]["format"] is None
    assert raw.calls[0]["on_token"] is sink


async def test_structured_output_is_parsed_and_not_streamed():
    raw = FakeRaw(['{"score": 8}'])
    result = await LLMGateway(raw).chat(model="m", messages=MSG, schema=SCHEMA)
    assert result.data == {"score": 8}
    assert raw.calls[0]["format"] == SCHEMA
    assert raw.calls[0]["on_token"] is None


async def test_repairs_invalid_output_then_succeeds():
    raw = FakeRaw(["not json", '{"score": "high"}', '{"score": 7}'])

    result = await LLMGateway(raw).chat(model="m", messages=MSG, schema=SCHEMA)

    assert result.data == {"score": 7}
    assert (result.tokens_in, result.tokens_out) == (30, 15)
    third_call = raw.calls[2]["messages"]
    assert third_call[-2] == ChatMessage("assistant", '{"score": "high"}')
    assert "score" in third_call[-1].content


async def test_gives_up_after_max_repairs():
    raw = FakeRaw(["x", "y", "z"])
    with pytest.raises(NodeError) as exc:
        await LLMGateway(raw, max_repairs=2).chat(model="m", messages=MSG, schema=SCHEMA)
    assert exc.value.code == ErrorCode.STRUCTURED_OUTPUT_FAILED
    assert exc.value.retryable is True
    assert len(raw.calls) == 3
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_llm_scripted.py tests/test_llm_gateway.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.llm'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/llm/__init__.py`

```python

```

**File:** `services/engine/engine/llm/base.py`

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

TokenSink = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class ChatResult:
    text: str
    data: Any = None  # parsed JSON when a schema was requested
    tokens_in: int = 0
    tokens_out: int = 0


class LLMClient(Protocol):
    """What nodes call. With `schema`, `data` holds a value validated against it."""

    async def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
        on_token: TokenSink | None = None,
    ) -> ChatResult: ...


class RawLLM(Protocol):
    """Transport to a model server. `format` is a JSON Schema the server should constrain output to."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        format: dict[str, Any] | None,
        temperature: float,
        on_token: TokenSink | None,
    ) -> ChatResult: ...
```

**File:** `services/engine/engine/llm/scripted.py`

```python
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from engine.llm.base import ChatMessage, ChatResult, TokenSink

Response = ChatResult | Exception | dict | str
Responder = Callable[[str, list[ChatMessage], dict | None], Response]


class ScriptedLLM:
    """Test double for LLMClient.

    `script` is a list of responses consumed in call order, or a function
    `(model, messages, schema) -> response` for order-independent (parallel) tests.
    A dict response is structured data, a str is text, an Exception is raised.
    """

    def __init__(self, script: list[Response] | Responder, *, delay: float = 0) -> None:
        self._script: list[Response] | Responder = script if callable(script) else list(script)
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
        on_token: TokenSink | None = None,
    ) -> ChatResult:
        self.calls.append({"model": model, "messages": list(messages), "schema": schema, "temperature": temperature})
        if self._delay:
            await asyncio.sleep(self._delay)
        if callable(self._script):
            response = self._script(model, list(messages), schema)
        else:
            if not self._script:
                raise AssertionError("ScriptedLLM: no scripted response left")
            response = self._script.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            return ChatResult(text=json.dumps(response, ensure_ascii=False), data=response)
        if isinstance(response, str):
            response = ChatResult(text=response)
        if on_token is not None and response.data is None:
            await on_token(response.text)
        return response

    def prompts(self) -> list[str]:
        """The last message of every call, in call order."""
        return [call["messages"][-1].content for call in self.calls]
```

**File:** `services/engine/engine/llm/gateway.py`

```python
from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage, ChatResult, RawLLM, TokenSink

REPAIR_PROMPT = (
    "직전 출력이 요구된 JSON 스키마를 만족하지 않습니다: {error}\n"
    "설명 없이 스키마를 만족하는 JSON만 다시 출력하세요."
)


class LLMGateway:
    """LLMClient over a RawLLM: structured output with validation and a bounded repair loop."""

    def __init__(self, raw: RawLLM, *, max_repairs: int = 2) -> None:
        self._raw = raw
        self._max_repairs = max_repairs

    async def chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
        on_token: TokenSink | None = None,
    ) -> ChatResult:
        if schema is None:
            return await self._raw.complete(
                model=model, messages=messages, format=None, temperature=temperature, on_token=on_token
            )
        validator = Draft202012Validator(schema)
        conversation = list(messages)
        tokens_in = tokens_out = 0
        last_error = ""
        for _ in range(self._max_repairs + 1):
            result = await self._raw.complete(
                model=model, messages=conversation, format=schema, temperature=temperature, on_token=None
            )
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            try:
                data = json.loads(result.text)
            except json.JSONDecodeError as exc:
                last_error = f"JSON이 아닙니다 ({exc.msg})"
            else:
                errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
                if not errors:
                    return ChatResult(text=result.text, data=data, tokens_in=tokens_in, tokens_out=tokens_out)
                last_error = "; ".join(
                    f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors[:5]
                )
            conversation = [
                *conversation,
                ChatMessage("assistant", result.text),
                ChatMessage("user", REPAIR_PROMPT.format(error=last_error)),
            ]
        raise NodeError(ErrorCode.STRUCTURED_OUTPUT_FAILED, f"구조화 출력 검증 실패: {last_error}", retryable=True)
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_llm_scripted.py tests/test_llm_gateway.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/llm services/engine/tests/test_llm_scripted.py services/engine/tests/test_llm_gateway.py
git commit -m "feat(engine): add LLM client contract, scripted double and structured-output gateway"
```

> **Post-review note (Task 6, as implemented):** commits `49a1df8` and `d0c10dc` made these changes on review:
>
> - A `RecursionError` from `iter_errors` (deep valid output against a recursive schema) is now repaired instead of escaping.
> - jsonschema messages are clipped: 200 chars per message and 1000 in total. The invalid answer echoed back is clipped to 4000 chars.
> - `1e400` is rejected.
> - `ScriptedLLM` deep-copies dict responses and records `streamed` in `calls`.
>
> Deferred to Plan 2 (roadmap): usage of failed attempts.
>
> The review also found two schema-side problems:
>
> - A tenant schema `pattern` can backtrack catastrophically (`^(a+)+$` on 31 chars took 48 s on the event loop).
> - An unresolvable `$ref` passes `check_schema` but raises inside `iter_errors`.
>
> Both belong at save time and are handled in Task 8's `check_object_schema`. Suite: 170.
>
> **Carried into Task 8:** the strict JSON parsing and short schema messages move to a shared `engine/jsondata.py` (`parse_json`, `schema_violations`, `clip`), used by the gateway and the template node. `check_object_schema` then does three things:
>
> - It rejects `pattern`/`patternProperties`.
> - It allows only local `#/...` `$ref`s that resolve.
> - It maps `RecursionError` (a too-deep schema) to a validation error.
>
> `schema_violations` catches `RecursionError`. The template node's JSON format rejects NaN, overflowing numbers and too-deep nesting as `TEMPLATE_ERROR`.

---

## Task 7: Ollama transport

`POST {base}/api/chat`. Non-streaming returns one JSON body; streaming returns NDJSON lines ending with `done: true` (token counts on the last line). Transport/5xx/429 errors are retryable `LLM_UNAVAILABLE`; 404 (model missing) and other 4xx are non-retryable `NODE_FAILED`. The model-wide concurrency semaphore is Plan 2 (needs Redis).

**Files:**

- Create: `services/engine/engine/llm/ollama.py`
- Test: `services/engine/tests/test_llm_ollama.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_llm_ollama.py`

```python
import json

import httpx
import pytest

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage
from engine.llm.ollama import OllamaRaw

MSG = [ChatMessage("user", "hi")]


def _raw(handler) -> OllamaRaw:
    return OllamaRaw("http://ollama:11434/", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_non_stream_request_and_response():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": "응답"}, "done": True,
                  "prompt_eval_count": 12, "eval_count": 7},
        )

    result = await _raw(handler).complete(
        model="qwen2.5:14b", messages=MSG, format={"type": "object"}, temperature=0.2, on_token=None
    )

    assert seen["url"] == "http://ollama:11434/api/chat"
    assert seen["body"] == {
        "model": "qwen2.5:14b",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "options": {"temperature": 0.2},
        "format": {"type": "object"},
    }
    assert (result.text, result.tokens_in, result.tokens_out) == ("응답", 12, 7)


async def test_streaming_emits_tokens_and_collects_counts():
    lines = [
        {"message": {"content": "안"}, "done": False},
        {"message": {"content": "녕"}, "done": False},
        {"message": {"content": ""}, "done": True, "prompt_eval_count": 3, "eval_count": 2},
    ]
    body = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        assert "format" not in json.loads(request.content)
        return httpx.Response(200, content=body)

    tokens: list[str] = []

    async def sink(text: str) -> None:
        tokens.append(text)

    result = await _raw(handler).complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=sink)

    assert tokens == ["안", "녕"]
    assert result.text == "안녕"
    assert (result.tokens_in, result.tokens_out) == (3, 2)


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(404, ErrorCode.NODE_FAILED, False), (400, ErrorCode.NODE_FAILED, False),
     (429, ErrorCode.LLM_UNAVAILABLE, True), (503, ErrorCode.LLM_UNAVAILABLE, True)],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_http_errors_map_to_node_errors(status, code, retryable, streaming):
    async def sink(text: str) -> None:
        return None

    raw = _raw(lambda request: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(NodeError) as exc:
        await raw.complete(model="m", messages=MSG, format=None, temperature=0.7,
                           on_token=sink if streaming else None)
    assert exc.value.code == code
    assert exc.value.retryable is retryable


async def test_connection_error_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(NodeError) as exc:
        await _raw(handler).complete(model="m", messages=MSG, format=None, temperature=0.7, on_token=None)
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert exc.value.retryable is True
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_llm_ollama.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.llm.ollama'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/llm/ollama.py`

```python
from __future__ import annotations

import json
from typing import Any

import httpx

from engine.errors import ErrorCode, NodeError
from engine.llm.base import ChatMessage, ChatResult, TokenSink


class OllamaRaw:
    """RawLLM over Ollama's native /api/chat endpoint."""

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None, timeout: float = 600) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        format: dict[str, Any] | None,
        temperature: float,
        on_token: TokenSink | None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": on_token is not None,
            "options": {"temperature": temperature},
        }
        if format is not None:
            body["format"] = format
        url = f"{self._base}/api/chat"
        try:
            if on_token is None:
                response = await self._client.post(url, json=body)
                _raise_for_status(response, model)
                data = response.json()
                return ChatResult(
                    text=data["message"]["content"],
                    tokens_in=data.get("prompt_eval_count", 0),
                    tokens_out=data.get("eval_count", 0),
                )
            return await self._stream(url, body, model, on_token)
        except httpx.TransportError as exc:
            raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"Ollama 연결 실패: {exc}", retryable=True) from exc

    async def _stream(self, url: str, body: dict[str, Any], model: str, on_token: TokenSink) -> ChatResult:
        parts: list[str] = []
        tokens_in = tokens_out = 0
        async with self._client.stream("POST", url, json=body) as response:
            if response.status_code >= 400:
                await response.aread()
                _raise_for_status(response, model)
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"Ollama 오류: {chunk['error']}", retryable=True)
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    parts.append(piece)
                    await on_token(piece)
                if chunk.get("done"):
                    tokens_in = chunk.get("prompt_eval_count", 0)
                    tokens_out = chunk.get("eval_count", 0)
        return ChatResult(text="".join(parts), tokens_in=tokens_in, tokens_out=tokens_out)


def _raise_for_status(response: httpx.Response, model: str) -> None:
    status = response.status_code
    if status < 400:
        return
    if status == 404:
        raise NodeError(ErrorCode.NODE_FAILED, f"모델을 찾을 수 없습니다: {model}", retryable=False)
    if status == 429 or status >= 500:
        raise NodeError(ErrorCode.LLM_UNAVAILABLE, f"Ollama 오류 HTTP {status}", retryable=True)
    raise NodeError(ErrorCode.NODE_FAILED, f"Ollama 요청 오류 HTTP {status}: {response.text[:200]}", retryable=False)
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_llm_ollama.py -v`
Expected: all PASS (11 tests)

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/llm/ollama.py services/engine/tests/test_llm_ollama.py
git commit -m "feat(engine): add Ollama chat transport"
```

---

## Task 8: Node spec contract, registry, start/end/template nodes

Spec 4.6–4.7. The wrapper (Task 16) renders every `TemplateField` before calling `execute`, so nodes receive plain values in `rendered[path]`. Branch nodes expose `route(config, output)`; the handle is always derived from the output, which also makes `onError=default` outputs routable.

**Files:**

- Create: `services/engine/engine/nodes/__init__.py`
- Create: `services/engine/engine/nodes/base.py`
- Create: `services/engine/engine/nodes/io.py`
- Create: `services/engine/engine/nodes/template.py`
- Create: `services/engine/engine/nodes/registry.py`
- Create: `services/engine/tests/helpers.py`
- Test: `services/engine/tests/test_nodes_basic.py`

- [ ]  **Step 1: Write the test helpers and failing tests**

**File:** `services/engine/tests/helpers.py`

```python
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
```

**File:** `services/engine/tests/test_nodes_basic.py`

```python
import pytest
from pydantic import ValidationError

from engine.errors import ErrorCode, NodeError
from engine.nodes.base import TemplateField
from engine.nodes.io import EndNode, StartNode
from engine.nodes.registry import default_registry
from engine.nodes.template import TemplateNode
from tests.helpers import make_ctx

INPUTS_SCHEMA = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}


async def test_start_returns_inputs_and_checks_schema():
    spec = StartNode()
    config = spec.parse_config({"inputs": INPUTS_SCHEMA})

    result = await spec.execute(make_ctx(inputs={"topic": "AI"}), config, {})

    assert result.output == {"topic": "AI"}
    assert spec.output_schema(config, {}) == INPUTS_SCHEMA
    with pytest.raises(NodeError) as exc:
        await spec.execute(make_ctx(inputs={}), config, {})
    assert exc.value.code == ErrorCode.TYPE_MISMATCH


@pytest.mark.parametrize("schema", [{"type": "string"}, {"type": 5}])
def test_start_rejects_invalid_input_schema(schema):
    with pytest.raises(ValidationError):
        StartNode().parse_config({"inputs": schema})


async def test_end_maps_rendered_outputs():
    spec = EndNode()
    config = spec.parse_config({"outputs": {"result": "{{llm_1.text}}"}})

    assert spec.handles(config) == []
    assert spec.template_fields(config) == [TemplateField("outputs.result", "{{llm_1.text}}", "any")]
    result = await spec.execute(make_ctx(), config, {"outputs.result": "본문"})
    assert result.output == {"result": "본문"}


def test_end_rejects_bad_output_names():
    with pytest.raises(ValidationError):
        EndNode().parse_config({"outputs": {"1bad": "x"}})


async def test_template_text_output():
    spec = TemplateNode()
    config = spec.parse_config({"template": "안녕 {{start.name}}"})
    assert spec.template_fields(config)[0].target == "string"
    result = await spec.execute(make_ctx(), config, {"template": "안녕 철수"})
    assert result.output == {"text": "안녕 철수"}


async def test_template_json_output():
    spec = TemplateNode()
    config = spec.parse_config({"template": '{"a": {{start.n}}}', "format": "json"})
    assert spec.template_fields(config)[0].target == "any"
    assert (await spec.execute(make_ctx(), config, {"template": '{"a": 1}'})).output == {"data": {"a": 1}}
    assert (await spec.execute(make_ctx(), config, {"template": [1, 2]})).output == {"data": [1, 2]}
    with pytest.raises(NodeError) as exc:
        await spec.execute(make_ctx(), config, {"template": "{broken"})
    assert exc.value.code == ErrorCode.TEMPLATE_ERROR


def test_registry_lookup():
    registry = default_registry()
    assert registry.get("start").type == "start"
    assert registry.get("nope") is None
    assert {"start", "end", "template"} <= {spec.type for spec in registry.all()}
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_nodes_basic.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.nodes'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/nodes/__init__.py`

```python

```

**File:** `services/engine/engine/nodes/base.py`

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel

from engine.dsl.models import Policy
from engine.dsl.types import Target
from engine.llm.base import LLMClient, TokenSink

TEMPLATE = {"x-template": True}  # json_schema_extra marker: the editor renders a template input
TEXT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


@dataclass(frozen=True)
class TemplateField:
    path: str  # key in `rendered`, e.g. "prompt", "outputs.result", "conditions.0.left"
    source: str
    target: Target


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class NodeResult:
    output: dict[str, Any]
    usage: Usage = field(default_factory=Usage)


@dataclass
class NodeContext:
    run_id: str
    node_id: str
    exec_index: int
    attempt: int
    inputs: dict[str, Any]
    outputs: dict[str, Any]  # latest output of every node that ran, keyed by node id
    pred_ids: list[str]  # forward predecessors in edge declaration order (merge uses it)
    llm: LLMClient
    on_token: TokenSink | None = None
    interrupt: Callable[[dict[str, Any]], Awaitable[Any]] | None = None


class NodeSpec(ABC):
    type: ClassVar[str]
    label: ClassVar[str]
    category: ClassVar[str]
    Config: ClassVar[type[BaseModel]]
    default_policy: ClassVar[Policy | None] = None  # None: the node does not accept a policy
    side_effects: ClassVar[bool] = False
    is_branch: ClassVar[bool] = False

    def parse_config(self, raw: dict[str, Any]) -> BaseModel:
        return self.Config.model_validate(raw)

    def handles(self, config: BaseModel) -> list[str]:
        return ["out"]

    def template_fields(self, config: BaseModel) -> list[TemplateField]:
        return []

    def route(self, config: BaseModel, output: dict[str, Any]) -> str:
        """Handle chosen for `output`. Only branch nodes override this."""
        return "out"

    def fallback_output(self, config: BaseModel) -> dict[str, Any] | None:
        """Output used for onError=default when the policy has no defaultOutput."""
        return None

    @abstractmethod
    def output_schema(self, config: BaseModel, pred_schemas: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """JSON Schema of the output. `pred_schemas` maps forward predecessors (declaration order)."""

    @abstractmethod
    async def execute(self, ctx: NodeContext, config: BaseModel, rendered: dict[str, Any]) -> NodeResult: ...


def check_object_schema(value: dict[str, Any], what: str) -> dict[str, Any]:
    """Pydantic validator body: `value` must be a valid JSON Schema of type object."""
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        raise ValueError(f"{what}가 올바른 JSON Schema가 아닙니다: {exc.message}") from None
    if value.get("type") != "object":
        raise ValueError(f"{what}의 type은 object여야 합니다")
    return value


def schema_violations(schema: dict[str, Any], value: Any) -> list[str]:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors[:5]]
```

**File:** `services/engine/engine/nodes/io.py`

```python
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.errors import ErrorCode, NodeError
from engine.nodes.base import (
    NodeContext,
    NodeResult,
    NodeSpec,
    TemplateField,
    check_object_schema,
    schema_violations,
)

_OUTPUT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class StartConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    @field_validator("inputs")
    @classmethod
    def _object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return check_object_schema(value, "입력 스키마")


class StartNode(NodeSpec):
    type = "start"
    label = "시작"
    category = "IO"
    Config = StartConfig

    def output_schema(self, config: StartConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return config.inputs

    async def execute(self, ctx: NodeContext, config: StartConfig, rendered: dict[str, Any]) -> NodeResult:
        violations = schema_violations(config.inputs, ctx.inputs)
        if violations:
            raise NodeError(
                ErrorCode.TYPE_MISMATCH,
                "실행 입력이 입력 스키마와 맞지 않습니다: " + "; ".join(violations),
                retryable=False,
            )
        return NodeResult(dict(ctx.inputs))


class EndConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outputs: dict[str, str] = Field(default_factory=dict)

    @field_validator("outputs")
    @classmethod
    def _output_names(cls, value: dict[str, str]) -> dict[str, str]:
        bad = [key for key in value if not _OUTPUT_KEY.match(key)]
        if bad:
            raise ValueError(f"출력 이름 형식이 잘못되었습니다: {', '.join(bad)}")
        return value


class EndNode(NodeSpec):
    type = "end"
    label = "끝"
    category = "IO"
    Config = EndConfig

    def handles(self, config: EndConfig) -> list[str]:
        return []

    def template_fields(self, config: EndConfig) -> list[TemplateField]:
        return [TemplateField(f"outputs.{key}", source, "any") for key, source in config.outputs.items()]

    def output_schema(self, config: EndConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {"type": "object", "properties": {key: {} for key in config.outputs}}

    async def execute(self, ctx: NodeContext, config: EndConfig, rendered: dict[str, Any]) -> NodeResult:
        return NodeResult({key: rendered[f"outputs.{key}"] for key in config.outputs})
```

**File:** `services/engine/engine/nodes/template.py`

```python
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.errors import ErrorCode, NodeError
from engine.nodes.base import TEMPLATE, TEXT_OUTPUT_SCHEMA, NodeContext, NodeResult, NodeSpec, TemplateField


class TemplateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(json_schema_extra=TEMPLATE)
    format: Literal["text", "json"] = "text"


class TemplateNode(NodeSpec):
    type = "template"
    label = "템플릿"
    category = "Logic"
    Config = TemplateConfig

    def template_fields(self, config: TemplateConfig) -> list[TemplateField]:
        return [TemplateField("template", config.template, "string" if config.format == "text" else "any")]

    def output_schema(self, config: TemplateConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        if config.format == "text":
            return TEXT_OUTPUT_SCHEMA
        return {"type": "object", "properties": {"data": {}}, "required": ["data"]}

    async def execute(self, ctx: NodeContext, config: TemplateConfig, rendered: dict[str, Any]) -> NodeResult:
        value = rendered["template"]
        if config.format == "text":
            return NodeResult({"text": value})
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise NodeError(ErrorCode.TEMPLATE_ERROR, f"JSON 파싱 실패: {exc.msg}", retryable=False) from exc
        return NodeResult({"data": value})
```

**File:** `services/engine/engine/nodes/registry.py`

```python
from __future__ import annotations

from engine.nodes.base import NodeSpec


class NodeRegistry:
    def __init__(self, specs: list[NodeSpec]) -> None:
        self._specs = {spec.type: spec for spec in specs}

    def get(self, node_type: str) -> NodeSpec | None:
        return self._specs.get(node_type)

    def all(self) -> list[NodeSpec]:
        return list(self._specs.values())


def default_registry() -> NodeRegistry:
    from engine.nodes.io import EndNode, StartNode
    from engine.nodes.template import TemplateNode

    return NodeRegistry([StartNode(), EndNode(), TemplateNode()])
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_nodes_basic.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/nodes services/engine/tests/helpers.py services/engine/tests/test_nodes_basic.py
git commit -m "feat(engine): add node spec contract, registry and start/end/template nodes"
```

> **Post-review note (Task 8, as implemented):** four review rounds changed this task substantially. The commits are `251c9e5`, `0900992`, `59069c1`, `4ec4f4d` and `61208a6`.
>
> **`engine/jsondata.py`** is shared by the LLM gateway, the nodes and later the validator.
>
> - **Parsing.** `parse_json` accepts standard JSON only. It rejects NaN/Infinity, overflowing numbers, integers over 4300 digits, duplicate keys, NUL, lone surrogates and too-deep nesting.
> - **Accepted schemas.** `schema_problems` limits tenant schemas to a subset:
>   - Keywords: `type`, `properties`, `required`, `additionalProperties`, `items`, `anyOf`, `oneOf`, `enum`, `const`, numeric bounds, length/count bounds (at most 10,000), annotations, `format` and `x-*`.
>   - Excluded: `$ref`, `pattern`, `uniqueItems` and everything else.
>   - Size limits: at most 32,000 chars, depth 32, 256 subschemas, 16 branches, and 256 `enum`/`required` entries.
>   - Measured reasons: a `pattern` took 48 s on 31 chars, `uniqueItems` took 78 s on 8,000 objects, and unresolvable `$ref`s raise errors.
> - **Validation.** `schema_violations` is an in-house validator for that subset. It no longer uses the jsonschema library, which built every error and every failing branch's context and blew up to 20 s / 3.6 GB on accepted schemas.
>   - It stops `anyOf`/`oneOf` early, returns at most 5 messages that never copy data, and looks up scalar `enum` values in a set.
>   - All work is charged to `MAX_VALIDATION_STEPS` (1,000,000 steps, at most about 3 s). Running out raises `ValidationBudgetExceeded`.
>   - A differential test against jsonschema on about 176K random cases found 0 mismatches.
>
> **Template target `"json"`** (`dsl/types.py`, `templates/env.py`, `templates/render.py`).
>
> - In a JSON template each `{{ }}` inserts a JSON value, and the renderer parses the result. Data can therefore never add JSON structure.
> - `TemplateNode` format `json` uses this target and never re-parses strings. The old re-parsing allowed injection through `{"a": "{{start.name}}"}`.
> - Writing `"{{ x }}"` inside quotes is an error and comes with a hint.
>
> **Node changes.**
>
> - Output names are checked with `fullmatch`.
> - Start inputs are deep-copied.
> - The registry rejects duplicate types.
> - Root `anyOf`/`oneOf` is rejected in object schemas.
> - `StartNode` maps an exhausted budget to `NODE_FAILED`, and the gateway maps it to `OUTPUT_TOO_LARGE`. Neither is retried or repaired.
>
> Suite: 319.
>
> **Carried into later tasks:**
>
> - **Task 9:** the LLM node's `outputSchema` goes through `check_object_schema`. `outputSchema` must be a schema the tenant subset accepts before it is sent to Ollama as `format`.
> - **Task 12:** the validator's `defaultOutput` check calls `schema_violations`, and must catch `ValidationBudgetExceeded` and report it as an `INVALID_POLICY` issue. Task 12 also adds `self` to `RESERVED_IDS` (see Task 4/5).

---

## Task 9: LLM and classifier nodes

**Files:**

- Create: `services/engine/engine/nodes/llm.py`
- Create: `services/engine/engine/nodes/classifier.py`
- Modify: `services/engine/engine/nodes/registry.py` (register both)
- Test: `services/engine/tests/test_nodes_ai.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_nodes_ai.py`

```python
import pytest
from pydantic import ValidationError

from engine.llm.base import ChatMessage, ChatResult
from engine.llm.scripted import ScriptedLLM
from engine.nodes.base import Usage
from engine.nodes.classifier import ClassifierNode
from engine.nodes.llm import LLMNode
from engine.nodes.registry import default_registry
from tests.helpers import make_ctx

SCORE_SCHEMA = {"type": "object", "properties": {"score": {"type": "number"}}, "required": ["score"]}
CATEGORIES = [{"id": "billing", "description": "결제"}, {"id": "tech", "description": "기술"}]


async def test_llm_text_output_messages_and_tokens():
    llm = ScriptedLLM([ChatResult(text="답", tokens_in=4, tokens_out=2)])
    spec = LLMNode()
    config = spec.parse_config({"model": "qwen2.5:14b", "system": "너는 작가", "prompt": "{{start.topic}} 써줘"})
    tokens: list[str] = []

    async def sink(text: str) -> None:
        tokens.append(text)

    result = await spec.execute(make_ctx(llm=llm, on_token=sink), config, {"system": "너는 작가", "prompt": "AI 써줘"})

    assert result.output == {"text": "답"}
    assert result.usage == Usage(4, 2)
    assert llm.calls[0]["model"] == "qwen2.5:14b"
    assert llm.calls[0]["messages"] == [ChatMessage("system", "너는 작가"), ChatMessage("user", "AI 써줘")]
    assert llm.calls[0]["schema"] is None
    assert tokens == ["답"]
    assert [f.path for f in spec.template_fields(config)] == ["prompt", "system"]
    assert spec.default_policy.retry.maxAttempts == 3


async def test_llm_structured_output():
    llm = ScriptedLLM([{"score": 9}])
    spec = LLMNode()
    config = spec.parse_config({"model": "m", "prompt": "p", "outputSchema": SCORE_SCHEMA})

    result = await spec.execute(make_ctx(llm=llm), config, {"prompt": "p"})

    assert result.output == {"score": 9}
    assert llm.calls[0]["schema"] == SCORE_SCHEMA
    assert spec.output_schema(config, {}) == SCORE_SCHEMA


@pytest.mark.parametrize("schema", [{"type": "string"}, {"type": 3}])
def test_llm_rejects_invalid_output_schema(schema):
    with pytest.raises(ValidationError):
        LLMNode().parse_config({"model": "m", "prompt": "p", "outputSchema": schema})


async def test_classifier_routes_by_category():
    llm = ScriptedLLM([{"category": "tech", "reason": "코드 질문"}])
    spec = ClassifierNode()
    config = spec.parse_config({"model": "m", "input": "{{start.q}}", "categories": CATEGORIES})

    result = await spec.execute(make_ctx(llm=llm), config, {"input": "버그가 있어요"})

    assert spec.handles(config) == ["billing", "tech", "default"]
    assert spec.route(config, result.output) == "tech"
    assert llm.calls[0]["schema"]["properties"]["category"]["enum"] == ["billing", "tech", "default"]
    assert "- billing: 결제" in llm.calls[0]["messages"][0].content
    assert llm.calls[0]["messages"][1] == ChatMessage("user", "버그가 있어요")
    assert spec.fallback_output(config) == {"category": "default", "reason": "error"}


@pytest.mark.parametrize(
    "categories",
    [[{"id": "default", "description": "x"}], [{"id": "a", "description": "x"}, {"id": "a", "description": "y"}]],
)
def test_classifier_rejects_reserved_or_duplicate_ids(categories):
    with pytest.raises(ValidationError):
        ClassifierNode().parse_config({"model": "m", "input": "i", "categories": categories})


def test_registry_contains_ai_nodes():
    assert {"llm", "classifier"} <= {spec.type for spec in default_registry().all()}
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_nodes_ai.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.nodes.classifier'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/nodes/llm.py`

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.dsl.models import Policy, RetrySpec
from engine.llm.base import ChatMessage
from engine.nodes.base import (
    TEMPLATE,
    TEXT_OUTPUT_SCHEMA,
    NodeContext,
    NodeResult,
    NodeSpec,
    TemplateField,
    Usage,
    check_object_schema,
)


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1)
    system: str = Field("", json_schema_extra=TEMPLATE)
    prompt: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    temperature: float = Field(0.7, ge=0, le=2)
    outputSchema: dict[str, Any] | None = None

    @field_validator("outputSchema")
    @classmethod
    def _object_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else check_object_schema(value, "outputSchema")


class LLMNode(NodeSpec):
    type = "llm"
    label = "LLM"
    category = "AI"
    Config = LLMConfig
    default_policy = Policy(timeoutSec=120, retry=RetrySpec(maxAttempts=3))

    def template_fields(self, config: LLMConfig) -> list[TemplateField]:
        fields = [TemplateField("prompt", config.prompt, "string")]
        if config.system:
            fields.append(TemplateField("system", config.system, "string"))
        return fields

    def output_schema(self, config: LLMConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return config.outputSchema or TEXT_OUTPUT_SCHEMA

    async def execute(self, ctx: NodeContext, config: LLMConfig, rendered: dict[str, Any]) -> NodeResult:
        messages = []
        if config.system:
            messages.append(ChatMessage("system", rendered["system"]))
        messages.append(ChatMessage("user", rendered["prompt"]))
        result = await ctx.llm.chat(
            model=config.model,
            messages=messages,
            schema=config.outputSchema,
            temperature=config.temperature,
            on_token=None if config.outputSchema else ctx.on_token,
        )
        output = result.data if config.outputSchema else {"text": result.text}
        return NodeResult(output, Usage(result.tokens_in, result.tokens_out))
```

**File:** `services/engine/engine/nodes/classifier.py`

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.dsl.models import Policy, RetrySpec
from engine.llm.base import ChatMessage
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField, Usage

SYSTEM_PROMPT = (
    "입력을 아래 카테고리 중 하나로 분류하세요. 어느 카테고리에도 맞지 않으면 \"default\"를 고르세요.\n"
    "카테고리:\n{categories}\n{instructions}"
    "category에는 카테고리 id만, reason에는 한 문장 근거만 JSON으로 출력하세요."
)


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    description: str = Field(min_length=1)


class ClassifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1)
    input: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    categories: list[Category] = Field(min_length=1, max_length=20)
    instructions: str = ""
    temperature: float = Field(0.0, ge=0, le=2)

    @model_validator(mode="after")
    def _unique_ids(self) -> ClassifierConfig:
        ids = [c.id for c in self.categories]
        if "default" in ids:
            raise ValueError("'default'는 예약된 카테고리 id입니다")
        if len(set(ids)) != len(ids):
            raise ValueError("카테고리 id가 중복되었습니다")
        return self


class ClassifierNode(NodeSpec):
    type = "classifier"
    label = "분류"
    category = "AI"
    Config = ClassifierConfig
    default_policy = Policy(timeoutSec=60, retry=RetrySpec(maxAttempts=3))
    is_branch = True

    def handles(self, config: ClassifierConfig) -> list[str]:
        return [c.id for c in config.categories] + ["default"]

    def template_fields(self, config: ClassifierConfig) -> list[TemplateField]:
        return [TemplateField("input", config.input, "string")]

    def route(self, config: ClassifierConfig, output: dict[str, Any]) -> str:
        return output["category"]

    def fallback_output(self, config: ClassifierConfig) -> dict[str, Any]:
        return {"category": "default", "reason": "error"}

    def output_schema(self, config: ClassifierConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": self.handles(config)},
                "reason": {"type": "string"},
            },
            "required": ["category", "reason"],
            "additionalProperties": False,
        }

    async def execute(self, ctx: NodeContext, config: ClassifierConfig, rendered: dict[str, Any]) -> NodeResult:
        categories = "\n".join(f"- {c.id}: {c.description}" for c in config.categories)
        instructions = f"{config.instructions}\n" if config.instructions else ""
        result = await ctx.llm.chat(
            model=config.model,
            messages=[
                ChatMessage("system", SYSTEM_PROMPT.format(categories=categories, instructions=instructions)),
                ChatMessage("user", rendered["input"]),
            ],
            schema=self.output_schema(config, {}),
            temperature=config.temperature,
        )
        return NodeResult(result.data, Usage(result.tokens_in, result.tokens_out))
```

**File:** `services/engine/engine/nodes/registry.py`

```python
from __future__ import annotations

from engine.nodes.base import NodeSpec


class NodeRegistry:
    def __init__(self, specs: list[NodeSpec]) -> None:
        self._specs = {spec.type: spec for spec in specs}

    def get(self, node_type: str) -> NodeSpec | None:
        return self._specs.get(node_type)

    def all(self) -> list[NodeSpec]:
        return list(self._specs.values())


def default_registry() -> NodeRegistry:
    from engine.nodes.classifier import ClassifierNode
    from engine.nodes.io import EndNode, StartNode
    from engine.nodes.llm import LLMNode
    from engine.nodes.template import TemplateNode

    return NodeRegistry([StartNode(), EndNode(), TemplateNode(), LLMNode(), ClassifierNode()])
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_nodes_ai.py tests/test_nodes_basic.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/nodes services/engine/tests/test_nodes_ai.py
git commit -m "feat(engine): add llm and classifier nodes"
```

> **Post-review note (Task 9, as implemented):** commits `a0c14b2` and `424d0af`.
>
> - The registry keeps the duplicate-type check and only adds the new nodes. Task 11 does the same.
> - Model names match `MODEL_NAME` (`^\S+$`) and are at most 200 chars.
> - `temperature` is `strict=True`: bool is rejected, int is accepted.
> - A `prompt`/`input` that renders empty raises non-retryable `TEMPLATE_ERROR` before any model call.
> - A system prompt that renders empty is omitted.
> - Classifier fields are bounded:
>   - `description` ≤ 500 chars, single line;
>   - `instructions` ≤ 4000 chars;
>   - `reason` ≤ 500 chars (`maxLength`).
> - Classifier `route` raises `NODE_FAILED` for a missing category or one outside `handles()`.
>
> Suite: 344.

---

## Task 10: Condition node

Spec 4.7. Operands are templates; their render targets depend on the operator (`>`/`<` → number, `contains` → string|array). Equality is "loose" so a typed literal like `"5"` or `"true"` matches `5` / `true`.

**Files:**

- Create: `services/engine/engine/nodes/condition.py`
- Test: `services/engine/tests/test_nodes_condition.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_nodes_condition.py`

```python
import pytest

from engine.nodes.condition import ConditionNode, evaluate
from tests.helpers import make_ctx


@pytest.mark.parametrize(
    ("op", "left", "right", "expected"),
    [
        ("==", "approve", "approve", True),
        ("==", 5, "5", True),
        ("==", 5.0, "5", True),
        ("==", True, "true", True),
        ("==", "a", "b", False),
        ("!=", "a", "b", True),
        (">=", 8, 8.0, True),
        ("<", 3, 8.0, True),
        ("contains", "hello world", "world", True),
        ("contains", ["a", "b"], "b", True),
        ("not_contains", ["a"], "b", True),
        ("is_empty", "", None, True),
        ("is_empty", [], None, True),
        ("is_not_empty", {"a": 1}, None, True),
    ],
)
def test_evaluate(op, left, right, expected):
    assert evaluate(op, left, right) is expected


async def test_condition_combinators_and_routing():
    spec = ConditionNode()
    raw = {
        "conditions": [
            {"left": "{{llm_eval.score}}", "op": ">=", "right": "8"},
            {"left": "{{llm_eval.ok}}", "op": "==", "right": "true"},
        ],
        "combinator": "and",
    }
    config = spec.parse_config(raw)
    assert [(f.path, f.target) for f in spec.template_fields(config)] == [
        ("conditions.0.left", "number"),
        ("conditions.0.right", "number"),
        ("conditions.1.left", "any"),
        ("conditions.1.right", "any"),
    ]
    rendered = {"conditions.0.left": 9, "conditions.0.right": 8.0, "conditions.1.left": True, "conditions.1.right": "true"}

    result = await spec.execute(make_ctx(), config, rendered)
    assert result.output == {"result": True}
    assert spec.route(config, result.output) == "true"

    rendered["conditions.0.left"] = 3
    assert (await spec.execute(make_ctx(), config, rendered)).output == {"result": False}
    or_config = spec.parse_config({**raw, "combinator": "or"})
    assert (await spec.execute(make_ctx(), or_config, rendered)).output == {"result": True}


def test_unary_ops_have_no_right_field():
    spec = ConditionNode()
    config = spec.parse_config({"conditions": [{"left": "{{a.b}}", "op": "is_empty"}]})
    assert [f.path for f in spec.template_fields(config)] == ["conditions.0.left"]
    assert spec.handles(config) == ["true", "false"]
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_nodes_condition.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.nodes.condition'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/nodes/condition.py`

```python
from __future__ import annotations

import operator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.dsl.types import Target, to_text
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField

Op = Literal["==", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "is_empty", "is_not_empty"]
NUMERIC_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
UNARY_OPS = frozenset({"is_empty", "is_not_empty"})


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str = Field(json_schema_extra=TEMPLATE)
    op: Op
    right: str = Field("", json_schema_extra=TEMPLATE)


class ConditionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conditions: list[Condition] = Field(min_length=1, max_length=10)
    combinator: Literal["and", "or"] = "and"


def operand_targets(op: str) -> tuple[Target, Target]:
    if op in NUMERIC_OPS:
        return "number", "number"
    if op in ("contains", "not_contains"):
        return "string|array", "any"
    return "any", "any"


def _loose_eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and not isinstance(b, str):
        a, b = b, a
    if not isinstance(a, str) and isinstance(b, str):
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            try:
                return float(b) == a
            except ValueError:
                return False
        return to_text(a) == b
    return a == b


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def evaluate(op: str, left: Any, right: Any) -> bool:
    if op == "is_empty":
        return _is_empty(left)
    if op == "is_not_empty":
        return not _is_empty(left)
    if op == "==":
        return _loose_eq(left, right)
    if op == "!=":
        return not _loose_eq(left, right)
    if op in NUMERIC_OPS:
        return NUMERIC_OPS[op](left, right)
    found = to_text(right) in left if isinstance(left, str) else any(_loose_eq(item, right) for item in left)
    return found if op == "contains" else not found


class ConditionNode(NodeSpec):
    type = "condition"
    label = "조건"
    category = "Logic"
    Config = ConditionConfig
    is_branch = True

    def handles(self, config: ConditionConfig) -> list[str]:
        return ["true", "false"]

    def template_fields(self, config: ConditionConfig) -> list[TemplateField]:
        fields: list[TemplateField] = []
        for index, cond in enumerate(config.conditions):
            left_target, right_target = operand_targets(cond.op)
            fields.append(TemplateField(f"conditions.{index}.left", cond.left, left_target))
            if cond.op not in UNARY_OPS:
                fields.append(TemplateField(f"conditions.{index}.right", cond.right, right_target))
        return fields

    def route(self, config: ConditionConfig, output: dict[str, Any]) -> str:
        return "true" if output["result"] else "false"

    def output_schema(self, config: ConditionConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {"type": "object", "properties": {"result": {"type": "boolean"}}, "required": ["result"]}

    async def execute(self, ctx: NodeContext, config: ConditionConfig, rendered: dict[str, Any]) -> NodeResult:
        results = [
            evaluate(cond.op, rendered[f"conditions.{i}.left"], rendered.get(f"conditions.{i}.right"))
            for i, cond in enumerate(config.conditions)
        ]
        result = all(results) if config.combinator == "and" else any(results)
        return NodeResult({"result": result})
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_nodes_condition.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/nodes/condition.py services/engine/tests/test_nodes_condition.py
git commit -m "feat(engine): add condition node"
```

> **Post-review note (Task 10, as implemented):** commits `4a1581c` and `d2707c3`.
>
> **Loose `==`.** The plan's version used Python equality, so `true == 1` and `"1_000" == 1000` were both true. It is now JSON equality via `engine.jsondata.json_equal`, which replaces the old private `_json_equal`. A string still matches the JSON value it spells, parsed with `parse_json`: `"5" == 5`, `"true" == true`, `"" == null`.
>
> **Operand checks.**
>
> - `contains`/`not_contains` need a non-empty `right` template.
> - A `null` needle never matches inside a string. It still matches a `null` array item.
> - Numeric ops and `contains` raise `TypeError` on mismatched operands. `execute` maps this to non-retryable `TYPE_MISMATCH`.
> - A `RecursionError` from deeply nested operands becomes `NODE_FAILED`.
> - `route` requires a bool `result`.
>
> Suite: 379.
>
> **Carried into Task 11** (prepared, not yet applied):
>
> - **`human_approval`:** check the resume answer's shape.
>   - Only the keys `decision`, `comment`, `editedValue`, `reviewedAt` are allowed.
>   - `comment` is a str of at most 10,000 chars.
>   - `reviewedAt` is a str of at most 64 chars.
>   - `editedValue` goes through `templates.env.json_value`, which enforces JSON data, bounds its size and copies it.
> - **`merge`:**
>   - Deep-copy upstream outputs.
>   - A missing predecessor output raises `NODE_FAILED` instead of `KeyError`.
> - **Registry:** keep the duplicate-type check.

---

## Task 11: Merge and human-approval nodes, final registry

Spec 5.6–5.7. `merge` orders branches by `ctx.pred_ids` (edge declaration order, supplied by the compiler). `human_approval` awaits `ctx.interrupt(payload)`, which the wrapper maps to LangGraph `interrupt()`; nothing before that call may have side effects because the node re-executes on resume.

**Files:**

- Create: `services/engine/engine/nodes/merge.py`
- Create: `services/engine/engine/nodes/human_approval.py`
- Modify: `services/engine/engine/nodes/registry.py` (register all MVP nodes)
- Test: `services/engine/tests/test_nodes_flow.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_nodes_flow.py`

```python
import pytest

from engine.errors import NodeError
from engine.nodes.human_approval import HumanApprovalNode
from engine.nodes.merge import MergeNode
from engine.nodes.registry import default_registry
from tests.helpers import make_ctx

OUTPUTS = {"llm_1": {"text": "a"}, "llm_2": {"text": "b"}}


async def test_merge_object_follows_pred_order():
    spec = MergeNode()
    config = spec.parse_config({})

    result = await spec.execute(make_ctx(outputs=OUTPUTS, pred_ids=["llm_2", "llm_1"]), config, {})

    assert list(result.output["branches"]) == ["llm_2", "llm_1"]
    schema = spec.output_schema(config, {"llm_2": {"type": "object"}, "llm_1": {"type": "object"}})
    assert list(schema["properties"]["branches"]["properties"]) == ["llm_2", "llm_1"]


async def test_merge_list():
    spec = MergeNode()
    config = spec.parse_config({"mode": "list"})
    result = await spec.execute(make_ctx(outputs=OUTPUTS, pred_ids=["llm_2", "llm_1"]), config, {})
    assert result.output == {"branches": [{"text": "b"}, {"text": "a"}]}


async def test_human_approval_interrupt_payload_and_output():
    captured: dict = {}

    async def fake_interrupt(payload: dict) -> dict:
        captured.update(payload)
        return {"decision": "approve", "comment": "좋아요", "editedValue": "수정본", "reviewedAt": "2026-09-11T00:00:00Z"}

    spec = HumanApprovalNode()
    config = spec.parse_config({"message": "검토해 주세요", "review": "{{start.draft}}", "allowEdit": True})
    ctx = make_ctx(node_id="human_approval_1", interrupt=fake_interrupt)

    result = await spec.execute(ctx, config, {"message": "검토해 주세요", "review": "초안"})

    assert captured == {
        "nodeId": "human_approval_1",
        "execIndex": 1,
        "message": "검토해 주세요",
        "review": "초안",
        "allowEdit": True,
    }
    assert result.output == {
        "decision": "approve",
        "comment": "좋아요",
        "editedValue": "수정본",
        "reviewedAt": "2026-09-11T00:00:00Z",
    }
    assert spec.route(config, result.output) == "approve"


async def test_human_approval_defaults_edited_value_to_review():
    async def fake_interrupt(payload: dict) -> dict:
        return {"decision": "reject"}

    spec = HumanApprovalNode()
    config = spec.parse_config({"message": "검토", "review": "{{start.draft}}"})

    result = await spec.execute(make_ctx(interrupt=fake_interrupt), config, {"message": "검토", "review": "초안"})

    assert result.output["editedValue"] == "초안"
    assert result.output["comment"] == ""
    assert result.output["reviewedAt"]
    assert spec.route(config, result.output) == "reject"


@pytest.mark.parametrize("answer", [{"decision": "maybe"}, "yes", {"decision": "approve", "editedValue": "x"}])
async def test_human_approval_rejects_invalid_answers(answer):
    async def fake_interrupt(payload: dict):
        return answer

    spec = HumanApprovalNode()
    config = spec.parse_config({"message": "검토"})
    with pytest.raises(NodeError):
        await spec.execute(make_ctx(interrupt=fake_interrupt), config, {"message": "검토"})


def test_registry_has_all_mvp_nodes():
    assert {spec.type for spec in default_registry().all()} == {
        "start", "end", "template", "llm", "classifier", "condition", "merge", "human_approval",
    }
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_nodes_flow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.nodes.human_approval'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/nodes/merge.py`

```python
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from engine.nodes.base import NodeContext, NodeResult, NodeSpec


class MergeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["object", "list"] = "object"


class MergeNode(NodeSpec):
    type = "merge"
    label = "합치기"
    category = "Logic"
    Config = MergeConfig

    def output_schema(self, config: MergeConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        if config.mode == "object":
            branches = {
                "type": "object",
                "properties": dict(pred_schemas),
                "required": list(pred_schemas),
            }
        else:
            branches = {"type": "array", "items": {}}
        return {"type": "object", "properties": {"branches": branches}, "required": ["branches"]}

    async def execute(self, ctx: NodeContext, config: MergeConfig, rendered: dict[str, Any]) -> NodeResult:
        if config.mode == "object":
            return NodeResult({"branches": {pred: ctx.outputs[pred] for pred in ctx.pred_ids}})
        return NodeResult({"branches": [ctx.outputs[pred] for pred in ctx.pred_ids]})
```

**File:** `services/engine/engine/nodes/human_approval.py`

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from engine.errors import ErrorCode, NodeError
from engine.nodes.base import TEMPLATE, NodeContext, NodeResult, NodeSpec, TemplateField


class HumanApprovalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, json_schema_extra=TEMPLATE)
    review: str = Field("", json_schema_extra=TEMPLATE)
    allowEdit: bool = False


class HumanApprovalNode(NodeSpec):
    """Pauses the run until a person approves or rejects.

    Re-executes from the top on resume (LangGraph semantics), so everything before
    `ctx.interrupt` must be side-effect free.
    """

    type = "human_approval"
    label = "사람 승인"
    category = "Human"
    Config = HumanApprovalConfig
    is_branch = True

    def handles(self, config: HumanApprovalConfig) -> list[str]:
        return ["approve", "reject"]

    def template_fields(self, config: HumanApprovalConfig) -> list[TemplateField]:
        fields = [TemplateField("message", config.message, "string")]
        if config.review:
            fields.append(TemplateField("review", config.review, "any"))
        return fields

    def route(self, config: HumanApprovalConfig, output: dict[str, Any]) -> str:
        return output["decision"]

    def output_schema(self, config: HumanApprovalConfig, pred_schemas: dict[str, dict]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["approve", "reject"]},
                "comment": {"type": "string"},
                "editedValue": {},
                "reviewedAt": {"type": "string"},
            },
            "required": ["decision", "comment", "editedValue", "reviewedAt"],
        }

    async def execute(self, ctx: NodeContext, config: HumanApprovalConfig, rendered: dict[str, Any]) -> NodeResult:
        if ctx.interrupt is None:
            raise NodeError(ErrorCode.NODE_FAILED, "승인 대기를 지원하지 않는 실행 환경입니다", retryable=False)
        review = rendered.get("review")
        answer = await ctx.interrupt(
            {
                "nodeId": ctx.node_id,
                "execIndex": ctx.exec_index,
                "message": rendered["message"],
                "review": review,
                "allowEdit": config.allowEdit,
            }
        )
        if not isinstance(answer, dict) or answer.get("decision") not in ("approve", "reject"):
            raise NodeError(ErrorCode.NODE_FAILED, "잘못된 승인 응답입니다", retryable=False)
        if "editedValue" in answer and not config.allowEdit:
            raise NodeError(ErrorCode.NODE_FAILED, "수정이 허용되지 않은 승인 노드입니다", retryable=False)
        return NodeResult(
            {
                "decision": answer["decision"],
                "comment": str(answer.get("comment") or ""),
                "editedValue": answer.get("editedValue", review),
                "reviewedAt": answer.get("reviewedAt") or datetime.now(UTC).isoformat(),
            }
        )
```

**File:** `services/engine/engine/nodes/registry.py`

```python
from __future__ import annotations

from engine.nodes.base import NodeSpec


class NodeRegistry:
    def __init__(self, specs: list[NodeSpec]) -> None:
        self._specs = {spec.type: spec for spec in specs}

    def get(self, node_type: str) -> NodeSpec | None:
        return self._specs.get(node_type)

    def all(self) -> list[NodeSpec]:
        return list(self._specs.values())


def default_registry() -> NodeRegistry:
    from engine.nodes.classifier import ClassifierNode
    from engine.nodes.condition import ConditionNode
    from engine.nodes.human_approval import HumanApprovalNode
    from engine.nodes.io import EndNode, StartNode
    from engine.nodes.llm import LLMNode
    from engine.nodes.merge import MergeNode
    from engine.nodes.template import TemplateNode

    return NodeRegistry(
        [
            StartNode(),
            EndNode(),
            TemplateNode(),
            LLMNode(),
            ClassifierNode(),
            ConditionNode(),
            MergeNode(),
            HumanApprovalNode(),
        ]
    )
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_nodes_flow.py tests/test_nodes_basic.py tests/test_nodes_ai.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/nodes services/engine/tests/test_nodes_flow.py
git commit -m "feat(engine): add merge and human approval nodes"
```

> **Post-review note (Task 11, as implemented):** commits `83e9fdb` and `e33e71e`.
>
> **`merge`.**
>
> - Each upstream output is copied with `json_value`. A missing output, an output that is too large, or one nested too deeply raises non-retryable `NODE_FAILED`.
> - `output_schema` deep-copies predecessor schemas. When the combined schema leaves the `schema_problems` subset (too deep or too large), each branch becomes `{}`; `required` is kept. This also stops merge-of-merge schemas from doubling in size.
>
> **`human_approval`.**
>
> - `route` checks `decision` is `approve` or `reject`.
> - A blank rendered message raises `TEMPLATE_ERROR` before the interrupt. The output schema bounds `comment` and sets `additionalProperties: false`.
> - Public `resume_output(answer, waiting)` checks the answer against the interrupt payload it answers, and builds the output. The node calls it on resume; Plan 2's API should call it before `waiting → queued`.
>   - Allowed keys are `nodeId`, `execIndex`, `decision`, `comment`, `editedValue` and `reviewedAt`. `nodeId`/`execIndex` are optional, but must equal the waiting values with the same type.
>   - `comment` is a str of at most 10,000 chars, with no NUL or lone surrogates.
>   - `editedValue` needs `allowEdit`. It goes through `json_value` and `jsondata.check_text`, and must have the review value's `json_kind` when the review is not null.
>   - `reviewedAt` must be ISO 8601 with a timezone and is normalized to UTC. The API sets it when it accepts the answer; it is never copied from the request. When absent, the server time is used.
> - `engine.jsondata` now exports `check_text` and `json_kind` (formerly `_check_text`, `_kind`).
>
> Suite: 412.
>
> **Carried into Tasks 13–14** (graph rules and references, where predecessors are known):
>
> - A `merge` needs at least 2 distinct forward predecessors and may not sit inside a branch chain (spec rule 8). Its `pred_ids` must be unique.
> - A merge output holds every branch output, so it can exceed the 1 MB node output cap even when each branch fits. Report this as a warning when two or more predecessors can produce large outputs, or document it in the node help text.
> - The merge schema fallback is all-or-nothing: when the combined schema is rejected, every branch loses its type, not only the largest. This is acceptable for the MVP; a later refinement could untype the largest branches first. Add a regression test that a merge of merges (e.g. 40 layers) stays inside `schema_problems`.
>
> **Carried into Task 16:** rendered values can still hold NUL or lone surrogates (e.g. `{{ '\x00' }}`, or a start input that contains one). They would reach node outputs and interrupt payloads and break `jsonb` storage. Call `jsondata.check_text` once in the wrapper, next to the output size check, on both the rendered values and the output. `resume_output` expects `waiting` to be the exact interrupt payload; it raises `KeyError` if `nodeId` or `execIndex` is missing.

---

## Task 12: Validator phase 1 — structure

Spec 4.2, 4.3, 4.8 (rules 1 and limits), 6.1. Produces `ParsedNode`s (parsed config + effective policy) that later phases and the compiler reuse. Phases run in order and stop at the first phase that reports errors, so users do not see cascades of follow-up errors.

**Files:**

- Create: `services/engine/engine/validator/__init__.py` (empty for now; facade in Task 14)
- Create: `services/engine/engine/validator/issues.py`
- Create: `services/engine/engine/validator/structure.py`
- Test: `services/engine/tests/test_validator_structure.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_validator_structure.py`

```python
import pytest

from engine.dsl.models import WorkflowDSL
from engine.nodes.registry import default_registry
from engine.validator.structure import check_structure


def _base() -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "p"}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "llm_1"},
            {"id": "e2", "source": "llm_1", "target": "end"},
        ],
    }


def _codes(raw: dict) -> list[str]:
    issues, _ = check_structure(WorkflowDSL.model_validate(raw), default_registry())
    return [issue.code for issue in issues]


def test_valid_structure_parses_configs_and_effective_policies():
    issues, parsed = check_structure(WorkflowDSL.model_validate(_base()), default_registry())
    assert issues == []
    assert parsed["llm_1"].policy.retry.maxAttempts == 3
    assert parsed["llm_1"].config.model == "m"
    assert parsed["start"].policy is None


def test_partial_policy_override_is_merged():
    raw = _base()
    raw["nodes"][1]["policy"] = {"timeoutSec": 30}
    _, parsed = check_structure(WorkflowDSL.model_validate(raw), default_registry())
    assert parsed["llm_1"].policy.timeoutSec == 30
    assert parsed["llm_1"].policy.retry.maxAttempts == 3


def test_classifier_default_on_error_needs_no_default_output():
    raw = _base()
    raw["nodes"][1] = {
        "id": "classifier_1",
        "type": "classifier",
        "config": {"model": "m", "input": "x", "categories": [{"id": "a", "description": "A"}]},
        "policy": {"onError": "default"},
    }
    raw["edges"] = [
        {"id": "e1", "source": "start", "target": "classifier_1"},
        {"id": "e2", "source": "classifier_1", "sourceHandle": "a", "target": "end"},
        {"id": "e3", "source": "classifier_1", "sourceHandle": "default", "target": "end"},
    ]
    assert _codes(raw) == []


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda r: r["nodes"].append({"id": "llm_1", "type": "template", "config": {"template": "x"}}), "DUPLICATE_NODE_ID"),
        (lambda r: r["nodes"].append({"id": "secret", "type": "template", "config": {"template": "x"}}), "RESERVED_NODE_ID"),
        (lambda r: r["nodes"].append({"id": "end_2", "type": "end"}), "END_COUNT"),
        (lambda r: r["nodes"].append({"id": "x_1", "type": "magic"}), "UNKNOWN_NODE_TYPE"),
        (lambda r: r["nodes"][1].update(config={}), "INVALID_CONFIG"),
        (lambda r: r["nodes"][0].update(policy={"timeoutSec": 5}), "POLICY_NOT_SUPPORTED"),
        (lambda r: r["nodes"][1].update(policy={"retry": {"maxAttempts": 0}}), "INVALID_POLICY"),
        (lambda r: r["nodes"][1].update(policy={"onError": "default"}), "DEFAULT_OUTPUT_REQUIRED"),
        (lambda r: r["nodes"][1].update(policy={"onError": "default", "defaultOutput": {"wrong": 1}}), "INVALID_POLICY"),
        (lambda r: r["edges"].append({"id": "e1", "source": "start", "target": "end"}), "DUPLICATE_EDGE_ID"),
        (lambda r: r["edges"].append({"id": "e9", "source": "llm_1", "target": "ghost"}), "EDGE_UNKNOWN_NODE"),
        (lambda r: r["edges"].append({"id": "e9", "source": "llm_1", "target": "start"}), "EDGE_INTO_START"),
        (lambda r: r["edges"].append({"id": "e9", "source": "llm_1", "sourceHandle": "true", "target": "end"}), "EDGE_UNKNOWN_HANDLE"),
        (lambda r: r["edges"].append({"id": "e9", "source": "end", "target": "llm_1"}), "EDGE_UNKNOWN_HANDLE"),
        (lambda r: r["nodes"].extend({"id": f"template_{i}", "type": "template", "config": {"template": "x"}} for i in range(100)), "LIMIT_EXCEEDED"),
    ],
)
def test_reports_structural_problems(mutate, code):
    raw = _base()
    mutate(raw)
    assert code in _codes(raw)


def test_start_node_must_use_fixed_id():
    raw = _base()
    raw["nodes"][0]["id"] = "begin"
    raw["edges"][0]["source"] = "begin"
    assert "RESERVED_NODE_ID" in _codes(raw)


def test_issue_serialization_omits_empty_locations():
    issues, _ = check_structure(
        WorkflowDSL.model_validate({**_base(), "nodes": _base()["nodes"] + [{"id": "x_1", "type": "magic"}]}),
        default_registry(),
    )
    assert issues[0].to_dict() == {
        "severity": "error",
        "code": "UNKNOWN_NODE_TYPE",
        "message": "알 수 없는 노드 종류입니다: magic",
        "nodeId": "x_1",
    }
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_validator_structure.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.validator'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/validator/__init__.py`

```python

```

**File:** `services/engine/engine/validator/issues.py`

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    nodeId: str | None = None
    edgeId: str | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def error(code: str, message: str, **where: Any) -> Issue:
    return Issue("error", code, message, **where)


def warning(code: str, message: str, **where: Any) -> Issue:
    return Issue("warning", code, message, **where)


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.severity == "error" for issue in issues)
```

**File:** `services/engine/engine/validator/structure.py`

```python
"""Phase 1: ids, node types, configs, policies, edge references, size limits."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from engine.dsl.models import Node, Policy, WorkflowDSL, merge_policy
from engine.nodes.base import NodeSpec, schema_violations
from engine.nodes.registry import NodeRegistry
from engine.validator.issues import Issue, error

MAX_NODES = 100
MAX_EDGES = 300
# Template roots ("secret", Jinja's "loop") and LangGraph state keys cannot be node ids.
RESERVED_IDS = frozenset({"secret", "loop", "inputs", "outputs", "routes", "loop_counters", "exec_counts"})
FIXED_IDS = {"start": "start", "end": "end"}  # node type -> required id


@dataclass(frozen=True)
class ParsedNode:
    node: Node
    spec: NodeSpec
    config: BaseModel
    policy: Policy | None  # effective policy; None for node types without policies


def pydantic_issues(exc: ValidationError, code: str, prefix: str, **where: Any) -> list[Issue]:
    issues = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"])
        issues.append(error(code, f"설정 오류: {err['msg']}", field=f"{prefix}{location}".rstrip("."), **where))
    return issues


def check_structure(dsl: WorkflowDSL, registry: NodeRegistry) -> tuple[list[Issue], dict[str, ParsedNode]]:
    issues: list[Issue] = []
    parsed: dict[str, ParsedNode] = {}
    if len(dsl.nodes) > MAX_NODES:
        issues.append(error("LIMIT_EXCEEDED", f"노드는 최대 {MAX_NODES}개까지 사용할 수 있습니다"))
    if len(dsl.edges) > MAX_EDGES:
        issues.append(error("LIMIT_EXCEEDED", f"연결은 최대 {MAX_EDGES}개까지 사용할 수 있습니다"))

    node_ids: set[str] = set()
    for node in dsl.nodes:
        if node.id in node_ids:
            issues.append(error("DUPLICATE_NODE_ID", f"노드 id가 중복되었습니다: {node.id}", nodeId=node.id))
            continue
        node_ids.add(node.id)
        issues.extend(_check_id(node))
        spec = registry.get(node.type)
        if spec is None:
            issues.append(error("UNKNOWN_NODE_TYPE", f"알 수 없는 노드 종류입니다: {node.type}", nodeId=node.id))
            continue
        try:
            config = spec.parse_config(node.config)
        except ValidationError as exc:
            issues.extend(pydantic_issues(exc, "INVALID_CONFIG", "config.", nodeId=node.id))
            continue
        policy, policy_issues = _effective_policy(node, spec, config)
        issues.extend(policy_issues)
        if not policy_issues:
            parsed[node.id] = ParsedNode(node, spec, config, policy)

    for node_type in FIXED_IDS:
        count = sum(1 for node in dsl.nodes if node.type == node_type)
        if count != 1:
            issues.append(
                error(f"{node_type.upper()}_COUNT", f"'{node_type}' 노드는 정확히 1개여야 합니다 (현재 {count}개)")
            )
    issues.extend(_check_edges(dsl, parsed, node_ids))
    return issues, parsed


def _check_id(node: Node) -> list[Issue]:
    if node.id in RESERVED_IDS:
        return [error("RESERVED_NODE_ID", f"'{node.id}'는 예약된 id입니다", nodeId=node.id)]
    fixed = FIXED_IDS.get(node.type)
    if fixed is not None and node.id != fixed:
        return [error("RESERVED_NODE_ID", f"'{node.type}' 노드의 id는 '{fixed}'여야 합니다", nodeId=node.id)]
    if node.id in FIXED_IDS.values() and node.type != node.id:
        return [error("RESERVED_NODE_ID", f"'{node.id}' id는 {node.id} 노드 전용입니다", nodeId=node.id)]
    return []


def _effective_policy(node: Node, spec: NodeSpec, config: BaseModel) -> tuple[Policy | None, list[Issue]]:
    if spec.default_policy is None:
        if node.policy is not None:
            return None, [
                error("POLICY_NOT_SUPPORTED", f"'{spec.type}' 노드는 실행 정책을 지원하지 않습니다",
                      nodeId=node.id, field="policy")
            ]
        return None, []
    try:
        policy = merge_policy(spec.default_policy, node.policy)
    except ValidationError as exc:
        return None, pydantic_issues(exc, "INVALID_POLICY", "policy.", nodeId=node.id)
    if policy.onError == "default":
        output = policy.defaultOutput if policy.defaultOutput is not None else spec.fallback_output(config)
        if output is None:
            return None, [
                error("DEFAULT_OUTPUT_REQUIRED", "onError가 default이면 defaultOutput이 필요합니다",
                      nodeId=node.id, field="policy.defaultOutput")
            ]
        violations = schema_violations(spec.output_schema(config, {}), output)
        if violations:
            return None, [
                error("INVALID_POLICY", "defaultOutput이 노드 출력 형식과 맞지 않습니다: " + "; ".join(violations),
                      nodeId=node.id, field="policy.defaultOutput")
            ]
    return policy, []


def _check_edges(dsl: WorkflowDSL, parsed: dict[str, ParsedNode], node_ids: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    edge_ids: set[str] = set()
    for edge in dsl.edges:
        if edge.id in edge_ids:
            issues.append(error("DUPLICATE_EDGE_ID", f"연결 id가 중복되었습니다: {edge.id}", edgeId=edge.id))
            continue
        edge_ids.add(edge.id)
        missing = [end for end in (edge.source, edge.target) if end not in node_ids]
        if missing:
            issues.append(
                error("EDGE_UNKNOWN_NODE", f"연결이 존재하지 않는 노드를 가리킵니다: {', '.join(missing)}", edgeId=edge.id)
            )
            continue
        if edge.target == "start":
            issues.append(error("EDGE_INTO_START", "시작 노드로 들어오는 연결은 만들 수 없습니다", edgeId=edge.id))
        source = parsed.get(edge.source)
        if source is not None and edge.sourceHandle not in source.spec.handles(source.config):
            issues.append(
                error("EDGE_UNKNOWN_HANDLE", f"'{edge.source}' 노드에 '{edge.sourceHandle}' 출력이 없습니다",
                      edgeId=edge.id)
            )
    return issues
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_validator_structure.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/validator services/engine/tests/test_validator_structure.py
git commit -m "feat(engine): add structural validation phase"
```

> **Post-review note (Task 12, as implemented):** commits `2e5e7c8`, `256876d` and `d864f89`.
>
> **Ids.** `RESERVED_IDS` adds the following, and a test keeps the LangGraph list in sync on upgrades:
>
> - Template names that break a root: `self`, `true`, `false`, `none`, `not`.
> - Names LangGraph refuses as node names at compile time: `checkpoint_id`, `checkpoint_map`, `checkpoint_ns`, `configurable`.
>
> **Bounded work and output.**
>
> - Over `MAX_NODES`/`MAX_EDGES`, `check_structure` returns only `LIMIT_EXCEEDED` and an empty `parsed`.
> - At most 100 issues per phase and 10 pydantic errors per config or policy. Tenant-chosen names in messages are clipped to 80 chars.
> - `check_structure(dsl, registry, budget=None)` charges every schema validation to one `jsondata.StepBudget` (default `MAX_VALIDATION_STEPS`). `schema_violations(..., budget=)` caps its steps at what the budget has left. **Task 14's `validate()` must create one budget and pass it through every phase.**
> - Once the budget is spent, later schema checks are skipped rather than blamed on nodes that may be fine, and one workflow-level `VALIDATION_TOO_COSTLY` error is added. The node whose check ran out still gets `INVALID_POLICY`. Task 14 should follow the same rule.
>
> **Policies.**
>
> - `defaultOutput` is checked whenever present, even with `onError: "fail"`. It must be JSON data of at most 64,000 chars (`json_value`), storable text (`check_text`), and match the node's output schema. A spent budget gives `INVALID_POLICY`.
> - `ParsedNode.policy` owns its data: `retry` is copied and `defaultOutput` is the validated copy.
> - `policy: {}` means no override, so policy-less nodes accept it.
>
> **Edges.**
>
> - `DUPLICATE_EDGE` is reported for a repeated `(source, sourceHandle, target)`.
> - Handles are checked whenever the source config parsed, even if its policy failed.
> - An edge from `end` says the node cannot start a connection.
>
> Suite: 468. The plan's Task 13/14 validator code and tests still pass on top of this.
>
> **Carried into Task 16:** `_fallback_output` returns `plan.policy.defaultOutput` itself, so every run of a cached compiled workflow would share one dict. Return a copy per use, and apply the output size and text checks on the fallback path too.
>
> **Carried into Plan 2/3 (roadmap):**
>
> - Pydantic's English messages appear inside Korean issue text; localize them by error `type`.
> - `Policy`/`RetrySpec` coerce loosely (`timeoutSec: true` → 1). Consider strict mode at the DSL boundary.

---

## Task 13: Validator phase 2 — graph rules

Spec 4.8 rules 2–9. Back-edges are the DFS back-edges from `start` (edges visited in declaration order); removing them leaves a DAG, which gives the topological order used by phase 3 and the compiler.

**MVP tightening of rules 7–9 (recorded in the spec):** a parallel region is a fan-out handle whose every branch is a **linear chain of ≥1 plain node** (not a branch node, one forward in-edge, one out-edge) ending in the **same `merge`**, and that merge receives exactly those branches. This makes duplicate execution (rule 7) and merge deadlock (rule 8) impossible by construction; nested parallel regions are out of MVP scope.

**Files:**

- Create: `services/engine/engine/validator/graph.py`
- Test: `services/engine/tests/test_validator_graph.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_validator_graph.py`

```python
from engine.dsl.models import WorkflowDSL
from engine.nodes.registry import default_registry
from engine.validator.graph import build_graph, check_graph
from engine.validator.structure import check_structure

START = {"id": "start", "type": "start"}
END = {"id": "end", "type": "end"}
MERGE = {"id": "merge_1", "type": "merge"}
COND = {"id": "condition_1", "type": "condition", "config": {"conditions": [{"left": "1", "op": "==", "right": "1"}]}}
CLASSIFIER = {
    "id": "classifier_1",
    "type": "classifier",
    "config": {"model": "m", "input": "x", "categories": [{"id": "a", "description": "A"}]},
}


def llm(i: int) -> dict:
    return {"id": f"llm_{i}", "type": "llm", "config": {"model": "m", "prompt": "p"}}


def tpl(i: int) -> dict:
    return {"id": f"template_{i}", "type": "template", "config": {"template": "t"}}


def e(edge_id: str, source: str, target: str, handle: str = "out", max_iterations: int | None = None) -> dict:
    edge = {"id": edge_id, "source": source, "sourceHandle": handle, "target": target}
    if max_iterations is not None:
        edge["maxIterations"] = max_iterations
    return edge


def _analyze(nodes: list[dict], edges: list[dict]):
    dsl = WorkflowDSL.model_validate({"nodes": nodes, "edges": edges})
    issues, parsed = check_structure(dsl, default_registry())
    assert issues == [], issues
    graph = build_graph(dsl, parsed)
    return graph, [issue.code for issue in check_graph(graph)]


def test_linear_graph_is_valid_and_ordered():
    graph, codes = _analyze([START, llm(1), END], [e("e1", "start", "llm_1"), e("e2", "llm_1", "end")])
    assert codes == []
    assert graph.order == ["start", "llm_1", "end"]
    assert graph.back_edges == {}


def test_unreachable_node():
    _, codes = _analyze(
        [START, llm(1), llm(2), END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "end"), e("e3", "llm_2", "end")],
    )
    assert "UNREACHABLE_FROM_START" in codes


def test_dead_end_node():
    _, codes = _analyze(
        [START, llm(1), llm(2), END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "end"), e("e3", "start", "llm_2")],
    )
    assert "HANDLE_NOT_CONNECTED" in codes
    assert "CANNOT_REACH_END" in codes


def test_valid_evaluator_loop():
    graph, codes = _analyze(
        [START, llm(1), COND, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "condition_1"),
         e("e3", "condition_1", "end", "true"), e("back", "condition_1", "llm_1", "false", 3)],
    )
    assert codes == []
    assert set(graph.back_edges) == {"back"}
    assert [edge.id for edge in graph.incoming["llm_1"]] == ["e1"]


def test_loop_without_limit():
    _, codes = _analyze(
        [START, llm(1), COND, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "condition_1"),
         e("e3", "condition_1", "end", "true"), e("back", "condition_1", "llm_1", "false")],
    )
    assert "BACK_EDGE_NO_LIMIT" in codes


def test_cycle_must_start_at_a_branch_node():
    _, codes = _analyze(
        [START, llm(1), llm(2), END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "llm_2"), e("e3", "llm_2", "llm_1"), e("e4", "llm_1", "end")],
    )
    assert "ILLEGAL_CYCLE" in codes


def test_max_iterations_only_on_back_edges():
    _, codes = _analyze([START, llm(1), END], [e("e1", "start", "llm_1", max_iterations=2), e("e2", "llm_1", "end")])
    assert "MAX_ITERATIONS_ON_FORWARD_EDGE" in codes


def test_both_condition_handles_looping_is_a_conflict():
    _, codes = _analyze(
        [START, llm(1), COND, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "condition_1"),
         e("b1", "condition_1", "llm_1", "true", 1), e("b2", "condition_1", "llm_1", "false", 1)],
    )
    assert "BACK_EDGE_HANDLE_CONFLICT" in codes


def test_classifier_default_cannot_loop():
    _, codes = _analyze(
        [START, llm(1), CLASSIFIER, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "classifier_1"),
         e("e3", "classifier_1", "end", "a"), e("back", "classifier_1", "llm_1", "default", 2)],
    )
    assert "BACK_EDGE_HANDLE_CONFLICT" in codes


def test_valid_parallel_region():
    _, codes = _analyze(
        [START, llm(1), llm(2), MERGE, END],
        [e("e1", "start", "llm_1"), e("e2", "start", "llm_2"),
         e("e3", "llm_1", "merge_1"), e("e4", "llm_2", "merge_1"), e("e5", "merge_1", "end")],
    )
    assert codes == []


def test_parallel_branches_must_merge():
    _, codes = _analyze(
        [START, llm(1), llm(2), END],
        [e("e1", "start", "llm_1"), e("e2", "start", "llm_2"), e("e3", "llm_1", "end"), e("e4", "llm_2", "end")],
    )
    assert "INVALID_PARALLEL_REGION" in codes


def test_no_branch_nodes_inside_parallel_region():
    _, codes = _analyze(
        [START, llm(1), COND, MERGE, END],
        [e("e1", "start", "llm_1"), e("e2", "start", "condition_1"),
         e("e3", "llm_1", "merge_1"), e("e4", "condition_1", "merge_1", "true"),
         e("e5", "condition_1", "merge_1", "false"), e("e6", "merge_1", "end")],
    )
    assert "INVALID_PARALLEL_REGION" in codes


def test_parallel_branch_needs_at_least_one_node():
    _, codes = _analyze(
        [START, llm(1), MERGE, END],
        [e("e1", "start", "llm_1"), e("e2", "start", "merge_1"), e("e3", "llm_1", "merge_1"), e("e4", "merge_1", "end")],
    )
    assert "INVALID_PARALLEL_REGION" in codes


def test_merge_without_fan_out():
    _, codes = _analyze(
        [START, llm(1), MERGE, END],
        [e("e1", "start", "llm_1"), e("e2", "llm_1", "merge_1"), e("e3", "merge_1", "end")],
    )
    assert "MERGE_WITHOUT_FAN_OUT" in codes


def test_parallel_region_inside_loop():
    _, codes = _analyze(
        [START, tpl(1), llm(1), llm(2), MERGE, COND, END],
        [e("e1", "start", "template_1"), e("e2", "template_1", "llm_1"), e("e3", "template_1", "llm_2"),
         e("e4", "llm_1", "merge_1"), e("e5", "llm_2", "merge_1"), e("e6", "merge_1", "condition_1"),
         e("e7", "condition_1", "end", "true"), e("back", "condition_1", "template_1", "false", 2)],
    )
    assert "PARALLEL_IN_LOOP" in codes


def test_fan_out_limit():
    nodes = [START, MERGE, END] + [llm(i) for i in range(11)]
    edges = [e(f"a{i}", "start", f"llm_{i}") for i in range(11)]
    edges += [e(f"b{i}", f"llm_{i}", "merge_1") for i in range(11)]
    edges.append(e("c", "merge_1", "end"))
    _, codes = _analyze(nodes, edges)
    assert "LIMIT_EXCEEDED" in codes
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_validator_graph.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.validator.graph'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/validator/graph.py`

```python
"""Phase 2: reachability, handles, loops (back-edges) and parallel regions."""
from __future__ import annotations

from dataclasses import dataclass

from engine.dsl.models import Edge, WorkflowDSL
from engine.validator.issues import Issue, error
from engine.validator.structure import ParsedNode

MAX_FAN_OUT = 10
MAX_BACK_EDGES = 10
LOOP_SOURCES = frozenset({"condition", "classifier"})


@dataclass
class Graph:
    nodes: dict[str, ParsedNode]
    edges: list[Edge]  # declaration order
    out: dict[str, dict[str, list[Edge]]]  # node -> handle -> edges (declaration order)
    incoming: dict[str, list[Edge]]  # forward in-edges only (back-edges excluded), declaration order
    back_edges: dict[str, Edge]
    reachable: set[str]
    order: list[str]  # topological order of reachable nodes over forward edges


def _adjacency(nodes: dict[str, ParsedNode], edges: list[Edge]) -> dict[str, list[Edge]]:
    adjacency: dict[str, list[Edge]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        adjacency[edge.source].append(edge)
    return adjacency


def build_graph(dsl: WorkflowDSL, parsed: dict[str, ParsedNode]) -> Graph:
    """Requires a structurally valid DSL (phase 1 without errors)."""
    edges = list(dsl.edges)
    out = {node_id: {handle: [] for handle in pn.spec.handles(pn.config)} for node_id, pn in parsed.items()}
    for edge in edges:
        out[edge.source][edge.sourceHandle].append(edge)
    adjacency = _adjacency(parsed, edges)
    back = _back_edge_ids(adjacency)
    reachable = _reach("start", adjacency)
    incoming: dict[str, list[Edge]] = {node_id: [] for node_id in parsed}
    for edge in edges:
        if edge.id not in back and edge.source in reachable:
            incoming[edge.target].append(edge)
    order = _topological_order(list(parsed), incoming, reachable)
    return Graph(parsed, edges, out, incoming, {e.id: e for e in edges if e.id in back}, reachable, order)


def _back_edge_ids(adjacency: dict[str, list[Edge]]) -> set[str]:
    state: dict[str, int] = {}  # 1 = on DFS stack, 2 = finished
    back: set[str] = set()

    def visit(node_id: str) -> None:
        state[node_id] = 1
        for edge in adjacency[node_id]:
            mark = state.get(edge.target, 0)
            if mark == 1:
                back.add(edge.id)
            elif mark == 0:
                visit(edge.target)
        state[node_id] = 2

    visit("start")
    return back


def _reach(origin: str, adjacency: dict[str, list[Edge]]) -> set[str]:
    seen = {origin}
    stack = [origin]
    while stack:
        for edge in adjacency[stack.pop()]:
            if edge.target not in seen:
                seen.add(edge.target)
                stack.append(edge.target)
    return seen


def _topological_order(node_ids: list[str], incoming: dict[str, list[Edge]], reachable: set[str]) -> list[str]:
    indegree = {node_id: len(incoming[node_id]) for node_id in reachable}
    children: dict[str, list[str]] = {node_id: [] for node_id in reachable}
    for node_id in reachable:
        for edge in incoming[node_id]:
            children[edge.source].append(node_id)
    queue = [node_id for node_id in node_ids if node_id in reachable and indegree[node_id] == 0]
    order: list[str] = []
    while queue:
        node_id = queue.pop(0)
        order.append(node_id)
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return order


def check_graph(graph: Graph) -> list[Issue]:
    return [
        *_check_reachability(graph),
        *_check_handles(graph),
        *_check_loops(graph),
        *_check_parallel(graph),
    ]


def _check_reachability(graph: Graph) -> list[Issue]:
    reverse: dict[str, list[Edge]] = {node_id: [] for node_id in graph.nodes}
    for edge in graph.edges:
        reverse[edge.target].append(Edge(id=edge.id, source=edge.target, target=edge.source))
    reaches_end = _reach("end", reverse)
    issues = []
    for node_id in graph.nodes:
        if node_id not in graph.reachable:
            issues.append(error("UNREACHABLE_FROM_START", "시작 노드에서 도달할 수 없는 노드입니다", nodeId=node_id))
        elif node_id not in reaches_end:
            issues.append(error("CANNOT_REACH_END", "끝 노드에 도달할 수 없는 노드입니다", nodeId=node_id))
    return issues


def _check_handles(graph: Graph) -> list[Issue]:
    return [
        error("HANDLE_NOT_CONNECTED", f"'{handle}' 출력이 연결되지 않았습니다", nodeId=node_id, field=f"handles.{handle}")
        for node_id, handles in graph.out.items()
        for handle, edges in handles.items()
        if not edges
    ]


def _check_loops(graph: Graph) -> list[Issue]:
    issues: list[Issue] = []
    if len(graph.back_edges) > MAX_BACK_EDGES:
        issues.append(error("LIMIT_EXCEEDED", f"되돌아가는 연결은 최대 {MAX_BACK_EDGES}개까지 사용할 수 있습니다"))
    for edge in graph.back_edges.values():
        if graph.nodes[edge.source].spec.type not in LOOP_SOURCES:
            issues.append(error("ILLEGAL_CYCLE", "순환 연결은 조건/분류 노드에서만 시작할 수 있습니다", edgeId=edge.id))
        elif edge.maxIterations is None:
            issues.append(error("BACK_EDGE_NO_LIMIT", "되돌아가는 연결에는 maxIterations가 필요합니다", edgeId=edge.id))
    for edge in graph.edges:
        if edge.id not in graph.back_edges and edge.maxIterations is not None:
            issues.append(
                error("MAX_ITERATIONS_ON_FORWARD_EDGE", "maxIterations는 되돌아가는 연결에만 지정할 수 있습니다",
                      edgeId=edge.id)
            )
    for node_id, handles in graph.out.items():
        loop_handles = [h for h, edges in handles.items() if any(e.id in graph.back_edges for e in edges)]
        if len(loop_handles) > 1:
            issues.append(error("BACK_EDGE_HANDLE_CONFLICT", "되돌아가는 연결은 한 출력에서만 나갈 수 있습니다",
                                nodeId=node_id))
        for handle in loop_handles:
            if len(handles[handle]) != 1:
                issues.append(error("BACK_EDGE_HANDLE_CONFLICT",
                                    "되돌아가는 연결이 있는 출력에는 다른 연결을 둘 수 없습니다",
                                    nodeId=node_id, field=f"handles.{handle}"))
            if graph.nodes[node_id].spec.type == "classifier" and handle == "default":
                issues.append(error("BACK_EDGE_HANDLE_CONFLICT",
                                    "분류 노드의 default 출력은 되돌아가는 연결이 될 수 없습니다",
                                    nodeId=node_id, field="handles.default"))
    return issues


def _in_cycle(graph: Graph, node_id: str) -> bool:
    adjacency = _adjacency(graph.nodes, graph.edges)
    seen: set[str] = set()
    stack = [edge.target for edge in adjacency[node_id]]
    while stack:
        current = stack.pop()
        if current == node_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(edge.target for edge in adjacency[current])
    return False


def _walk_branch(graph: Graph, first: Edge) -> tuple[str, str] | Issue:
    """Follow one fan-out branch to its merge. Returns (merge_id, closing_edge_id) or an Issue."""
    back_targets = {edge.target for edge in graph.back_edges.values()}
    edge = first
    steps = 0
    while True:
        target_id = edge.target
        target = graph.nodes[target_id]
        if target.spec.type == "merge":
            if steps == 0:
                return error("INVALID_PARALLEL_REGION", "병렬 분기에는 합치기 전에 노드가 하나 이상 있어야 합니다",
                             edgeId=first.id)
            return target_id, edge.id
        out_edges = [e for edges in graph.out[target_id].values() for e in edges]
        is_plain = (
            not target.spec.is_branch
            and target.spec.type not in ("start", "end")
            and len(graph.incoming[target_id]) == 1
            and target_id not in back_targets
            and len(out_edges) == 1
            and out_edges[0].id not in graph.back_edges
        )
        steps += 1
        if not is_plain or steps > len(graph.nodes):
            return error(
                "INVALID_PARALLEL_REGION",
                "병렬 분기는 조건·분류·승인 노드나 다른 분기·합류 없이 하나의 합치기 노드로 모여야 합니다",
                nodeId=target_id,
            )
        edge = out_edges[0]


def _check_parallel(graph: Graph) -> list[Issue]:
    issues: list[Issue] = []
    claimed_merges: set[str] = set()
    for node_id, handles in graph.out.items():
        for handle, edges in handles.items():
            if len(edges) < 2:
                continue
            where = {"nodeId": node_id, "field": f"handles.{handle}"}
            if len(edges) > MAX_FAN_OUT:
                issues.append(error("LIMIT_EXCEEDED", f"병렬 분기는 최대 {MAX_FAN_OUT}개까지 사용할 수 있습니다", **where))
            if _in_cycle(graph, node_id):
                issues.append(error("PARALLEL_IN_LOOP", "반복(루프) 안에서는 병렬 분기를 사용할 수 없습니다", **where))
                continue
            merges: set[str] = set()
            closing: set[str] = set()
            branch_issues: list[Issue] = []
            for edge in edges:
                result = _walk_branch(graph, edge)
                if isinstance(result, Issue):
                    branch_issues.append(result)
                else:
                    merges.add(result[0])
                    closing.add(result[1])
            claimed_merges |= merges
            if branch_issues:
                issues.extend(branch_issues)
                continue
            if len(merges) != 1:
                issues.append(error("INVALID_PARALLEL_REGION", "병렬 분기는 모두 같은 합치기 노드로 모여야 합니다", **where))
                continue
            merge_id = next(iter(merges))
            if {edge.id for edge in graph.incoming[merge_id]} != closing:
                issues.append(error("INVALID_PARALLEL_REGION",
                                    f"합치기 노드 '{merge_id}'에는 이 병렬 분기만 들어올 수 있습니다", nodeId=merge_id))
    for node_id, pn in graph.nodes.items():
        if pn.spec.type == "merge" and node_id in graph.reachable and node_id not in claimed_merges:
            issues.append(error("MERGE_WITHOUT_FAN_OUT",
                                "합치기 노드는 한 출력에서 갈라진 병렬 분기를 모아야 합니다", nodeId=node_id))
    return issues
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_validator_graph.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/validator/graph.py services/engine/tests/test_validator_graph.py
git commit -m "feat(engine): add graph validation phase (loops, parallel regions)"
```

> **Post-review note (Task 13, as implemented):** commits `9daaab9` and `68759e5`.
>
> **Back-edges are declared, not discovered.** The plan's DFS back-edges depended on edge declaration order. Deleting and redrawing one edge could flip a valid workflow to `ILLEGAL_CYCLE`, and change `incoming`, the Task 14 "runs before" sets and loop counters. Now (spec 4.8 rule 4 updated):
>
> - `back_edges` are the edges that carry `maxIterations`, and the forward edges must be acyclic.
> - A forward cycle containing a condition or classifier edge gives `BACK_EDGE_NO_LIMIT` on those edges. A forward cycle with neither gives one `ILLEGAL_CYCLE` per cycle.
> - A declared back-edge must close a cycle through forward edges (else `MAX_ITERATIONS_ON_FORWARD_EDGE`) and start at a condition or classifier (else `ILLEGAL_CYCLE`). Handle-conflict checks apply only to loop sources.
> - `order` is Kahn's algorithm, with ties broken by node declaration order.
>
> **Parallel regions.**
>
> - `PARALLEL_IN_LOOP` is also reported when the region's merge is a back-edge target or lies on a cycle. Before this, a back-edge into a merge passed and re-ran only the nodes after the merge.
> - A fan-out handle counts only forward edges.
> - Branches that meet at a non-merge node are reported once, on the fan-out handle, telling the user to add a merge.
>
> **Less noise.**
>
> - Issues are de-duplicated and capped at `MAX_ISSUES`.
> - A merge a broken region heads for is not also reported as `MERGE_WITHOUT_FAN_OUT`.
> - Unreachable nodes get no `HANDLE_NOT_CONNECTED`.
> - `CANNOT_REACH_END` is reported only on the dead end itself, not on the nodes upstream of it.
>
> **Guaranteed by the rules** (Task 11 carry-over): a reachable merge in a valid graph has at least 2 distinct forward predecessors, all of them branch-chain ends, and never sits inside a branch chain. The 40-layer merge-of-merges schema regression test is in `test_nodes_flow.py`.
>
> Suite: 503. The plan's Task 14 refs tests pass on top.
>
> **Carried into Plan 3 (editor):**
>
> - When the user draws an edge that closes a cycle from a condition or classifier, ask for `maxIterations`. That edge becomes the back-edge.
> - A condition self-loop (`false → itself`) is accepted but re-evaluates the same inputs; show a warning.
> - A merge output holds every branch output and can exceed the 1 MB node output cap; say so in the merge node's help text.

---

## Task 14: Validator phase 3 — references and types, `validate()` facade

Spec 4.4 (guaranteed-before sets, `| default` rule), 4.5 (type compatibility), 6.1. `analyze()` is what the compiler uses: it returns the issues plus the `Graph` when phases 1–2 passed.

**Files:**

- Create: `services/engine/engine/validator/refs.py`
- Modify: `services/engine/engine/validator/__init__.py` (facade)
- Test: `services/engine/tests/test_validator_refs.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_validator_refs.py`

```python
from engine.validator import analyze, validate

INPUTS = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}
START = {"id": "start", "type": "start", "config": {"inputs": INPUTS}}
SCORE_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["score", "reason"],
}


def _codes(raw: dict) -> list[tuple[str, str]]:
    return [(issue.severity, issue.code) for issue in validate(raw)]


def chain(prompt: str, end_output: str = "{{llm_1.text}}") -> dict:
    return {
        "nodes": [
            START,
            {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": prompt}},
            {"id": "end", "type": "end", "config": {"outputs": {"r": end_output}}},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "llm_1"}, {"id": "e2", "source": "llm_1", "target": "end"}],
    }


def routing(end_output: str) -> dict:
    categories = [{"id": "a", "description": "A"}, {"id": "b", "description": "B"}]
    return {
        "nodes": [
            START,
            {"id": "classifier_1", "type": "classifier",
             "config": {"model": "m", "input": "{{start.topic}}", "categories": categories}},
            {"id": "llm_a", "type": "llm", "config": {"model": "m", "prompt": "A"}},
            {"id": "llm_b", "type": "llm", "config": {"model": "m", "prompt": "B"}},
            {"id": "end", "type": "end", "config": {"outputs": {"r": end_output}}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "classifier_1"},
            {"id": "e2", "source": "classifier_1", "sourceHandle": "a", "target": "llm_a"},
            {"id": "e3", "source": "classifier_1", "sourceHandle": "b", "target": "llm_b"},
            {"id": "e4", "source": "classifier_1", "sourceHandle": "default", "target": "end"},
            {"id": "e5", "source": "llm_a", "target": "end"},
            {"id": "e6", "source": "llm_b", "target": "end"},
        ],
    }


def parallel(end_output: str) -> dict:
    return {
        "nodes": [
            START,
            {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "1"}},
            {"id": "llm_2", "type": "llm", "config": {"model": "m", "prompt": "2"}},
            {"id": "merge_1", "type": "merge"},
            {"id": "end", "type": "end", "config": {"outputs": {"r": end_output}}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "llm_1"},
            {"id": "e2", "source": "start", "target": "llm_2"},
            {"id": "e3", "source": "llm_1", "target": "merge_1"},
            {"id": "e4", "source": "llm_2", "target": "merge_1"},
            {"id": "e5", "source": "merge_1", "target": "end"},
        ],
    }


def condition(left: str, right: str, op: str = ">=") -> dict:
    return {
        "nodes": [
            START,
            {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "p", "outputSchema": SCORE_SCHEMA}},
            {"id": "condition_1", "type": "condition",
             "config": {"conditions": [{"left": left, "op": op, "right": right}]}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "llm_1"},
            {"id": "e2", "source": "llm_1", "target": "condition_1"},
            {"id": "e3", "source": "condition_1", "sourceHandle": "true", "target": "end"},
            {"id": "e4", "source": "condition_1", "sourceHandle": "false", "target": "end"},
        ],
    }


def test_valid_chain_and_analysis():
    raw = chain("{{start.topic}}에 대해 써줘")
    assert validate(raw) == []
    analysis = analyze(raw)
    assert analysis.graph is not None
    assert analysis.graph.order == ["start", "llm_1", "end"]


def test_malformed_dsl():
    assert _codes({"nodes": "x", "edges": []})[0] == ("error", "DSL_INVALID")


def test_phases_stop_at_first_failing_phase():
    raw = chain("{{llm_9.text}}")
    raw["nodes"].append({"id": "x_1", "type": "magic"})
    assert _codes(raw) == [("error", "UNKNOWN_NODE_TYPE")]


def test_reference_errors():
    assert ("error", "REF_UNKNOWN_NODE") in _codes(chain("{{llm_9.text}}"))
    assert ("error", "REF_UNKNOWN_NODE") in _codes(chain("{{end.r}}"))
    assert ("error", "REF_UNKNOWN_FIELD") in _codes(chain("{{start.nope}}"))
    assert ("error", "SECRET_NOT_ALLOWED") in _codes(chain("{{secret.API_KEY}}"))
    assert ("error", "TEMPLATE_SYNTAX") in _codes(chain("{{ start. }}"))
    assert ("error", "TEMPLATE_FORBIDDEN") in _codes(chain("{{ start.topic | safe }}"))


def test_self_reference_requires_default():
    assert ("error", "REF_NOT_GUARANTEED") in _codes(chain("{{llm_1.text}}"))
    assert validate(chain("이전: {{llm_1.text | default('')}}")) == []


def test_object_interpolation_warns():
    assert _codes(chain("입력 전체: {{start}}")) == [("warning", "TYPE_WARNING")]
    assert validate(chain("입력 전체: {{start | tojson}}")) == []


def test_branch_only_reference_requires_default():
    assert ("error", "REF_NOT_GUARANTEED") in _codes(routing("{{llm_a.text}}"))
    assert validate(routing("{{llm_a.text | default('')}}{{llm_b.text | default('')}}")) == []
    assert validate(routing("{{classifier_1.category}}")) == []


def test_merge_guarantees_all_branches():
    assert validate(parallel("{{llm_1.text}} {{llm_2.text}}")) == []
    assert validate(parallel("{{merge_1.branches.llm_1.text}}")) == []
    assert ("error", "REF_UNKNOWN_FIELD") in _codes(parallel("{{merge_1.branches.llm_3.text}}"))


def test_condition_operand_types():
    assert validate(condition("{{llm_1.score}}", "8")) == []
    assert ("error", "TYPE_INCOMPATIBLE") in _codes(condition("{{llm_1.reason}}", "8"))
    assert ("error", "LITERAL_NOT_NUMBER") in _codes(condition("{{llm_1.score}}", "여덟"))
    assert ("warning", "TYPE_WARNING") in _codes(condition("점수 {{llm_1.score}}", "8"))
    assert ("error", "TYPE_INCOMPATIBLE") in _codes(condition("{{llm_1.score}}", "x", op="contains"))
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_validator_refs.py -v`
Expected: FAIL — `ImportError: cannot import name 'analyze' from 'engine.validator'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/validator/refs.py`

```python
"""Phase 3: guaranteed-before sets, output schemas, template references and types."""
from __future__ import annotations

from typing import Any

from engine.dsl.types import compat, kinds_of, resolve_path
from engine.nodes.base import TemplateField
from engine.templates.parser import ParsedTemplate, Ref, TemplateParseError, parse_template
from engine.validator.graph import Graph
from engine.validator.issues import Issue, error, warning


def compute_before(graph: Graph) -> dict[str, frozenset[str]]:
    """Nodes guaranteed to have run before each node (spec 4.4): ∩ over predecessors, ∪ for merge."""
    before: dict[str, frozenset[str]] = {}
    for node_id in graph.order:
        preds = [edge.source for edge in graph.incoming[node_id]]
        if not preds:
            before[node_id] = frozenset()
            continue
        sets = [before[pred] | {pred} for pred in preds]
        if graph.nodes[node_id].spec.type == "merge":
            before[node_id] = frozenset().union(*sets)
        else:
            before[node_id] = frozenset.intersection(*sets)
    return before


def compute_schemas(graph: Graph) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for node_id in graph.order:
        pn = graph.nodes[node_id]
        preds = {edge.source: schemas[edge.source] for edge in graph.incoming[node_id]}
        schemas[node_id] = pn.spec.output_schema(pn.config, preds)
    return schemas


def check_refs(graph: Graph, before: dict[str, frozenset[str]], schemas: dict[str, dict[str, Any]]) -> list[Issue]:
    issues: list[Issue] = []
    for node_id in graph.order:
        pn = graph.nodes[node_id]
        for template_field in pn.spec.template_fields(pn.config):
            issues.extend(_check_field(node_id, template_field, graph, before[node_id], schemas))
    return issues


def _is_number(text: str) -> bool:
    try:
        float(text.strip())
    except ValueError:
        return False
    return True


def _check_field(
    node_id: str,
    template_field: TemplateField,
    graph: Graph,
    guaranteed: frozenset[str],
    schemas: dict[str, dict[str, Any]],
) -> list[Issue]:
    where = {"nodeId": node_id, "field": f"config.{template_field.path}"}
    try:
        parsed = parse_template(template_field.source)
    except TemplateParseError as exc:
        return [error("TEMPLATE_SYNTAX", f"템플릿 문법 오류: {exc}", **where)]
    issues = [error("TEMPLATE_FORBIDDEN", problem, **where) for problem in parsed.problems]
    for ref in parsed.refs:
        issues.extend(_check_ref(ref, parsed, template_field, graph, guaranteed, schemas, where))
    if template_field.target == "number":
        if not parsed.refs and not _is_number(template_field.source):
            issues.append(error("LITERAL_NOT_NUMBER", f"숫자가 필요합니다: {template_field.source!r}", **where))
        elif parsed.refs and parsed.whole_value is None:
            issues.append(warning("TYPE_WARNING", "문자열로 조합된 값은 실행 시 숫자로 변환됩니다", **where))
    return issues


def _check_ref(
    ref: Ref,
    parsed: ParsedTemplate,
    template_field: TemplateField,
    graph: Graph,
    guaranteed: frozenset[str],
    schemas: dict[str, dict[str, Any]],
    where: dict[str, str],
) -> list[Issue]:
    label = ".".join((ref.root, *ref.path))
    if ref.root == "secret":
        return [error("SECRET_NOT_ALLOWED", "시크릿은 HTTP 요청 노드에서만 참조할 수 있습니다", **where)]
    if ref.root not in graph.nodes or ref.root == "end":
        return [error("REF_UNKNOWN_NODE", f"존재하지 않거나 참조할 수 없는 노드입니다: {ref.root}", **where)]
    issues: list[Issue] = []
    if ref.root not in guaranteed and not ref.has_default:
        issues.append(error(
            "REF_NOT_GUARANTEED",
            f"'{ref.root}' 노드가 항상 먼저 실행된다는 보장이 없습니다. | default(...)를 붙이세요: {label}",
            **where,
        ))
    sub_schema = resolve_path(schemas.get(ref.root), ref.path)
    if sub_schema is None:
        issues.append(error("REF_UNKNOWN_FIELD", f"'{ref.root}' 노드 출력에 없는 필드입니다: {label}", **where))
        return issues
    kinds = kinds_of(sub_schema)
    if ref.has_default:
        kinds = (kinds - {"null"}) or {"unknown"}
    shown = ", ".join(sorted(kinds))
    if parsed.whole_value == ref:
        severity = compat(kinds, template_field.target)
        if severity == "error":
            issues.append(error("TYPE_INCOMPATIBLE",
                                f"{label} 값({shown})은 {template_field.target} 필드에 넣을 수 없습니다", **where))
        elif severity == "warning":
            issues.append(warning("TYPE_WARNING",
                                  f"{label} 값({shown})이 {template_field.target} 형식인지 실행 시 확인합니다", **where))
    elif ref.direct and kinds & {"object", "array", "null"}:
        issues.append(warning("TYPE_WARNING",
                              f"{label} 값이 문자열로 바뀌어 들어갑니다 (객체·배열은 JSON, 빈 값은 빈 문자열)", **where))
    return issues
```

**File:** `services/engine/engine/validator/__init__.py`

```python
"""Three-phase DSL validation (spec 6.1): structure → graph → references/types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from engine.dsl.models import WorkflowDSL
from engine.nodes.registry import NodeRegistry, default_registry
from engine.validator.graph import Graph, build_graph, check_graph
from engine.validator.issues import Issue, has_errors
from engine.validator.refs import check_refs, compute_before, compute_schemas
from engine.validator.structure import check_structure, pydantic_issues

__all__ = ["Analysis", "Issue", "analyze", "has_errors", "validate"]


@dataclass
class Analysis:
    issues: list[Issue]
    dsl: WorkflowDSL | None = None
    graph: Graph | None = None  # set once phases 1–2 passed


def analyze(raw: dict[str, Any] | WorkflowDSL, registry: NodeRegistry | None = None) -> Analysis:
    registry = registry or default_registry()
    if isinstance(raw, WorkflowDSL):
        dsl = raw
    else:
        try:
            dsl = WorkflowDSL.model_validate(raw)
        except ValidationError as exc:
            return Analysis(pydantic_issues(exc, "DSL_INVALID", ""))
    issues, parsed = check_structure(dsl, registry)
    if has_errors(issues):
        return Analysis(issues, dsl)
    graph = build_graph(dsl, parsed)
    issues += check_graph(graph)
    if has_errors(issues):
        return Analysis(issues, dsl)
    issues += check_refs(graph, compute_before(graph), compute_schemas(graph))
    return Analysis(issues, dsl, graph)


def validate(raw: dict[str, Any] | WorkflowDSL, registry: NodeRegistry | None = None) -> list[Issue]:
    return analyze(raw, registry).issues
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_validator_refs.py tests/test_validator_graph.py tests/test_validator_structure.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/validator services/engine/tests/test_validator_refs.py
git commit -m "feat(engine): add reference/type validation phase and validate facade"
```

> **Post-review note (Task 14, as implemented):** commits `f2bca1f`, `5c0faa3`, `7d2bc53` and `e158ccb`; the Task 13 follow-up is `e55dca5`.
>
> **Facade (`analyze`).**
> - DSL text with NUL or lone surrogates, or values nested too deeply to serialize, is `DSL_INVALID` ("워크플로 형식 오류").
> - A DSL over 512 KB (UTF-8 JSON, measured without filled-in defaults) is `LIMIT_EXCEEDED`.
> - One `StepBudget` is shared by every phase.
> - `issues.bounded()` de-duplicates issues, puts errors before warnings and caps the list at 100, in every phase. Warnings can no longer crowd out an error and let an invalid workflow compile.
> - `graph` is set once phases 1–2 pass; callers must still check `has_errors`.
>
> **Guaranteed-before sets** are a greatest fixpoint over every reachable predecessor, including back-edges (∩, and ∪ for merge). The plan's forward-only rule wrongly rejected `{{ start.x }}` inside a loop that starts at its condition (start → condition → body → condition). `graph.order` is a forward topological order, not first-execution order.
>
> **Types.**
> - `true`/`false` JSON subschemas no longer crash `kinds_of`/`resolve_path`: `true` means unknown, `false` means the field does not exist.
> - `| default(x)` keeps `null` (Jinja only replaces a missing value) unless it is written `default(x, true)`, and the kind of `x` is added. `Ref` records `default_kind` and `default_replaces_null`. Spec 4.5 is updated.
> - Number literals must be finite.
>
> **Templates.**
> - Field or index access on a filter result (`(x | default({})).y`) is forbidden, because it skipped the reference checks.
> - A `format: "json"` template without `{{`/`{%` is parsed at validation. A quoted substitution warns. `render.QUOTED_SUBSTITUTION` is now public.
>
> **Messages.** Type names are in Korean. Quoted names, references and literals are clipped. A self-reference explains that the first run has no previous result.
>
> Suite: 560.
>
> **Known and accepted:**
> - `피드백: {{ start.s | default('') }}` on a nullable string warns, although `null` and `''` render the same.
> - A slice of a filter result (`(x | trim)[0:3]`) is forbidden along with other field access on filters.
>
> **Carried into Plan 2 (roadmap):** template parsing within the 512 KB limit can take about 3–4 s per validation. Run `validate()` off the event loop and rate-limit saves (already listed). Consider charging parsed template nodes to the step budget.

---

## Task 15: Run state, routing, and runtime ports (recorder, guard, deps)

Spec 5.3, 5.8. Routing decisions and loop counters are written **into state by the branch node itself**; the LangGraph router only reads `state["routes"][node_id]`, so a resumed run re-takes the same path. `Recorder` is the port Plan 2 implements with Postgres (`node_runs` + `run_events`); `RunGuard` is where Plan 2 plugs cancellation and lease checks.

**Files:**

- Create: `services/engine/engine/compiler/__init__.py`
- Create: `services/engine/engine/compiler/state.py`
- Create: `services/engine/engine/compiler/routing.py`
- Create: `services/engine/engine/runtime/__init__.py`
- Create: `services/engine/engine/runtime/recorder.py`
- Create: `services/engine/engine/runtime/guard.py`
- Create: `services/engine/engine/runtime/deps.py`
- Test: `services/engine/tests/test_compiler_routing.py`
- Test: `services/engine/tests/test_runtime_ports.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_compiler_routing.py`

```python
import pytest

from engine.compiler.routing import resolve_route
from engine.compiler.state import initial_state, merge_dicts
from engine.dsl.models import Edge

EXIT = Edge(id="exit", source="c", sourceHandle="true", target="end")
BACK = Edge(id="back", source="c", sourceHandle="false", target="gen", maxIterations=2)
LOOP_EDGES = {"true": [EXIT], "false": [BACK]}


def test_merge_dicts_and_initial_state():
    assert merge_dicts({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert merge_dicts(None, {"b": 2}) == {"b": 2}
    assert initial_state({"x": 1}) == {"inputs": {"x": 1}, "outputs": {}, "routes": {}, "loop_counters": {}, "exec_counts": {}}


def test_forward_handle_returns_all_targets_in_declaration_order():
    edges = {"out": [Edge(id="e1", source="s", target="a"), Edge(id="e2", source="s", target="b")]}
    decision = resolve_route("template", "out", edges, frozenset(), {})
    assert decision.targets == ["a", "b"]
    assert decision.counters == {}


def test_loop_is_taken_and_counted():
    decision = resolve_route("condition", "false", LOOP_EDGES, frozenset({"back"}), {})
    assert (decision.handle, decision.targets, decision.counters, decision.loop_exhausted) == ("false", ["gen"], {"back": 1}, False)


def test_exhausted_condition_loop_takes_the_opposite_handle():
    decision = resolve_route("condition", "false", LOOP_EDGES, frozenset({"back"}), {"back": 2})
    assert (decision.handle, decision.targets, decision.counters, decision.loop_exhausted) == ("true", ["end"], {}, True)


def test_exhausted_classifier_loop_takes_default():
    edges = {
        "retry": [Edge(id="back", source="k", sourceHandle="retry", target="gen", maxIterations=1)],
        "default": [Edge(id="d", source="k", sourceHandle="default", target="end")],
    }
    decision = resolve_route("classifier", "retry", edges, frozenset({"back"}), {"back": 1})
    assert (decision.handle, decision.targets) == ("default", ["end"])


def test_unknown_handle_raises():
    with pytest.raises(ValueError):
        resolve_route("classifier", "nope", LOOP_EDGES, frozenset(), {})
```

**File:** `services/engine/tests/test_runtime_ports.py`

```python
import pytest

from engine.errors import RunCancelled
from engine.nodes.base import Usage
from engine.runtime.guard import FlagGuard, NoopGuard
from engine.runtime.recorder import InMemoryRecorder


async def test_recorder_tracks_attempt_lifecycle():
    recorder = InMemoryRecorder()
    await recorder.node_started("llm_1", 1, 1, {"prompt": "p"})
    await recorder.node_failed("llm_1", 1, 1, {"code": "LLM_UNAVAILABLE", "message": "x"}, will_retry=True)
    await recorder.node_started("llm_1", 1, 2, {"prompt": "p"})
    await recorder.node_succeeded("llm_1", 1, 2, {"text": "t"}, Usage(3, 4), defaulted=False, meta={})

    assert await recorder.attempts_so_far("llm_1", 1) == 2
    assert [r.status for r in recorder.for_node("llm_1")] == ["failed", "succeeded"]
    assert recorder.for_node("llm_1")[1].usage == Usage(3, 4)
    assert [e["type"] for e in recorder.events] == ["node_started", "node_failed", "node_started", "node_finished"]
    assert recorder.events[1]["willRetry"] is True


async def test_recorder_waiting_and_defaulted():
    recorder = InMemoryRecorder()
    await recorder.node_started("human_approval_1", 1, 1, {})
    await recorder.node_waiting("human_approval_1", 1, 1, {"message": "m"})
    assert await recorder.find_waiting("human_approval_1", 1) == 1
    assert await recorder.find_waiting("human_approval_1", 2) is None

    await recorder.node_succeeded("human_approval_1", 1, 1, {"decision": "approve"}, Usage(), defaulted=True, meta={"handle": "approve"})
    assert recorder.for_node("human_approval_1")[0].status == "defaulted"
    assert await recorder.find_waiting("human_approval_1", 1) is None
    assert recorder.events[-1]["handle"] == "approve"


def test_guards():
    NoopGuard().check()
    guard = FlagGuard()
    guard.check()
    guard.cancel()
    with pytest.raises(RunCancelled):
        guard.check()
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_compiler_routing.py tests/test_runtime_ports.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.compiler'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/compiler/__init__.py`

```python

```

**File:** `services/engine/engine/compiler/state.py`

```python
from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    """Reducer: shallow merge, so parallel nodes can write disjoint keys in the same superstep."""
    return {**(left or {}), **(right or {})}


class RunState(TypedDict, total=False):
    inputs: dict[str, Any]
    outputs: Annotated[dict[str, Any], merge_dicts]  # node id -> latest output
    routes: Annotated[dict[str, list[str]], merge_dicts]  # branch node id -> chosen next node ids
    loop_counters: Annotated[dict[str, int], merge_dicts]  # back-edge id -> traversals so far (never reset)
    exec_counts: Annotated[dict[str, int], merge_dicts]  # node id -> completed executions


def initial_state(inputs: dict[str, Any]) -> RunState:
    return {"inputs": inputs, "outputs": {}, "routes": {}, "loop_counters": {}, "exec_counts": {}}
```

**File:** `services/engine/engine/compiler/routing.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field

from engine.dsl.models import Edge

_OPPOSITE = {"true": "false", "false": "true"}


@dataclass(frozen=True)
class RouteDecision:
    handle: str  # handle actually taken (differs from the chosen one when a loop is exhausted)
    targets: list[str]
    counters: dict[str, int] = field(default_factory=dict)  # back-edge traversal counts to write to state
    loop_exhausted: bool = False


def resolve_route(
    node_type: str,
    chosen: str,
    handle_edges: dict[str, list[Edge]],
    back_edge_ids: frozenset[str],
    loop_counters: dict[str, int],
) -> RouteDecision:
    """Spec 5.8: take the chosen handle; a back-edge at its limit falls through to the exit handle."""
    if chosen not in handle_edges:
        raise ValueError(f"알 수 없는 출력입니다: {chosen}")
    edges = handle_edges[chosen]
    loop = next((edge for edge in edges if edge.id in back_edge_ids), None)
    if loop is None:
        return RouteDecision(chosen, [edge.target for edge in edges])
    used = loop_counters.get(loop.id, 0)
    if loop.maxIterations is not None and used >= loop.maxIterations:
        exit_handle = "default" if node_type == "classifier" else _OPPOSITE[chosen]
        return RouteDecision(exit_handle, [edge.target for edge in handle_edges[exit_handle]], loop_exhausted=True)
    return RouteDecision(chosen, [loop.target], {loop.id: used + 1})
```

**File:** `services/engine/engine/runtime/__init__.py`

```python

```

**File:** `services/engine/engine/runtime/recorder.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from engine.nodes.base import Usage


@dataclass
class NodeRunRecord:
    node_id: str
    exec_index: int
    attempt: int
    status: str  # running | succeeded | defaulted | failed | waiting
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    meta: dict[str, Any] = field(default_factory=dict)


class Recorder(Protocol):
    """Observation log of node executions (spec 3.3 node_runs, 7.1 events). Never the source of truth."""

    async def attempts_so_far(self, node_id: str, exec_index: int) -> int: ...

    async def find_waiting(self, node_id: str, exec_index: int) -> int | None:
        """Attempt number of a `waiting` record for this execution, if any."""

    async def node_started(self, node_id: str, exec_index: int, attempt: int, input: dict[str, Any] | None) -> None: ...

    async def node_succeeded(
        self, node_id: str, exec_index: int, attempt: int, output: dict[str, Any], usage: Usage,
        *, defaulted: bool, meta: dict[str, Any],
    ) -> None: ...

    async def node_failed(
        self, node_id: str, exec_index: int, attempt: int, error: dict[str, Any], *, will_retry: bool
    ) -> None: ...

    async def node_waiting(self, node_id: str, exec_index: int, attempt: int, payload: dict[str, Any]) -> None: ...

    async def node_token(self, node_id: str, exec_index: int, text: str) -> None: ...


class InMemoryRecorder:
    def __init__(self) -> None:
        self.records: list[NodeRunRecord] = []
        self.events: list[dict[str, Any]] = []

    def for_node(self, node_id: str) -> list[NodeRunRecord]:
        return [record for record in self.records if record.node_id == node_id]

    def _find(self, node_id: str, exec_index: int, attempt: int) -> NodeRunRecord:
        for record in self.records:
            if (record.node_id, record.exec_index, record.attempt) == (node_id, exec_index, attempt):
                return record
        raise KeyError((node_id, exec_index, attempt))

    def _emit(self, event_type: str, node_id: str, exec_index: int, attempt: int | None, **payload: Any) -> None:
        self.events.append(
            {"type": event_type, "nodeId": node_id, "execIndex": exec_index, "attempt": attempt, **payload}
        )

    async def attempts_so_far(self, node_id: str, exec_index: int) -> int:
        return sum(1 for r in self.records if r.node_id == node_id and r.exec_index == exec_index)

    async def find_waiting(self, node_id: str, exec_index: int) -> int | None:
        for record in self.records:
            if record.node_id == node_id and record.exec_index == exec_index and record.status == "waiting":
                return record.attempt
        return None

    async def node_started(self, node_id: str, exec_index: int, attempt: int, input: dict[str, Any] | None) -> None:
        self.records.append(NodeRunRecord(node_id, exec_index, attempt, "running", input=input))
        self._emit("node_started", node_id, exec_index, attempt)

    async def node_succeeded(
        self, node_id: str, exec_index: int, attempt: int, output: dict[str, Any], usage: Usage,
        *, defaulted: bool, meta: dict[str, Any],
    ) -> None:
        record = self._find(node_id, exec_index, attempt)
        record.status = "defaulted" if defaulted else "succeeded"
        record.output = output
        record.usage = usage
        record.meta = meta
        self._emit("node_finished", node_id, exec_index, attempt, defaulted=defaulted, **meta)

    async def node_failed(
        self, node_id: str, exec_index: int, attempt: int, error: dict[str, Any], *, will_retry: bool
    ) -> None:
        record = self._find(node_id, exec_index, attempt)
        record.status = "failed"
        record.error = error
        self._emit("node_failed", node_id, exec_index, attempt, error=error, willRetry=will_retry)

    async def node_waiting(self, node_id: str, exec_index: int, attempt: int, payload: dict[str, Any]) -> None:
        record = self._find(node_id, exec_index, attempt)
        record.status = "waiting"
        record.meta = {"waiting": payload}
        self._emit("node_waiting", node_id, exec_index, attempt, payload=payload)

    async def node_token(self, node_id: str, exec_index: int, text: str) -> None:
        self._emit("node_token", node_id, exec_index, None, text=text)
```

**File:** `services/engine/engine/runtime/guard.py`

```python
from __future__ import annotations

from typing import Protocol

from engine.errors import RunCancelled


class RunGuard(Protocol):
    def check(self) -> None:
        """Raise RunCancelled when the run must stop (cancel requested, lease lost)."""


class NoopGuard:
    def check(self) -> None:
        return None


class FlagGuard:
    """In-process guard; Plan 2 sets it from the Redis control channel and the lease heartbeat."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def check(self) -> None:
        if self.cancelled:
            raise RunCancelled()
```

**File:** `services/engine/engine/runtime/deps.py`

```python
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from engine.llm.base import LLMClient
from engine.runtime.guard import NoopGuard, RunGuard
from engine.runtime.recorder import Recorder


@dataclass
class RunDeps:
    """Per-run dependencies, passed to LangGraph as `context=` (compiled graphs are shared across runs)."""

    run_id: str
    llm: LLMClient
    recorder: Recorder
    guard: RunGuard = field(default_factory=NoopGuard)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_compiler_routing.py tests/test_runtime_ports.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/compiler services/engine/engine/runtime services/engine/tests/test_compiler_routing.py services/engine/tests/test_runtime_ports.py
git commit -m "feat(engine): add run state, routing and runtime ports"
```

> **Post-review note (Task 15, as implemented):** commits `4da65b4`, `f53d266` and `8c4d800`.
>
> **Resume detection.** `Recorder.find_waiting` returns the latest attempt of the execution that ever recorded `node_waiting`, even after that attempt finished. The plan's version returned `None` once the row closed, so a replay after a crash (resume succeeded, checkpoint not yet saved) opened attempt 2 and emitted a bogus `node_waiting`. The plan test now expects `1`. A replay therefore reuses the waited attempt and closes it again, which duplicates `node_finished`. Spec 5.3 describes a new row per replay; Task 16 decides.
>
> **Lost lease.**
> - `errors.LeaseLost` subclasses `RunCancelled`, and `FlagGuard.lose_lease()` raises it before `cancelled` is checked.
> - `DuplicateAttempt`, raised when an attempt already exists (the Postgres unique key), is a `LeaseLost`, so the wrapper's `except RunCancelled: raise` stops instead of treating it as a node error.
>
> **Strict test double.** `InMemoryRecorder` rejects duplicate attempts and stores inputs, outputs, errors, meta and event payloads as strict JSON copies (`allow_nan=False`). A rejected write changes nothing. Events stay flat (spec 7.1 nests `payload`; the Plan 2 recorder nests them). A recorder is bound to one run, and `RunDeps` is never shared between runs.
>
> Suite: 577. The plan's Task 16–18 code and tests pass on top, except `test_output_too_large` (see below).
>
> **Carried into Task 16:**
> - `test_output_too_large` fails because the template renderer caps output first. Build the oversized output another way.
> - `_check_size` uses `json.dumps(default=str)`, so a non-JSON output passes it and the recorder then raises outside the wrapper's `try`. Use strict `json.dumps(..., allow_nan=False)` so it becomes a node error.
> - Pass `resuming` for the whole node call. Otherwise a retry after a resumed interrupt records `node_waiting` again.
> - Decide whether a replay of a closed waited attempt opens a new row (spec 5.3) or reuses it.
>
> **Carried into Task 17:** the runner maps `LeaseLost` to "stop and write nothing", not to `cancelled`.
>
> **Carried into Plan 2 (roadmap):**
> - Closing an attempt must work on a row that is already closed (replays).
> - Run-level cancel closes `running` rows as `cancelled` (spec 5.9).
> - Token usage of failed attempts (already listed).

---

## Task 16: Node wrapper — render, retry, timeout, onError, record, route

Spec 5.3–5.6. One wrapper for every node type. Order inside an attempt: guard check → exec_index → render templates → `node_started` → `execute` under `asyncio.timeout` → output size check → routing decision → `node_succeeded` + state write. `GraphInterrupt` and `RunCancelled` pass straight through (they are control flow, and `GraphInterrupt` subclasses `Exception`, so it must be caught first).

**Files:**

- Create: `services/engine/engine/compiler/wrapper.py`
- Test: `services/engine/tests/test_compiler_wrapper.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_compiler_wrapper.py`

```python
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from engine.compiler.state import RunState, initial_state
from engine.compiler.wrapper import NodePlan, backoff_delay, make_node_fn
from engine.dsl.models import Edge, Node, Policy, RetrySpec
from engine.errors import ErrorCode, NodeError, NodeFailedError, RunCancelled
from engine.llm.scripted import ScriptedLLM
from engine.nodes.condition import ConditionNode
from engine.nodes.llm import LLMNode
from engine.nodes.template import TemplateNode
from engine.runtime.deps import RunDeps
from engine.runtime.guard import FlagGuard
from engine.runtime.recorder import InMemoryRecorder

LLM_CONFIG = {"model": "m", "prompt": "{{start.topic}}"}


def _plan(spec, raw_config, *, policy=None, handle_edges=None, back=frozenset(), node_id="n") -> NodePlan:
    config = spec.parse_config(raw_config)
    return NodePlan(
        node=Node(id=node_id, type=spec.type, config=raw_config),
        spec=spec,
        config=config,
        policy=policy,
        pred_ids=(),
        handle_edges=handle_edges or {handle: [] for handle in spec.handles(config)},
        back_edge_ids=back,
    )


def _deps(llm=None, guard=None) -> tuple[RunDeps, InMemoryRecorder, list[float]]:
    recorder = InMemoryRecorder()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    deps = RunDeps(run_id="r1", llm=llm or ScriptedLLM([]), recorder=recorder, sleep=fake_sleep)
    if guard is not None:
        deps.guard = guard
    return deps, recorder, sleeps


async def _run(plan: NodePlan, deps: RunDeps, *, start=None, loop_counters=None) -> dict:
    graph = StateGraph(RunState, context_schema=RunDeps)
    graph.add_node(plan.node.id, make_node_fn(plan))
    graph.add_edge(START, plan.node.id)
    graph.add_edge(plan.node.id, END)
    app = graph.compile(checkpointer=InMemorySaver())
    state = initial_state({})
    state["outputs"] = {"start": start or {"topic": "AI"}}
    state["loop_counters"] = loop_counters or {}
    return await app.ainvoke(state, {"configurable": {"thread_id": "t"}}, context=deps)


def test_backoff_delay():
    assert backoff_delay(RetrySpec(backoff="fixed", initialDelaySec=3), 4) == 3
    assert backoff_delay(RetrySpec(initialDelaySec=2), 1) == 2
    assert backoff_delay(RetrySpec(initialDelaySec=2), 3) == 8
    assert backoff_delay(RetrySpec(initialDelaySec=30), 5) == 60


async def test_success_writes_state_and_records_rendered_input():
    deps, recorder, _ = _deps(ScriptedLLM(["답"]))
    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)
    assert result["outputs"]["n"] == {"text": "답"}
    assert result["exec_counts"] == {"n": 1}
    assert recorder.records[0].status == "succeeded"
    assert recorder.records[0].input == {"prompt": "AI"}


async def test_retryable_error_is_retried_with_backoff():
    llm = ScriptedLLM([NodeError(ErrorCode.LLM_UNAVAILABLE, "down", retryable=True), "답"])
    deps, recorder, sleeps = _deps(llm)
    policy = Policy(retry=RetrySpec(maxAttempts=3, initialDelaySec=1.5))

    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=policy), deps)

    assert result["outputs"]["n"] == {"text": "답"}
    assert [(r.attempt, r.status) for r in recorder.records] == [(1, "failed"), (2, "succeeded")]
    assert sleeps == [1.5]
    assert [e["willRetry"] for e in recorder.events if e["type"] == "node_failed"] == [True]


async def test_exhausted_retries_fail_the_node():
    down = NodeError(ErrorCode.LLM_UNAVAILABLE, "down", retryable=True)
    deps, recorder, sleeps = _deps(ScriptedLLM([down, down, down]))

    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)

    assert exc.value.error.code == ErrorCode.LLM_UNAVAILABLE
    assert [r.status for r in recorder.records] == ["failed", "failed", "failed"]
    assert sleeps == [2.0, 4.0]


async def test_non_retryable_error_is_not_retried():
    deps, recorder, _ = _deps(ScriptedLLM([ValueError("bad")]))
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)
    assert exc.value.error.code == ErrorCode.NODE_FAILED
    assert len(recorder.records) == 1


async def test_on_error_default_uses_default_output():
    deps, recorder, _ = _deps(ScriptedLLM([ValueError("bad")]))
    policy = Policy(retry=RetrySpec(maxAttempts=1), onError="default", defaultOutput={"text": "기본"})

    result = await _run(_plan(LLMNode(), LLM_CONFIG, policy=policy), deps)

    assert result["outputs"]["n"] == {"text": "기본"}
    assert recorder.records[-1].status == "defaulted"


async def test_timeout():
    deps, _, _ = _deps(ScriptedLLM(["늦음"], delay=5))
    policy = Policy(timeoutSec=1, retry=RetrySpec(maxAttempts=1))
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=policy), deps)
    assert exc.value.error.code == ErrorCode.NODE_TIMEOUT


async def test_template_error_is_recorded_as_failed_attempt():
    deps, recorder, _ = _deps()
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(TemplateNode(), {"template": "{{start.nope}}"}), deps)
    assert exc.value.error.code == ErrorCode.TEMPLATE_ERROR
    assert (recorder.records[0].status, recorder.records[0].input) == ("failed", None)


async def test_output_too_large():
    deps, _, _ = _deps()
    with pytest.raises(NodeFailedError) as exc:
        await _run(_plan(TemplateNode(), {"template": "{{start.big}}"}), deps, start={"big": "x" * 1_100_000})
    assert exc.value.error.code == ErrorCode.OUTPUT_TOO_LARGE


async def test_branch_node_writes_route_and_loop_counter():
    edges = {
        "true": [Edge(id="exit", source="n", sourceHandle="true", target="end")],
        "false": [Edge(id="back", source="n", sourceHandle="false", target="gen", maxIterations=2)],
    }
    config = {"conditions": [{"left": "{{start.n}}", "op": ">=", "right": "3"}]}
    plan = _plan(ConditionNode(), config, handle_edges=edges, back=frozenset({"back"}))

    deps, recorder, _ = _deps()
    result = await _run(plan, deps, start={"n": 1})
    assert result["routes"] == {"n": ["gen"]}
    assert result["loop_counters"] == {"back": 1}
    assert recorder.records[0].meta == {"handle": "false", "loopExhausted": False}

    deps, recorder, _ = _deps()
    result = await _run(plan, deps, start={"n": 1}, loop_counters={"back": 2})
    assert result["routes"] == {"n": ["end"]}
    assert recorder.records[0].meta == {"handle": "true", "loopExhausted": True}


async def test_cancelled_guard_stops_before_execution():
    guard = FlagGuard()
    guard.cancel()
    deps, recorder, _ = _deps(ScriptedLLM(["답"]), guard=guard)
    with pytest.raises(RunCancelled):
        await _run(_plan(LLMNode(), LLM_CONFIG, policy=LLMNode.default_policy), deps)
    assert recorder.records == []
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_compiler_wrapper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.compiler.wrapper'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/compiler/wrapper.py`

```python
"""The single LangGraph node function used for every DSL node (spec 5.3 node wrapper)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel

from engine.compiler.routing import RouteDecision, resolve_route
from engine.compiler.state import RunState
from engine.dsl.models import Edge, Node, Policy, RetrySpec
from engine.errors import ErrorCode, NodeError, NodeFailedError, RunCancelled
from engine.nodes.base import NodeContext, NodeResult, NodeSpec, TemplateField
from engine.runtime.deps import RunDeps
from engine.templates.render import TemplateRenderError, render_template

MAX_OUTPUT_BYTES = 1_000_000


@dataclass(frozen=True)
class NodePlan:
    node: Node
    spec: NodeSpec
    config: BaseModel
    policy: Policy | None
    pred_ids: tuple[str, ...]  # forward predecessors in edge declaration order
    handle_edges: dict[str, list[Edge]]
    back_edge_ids: frozenset[str]


def backoff_delay(retry: RetrySpec, failed_tries: int) -> float:
    if retry.backoff == "fixed":
        return retry.initialDelaySec
    return min(retry.initialDelaySec * 2 ** (failed_tries - 1), 60.0)


def _render(fields: list[TemplateField], outputs: dict[str, Any]) -> dict[str, Any]:
    return {f.path: render_template(f.source, outputs, f.target) for f in fields}


def _check_size(output: dict[str, Any]) -> None:
    size = len(json.dumps(output, ensure_ascii=False, default=str).encode("utf-8"))
    if size > MAX_OUTPUT_BYTES:
        raise NodeError(ErrorCode.OUTPUT_TOO_LARGE, f"노드 출력이 너무 큽니다 ({size} bytes)", retryable=False)


def _as_node_error(exc: Exception, timeout: float | None) -> NodeError:
    if isinstance(exc, NodeError):
        return exc
    if isinstance(exc, TimeoutError):
        limit = f"{timeout:g}초 " if timeout else ""
        return NodeError(ErrorCode.NODE_TIMEOUT, f"제한 시간 {limit}초과", retryable=True)
    if isinstance(exc, TemplateRenderError):
        return NodeError(exc.code, str(exc), retryable=False)
    return NodeError(ErrorCode.NODE_FAILED, f"{type(exc).__name__}: {exc}", retryable=False)


def _decide(plan: NodePlan, output: dict[str, Any], loop_counters: dict[str, int]) -> RouteDecision | None:
    if not plan.spec.is_branch:
        return None
    try:
        chosen = plan.spec.route(plan.config, output)
        return resolve_route(plan.spec.type, chosen, plan.handle_edges, plan.back_edge_ids, loop_counters)
    except (KeyError, ValueError) as exc:
        raise NodeError(ErrorCode.TYPE_MISMATCH, f"분기를 결정할 수 없습니다: {exc}", retryable=False) from exc


def _fallback_output(plan: NodePlan) -> dict[str, Any] | None:
    if plan.policy is None or plan.policy.onError != "default":
        return None
    if plan.policy.defaultOutput is not None:
        return plan.policy.defaultOutput
    return plan.spec.fallback_output(plan.config)


def _context(
    plan: NodePlan, deps: RunDeps, state: RunState, exec_index: int, attempt: int, resuming: bool
) -> NodeContext:
    node_id = plan.node.id

    async def on_token(text: str) -> None:
        await deps.recorder.node_token(node_id, exec_index, text)

    async def wait_for_human(payload: dict[str, Any]) -> Any:
        # On resume LangGraph re-runs the node and interrupt() returns the resume value instead of raising.
        if not resuming:
            await deps.recorder.node_waiting(node_id, exec_index, attempt, payload)
        return interrupt(payload)

    return NodeContext(
        run_id=deps.run_id,
        node_id=node_id,
        exec_index=exec_index,
        attempt=attempt,
        inputs=state.get("inputs", {}),
        outputs=state.get("outputs", {}),
        pred_ids=list(plan.pred_ids),
        llm=deps.llm,
        on_token=on_token,
        interrupt=wait_for_human,
    )


async def _succeed(
    plan: NodePlan,
    deps: RunDeps,
    exec_index: int,
    attempt: int,
    result: NodeResult,
    decision: RouteDecision | None,
    *,
    defaulted: bool,
) -> dict[str, Any]:
    node_id = plan.node.id
    write: dict[str, Any] = {"outputs": {node_id: result.output}, "exec_counts": {node_id: exec_index}}
    meta: dict[str, Any] = {}
    if decision is not None:
        write["routes"] = {node_id: decision.targets}
        if decision.counters:
            write["loop_counters"] = decision.counters
        meta = {"handle": decision.handle, "loopExhausted": decision.loop_exhausted}
    await deps.recorder.node_succeeded(
        node_id, exec_index, attempt, result.output, result.usage, defaulted=defaulted, meta=meta
    )
    return write


def make_node_fn(plan: NodePlan):
    fields = plan.spec.template_fields(plan.config)
    node_id = plan.node.id
    max_attempts = plan.policy.retry.maxAttempts if plan.policy else 1
    timeout = plan.policy.timeoutSec if plan.policy else None

    async def node_fn(state: RunState, runtime: Runtime[RunDeps]) -> dict[str, Any]:
        deps = runtime.context
        recorder = deps.recorder
        deps.guard.check()
        exec_index = state.get("exec_counts", {}).get(node_id, 0) + 1
        outputs = state.get("outputs", {})
        loop_counters = state.get("loop_counters", {})
        resuming = await recorder.find_waiting(node_id, exec_index)
        attempt = resuming if resuming is not None else await recorder.attempts_so_far(node_id, exec_index) + 1
        error: NodeError | None = None

        for tries in range(1, max_attempts + 1):
            started = resuming is not None
            try:
                rendered = _render(fields, outputs)
                if not started:
                    await recorder.node_started(node_id, exec_index, attempt, rendered)
                    started = True
                ctx = _context(plan, deps, state, exec_index, attempt, resuming is not None)
                async with asyncio.timeout(timeout):
                    result = await plan.spec.execute(ctx, plan.config, rendered)
                _check_size(result.output)
                decision = _decide(plan, result.output, loop_counters)
            except (GraphInterrupt, RunCancelled):
                raise
            except Exception as exc:  # noqa: BLE001 - every other failure is a node error
                error = _as_node_error(exc, timeout)
            else:
                return await _succeed(plan, deps, exec_index, attempt, result, decision, defaulted=False)

            if not started:
                await recorder.node_started(node_id, exec_index, attempt, None)
            will_retry = error.retryable and tries < max_attempts
            await recorder.node_failed(node_id, exec_index, attempt, error.to_dict(), will_retry=will_retry)
            if not will_retry:
                break
            await deps.sleep(backoff_delay(plan.policy.retry, tries))
            deps.guard.check()
            attempt += 1
            resuming = None

        fallback = _fallback_output(plan)
        if fallback is None:
            raise NodeFailedError(node_id, error)
        try:
            decision = _decide(plan, fallback, loop_counters)
        except NodeError as exc:
            raise NodeFailedError(node_id, exc) from exc
        return await _succeed(plan, deps, exec_index, attempt, NodeResult(fallback), decision, defaulted=True)

    return node_fn
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_compiler_wrapper.py -v`
Expected: all PASS (the timeout test takes ~1 s)

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/compiler/wrapper.py services/engine/tests/test_compiler_wrapper.py
git commit -m "feat(engine): add generic node wrapper with retry, timeout, onError and routing"
```

> **Post-review note (Task 16, as implemented):** commits `8f48078`, `17f565b` and `043ed02`.
>
> **Stored values.**
> - Rendered values with NUL or lone surrogates are `TEMPLATE_ERROR`.
> - `_check_output` replaces `_check_size`: strict JSON (`allow_nan=False`) plus `check_text` (`NODE_FAILED`), and a 1 MB cap (`OUTPUT_TOO_LARGE`).
> - `defaultOutput` is deep-copied per use and checked the same way before routing.
>
> **Resume and replay.**
> - `resumed` (the execution already recorded `node_waiting`) holds for the whole node call, and a replay reuses the waited attempt. Spec 5.3's "new row per replay" is amended in practice: Plan 2 must accept closing an already-closed attempt and tolerate a duplicate `node_finished`.
> - Retries take `attempts_so_far() + 1`.
> - A node that waits must not retry, because a second `interrupt()` in one call waits again without a record. `human_approval` accepts no policy.
>
> **Infrastructure faults are not node errors.**
> - Recorder reads and writes go through `_recorded()`: `RunCancelled` (including `LeaseLost` and `DuplicateAttempt`) passes through, and anything else becomes the new `errors.EngineFault`, which escapes the run.
> - `node_started` sits outside the attempt `try`.
> - `on_token` is best effort, and a failure is logged once per attempt.
> - The re-raise set is `(GraphBubbleUp, RunCancelled, EngineFault)`.
> - Routing bugs are `NODE_FAILED`, not `TYPE_MISMATCH`, and exception text is clipped.
>
> **Tests.** `test_output_too_large` merges two 600 KB branch outputs, because the renderer caps a single field first.
>
> Suite: 602.
>
> **Carried into Plan 2 (roadmap):**
> - The worker must release a run whose task raised `EngineFault` or any unexpected exception: stop the heartbeat, or do a fenced requeue with `recovery_count + 1`. Otherwise the lease never expires.
> - Close orphaned `running` attempt rows at run level, e.g. a cancelled parallel sibling or a crash mid-write.
> - Token events carry no attempt, so the UI clears streamed text on each `node_started`.

---

## Task 17: Compile DSL to LangGraph and run it

Spec 6.2, 6.3. Compilation mapping: every DSL node → `add_node(id, wrapper)`; `START → start`, `end → END`; plain edges → `add_edge`; branch nodes → `add_conditional_edges(id, state["routes"][id])`; merge → `add_edge([forward preds...], merge)`. `recursion_limit = nodes × (1 + Σ maxIterations) + 10` (a conservative upper bound; hitting it is an engine bug → `ENGINE_RECURSION_LIMIT`).

`execute_run` picks its input from the checkpoint: fresh thread → initial state; `resume` given → `Command(resume=...)`; otherwise `None` (continue after crash recovery or manual retry). After the invocation the checkpoint snapshot decides the outcome: pending interrupt → `waiting`; no next nodes → `succeeded` with the `end` node output.

**Files:**

- Create: `services/engine/engine/compiler/build.py`
- Create: `services/engine/engine/runtime/runner.py`
- Test: `services/engine/tests/test_compiler_build.py`

- [ ]  **Step 1: Write the failing tests**

**File:** `services/engine/tests/test_compiler_build.py`

```python
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from engine.compiler.build import WorkflowInvalid, compile_workflow
from engine.dsl.models import WorkflowDSL, dsl_hash
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import execute_run

DSL = {
    "nodes": [
        {"id": "start", "type": "start",
         "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}},
        {"id": "llm_1", "type": "llm", "config": {"model": "m", "prompt": "{{start.topic}}"}},
        {"id": "end", "type": "end", "config": {"outputs": {"result": "{{llm_1.text}}"}}},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "llm_1"}, {"id": "e2", "source": "llm_1", "target": "end"}],
}


def test_invalid_workflow_is_rejected():
    broken = {**DSL, "edges": DSL["edges"][:1]}
    with pytest.raises(WorkflowInvalid) as exc:
        compile_workflow(broken, checkpointer=InMemorySaver())
    assert {issue.code for issue in exc.value.issues} >= {"HANDLE_NOT_CONNECTED"}


def test_compiled_metadata():
    compiled = compile_workflow(DSL, checkpointer=InMemorySaver())
    assert compiled.dsl_hash == dsl_hash(WorkflowDSL.model_validate(DSL))
    assert compiled.recursion_limit == 3 * (1 + 0) + 10


async def test_runs_to_completion_and_is_idempotent_when_called_again():
    compiled = compile_workflow(DSL, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-1", llm=ScriptedLLM(["결과"]), recorder=InMemoryRecorder())

    outcome = await execute_run(compiled, deps=deps, inputs={"topic": "AI"})
    again = await execute_run(compiled, deps=deps)

    assert (outcome.status, outcome.outputs) == ("succeeded", {"result": "결과"})
    assert (again.status, again.outputs) == ("succeeded", {"result": "결과"})
    assert len(deps.recorder.for_node("llm_1")) == 1


async def test_start_input_validation_fails_the_run():
    compiled = compile_workflow(DSL, checkpointer=InMemorySaver())
    deps = RunDeps(run_id="run-2", llm=ScriptedLLM([]), recorder=InMemoryRecorder())
    outcome = await execute_run(compiled, deps=deps, inputs={})
    assert outcome.status == "failed"
    assert (outcome.error["code"], outcome.error["nodeId"]) == ("TYPE_MISMATCH", "start")
```

- [ ]  **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_compiler_build.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.compiler.build'`

- [ ]  **Step 3: Implement**

**File:** `services/engine/engine/compiler/build.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from engine.compiler.state import RunState
from engine.compiler.wrapper import NodePlan, make_node_fn
from engine.dsl.models import WorkflowDSL, dsl_hash
from engine.nodes.registry import NodeRegistry
from engine.runtime.deps import RunDeps
from engine.validator import analyze
from engine.validator.issues import Issue, has_errors


class WorkflowInvalid(Exception):
    def __init__(self, issues: list[Issue]) -> None:
        errors = sum(1 for issue in issues if issue.severity == "error")
        super().__init__(f"workflow has {errors} validation error(s)")
        self.issues = issues


@dataclass(frozen=True)
class CompiledWorkflow:
    graph: CompiledStateGraph
    dsl: WorkflowDSL
    dsl_hash: str
    recursion_limit: int


def _router(node_id: str):
    def route(state: RunState) -> list[str]:
        return state["routes"][node_id]

    return route


def compile_workflow(
    raw: dict[str, Any] | WorkflowDSL,
    *,
    checkpointer: BaseCheckpointSaver,
    registry: NodeRegistry | None = None,
) -> CompiledWorkflow:
    analysis = analyze(raw, registry)
    if has_errors(analysis.issues) or analysis.graph is None or analysis.dsl is None:
        raise WorkflowInvalid(analysis.issues)
    graph = analysis.graph
    back_edge_ids = frozenset(graph.back_edges)

    builder = StateGraph(RunState, context_schema=RunDeps)
    for node_id, pn in graph.nodes.items():
        plan = NodePlan(
            node=pn.node,
            spec=pn.spec,
            config=pn.config,
            policy=pn.policy,
            pred_ids=tuple(edge.source for edge in graph.incoming[node_id]),
            handle_edges=graph.out[node_id],
            back_edge_ids=back_edge_ids,
        )
        builder.add_node(node_id, make_node_fn(plan))
    builder.add_edge(START, "start")
    builder.add_edge("end", END)
    for node_id, pn in graph.nodes.items():
        if pn.spec.type == "merge":
            builder.add_edge([edge.source for edge in graph.incoming[node_id]], node_id)
        elif pn.spec.is_branch:
            targets = sorted({edge.target for edges in graph.out[node_id].values() for edge in edges})
            builder.add_conditional_edges(node_id, _router(node_id), targets)
    for edge in graph.edges:
        if graph.nodes[edge.source].spec.is_branch or graph.nodes[edge.target].spec.type == "merge":
            continue
        builder.add_edge(edge.source, edge.target)

    iterations = sum(edge.maxIterations or 0 for edge in graph.back_edges.values())
    return CompiledWorkflow(
        graph=builder.compile(checkpointer=checkpointer),
        dsl=analysis.dsl,
        dsl_hash=dsl_hash(analysis.dsl),
        recursion_limit=len(graph.nodes) * (1 + iterations) + 10,
    )
```

**File:** `services/engine/engine/runtime/runner.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from engine.compiler.build import CompiledWorkflow
from engine.compiler.state import initial_state
from engine.errors import ErrorCode, NodeFailedError, RunCancelled
from engine.runtime.deps import RunDeps


@dataclass
class RunOutcome:
    status: Literal["succeeded", "waiting", "failed", "cancelled"]
    outputs: dict[str, Any] | None = None
    waiting: dict[str, Any] | None = None  # interrupt payload: nodeId, execIndex, message, review, allowEdit
    error: dict[str, Any] | None = None  # {"code", "message", "nodeId"?}


async def execute_run(
    compiled: CompiledWorkflow,
    *,
    deps: RunDeps,
    inputs: dict[str, Any] | None = None,
    resume: dict[str, Any] | None = None,
) -> RunOutcome:
    """Start, resume, or continue (crash recovery / manual retry) the run whose thread id is deps.run_id."""
    config = {"configurable": {"thread_id": deps.run_id}, "recursion_limit": compiled.recursion_limit}
    if resume is not None:
        graph_input: Any = Command(resume=resume)
    elif not (await compiled.graph.aget_state(config)).values:
        graph_input = initial_state(inputs or {})
    else:
        graph_input = None
    try:
        await compiled.graph.ainvoke(graph_input, config, context=deps, durability="sync")
    except NodeFailedError as exc:
        return RunOutcome("failed", error={**exc.error.to_dict(), "nodeId": exc.node_id})
    except GraphRecursionError:
        return RunOutcome(
            "failed",
            error={"code": str(ErrorCode.ENGINE_RECURSION_LIMIT), "message": "실행 단계 한도를 초과했습니다"},
        )
    except RunCancelled:
        return RunOutcome("cancelled")

    snapshot = await compiled.graph.aget_state(config)
    interrupts = [item for task in snapshot.tasks for item in task.interrupts]
    if interrupts:
        return RunOutcome("waiting", waiting=interrupts[0].value)
    if snapshot.next:
        return RunOutcome(
            "failed",
            error={"code": str(ErrorCode.NODE_FAILED), "message": f"실행이 끝나지 않았습니다: {', '.join(snapshot.next)}"},
        )
    return RunOutcome("succeeded", outputs=snapshot.values.get("outputs", {}).get("end", {}))
```

- [ ]  **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_compiler_build.py -v`
Expected: all PASS

- [ ]  **Step 5: Commit**

```bash
git add services/engine/engine/compiler/build.py services/engine/engine/runtime/runner.py services/engine/tests/test_compiler_build.py
git commit -m "feat(engine): compile validated DSL to LangGraph and execute runs"
```

> **Post-review note (Task 17, as implemented):** commits `c9eb70f` and `842ad66`.
>
> **Resume.**
> - `execute_run(resume=…)` requires the approval's target, `nodeId` (str) and `execIndex` (int), in the answer.
> - When that approval is the pending interrupt, the answer is checked with `human_approval.resume_output` before `Command(resume=…)`. A bad answer raises the new `ResumeRejected` and nothing reaches the graph. LangGraph keeps the first resume value of an interrupt, so a bad answer would otherwise fail the run forever.
> - When the named approval is no longer waiting, the answer was already used before a crash, so the run just continues from its checkpoint. A replayed answer never answers a later approval, e.g. the next pass of an approval inside a loop.
> - A resume for a run with no checkpoint is `ResumeRejected`.
>
> **Escaping exceptions** (documented in the docstring):
> - `LeaseLost` is re-raised; plain `RunCancelled` still returns `cancelled`.
> - `ResumeRejected`, `EngineFault` and unexpected engine bugs also escape, and the worker handles them.
> - `inputs` are ignored for a run that already has a checkpoint.
>
> **Caching.** `CompiledWorkflow` is bound to one checkpointer and one registry, so a cache keyed only by `dsl_hash` needs a single registry and checkpointer per process.
>
> **Confirmed by probes.**
> - Nested loops at maxIterations 20 run 127 node executions under a `recursion_limit` of 256.
> - A classifier self-loop exits through `default`.
> - A loop exit that fans out merges once.
> - A crash before the merge recovers without extra LLM calls.
>
> **Known.** An exhausted loop keeps the branch node's chosen output (e.g. `category: "again"`); `loopExhausted` appears only in the event. Document this for editor users (Plan 3).
>
> Suite: 611.
>
> **Carried into Task 18:** HITL golden resumes pass the target; add loop-exit fan-out and self-loop flows.
>
> **Carried into Plan 2 (roadmap):**
> - Store `nodeId`/`execIndex` in `resume_payload`, and clear it whenever the resumed invocation returns, whatever the outcome. A leftover payload on a failed run would act like a retry.
> - On `ResumeRejected`, keep the run `waiting`.
> - The API still returns `409 RESUME_TARGET_MISMATCH` itself, because the engine silently continues on a mismatched target.

---

## Task 18: Golden pattern flows end-to-end

Spec 2.3 success criteria 1, 3, 4 and 12 (compiler golden tests). One DSL fixture per pattern; Plan 3 reuses them as editor E2E fixtures.

**Files:**

- Create: `services/engine/tests/golden/chaining.json`
- Create: `services/engine/tests/golden/routing.json`
- Create: `services/engine/tests/golden/parallel.json`
- Create: `services/engine/tests/golden/evaluator_loop.json`
- Create: `services/engine/tests/golden/hitl.json`
- Test: `services/engine/tests/test_golden_patterns.py`
- Test: `services/engine/tests/test_runner_recovery.py`

- [ ]  **Step 1: Write the golden fixtures**

**File:** `services/engine/tests/golden/chaining.json`

```json
{
  "version": "1",
  "nodes": [
    {"id": "start", "type": "start", "label": "시작",
     "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}},
    {"id": "llm_1", "type": "llm", "label": "개요",
     "config": {"model": "qwen2.5:14b", "prompt": "{{start.topic}} 글의 개요를 써줘"}},
    {"id": "llm_2", "type": "llm", "label": "본문",
     "config": {"model": "qwen2.5:14b", "prompt": "다음 개요로 본문을 써줘:\n{{llm_1.text}}"}},
    {"id": "end", "type": "end", "label": "끝", "config": {"outputs": {"result": "{{llm_2.text}}"}}}
  ],
  "edges": [
    {"id": "e1", "source": "start", "target": "llm_1"},
    {"id": "e2", "source": "llm_1", "target": "llm_2"},
    {"id": "e3", "source": "llm_2", "target": "end"}
  ]
}
```

**File:** `services/engine/tests/golden/routing.json`

```json
{
  "version": "1",
  "nodes": [
    {"id": "start", "type": "start", "label": "시작",
     "config": {"inputs": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}},
    {"id": "classifier_1", "type": "classifier", "label": "문의 분류",
     "config": {"model": "qwen2.5:14b", "input": "{{start.question}}",
                "categories": [{"id": "billing", "description": "결제·환불 문의"}, {"id": "tech", "description": "기술 지원"}]}},
    {"id": "llm_billing", "type": "llm", "label": "결제 답변",
     "config": {"model": "qwen2.5:14b", "prompt": "결제 문의에 답해줘: {{start.question}}"}},
    {"id": "llm_tech", "type": "llm", "label": "기술 답변",
     "config": {"model": "qwen2.5:14b", "prompt": "기술 문의에 답해줘: {{start.question}}"}},
    {"id": "template_1", "type": "template", "label": "안내", "config": {"template": "담당 부서를 찾지 못했습니다."}},
    {"id": "end", "type": "end", "label": "끝",
     "config": {"outputs": {
       "category": "{{classifier_1.category}}",
       "answer": "{{llm_billing.text | default('')}}{{llm_tech.text | default('')}}{{template_1.text | default('')}}"}}}
  ],
  "edges": [
    {"id": "e1", "source": "start", "target": "classifier_1"},
    {"id": "e2", "source": "classifier_1", "sourceHandle": "billing", "target": "llm_billing"},
    {"id": "e3", "source": "classifier_1", "sourceHandle": "tech", "target": "llm_tech"},
    {"id": "e4", "source": "classifier_1", "sourceHandle": "default", "target": "template_1"},
    {"id": "e5", "source": "llm_billing", "target": "end"},
    {"id": "e6", "source": "llm_tech", "target": "end"},
    {"id": "e7", "source": "template_1", "target": "end"}
  ]
}
```

**File:** `services/engine/tests/golden/parallel.json`

```json
{
  "version": "1",
  "nodes": [
    {"id": "start", "type": "start", "label": "시작",
     "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}},
    {"id": "llm_pros", "type": "llm", "label": "장점", "config": {"model": "qwen2.5:14b", "prompt": "{{start.topic}}의 장점"}},
    {"id": "llm_cons", "type": "llm", "label": "단점", "config": {"model": "qwen2.5:14b", "prompt": "{{start.topic}}의 단점"}},
    {"id": "merge_1", "type": "merge", "label": "합치기", "config": {"mode": "object"}},
    {"id": "llm_sum", "type": "llm", "label": "종합",
     "config": {"model": "qwen2.5:14b",
                "prompt": "종합해줘\n장점: {{merge_1.branches.llm_pros.text}}\n단점: {{merge_1.branches.llm_cons.text}}"}},
    {"id": "end", "type": "end", "label": "끝",
     "config": {"outputs": {"summary": "{{llm_sum.text}}", "branchOrder": "{{merge_1.branches | tojson}}"}}}
  ],
  "edges": [
    {"id": "e1", "source": "start", "target": "llm_pros"},
    {"id": "e2", "source": "start", "target": "llm_cons"},
    {"id": "e3", "source": "llm_cons", "target": "merge_1"},
    {"id": "e4", "source": "llm_pros", "target": "merge_1"},
    {"id": "e5", "source": "merge_1", "target": "llm_sum"},
    {"id": "e6", "source": "llm_sum", "target": "end"}
  ]
}
```

**File:** `services/engine/tests/golden/evaluator_loop.json`

```json
{
  "version": "1",
  "nodes": [
    {"id": "start", "type": "start", "label": "시작",
     "config": {"inputs": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}},
    {"id": "llm_gen", "type": "llm", "label": "작성",
     "config": {"model": "qwen2.5:14b",
                "prompt": "{{start.topic}}에 대한 글을 써줘. 이전 피드백: {{llm_eval.feedback | default('없음')}}"}},
    {"id": "llm_eval", "type": "llm", "label": "평가",
     "config": {"model": "qwen2.5:14b", "prompt": "다음 글을 10점 만점으로 평가해줘:\n{{llm_gen.text}}",
                "outputSchema": {"type": "object",
                                 "properties": {"score": {"type": "number"}, "feedback": {"type": "string"}},
                                 "required": ["score", "feedback"]}}},
    {"id": "condition_1", "type": "condition", "label": "통과?",
     "config": {"conditions": [{"left": "{{llm_eval.score}}", "op": ">=", "right": "8"}]}},
    {"id": "end", "type": "end", "label": "끝",
     "config": {"outputs": {"text": "{{llm_gen.text}}", "score": "{{llm_eval.score}}"}}}
  ],
  "edges": [
    {"id": "e1", "source": "start", "target": "llm_gen"},
    {"id": "e2", "source": "llm_gen", "target": "llm_eval"},
    {"id": "e3", "source": "llm_eval", "target": "condition_1"},
    {"id": "e4", "source": "condition_1", "sourceHandle": "true", "target": "end"},
    {"id": "e5", "source": "condition_1", "sourceHandle": "false", "target": "llm_gen", "maxIterations": 2}
  ]
}
```

**File:** `services/engine/tests/golden/hitl.json`

```json
{
  "version": "1",
  "nodes": [
    {"id": "start", "type": "start", "label": "시작",
     "config": {"inputs": {"type": "object", "properties": {"draft": {"type": "string"}}, "required": ["draft"]}}},
    {"id": "human_approval_1", "type": "human_approval", "label": "검토",
     "config": {"message": "초안을 검토해 주세요", "review": "{{start.draft}}", "allowEdit": true}},
    {"id": "template_rejected", "type": "template", "label": "반려 안내", "config": {"template": "반려되었습니다"}},
    {"id": "end", "type": "end", "label": "끝",
     "config": {"outputs": {"final": "{{human_approval_1.editedValue}}",
                            "decision": "{{human_approval_1.decision}}",
                            "note": "{{template_rejected.text | default('')}}"}}}
  ],
  "edges": [
    {"id": "e1", "source": "start", "target": "human_approval_1"},
    {"id": "e2", "source": "human_approval_1", "sourceHandle": "approve", "target": "end"},
    {"id": "e3", "source": "human_approval_1", "sourceHandle": "reject", "target": "template_rejected"},
    {"id": "e4", "source": "template_rejected", "target": "end"}
  ]
}
```

- [ ]  **Step 2: Write the pattern tests**

**File:** `services/engine/tests/test_golden_patterns.py`

```python
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from engine.compiler.build import compile_workflow
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import execute_run
from engine.validator import validate
from tests.helpers import load_golden

PATTERNS = ["chaining", "routing", "parallel", "evaluator_loop", "hitl"]


def _deps(llm, run_id: str = "run-1") -> RunDeps:
    return RunDeps(run_id=run_id, llm=llm, recorder=InMemoryRecorder())


@pytest.mark.parametrize("name", PATTERNS)
def test_golden_fixtures_validate_cleanly(name):
    assert validate(load_golden(name)) == []


async def test_chaining_passes_outputs_forward_and_streams_tokens():
    compiled = compile_workflow(load_golden("chaining"), checkpointer=InMemorySaver())
    llm = ScriptedLLM(["개요", "본문"])
    deps = _deps(llm)

    outcome = await execute_run(compiled, deps=deps, inputs={"topic": "AI"})

    assert (outcome.status, outcome.outputs) == ("succeeded", {"result": "본문"})
    assert llm.prompts() == ["AI 글의 개요를 써줘", "다음 개요로 본문을 써줘:\n개요"]
    assert {"type": "node_token", "nodeId": "llm_1", "execIndex": 1, "attempt": None, "text": "개요"} in deps.recorder.events


@pytest.mark.parametrize(
    ("category", "responses", "answer"),
    [
        ("tech", [{"category": "tech", "reason": "r"}, "기술 답변"], "기술 답변"),
        ("billing", [{"category": "billing", "reason": "r"}, "결제 답변"], "결제 답변"),
        ("default", [{"category": "default", "reason": "r"}], "담당 부서를 찾지 못했습니다."),
    ],
)
async def test_routing_takes_exactly_one_branch(category, responses, answer):
    compiled = compile_workflow(load_golden("routing"), checkpointer=InMemorySaver())
    deps = _deps(ScriptedLLM(responses))

    outcome = await execute_run(compiled, deps=deps, inputs={"question": "질문"})

    assert outcome.status == "succeeded"
    assert outcome.outputs == {"category": category, "answer": answer}
    ran = {record.node_id for record in deps.recorder.records}
    assert len(ran & {"llm_billing", "llm_tech", "template_1"}) == 1


def _parallel_responder(model, messages, schema):
    prompt = messages[-1].content
    if prompt.startswith("종합"):
        return "종합 결과"
    return "장점 목록" if "장점" in prompt else "단점 목록"


async def test_parallel_branches_merge_in_declaration_order():
    compiled = compile_workflow(load_golden("parallel"), checkpointer=InMemorySaver())
    llm = ScriptedLLM(_parallel_responder)

    outcome = await execute_run(compiled, deps=_deps(llm), inputs={"topic": "재택근무"})

    assert outcome.status == "succeeded"
    assert outcome.outputs["summary"] == "종합 결과"
    assert list(json.loads(outcome.outputs["branchOrder"])) == ["llm_cons", "llm_pros"]
    assert "장점: 장점 목록\n단점: 단점 목록" in llm.prompts()[-1]


async def test_evaluator_loop_stops_when_the_score_passes():
    compiled = compile_workflow(load_golden("evaluator_loop"), checkpointer=InMemorySaver())
    llm = ScriptedLLM(["초안1", {"score": 5, "feedback": "더 구체적으로"}, "초안2", {"score": 9, "feedback": "좋음"}])

    outcome = await execute_run(compiled, deps=_deps(llm), inputs={"topic": "AI"})

    assert (outcome.status, outcome.outputs) == ("succeeded", {"text": "초안2", "score": 9})
    assert "이전 피드백: 없음" in llm.prompts()[0]
    assert "이전 피드백: 더 구체적으로" in llm.prompts()[2]


async def test_evaluator_loop_exits_when_iterations_are_exhausted():
    compiled = compile_workflow(load_golden("evaluator_loop"), checkpointer=InMemorySaver())
    script = []
    for i in range(1, 4):
        script += [f"초안{i}", {"score": 3, "feedback": "부족"}]
    deps = _deps(ScriptedLLM(script))

    outcome = await execute_run(compiled, deps=deps, inputs={"topic": "AI"})

    assert (outcome.status, outcome.outputs) == ("succeeded", {"text": "초안3", "score": 3})
    assert [r.exec_index for r in deps.recorder.for_node("llm_gen")] == [1, 2, 3]
    assert deps.recorder.for_node("condition_1")[-1].meta == {"handle": "true", "loopExhausted": True}


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"decision": "approve", "editedValue": "수정 원고"}, {"final": "수정 원고", "decision": "approve", "note": ""}),
        ({"decision": "reject", "comment": "다시"}, {"final": "원고", "decision": "reject", "note": "반려되었습니다"}),
    ],
)
async def test_hitl_waits_then_resumes_without_duplicate_attempts(answer, expected):
    compiled = compile_workflow(load_golden("hitl"), checkpointer=InMemorySaver())
    deps = _deps(ScriptedLLM([]))

    first = await execute_run(compiled, deps=deps, inputs={"draft": "원고"})
    assert first.status == "waiting"
    assert first.waiting == {
        "nodeId": "human_approval_1",
        "execIndex": 1,
        "message": "초안을 검토해 주세요",
        "review": "원고",
        "allowEdit": True,
    }

    second = await execute_run(compiled, deps=deps, resume=answer)

    assert (second.status, second.outputs) == ("succeeded", expected)
    records = deps.recorder.for_node("human_approval_1")
    assert [(r.attempt, r.status) for r in records] == [(1, "succeeded")]
```

**File:** `services/engine/tests/test_runner_recovery.py`

```python
from langgraph.checkpoint.memory import InMemorySaver

from engine.compiler.build import compile_workflow
from engine.llm.scripted import ScriptedLLM
from engine.runtime.deps import RunDeps
from engine.runtime.guard import FlagGuard
from engine.runtime.recorder import InMemoryRecorder
from engine.runtime.runner import execute_run
from tests.helpers import load_golden


async def test_manual_retry_resumes_from_the_failed_node():
    compiled = compile_workflow(load_golden("chaining"), checkpointer=InMemorySaver())
    recorder = InMemoryRecorder()
    failing = RunDeps(run_id="run-r", llm=ScriptedLLM(["개요", ValueError("모델 오류")]), recorder=recorder)

    failed = await execute_run(compiled, deps=failing, inputs={"topic": "AI"})
    assert failed.status == "failed"
    assert (failed.error["code"], failed.error["nodeId"]) == ("NODE_FAILED", "llm_2")

    retry = RunDeps(run_id="run-r", llm=ScriptedLLM(["본문"]), recorder=recorder)
    outcome = await execute_run(compiled, deps=retry)

    assert (outcome.status, outcome.outputs) == ("succeeded", {"result": "본문"})
    assert len(recorder.for_node("llm_1")) == 1
    assert [(r.attempt, r.status) for r in recorder.for_node("llm_2")] == [(1, "failed"), (2, "succeeded")]


async def test_completed_parallel_sibling_is_kept_after_a_failure():
    compiled = compile_workflow(load_golden("parallel"), checkpointer=InMemorySaver())
    calls = {"cons": 0}

    def responder(model, messages, schema):
        prompt = messages[-1].content
        if prompt.startswith("종합"):
            return "종합 결과"
        if "단점" in prompt:
            calls["cons"] += 1
            return ValueError("일시 오류") if calls["cons"] == 1 else "단점 목록"
        return "장점 목록"

    llm = ScriptedLLM(responder)
    deps = RunDeps(run_id="run-p", llm=llm, recorder=InMemoryRecorder())

    failed = await execute_run(compiled, deps=deps, inputs={"topic": "재택근무"})
    assert (failed.status, failed.error["nodeId"]) == ("failed", "llm_cons")

    outcome = await execute_run(compiled, deps=deps)

    assert outcome.status == "succeeded"
    assert sum(1 for prompt in llm.prompts() if "장점" in prompt and not prompt.startswith("종합")) == 1


async def test_cancelled_run():
    compiled = compile_workflow(load_golden("chaining"), checkpointer=InMemorySaver())
    guard = FlagGuard()
    guard.cancel()
    deps = RunDeps(run_id="run-c", llm=ScriptedLLM([]), recorder=InMemoryRecorder(), guard=guard)

    outcome = await execute_run(compiled, deps=deps, inputs={"topic": "AI"})

    assert outcome.status == "cancelled"
```

- [ ]  **Step 3: Run**

Run: `uv run pytest tests/test_golden_patterns.py tests/test_runner_recovery.py -v`
Expected: all PASS. These exercise Tasks 2–17 together; if one fails, fix the component, not the test.

- [ ]  **Step 4: Commit**

```bash
git add services/engine/tests/golden services/engine/tests/test_golden_patterns.py services/engine/tests/test_runner_recovery.py
git commit -m "test(engine): golden pattern flows and recovery scenarios"
```

> **Post-review note (Task 18, as implemented):** commit `d9dc140`.
>
> **Deviations.**
> - The HITL golden resume sends `{"nodeId": "human_approval_1", "execIndex": 1, **answer}`, because Task 17 made `execute_run` require the approval's target.
> - Added `test_a_loop_exit_can_fan_out_and_merges_once` (exit on `true` and exit when exhausted) and `test_a_condition_self_loop_stops_at_its_limit`.
>
> **Mutation check.** The tests catch a router firing two branches, loop counters that reset, a retry that ignores the checkpoint, and a replayed approval that records a second attempt.
>
> **Not covered.** No flow reaches `MergeNode`'s missing-predecessor guard, because both merge predecessors always finish in the same superstep. Add a direct unit test if routing into merges changes.
>
> Suite: 631.

---

## Task 19: Full verification

- [ ]  **Step 1: Run the whole suite**

Run: `uv run pytest -q`
Expected: all tests pass, 0 failures, no warnings about un-awaited coroutines.

- [ ]  **Step 2: Lint**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ]  **Step 3: Confirm the spec coverage table below still holds, then commit any lint fixes**

```bash
git add -A services/engine
git commit -m "chore(engine): lint fixes"
```

(Skip the commit if there is nothing to commit.)

> **Verification (Task 19):** at `d9dc140`, `uv run pytest -q` reports 631 passed, with no warnings (also clean under `-W error::RuntimeWarning`), and `uv run ruff check .` reports `All checks passed!`. No lint fixes were needed. The coverage table below still holds.

> **Final branch review:** commits `7a2dae0` and `2179ddd`.
>
> **Run state depth.** LangGraph's checkpoint serializer fails at about 250 nesting levels with a raw `TypeError`, below what the engine's checks allowed. The fix:
> - `engine.jsondata.MAX_JSON_DEPTH` (100) and `check_storable` apply to rendered fields (`TEMPLATE_ERROR`), node outputs (`NODE_FAILED`), `defaultOutput` (`INVALID_POLICY`) and fresh run inputs (a failed outcome at `start`).
> - `resume_output` checks the whole approval output, so an answer it accepts always fits the node output.
>
> **Unsafe text in messages.** The duplicate-key message quoted raw keys, putting NUL and lone surrogates into `NodeError` and `Issue` messages. `safe_text` now cleans that message. `NodeError` and `Issue` replace unsafe characters as a backstop, and `InMemoryRecorder` rejects them as jsonb would.
>
> **Also.** `ruff` is bounded `<1`. The roadmap interface table now names the real modules.
>
> **Carried into Plan 2 (roadmap):** quadratic checkpoint growth, one shared node registry, `dsl_hash` of `policy: {}`, run input depth at the API.
>
> **Known follow-ups.**
> - A JSON template with no `{{ }}` nested deeper than 99 passes `validate()` but always fails at runtime; `_check_json_template` could run `check_storable` on the parsed value.
> - `_is_number` is duplicated in `jsondata`, `templates/env` and `nodes/condition`.
>
> Suite: 643.

---

## Spec coverage (Plan 1)


| Spec                                                                       | Covered by                                        |
| ---------------------------------------------------------------------------- | --------------------------------------------------- |
| 4.1 DSL structure,`settings`, declaration order                            | Task 2, Task 17 (merge order), Task 18 (parallel) |
| 4.2 immutable ids, labels,`dsl_hash`                                       | Task 2, Task 12                                   |
| 4.3 policy schema, retryable classes                                       | Task 2, Task 12, Task 16                          |
| 4.4 templates, whole-value vs interpolation, before-sets,`default`         | Tasks 4, 5, 14                                    |
| 4.5 type system                                                            | Tasks 3, 14                                       |
| 4.6–4.7 node specs, 8 MVP nodes (`http_request` → Plan 2)                | Tasks 8–11                                       |
| 4.8 graph rules 1–10 (+ MVP parallel tightening)                          | Tasks 12, 13, 14                                  |
| 5.3 exec_index / attempt / success = checkpoint write, wrapper-owned retry | Tasks 15, 16, 18                                  |
| 5.4 at-least-once (engine side)                                            | Task 18 recovery tests; idempotency key → Plan 2 |
| 5.5 manual retry from checkpoint                                           | Task 17, Task 18                                  |
| 5.6 HITL re-entry                                                          | Tasks 11, 16, 18                                  |
| 5.7 parallel + merge semantics                                             | Tasks 11, 13, 17, 18                              |
| 5.8 loops, counters, recursion limit                                       | Tasks 15, 17, 18                                  |
| 5.9 cancellation (engine side: guard)                                      | Tasks 15, 16, 18; control channel → Plan 2       |
| 6.1 validator, 6.2 compiler, 6.4 LLM gateway                               | Tasks 6, 7, 12–14, 17                            |
| 5.1–5.2, 7, 8, 10, 11.1 (DSL size, inputs size, run time), 6.3 worker     | Plan 2 (see roadmap)                              |
