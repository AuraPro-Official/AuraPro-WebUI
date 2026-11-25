"""Persistent ``sqlite-vec`` backend for derived EPUB retrieval windows.

The vector virtual tables deliberately contain no source text.  Their durable
metadata is keyed by one immutable retrieval unit, which in turn proves its
exact source-passage offsets and hash before this backend ever receives it.

**Transaction ownership.**  This backend does not own its connection; it
borrows the canonical store's.  The rule it therefore follows everywhere is
*whoever opened the transaction closes it*.  Nothing in this module calls
``commit()`` or ``rollback()``: when the caller already has a transaction
open, every statement here joins it and the caller's later rollback undoes
this backend's work too; when there is none, ``_write()`` opens the store's
own and SQLite's autocommit covers the bare DDL.  A ``commit()`` here would
silently end a transaction the store opened, and a rollback after that point
would leave the write half-applied -- which is exactly what an indexing or
uninstall routine must never do.
"""

from __future__ import annotations

from contextlib import nullcontext
from hashlib import sha256
import json
import math
import re
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
        if not callable(getattr(store, '_connection', None)):
            raise TypeError('SQLiteVecDerivedVectorBackend requires a SQLite EPUB store connection')
        self._store = store
        self._loaded_connection_ids: set[int] = set()
        self.health = self._load_and_migrate()

    def upsert(self, record: DerivedVectorRecord) -> None:
        vector = _validated_vector(record.vector)
        table = _table_name(record.embedding_profile, len(vector))
        with self._write() as connection:
            # Inside the write, not before it.  The table only has to exist for
            # the rows written below, and creating it here means a caller's
            # rollback undoes the whole upsert rather than leaving a virtual
            # table behind for a write that never happened.
            self._ensure_table(connection, table, len(vector))
            existing = connection.execute(
                """SELECT passage_id, start_codepoint, end_codepoint, content_sha256,
                          embedding_profile, vector_table, vector_rowid
                   FROM epub_derived_vectors WHERE retrieval_unit_id = ?""",
                (record.retrieval_unit_id,),
            ).fetchone()
            identity = _identity(record)
            if existing is not None:
                current = (
                    existing['passage_id'],
                    int(existing['start_codepoint']),
                    int(existing['end_codepoint']),
                    existing['content_sha256'],
                    existing['embedding_profile'],
                )
                if current != identity:
                    raise VectorIndexError(
                        'a retrieval-unit ID cannot be rebound to a different source window or profile'
                    )
                if existing['vector_table'] != table:
                    raise VectorIndexError('a retrieval-unit embedding dimension cannot change')
                connection.execute(
                    f'UPDATE "{table}" SET embedding = ? WHERE rowid = ?',
                    (_vector_json(vector), existing['vector_rowid']),
                )
                connection.execute(
                    'UPDATE epub_derived_vectors SET vector_json = ? WHERE retrieval_unit_id = ?',
                    (_vector_json(vector), record.retrieval_unit_id),
                )
                return

            rowid = self._allocate_rowid(connection, table)
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

    def delete(self, retrieval_unit_id: str) -> bool:
        """Remove one derived vector and its metadata, reporting whether it existed.

        Absence is a no-op rather than an error; see
        :class:`~.vector_index.DerivedVectorBackend` for why removal has to be
        idempotent.
        """
        return self.delete_many((retrieval_unit_id,)) > 0

    def delete_many(self, retrieval_unit_ids: Sequence[str]) -> int:
        """Remove several derived vectors in one transaction, returning the count.

        The vec0 row and the ``epub_derived_vectors`` row are deleted together,
        in one transaction, because ``vec0`` is a virtual table and cannot
        carry a foreign key: nothing in SQLite would notice an orphan on
        either side.  A metadata row without its vector makes search return a
        candidate whose embedding is gone; a vector without its metadata is
        invisible to ``PRAGMA foreign_key_check`` *and* to this module's own
        queries, while still occupying the rowid it was inserted at.

        A metadata row whose vec0 table has since been dropped is still
        removed: cleaning up half a state is the point, and refusing would
        strand the row for good.
        """
        if isinstance(retrieval_unit_ids, str):
            raise VectorIndexError('delete_many takes a sequence of retrieval-unit IDs, not a single ID')
        unit_ids = list(dict.fromkeys(str(unit_id) for unit_id in retrieval_unit_ids))
        if not unit_ids:
            return 0
        removed = 0
        with self._write() as connection:
            for unit_id in unit_ids:
                row = connection.execute(
                    'SELECT vector_table, vector_rowid FROM epub_derived_vectors WHERE retrieval_unit_id = ?',
                    (unit_id,),
                ).fetchone()
                if row is None:
                    continue
                table = _validated_table_name(str(row['vector_table']))
                if self._table_exists(table, connection):
                    connection.execute(f'DELETE FROM "{table}" WHERE rowid = ?', (int(row['vector_rowid']),))
                connection.execute('DELETE FROM epub_derived_vectors WHERE retrieval_unit_id = ?', (unit_id,))
                removed += 1
        return removed

    def search(self, query_vector: Sequence[float], *, embedding_profile: str, limit: int) -> list[DerivedVectorRecord]:
        vector = _validated_vector(query_vector)
        if not embedding_profile or not embedding_profile.strip():
            raise VectorIndexError('embedding profile cannot be empty')
        if limit < 1:
            raise VectorIndexError('vector search limit must be positive')
        table = _table_name(embedding_profile, len(vector))
        if not self._table_exists(table):
            return []
        rows = (
            self._connection()
            .execute(
                f'''SELECT metadata.retrieval_unit_id, metadata.passage_id, metadata.start_codepoint,
                       metadata.end_codepoint, metadata.content_sha256, metadata.embedding_profile,
                       metadata.vector_json
                FROM "{table}" AS vectors
                JOIN epub_derived_vectors AS metadata
                  ON metadata.vector_table = ? AND metadata.vector_rowid = vectors.rowid
                WHERE vectors.embedding MATCH ? AND k = ?''',
                (table, _vector_json(vector), limit),
            )
            .fetchall()
        )
        return [
            DerivedVectorRecord(
                retrieval_unit_id=str(row['retrieval_unit_id']),
                passage_id=str(row['passage_id']),
                start_codepoint=int(row['start_codepoint']),
                end_codepoint=int(row['end_codepoint']),
                content_sha256=str(row['content_sha256']),
                embedding_profile=str(row['embedding_profile']),
                vector=tuple(_validated_vector(json.loads(row['vector_json']))),
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
            raise SQLiteVecUnavailable(f'sqlite-vec failed its SQL health check: {error}') from error

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
        # The rowid high-water mark per vec0 table.  It deliberately does NOT
        # live in ``epub_derived_vectors``, whose rows are exactly what
        # ``delete_many`` removes, and whose ``MAX(vector_rowid)`` therefore
        # falls back down after every delete.  Two distinct things go wrong if
        # allocation reads that MAX:
        #
        #   * A rowid gets *re-issued*.  A vec0 rowid is the only thing the
        #     search JOIN matches metadata on, so handing a freed one to a
        #     different retrieval unit means any reference still holding the
        #     old number now resolves to another passage's vector.  In a
        #     module whose entire job is proving a vector belongs to one exact
        #     source window, silently re-pointing that link is the worst
        #     available failure -- worse than an error, because it is a wrong
        #     citation rather than a missing one.
        #   * Allocation *jams*, permanently.  vec0 is a virtual table and can
        #     hold no foreign key, so a vector whose metadata row went without
        #     it is invisible -- ``PRAGMA foreign_key_check`` reports nothing --
        #     while still occupying its rowid.  MAX over the metadata has then
        #     already dropped below a live vec0 rowid, and every subsequent
        #     insert for that profile fails ``UNIQUE constraint failed`` and
        #     keeps failing.  ``delete_many`` removes both sides together so
        #     this backend does not create orphans, but an interrupted or
        #     hand-written delete can, and the allocator should not be the
        #     thing that turns a recoverable mess into a dead profile.
        #
        # A separate row per table, never decremented, is monotonic by
        # construction and answers both.  Being an ordinary durable table
        # rather than an in-process counter, it also survives a restart, which
        # is what makes it correct across the desktop app's start/stop cycle.
        # It is read and bumped inside the caller's write transaction, so the
        # store's ``BEGIN IMMEDIATE`` serialises two writers, and a rolled-back
        # insert gives its rowid back rather than burning it.
        connection.execute(
            """CREATE TABLE IF NOT EXISTS epub_derived_vector_rowids (
                vector_table TEXT PRIMARY KEY,
                next_rowid INTEGER NOT NULL CHECK (next_rowid > 0)
            )"""
        )
        return health

    def _allocate_rowid(self, connection: Any, table: str) -> int:
        """Issue the next never-yet-used rowid for one vec0 table."""
        row = connection.execute(
            'SELECT next_rowid FROM epub_derived_vector_rowids WHERE vector_table = ?', (table,)
        ).fetchone()
        if row is None:
            # Seeding from the metadata table is exact here rather than merely
            # close: a database with no allocator row either has never written
            # this vec0 table, or predates this module's ``delete`` -- and
            # before there was a delete, no metadata row could ever have been
            # removed, so its MAX is the vec0 table's MAX.
            seed = connection.execute(
                'SELECT COALESCE(MAX(vector_rowid), 0) + 1 FROM epub_derived_vectors WHERE vector_table = ?',
                (table,),
            ).fetchone()
            rowid = int(seed[0])
            connection.execute(
                'INSERT INTO epub_derived_vector_rowids(vector_table, next_rowid) VALUES (?, ?)',
                (table, rowid + 1),
            )
            return rowid
        rowid = int(row[0])
        connection.execute(
            'UPDATE epub_derived_vector_rowids SET next_rowid = ? WHERE vector_table = ?', (rowid + 1, table)
        )
        return rowid

    def _ensure_table(self, connection: Any, table: str, dimension: int) -> None:
        if dimension < 1:
            raise VectorIndexError('embedding vector cannot be empty')
        # DDL inside a transaction is legal in SQLite and is deliberate here:
        # it is the commit that would have been wrong, not the CREATE.
        connection.execute(
            f'CREATE VIRTUAL TABLE IF NOT EXISTS "{table}" USING vec0(embedding float[{dimension}] distance_metric=cosine)'
        )

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
            row = connection.execute('SELECT vec_version()').fetchone()
        except Exception as error:
            raise SQLiteVecUnavailable(f'sqlite-vec failed its SQL health check: {error}') from error
        if row is None or not isinstance(row[0], str):
            raise SQLiteVecUnavailable('sqlite-vec did not return a version')
        return SQLiteVecHealth(version=str(row[0]))

    def _table_exists(self, table: str, connection: Any | None = None) -> bool:
        return (connection if connection is not None else self._connection()).execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    def _write(self) -> Iterator[Any]:
        """Enter the caller's transaction, or open the store's if there is none.

        This is the one place the module's ownership rule is enforced.  The
        connection belongs to the store, so when it is already inside a
        transaction this backend joins it and commits nothing: the opener
        commits, and the opener's rollback undoes this backend's writes with
        the rest.  Only when the connection is in autocommit does this borrow
        the store's own write transaction.
        """
        connection = self._connection()
        if getattr(connection, 'in_transaction', False):
            return nullcontext(connection)
        write = getattr(self._store, '_write', None)
        if callable(write):
            return write()
        return nullcontext(connection)


def _table_name(profile: str, dimension: int) -> str:
    if not profile or not profile.strip():
        raise VectorIndexError('embedding profile cannot be empty')
    digest = sha256(profile.encode('utf-8')).hexdigest()[:16]
    return f'epub_vec_{digest}_{dimension}'


# Every other table name this module interpolates is one _table_name just
# produced.  A delete's is read back out of the database, so it is checked
# against the shape _table_name can emit before it reaches an SQL string.
_TABLE_NAME_PATTERN = re.compile(r'^epub_vec_[0-9a-f]{16}_[1-9][0-9]*$')


def _validated_table_name(table: str) -> str:
    if not _TABLE_NAME_PATTERN.match(table):
        raise VectorIndexError('derived vector metadata names a vector table this backend could not have created')
    return table


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
        raise VectorIndexError('vector cannot be empty')
    values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise VectorIndexError('vector contains a non-finite value')
        values.append(float(value))
    return tuple(values)


def _vector_json(vector: Sequence[float]) -> str:
    return json.dumps(list(vector), separators=(',', ':'))
