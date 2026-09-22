"""FastAPI application. API and worker share one process-wide node registry (design 8.3)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from engine.api import errors
from engine.api.routers import knowledge_bases, node_types, runs, secrets, workflows
from engine.api.security import TokenAuthMiddleware
from engine.config import EngineConfig
from engine.nodes.registry import NodeRegistry, default_registry

log = logging.getLogger(__name__)

_HEALTHZ_TIMEOUT_SEC = 2


def create_app(config: EngineConfig, pool: AsyncConnectionPool, redis: Any,
               registry: NodeRegistry | None = None) -> FastAPI:
    if not config.api_token:
        # Design 8.1 deliberately allows this for local development (see TokenAuthMiddleware), but it
        # means every request is accepted unauthenticated -- make that loud at startup. load_config()
        # already maps ENGINE_API_TOKEN="" to None, and create_app is reachable without going through
        # main.py (e.g. from tests), so the warning belongs here rather than in an entrypoint.
        log.warning("ENGINE_API_TOKEN is not set: the API is accepting every request without authentication")

    app = FastAPI(title="Agentic Workflow Engine")
    app.state.config = config
    app.state.pool = pool
    app.state.redis = redis
    app.state.registry = registry or default_registry()
    app.state.node_types_payload = node_types.build_payload(app.state.registry)
    app.add_middleware(TokenAuthMiddleware)
    errors.install(app)
    app.include_router(node_types.router)
    app.include_router(workflows.router)
    app.include_router(runs.router)
    app.include_router(secrets.router)
    app.include_router(knowledge_bases.router)

    # /healthz stays behind the shared token like every other route: TokenAuthMiddleware runs in front of
    # routing for every path unconditionally, so there is no route-level opt-out to reason about here.
    # (FastAPI's app-level `dependencies=[...]` is narrower than that -- an API route cannot opt out of
    # it with a route-level `dependencies=[]`, but a route registered outside `add_api_route`, such as
    # FastAPI's own docs routes, is never covered by it at all. That gap is why this app uses middleware
    # instead.) The editor never calls this anonymously, and it reports DB/Redis connectivity, so keeping
    # it authenticated like every other route is the deliberate choice, not an oversight.
    @app.get("/healthz")
    async def healthz() -> dict:
        db = redis_ok = False
        try:
            async with asyncio.timeout(_HEALTHZ_TIMEOUT_SEC):
                async with pool.connection() as conn:
                    await conn.execute("SELECT 1")
            db = True
        except Exception:  # any failure -- including a timeout -- means Postgres is unreachable; degrade
            db = False
        try:
            async with asyncio.timeout(_HEALTHZ_TIMEOUT_SEC):
                redis_ok = bool(await redis.ping())
        except Exception:  # same for Redis
            redis_ok = False
        return {"status": "ok" if db and redis_ok else "degraded", "db": db, "redis": redis_ok}

    return app
