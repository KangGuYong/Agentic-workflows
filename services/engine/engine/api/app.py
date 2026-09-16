"""FastAPI application. API and worker share one process-wide node registry (design 8.3)."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI
from psycopg_pool import AsyncConnectionPool

from engine.api import errors
from engine.api.routers import node_types
from engine.api.security import require_token
from engine.config import EngineConfig
from engine.nodes.registry import NodeRegistry, default_registry


def create_app(config: EngineConfig, pool: AsyncConnectionPool, redis: Any,
               registry: NodeRegistry | None = None) -> FastAPI:
    app = FastAPI(title="Agentic Workflow Engine", dependencies=[Depends(require_token)])
    app.state.config = config
    app.state.pool = pool
    app.state.redis = redis
    app.state.registry = registry or default_registry()
    errors.install(app)
    app.include_router(node_types.router)

    # /healthz stays behind the shared token: FastAPI's app-level `dependencies=[...]` always runs for
    # every route, so a route-level `dependencies=[]` here would be a no-op, not an opt-out. The editor
    # never calls this anonymously, and it reports DB/Redis connectivity, so keeping it authenticated
    # like every other route is the deliberate choice, not an oversight.
    @app.get("/healthz")
    async def healthz() -> dict:
        db = redis_ok = False
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
            db = True
        except Exception:  # any failure means Postgres is unreachable; report degraded, don't crash the probe
            db = False
        try:
            redis_ok = bool(await redis.ping())
        except Exception:  # same for Redis
            redis_ok = False
        return {"status": "ok" if db and redis_ok else "degraded", "db": db, "redis": redis_ok}

    return app
