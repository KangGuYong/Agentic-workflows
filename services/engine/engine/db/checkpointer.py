"""The run checkpointer (design 5.3).

Checkpoints hold every node output, so they are encrypted at rest with `LANGGRAPH_AES_KEY`. The serializer
has to be in place from the first checkpoint a deployment writes: turning it on later leaves the existing
rows unreadable, which is why `load_config` refuses to start without a key unless ENGINE_DEV_INSECURE is set.
"""
from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from psycopg_pool import AsyncConnectionPool

from engine.config import EngineConfig


def make_checkpointer(pool: AsyncConnectionPool, config: EngineConfig) -> AsyncPostgresSaver:
    """A saver over `pool`, encrypted unless the process was started in development-insecure mode.

    The pool must hand out autocommit connections (see `engine.db.pool.make_pool`).
    """
    if not config.encrypt_checkpoints:
        return AsyncPostgresSaver(pool)
    return AsyncPostgresSaver(pool, serde=EncryptedSerializer.from_pycryptodome_aes())
