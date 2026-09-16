"""Node palette for the editor (MVP design 4.6): the Python spec is the single source of truth."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/node-types")
async def node_types(request: Request) -> dict:
    registry = request.app.state.registry
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
