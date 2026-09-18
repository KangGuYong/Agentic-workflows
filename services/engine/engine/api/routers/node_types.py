"""Node palette for the editor (MVP design 4.6): the Python spec is the single source of truth."""
from __future__ import annotations

from fastapi import APIRouter, Request

from engine.nodes.registry import NodeRegistry

router = APIRouter()


def build_payload(registry: NodeRegistry) -> dict:
    """The /node-types response body for `registry`. Each spec's `Config.model_json_schema()` walks a
    pydantic model (~1.8ms of event-loop CPU across the built-in specs) -- call this once in create_app
    and cache the result, rather than rebuilding it on every request for data that never changes after
    the registry is built."""
    return {"nodeTypes": [
        {
            "type": spec.type,
            "label": spec.label,
            "category": spec.category,
            "isBranch": spec.is_branch,
            "sideEffects": spec.side_effects,
            "configSchema": spec.Config.model_json_schema(),
            "defaultPolicy": spec.default_policy.model_dump() if spec.default_policy else None,
        }
        for spec in registry.all()
    ]}


@router.get("/node-types")
async def node_types(request: Request) -> dict:
    return request.app.state.node_types_payload
