from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel

from engine.dsl.models import Policy
from engine.dsl.types import Target
from engine.jsondata import schema_problems
from engine.jsondata import schema_violations as schema_violations  # re-exported for node modules
from engine.llm.base import LLMClient, TokenSink

TEMPLATE = {"x-template": True}  # json_schema_extra marker: the editor renders a template input
MODEL_NAME = r"^\S+$"  # Ollama model names such as "qwen2.5:14b" contain no whitespace
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
        """JSON Schema of the output, read-only. `pred_schemas` maps forward predecessors (declaration order)."""

    @abstractmethod
    async def execute(self, ctx: NodeContext, config: BaseModel, rendered: dict[str, Any]) -> NodeResult: ...


def check_object_schema(value: dict[str, Any], what: str) -> dict[str, Any]:
    """Pydantic validator body: `value` must be an accepted tenant JSON Schema (see engine.jsondata) of type object."""
    problems = schema_problems(value)
    if problems:
        raise ValueError(f"{what}: " + "; ".join(problems))
    if value.get("type") != "object":
        raise ValueError(f"{what}의 type은 object여야 합니다")
    if "anyOf" in value or "oneOf" in value:
        raise ValueError(f"{what}의 최상위에는 anyOf/oneOf를 쓸 수 없습니다")
    return value
