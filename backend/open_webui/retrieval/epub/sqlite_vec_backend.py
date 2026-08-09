"""Persistent ``sqlite-vec`` backend for derived EPUB retrieval windows.

The vector virtual tables deliberately contain no source text.  Their durable
metadata is keyed by one immutable retrieval unit, which in turn proves its
exact source-passage offsets and hash before this backend ever receives it.
"""

from __future__ import annotations

from contextlib import nullcontext
from hashlib import sha256
import json
import math
from typing import Any, Iterator, Sequence

from .sqlite_vec import SQLiteVecHealth, SQLiteVecUnavailable, load_sqlite_vec
from .vector_index import DerivedVectorRecord, VectorIndexError


class SQLiteVecDerivedVectorBackend:
    """Store and KNN-query derived vectors in profile/dimension-specific vec0 tables.

    A ``vec0`` column has a fixed dimension.  A separate, deterministically
    named virtual table per profile/dimension prevents silently comparing
    embeddings produced by different local models.
    """

    def __init__(self, store: Any):
        if not callable(getattr(store, "_connection", None)):
            raise TypeError("SQLiteVecDerivedVectorBackend requires a SQLite EPUB store connection")
        self._store = store
        self._loaded_connection_ids: set[int] = set()
        self.health = self._load_and_migrate()

    def upsert(self, record: DerivedVectorRecord) -> None:
        vector = _validated_vector(record.vector)
        table = _table_name(record.embedding_profile, len(vector))
        self._ensure_table(table, len(vector))
        with self._write() as connection:
            existing = connection.execute(
                """SELECT passage_id, start_codepoint, end_codepoint, content_sha256,
                          embedding_profile, vector_table, vector_rowid
                   FROM epub_derived_vectors WHERE retrieval_unit_id = ?""",
                (record.retrieval_unit_id,),
            ).fetchone()
            identity = _identity(record)
            if existing is not None:
                current = (
                    existing["passage_id"],
                    int(existing["start_codepoint"]),
                    int(existing["end_codepoint"]),
                    existing["content_sha256"],
                    existing["embedding_profile"],
                )
                if current != identity:
                    raise VectorIndexError(
                        "a retrieval-unit ID cannot be rebound to a different source window or profile"
                    )
                if existing["vector_table"] != table:
                    raise VectorIndexError("a retrieval-unit embedding dimension cannot change")
                connection.execute(
                    f'UPDATE "{table}" SET embedding = ? WHERE rowid = ?',
                    (_vector_json(vector), existing["vector_rowid"]),
                )
                connection.execute(
                    "UPDATE epub_derived_vectors SET vector_json = ? WHERE retrieval_unit_id = ?",
                    (_vector_json(vector), record.retrieval_unit_id),
                )
                return

            row = connection.execute(
                "SELECT COALESCE(MAX(vector_rowid), 0) + 1 FROM epub_derived_vectors WHERE vector_table = ?",
                (table,),
            ).fetchone()
            rowid = int(row[0])
            connection.execute(
                f'INSERT INTO "{table}" (rowid, embedding) VALUES (?, ?)',
                (rowid, _vector_json(vector)),
            )
            connection.execute(
                """INSERT INTO epub_derived_vectors(
                       retrieval_unit_id, passage_id, start_codepoint, end_codepoint, content_sha256,
                       embedding_profile, vector_table, vector_rowid, vector_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.retrieval_unit_id, *identity, table, rowid, _vector_json(vector)),
            )

    def search(
        self, query_vector: Sequence[float], *, embedding_profile: str, limit: int
    ) -> list[DerivedVectorRecord]:
        vector = _validated_vector(query_vector)
        if not embedding_profile or not embedding_profile.strip():
            raise VectorIndexError("embedding profile cannot be empty")
        if limit < 1:
            raise VectorIndexError("vector search limit must be positive")
        table = _table_name(embedding_profile, len(vector))
        if not self._table_exists(table):
            return []
        rows = self._connection().execute(
            f'''SELECT metadata.retrieval_unit_id, metadata.passage_id, metadata.start_codepoint,
                       metadata.end_codepoint, metadata.content_sha256, metadata.embedding_profile,
                       metadata.vector_json
                FROM "{table}" AS vectors
                JOIN epub_derived_vectors AS metadata
                  ON metadata.vector_table = ? AND metadata.vector_rowid = vectors.rowid
                WHERE vectors.embedding MATCH ? AND k = ?''',
            (table, _vector_json(vector), limit),
        ).fetchall()
        return [
            DerivedVectorRecord(
                retrieval_unit_id=str(row["retrieval_unit_id"]),
                passage_id=str(row["passage_id"]),
                start_codepoint=int(row["start_codepoint"]),
                end_codepoint=int(row["end_codepoint"]),
                content_sha256=str(row["content_sha256"]),
                embedding_profile=str(row["embedding_profile"]),
                vector=tuple(_validated_vector(json.loads(row["vector_json"]))),
            )
            for row in rows
        ]

    def healthcheck(self) -> SQLiteVecHealth:
        """Prove sqlite-vec remains available on the store's active connection.

        sqlite-vec is connection-scoped.  Startup success is therefore not a
        durable guarantee if a store implementation rotates connections.  The
        admin runtime-status surface calls this inexpensive SQL check and can
        report a degraded vector subsystem without a cloud fallback.
        """
        try:
            return self._ensure_loaded(self._store._connection())
        except SQLiteVecUnavailable:
            raise
        except Exception as error:
            raise SQLiteVecUnavailable(f"sqlite-vec failed its SQL health check: {error}") from error

    def _load_and_migrate(self) -> SQLiteVecHealth:
        connection = self._connection()
        health = self._ensure_loaded(connection)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS epub_derived_vectors (
                retrieval_unit_id TEXT PRIMARY KEY REFERENCES retrieval_units(retrieval_unit_id) ON DELETE RESTRICT,
                passage_id TEXT NOT NULL,
                start_codepoint INTEGER NOT NULL,
                end_codepoint INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                embedding_profile TEXT NOT NULL,
                vector_table TEXT NOT NULL,
                vector_rowid INTEGER NOT NULL,
                vector_json TEXT NOT NULL,
                UNIQUE(vector_table, vector_rowid)
            )"""
        )
        connection.commit()
        return health

    def _ensure_table(self, table: str, dimension: int) -> None:
        if dimension < 1:
            raise VectorIndexError("embedding vector cannot be empty")
        connection = self._connection()
        self._ensure_loaded(connection)
        connection.execute(
            f'CREATE VIRTUAL TABLE IF NOT EXISTS "{table}" USING vec0(embedding float[{dimension}] distance_metric=cosine)'
        )
        connection.commit()

    def _connection(self) -> Any:
        connection = self._store._connection()
        self._ensure_loaded(connection)
        return connection

    def _ensure_loaded(self, connection: Any) -> SQLiteVecHealth:
        marker = id(connection)
        if marker not in self._loaded_connection_ids:
            health = load_sqlite_vec(connection)
            self._loaded_connection_ids.add(marker)
            return health
        try:
            row = connection.execute("SELECT vec_version()").fetchone()
        except Exception as error:
            raise SQLiteVecUnavailable(f"sqlite-vec failed its SQL health check: {error}") from error
        if row is None or not isinstance(row[0], str):
            raise SQLiteVecUnavailable("sqlite-vec did not return a version")
        return SQLiteVecHealth(version=str(row[0]))

    def _table_exists(self, table: str) -> bool:
        return self._connection().execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    def _write(self) -> Iterator[Any]:
        write = getattr(self._store, "_write", None)
        if callable(write):
            return write()
        return nullcontext(self._connection())


def _table_name(profile: str, dimension: int) -> str:
    if not profile or not profile.strip():
        raise VectorIndexError("embedding profile cannot be empty")
    digest = sha256(profile.encode("utf-8")).hexdigest()[:16]
    return f"epub_vec_{digest}_{dimension}"


def _identity(record: DerivedVectorRecord) -> tuple[str, str, int, int, str, str]:
    return (
        record.passage_id,
        record.start_codepoint,
        record.end_codepoint,
        record.content_sha256,
        record.embedding_profile,
    )


def _validated_vector(vector: Sequence[float]) -> tuple[float, ...]:
    if not vector:
        raise VectorIndexError("vector cannot be empty")
    values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise VectorIndexError("vector contains a non-finite value")
        values.append(float(value))
    return tuple(values)


def _vector_json(vector: Sequence[float]) -> str:
    return json.dumps(list(vector), separators=(",", ":"))
