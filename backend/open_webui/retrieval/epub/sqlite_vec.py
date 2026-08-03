"""Explicit sqlite-vec loading and health checks for the EPUB store.

The extension is loaded only for the EPUB database connection.  It is never a
silent optional dependency: callers get a clear error and must surface a
degraded/failed index state rather than attempting a cloud-vector fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3


class SQLiteVecUnavailable(RuntimeError):
    """sqlite-vec cannot be loaded for this SQLite runtime."""


@dataclass(frozen=True, slots=True)
class SQLiteVecHealth:
    version: str


def load_sqlite_vec(connection: sqlite3.Connection) -> SQLiteVecHealth:
    """Load sqlite-vec and prove the connection can execute its SQL function.

    Extension loading is enabled only for the short loading window and is
    disabled again even when import or loading fails.
    """
    enable = getattr(connection, "enable_load_extension", None)
    if not callable(enable):
        raise SQLiteVecUnavailable(
            "this Python SQLite build does not support loading sqlite extensions"
        )
    try:
        import sqlite_vec
    except ImportError as error:
        raise SQLiteVecUnavailable("sqlite-vec is not installed") from error

    try:
        enable(True)
        sqlite_vec.load(connection)
        row = connection.execute("SELECT vec_version()").fetchone()
    except sqlite3.Error as error:
        raise SQLiteVecUnavailable(f"sqlite-vec failed its SQL health check: {error}") from error
    finally:
        try:
            enable(False)
        except sqlite3.Error:
            # Do not hide the primary extension-loading error.  A successfully
            # loaded connection is still protected by callers not exposing it.
            pass
    if row is None or not isinstance(row[0], str):
        raise SQLiteVecUnavailable("sqlite-vec did not return a version")
    return SQLiteVecHealth(version=row[0])
