"""Unified SQLite connection utility.

Consolidates the scattered `sqlite3.connect(...)` boilerplate of
token_proxy / proxy_usage / server_search / server into
`with conn(db_path) as c:` — auto-commit + rollback on exception +
always close, avoiding connection leaks and duplicated boilerplate.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager


@contextmanager
def conn(
    db_path: str, *, timeout: float = 5.0
) -> Generator[sqlite3.Connection, None, None]:
    """SQLite connection context manager.

    Commits on normal exit, rolls back on exception, and always closes
    the connection.

    Usage::

        with conn(DB_PATH) as c:
            c.execute("INSERT INTO t (a) VALUES (?)", (1,))
    """
    connection = sqlite3.connect(db_path, timeout=timeout)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
