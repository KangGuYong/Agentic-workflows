"""Bring a database up to date: application tables (Alembic), then LangGraph's checkpoint tables."""
from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

HERE = Path(__file__).parent


def _alembic_config(url: str) -> Config:
    config = Config(str(HERE / "alembic.ini"))
    config.set_main_option("script_location", str(HERE / "migrations"))
    # SQLAlchemy (used only by Alembic) needs the driver spelled out, and treats % as interpolation.
    sqlalchemy_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    config.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))
    return config


async def prepare_database(url: str) -> None:
    """Idempotent: safe to call at every startup and from tests."""
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
