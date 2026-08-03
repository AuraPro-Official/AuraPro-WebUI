"""Versioned EPUB data-domain interfaces and implementations."""

from .store import (
    DuplicateEpubError,
    EpubStore,
    IntegrityError,
    SQLiteEpubStore,
    VersionCreation,
)
from .sqlite_vec import SQLiteVecHealth, SQLiteVecUnavailable, load_sqlite_vec

__all__ = [
    "DuplicateEpubError",
    "EpubStore",
    "IntegrityError",
    "SQLiteEpubStore",
    "SQLiteVecHealth",
    "SQLiteVecUnavailable",
    "VersionCreation",
    "load_sqlite_vec",
]
