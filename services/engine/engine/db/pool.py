"""Postgres access. Every query in this package is written by hand: the important ones are CAS updates."""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def make_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    """A pool of autocommit connections with dict rows.

    Autocommit is what LangGraph's Postgres saver needs, and it keeps single statements out of an implicit
    transaction; multi-statement work opens `conn.transaction()` explicitly.
    """
    return AsyncConnectionPool(
        url, min_size=min_size, max_size=max_size, open=False,
        # Shorter than WORKER_LEASE_SEC on purpose (2b design §8.1). psycopg's default is 30s, which
        # equals the default lease: a caller queued behind a full pool can wait out the very lease it is
        # trying to extend and never learn it lost it. A checkout that fails and says so is better.
        timeout=5.0,
        kwargs={"row_factory": dict_row, "autocommit": True},
    )
