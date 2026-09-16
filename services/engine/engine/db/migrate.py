"""Bring a database up to date: application tables (Alembic), then LangGraph's checkpoint tables."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

HERE = Path(__file__).parent

# Arbitrary, stable: identifies "a migration is running" among advisory locks (distinct from the reaper's
# LOCK_KEY in engine/worker/reaper.py). The API and every worker call prepare_database at startup, so more
# than one process can reach it at the same moment against a brand-new database -- see prepare_database.
_MIGRATION_LOCK_KEY = 8_273_441_003
_LOCK_POLL_INTERVAL_SEC = 0.2
# How long a caller will poll for the lock before giving up. The holder is normally only ever mid-Alembic-
# upgrade (a few seconds at most), so a long wait here means something is actually stuck -- raise rather
# than spin forever with no way for an operator to notice (see prepare_database).
_LOCK_WAIT_TIMEOUT_SEC = 60.0


def _alembic_config(url: str) -> Config:
    config = Config(str(HERE / "alembic.ini"))
    config.set_main_option("script_location", str(HERE / "migrations"))
    # SQLAlchemy (used only by Alembic) needs the driver spelled out, and treats % as interpolation.
    sqlalchemy_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    config.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))
    return config


async def prepare_database(url: str, *, lock_timeout_sec: float = _LOCK_WAIT_TIMEOUT_SEC) -> None:
    """Idempotent: safe to call at every startup and from tests -- including concurrently, from several
    processes at once.

    Alembic's own `CREATE TABLE` statements (engine/db/migrations/versions/0001_initial.py) have no
    `IF NOT EXISTS` guard, and two `upgrade head` runs starting from the same empty database race to
    create the same relation. That is a fast, unclean failure, not a hang: racing the pre-lock body six
    times against fresh databases, every run failed in 0.08-0.11s, with every caller but one dying on
    `UniqueViolation` -- against a system catalog index (`pg_type_typname_nsp_index`) or one of Alembic's
    own tables (`checkpoint_migrations_pkey`) -- or on `KeyError: 'config'` from Alembic's non-thread-safe
    module globals when two upgrades race inside the same process. A session-level Postgres advisory lock
    serializes the whole upgrade (Alembic's tables and then LangGraph's) across processes: the loser polls
    with `pg_try_advisory_lock` until the winner finishes and commits, then runs its own `upgrade head`
    against a schema that is already current, which is a fast no-op.

    The lock is acquired by polling rather than a blocking `pg_advisory_lock`, on purpose: LangGraph's own
    setup() (below) runs `CREATE INDEX CONCURRENTLY`, which must wait out every other session's open
    snapshot, including one sitting inside a *blocked* `pg_advisory_lock` call. A blocking wait there would
    deadlock against the winner's own index build; a poll leaves the losing connection genuinely idle
    (no open statement) between attempts, so it holds no snapshot open for the index build to wait on.

    The poll is bounded by `lock_timeout_sec`: a stuck lock holder (wedged mid-Alembic-upgrade) would
    otherwise leave every other caller spinning silently forever, with no deadline and nothing in the logs
    to say why the process never finished starting. The first failed attempt logs once at INFO, and a
    caller that still cannot get the lock once the deadline passes gets a clear `TimeoutError` instead.
    """
    async with await AsyncConnection.connect(url, autocommit=True, row_factory=dict_row) as lock_conn:
        logged_wait = False
        try:
            async with asyncio.timeout(lock_timeout_sec):
                while True:
                    row = await (await lock_conn.execute(
                        "SELECT pg_try_advisory_lock(%s) AS ok", (_MIGRATION_LOCK_KEY,),
                    )).fetchone()
                    if row["ok"]:
                        break
                    if not logged_wait:
                        log.info("prepare_database: migration lock held by another process; waiting "
                                 "(deadline %.0fs)", lock_timeout_sec)
                        logged_wait = True
                    await asyncio.sleep(_LOCK_POLL_INTERVAL_SEC)
        except TimeoutError:
            raise TimeoutError(
                f"prepare_database: could not acquire the migration advisory lock within "
                f"{lock_timeout_sec:.0f}s; the current holder may be stuck"
            ) from None
        try:
            await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")
            async with AsyncPostgresSaver.from_conn_string(url) as saver:
                await saver.setup()
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
