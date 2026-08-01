"""Canonical, versioned storage for faithful EPUB source material.

This module deliberately does not depend on Open WebUI's ORM or generic RAG
store.  The rows here are the EPUB domain's source of truth; vector windows and
Batch output are explicitly derived records which always retain a reference to
an immutable source passage.

The public ``EpubStore`` protocol is versioned so a PostgreSQL implementation
can be introduced without making services depend on ``sqlite3``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
import threading
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence
from uuid import uuid4


SCHEMA_VERSION = 1


class IntegrityError(ValueError):
    """Raised before a would-be write violates EPUB source invariants."""


class DuplicateEpubError(IntegrityError):
    """The complete EPUB hash already identifies an existing book version."""


@dataclass(frozen=True)
class VersionCreation:
    """Result of attempting to create a canonical book version."""

    version_id: str
    book_id: str
    epub_sha256: str
    created: bool


class EpubStore(Protocol):
    """Version 1 canonical EPUB repository contract.

    Values returned by implementations are plain dictionaries so API and parser
    layers do not need a database-specific model dependency.
    """

    interface_version: int

    def create_book(self, title: str, *, book_id: str | None = None) -> str: ...

    def create_book_version(
        self,
        book_id: str,
        *,
        epub_bytes: bytes,
        version_id: str | None = None,
        source_locator: str | None = None,
    ) -> VersionCreation: ...

    def add_passages(
        self, version_id: str, passages: Iterable[Mapping[str, Any]]
    ) -> list[str]: ...

    def add_retrieval_unit(
        self, passage_id: str, start_codepoint: int, end_codepoint: int, **metadata: Any
    ) -> str: ...


_MIGRATION_1: tuple[str, ...] = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE books (
        book_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        current_version_id TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE book_versions (
        version_id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE RESTRICT,
        epub_sha256 TEXT NOT NULL UNIQUE,
        source_locator TEXT,
        status TEXT NOT NULL DEFAULT 'PARSING'
            CHECK (status IN ('PARSING', 'READY', 'FAILED', 'ARCHIVED')),
        parser_version TEXT NOT NULL DEFAULT '1',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ready_at TEXT,
        failure_reason TEXT
    )
    """,
    "CREATE INDEX idx_book_versions_book ON book_versions(book_id, created_at)",
    """
    CREATE TABLE epub_blobs (
        version_id TEXT PRIMARY KEY REFERENCES book_versions(version_id) ON DELETE CASCADE,
        content BLOB NOT NULL,
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE toc_nodes (
        toc_node_id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL REFERENCES book_versions(version_id) ON DELETE CASCADE,
        parent_toc_node_id TEXT REFERENCES toc_nodes(toc_node_id) ON DELETE RESTRICT,
        title TEXT NOT NULL,
        href TEXT,
        fragment TEXT,
        spine_index INTEGER NOT NULL CHECK (spine_index >= 0),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        UNIQUE(version_id, ordinal)
    )
    """,
    "CREATE INDEX idx_toc_nodes_version_parent ON toc_nodes(version_id, parent_toc_node_id, ordinal)",
    """
    CREATE TABLE passages (
        passage_id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL REFERENCES book_versions(version_id) ON DELETE RESTRICT,
        toc_node_id TEXT REFERENCES toc_nodes(toc_node_id) ON DELETE RESTRICT,
        source_href TEXT NOT NULL,
        source_fragment TEXT,
        spine_index INTEGER NOT NULL CHECK (spine_index >= 0),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        content_kind TEXT NOT NULL CHECK (
            content_kind IN ('paragraph', 'heading', 'list_item', 'blockquote', 'pre', 'fallback')
        ),
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(version_id, ordinal)
    )
    """,
    "CREATE INDEX idx_passages_version_order ON passages(version_id, spine_index, ordinal)",
    "CREATE INDEX idx_passages_toc ON passages(toc_node_id)",
    """
    CREATE TRIGGER passages_content_is_immutable
    BEFORE UPDATE OF content, content_sha256, version_id, source_href, source_fragment,
                     spine_index, ordinal, content_kind ON passages
    BEGIN
        SELECT RAISE(ABORT, 'EPUB passage source fields are immutable');
    END
    """,
    """
    CREATE TABLE retrieval_units (
        retrieval_unit_id TEXT PRIMARY KEY,
        passage_id TEXT NOT NULL REFERENCES passages(passage_id) ON DELETE CASCADE,
        start_codepoint INTEGER NOT NULL CHECK (start_codepoint >= 0),
        end_codepoint INTEGER NOT NULL CHECK (end_codepoint > start_codepoint),
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        embedding_profile TEXT,
        vector_state TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (vector_state IN ('PENDING', 'READY', 'FAILED')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(passage_id, start_codepoint, end_codepoint, embedding_profile)
    )
    """,
    "CREATE INDEX idx_retrieval_units_passage ON retrieval_units(passage_id)",
    """
    CREATE TABLE concepts (
        concept_id TEXT PRIMARY KEY,
        canonical_name TEXT NOT NULL,
        normalized_name TEXT NOT NULL UNIQUE,
        definition TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'PROVISIONAL'
            CHECK (status IN ('PROVISIONAL', 'APPROVED', 'REJECTED')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE concept_aliases (
        alias_id TEXT PRIMARY KEY,
        concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
        alias TEXT NOT NULL,
        normalized_alias TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL DEFAULT 'MODEL'
            CHECK (source IN ('SEED', 'MODEL', 'ADMIN')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX idx_concept_aliases_concept ON concept_aliases(concept_id)",
    """
    CREATE TABLE concept_mentions (
        mention_id TEXT PRIMARY KEY,
        concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE RESTRICT,
        passage_id TEXT NOT NULL REFERENCES passages(passage_id) ON DELETE RESTRICT,
        start_codepoint INTEGER,
        end_codepoint INTEGER,
        evidence TEXT,
        source TEXT NOT NULL DEFAULT 'MODEL'
            CHECK (source IN ('SEED', 'MODEL', 'ADMIN')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK ((start_codepoint IS NULL AND end_codepoint IS NULL) OR
               (start_codepoint >= 0 AND end_codepoint > start_codepoint)),
        UNIQUE(concept_id, passage_id, start_codepoint, end_codepoint)
    )
    """,
    "CREATE INDEX idx_concept_mentions_concept ON concept_mentions(concept_id, passage_id)",
    "CREATE INDEX idx_concept_mentions_passage ON concept_mentions(passage_id)",
    """
    CREATE TABLE batch_jobs (
        batch_job_id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL REFERENCES book_versions(version_id) ON DELETE RESTRICT,
        provider TEXT NOT NULL,
        provider_job_id TEXT UNIQUE,
        profile_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'DRAFT'
            CHECK (status IN ('DRAFT', 'SUBMITTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
        is_sample INTEGER NOT NULL DEFAULT 0 CHECK (is_sample IN (0, 1)),
        submitted_at TEXT,
        completed_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX idx_batch_jobs_version ON batch_jobs(version_id, created_at)",
    """
    CREATE TABLE batch_items (
        batch_item_id TEXT PRIMARY KEY,
        batch_job_id TEXT NOT NULL REFERENCES batch_jobs(batch_job_id) ON DELETE CASCADE,
        passage_id TEXT NOT NULL REFERENCES passages(passage_id) ON DELETE RESTRICT,
        custom_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'SUBMITTED', 'SUCCEEDED', 'FAILED', 'RETRY')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        request_json TEXT NOT NULL,
        response_json TEXT,
        error_text TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(batch_job_id, custom_id),
        UNIQUE(batch_job_id, passage_id)
    )
    """,
    "CREATE INDEX idx_batch_items_job_status ON batch_items(batch_job_id, status)",
)


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    normalized = " ".join(value.split()).casefold()
    if not normalized:
        raise IntegrityError("a concept name or alias cannot be empty")
    return normalized


class SQLiteEpubStore:
    """SQLite implementation of :class:`EpubStore` interface version 1.

    Each instance owns a database path rather than being a process-wide
    singleton.  This prevents test contamination and lets local profiles use a
    deliberate independent database file.  All connections enforce foreign keys
    and the schema never uses ``INSERT OR REPLACE``: replacing a parent in
    SQLite deletes it before reinserting it and would corrupt dependent rows.
    """

    interface_version = 1
    _schema_lock = threading.Lock()

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._migrate()

    def close(self) -> None:
        """Close this thread's connection, primarily useful for tests/shutdown."""
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            del self._local.connection

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                connection.execute("PRAGMA journal_mode = WAL")
            self._local.connection = connection
        return connection

    def _migrate(self) -> None:
        with self._schema_lock:
            connection = self._connection()
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if 1 not in applied:
                try:
                    connection.execute("BEGIN")
                    for statement in _MIGRATION_1[1:]:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (1,))
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_book(self, title: str, *, book_id: str | None = None) -> str:
        if not title or not title.strip():
            raise IntegrityError("book title cannot be empty")
        resolved_id = book_id or str(uuid4())
        with self._write() as connection:
            connection.execute(
                "INSERT INTO books(book_id, title) VALUES (?, ?)", (resolved_id, title)
            )
        return resolved_id

    def get_book(self, book_id: str) -> dict[str, Any] | None:
        return self._row(
            self._connection().execute("SELECT * FROM books WHERE book_id = ?", (book_id,)).fetchone()
        )

    def list_books(self) -> list[dict[str, Any]]:
        """Return the shared EPUB catalogue without exposing archive bytes."""
        return [
            dict(row)
            for row in self._connection()
            .execute(
                """SELECT b.book_id, b.title, b.current_version_id, b.created_at, b.updated_at,
                           v.status AS current_version_status, v.epub_sha256 AS current_epub_sha256
                     FROM books AS b
                     LEFT JOIN book_versions AS v ON v.version_id = b.current_version_id
                     ORDER BY b.title COLLATE NOCASE, b.book_id"""
            )
            .fetchall()
        ]

    def list_versions(self, book_id: str) -> list[dict[str, Any]]:
        """List one book's versions in a stable order, excluding raw EPUB blobs."""
        return [
            dict(row)
            for row in self._connection()
            .execute(
                """SELECT version_id, book_id, epub_sha256, source_locator, status,
                           parser_version, created_at, ready_at, failure_reason
                     FROM book_versions
                     WHERE book_id = ?
                     ORDER BY created_at, version_id""",
                (book_id,),
            )
            .fetchall()
        ]

    def find_version_by_sha256(self, epub_sha256: str) -> dict[str, Any] | None:
        """Look up an already-ingested complete archive by its full SHA-256."""
        if len(epub_sha256) != 64:
            raise IntegrityError("EPUB SHA-256 must be a complete 64-character digest")
        return self._row(
            self._connection()
            .execute(
                """SELECT v.version_id, v.book_id, v.epub_sha256, v.status, b.title AS book_title
                     FROM book_versions AS v JOIN books AS b ON b.book_id = v.book_id
                     WHERE v.epub_sha256 = ?""",
                (epub_sha256,),
            )
            .fetchone()
        )

    def create_book_version(
        self,
        book_id: str,
        *,
        epub_bytes: bytes,
        version_id: str | None = None,
        source_locator: str | None = None,
    ) -> VersionCreation:
        """Persist a new exact EPUB version, or return the canonical duplicate.

        The file blob and full SHA-256 are stored together.  A duplicate hash is
        deliberately not attached to a same-titled (or any other) book because
        its canonical identity is the already-existing version.
        """
        if not isinstance(epub_bytes, bytes) or not epub_bytes:
            raise IntegrityError("epub_bytes must contain the complete EPUB archive")
        epub_hash = sha256(epub_bytes).hexdigest()
        resolved_id = version_id or str(uuid4())
        with self._write() as connection:
            if connection.execute("SELECT 1 FROM books WHERE book_id = ?", (book_id,)).fetchone() is None:
                raise IntegrityError(f"unknown book_id: {book_id}")
            duplicate = connection.execute(
                "SELECT version_id, book_id FROM book_versions WHERE epub_sha256 = ?", (epub_hash,)
            ).fetchone()
            if duplicate is not None:
                return VersionCreation(
                    version_id=duplicate["version_id"],
                    book_id=duplicate["book_id"],
                    epub_sha256=epub_hash,
                    created=False,
                )
            connection.execute(
                """INSERT INTO book_versions(version_id, book_id, epub_sha256, source_locator)
                   VALUES (?, ?, ?, ?)""",
                (resolved_id, book_id, epub_hash, source_locator),
            )
            connection.execute(
                "INSERT INTO epub_blobs(version_id, content, byte_count, sha256) VALUES (?, ?, ?, ?)",
                (resolved_id, epub_bytes, len(epub_bytes), epub_hash),
            )
        return VersionCreation(resolved_id, book_id, epub_hash, True)

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        return self._row(
            self._connection().execute(
                "SELECT * FROM book_versions WHERE version_id = ?", (version_id,)
            ).fetchone()
        )

    def get_epub_bytes(self, version_id: str) -> bytes | None:
        row = self._connection().execute(
            "SELECT content FROM epub_blobs WHERE version_id = ?", (version_id,)
        ).fetchone()
        return bytes(row[0]) if row is not None else None

    def set_version_status(
        self, version_id: str, status: str, *, failure_reason: str | None = None
    ) -> None:
        if status not in {"PARSING", "READY", "FAILED", "ARCHIVED"}:
            raise IntegrityError(f"invalid book version status: {status}")
        with self._write() as connection:
            cursor = connection.execute(
                """UPDATE book_versions
                   SET status = ?, failure_reason = ?,
                       ready_at = CASE WHEN ? = 'READY' THEN CURRENT_TIMESTAMP ELSE ready_at END
                   WHERE version_id = ?""",
                (status, failure_reason, status, version_id),
            )
            if cursor.rowcount != 1:
                raise IntegrityError(f"unknown version_id: {version_id}")
            if status == "READY":
                connection.execute(
                    """UPDATE books SET current_version_id = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE book_id = (SELECT book_id FROM book_versions WHERE version_id = ?)""",
                    (version_id, version_id),
                )

    def add_toc_nodes(
        self, version_id: str, nodes: Iterable[Mapping[str, Any]]
    ) -> list[str]:
        ids: list[str] = []
        with self._write() as connection:
            self._require_version(connection, version_id)
            for node in nodes:
                node_id = str(node.get("toc_node_id") or uuid4())
                parent_id = node.get("parent_toc_node_id")
                if parent_id:
                    parent = connection.execute(
                        "SELECT version_id FROM toc_nodes WHERE toc_node_id = ?", (parent_id,)
                    ).fetchone()
                    if parent is None or parent["version_id"] != version_id:
                        raise IntegrityError("a TOC node parent must belong to the same version")
                connection.execute(
                    """INSERT INTO toc_nodes(
                         toc_node_id, version_id, parent_toc_node_id, title, href, fragment, spine_index, ordinal
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        node_id,
                        version_id,
                        parent_id,
                        self._required_text(node, "title"),
                        node.get("href"),
                        node.get("fragment"),
                        self._nonnegative_int(node, "spine_index"),
                        self._nonnegative_int(node, "ordinal"),
                    ),
                )
                ids.append(node_id)
        return ids

    def add_passages(
        self, version_id: str, passages: Iterable[Mapping[str, Any]]
    ) -> list[str]:
        """Persist untouched source passages once.  Existing rows are never replaced."""
        ids: list[str] = []
        with self._write() as connection:
            self._require_version(connection, version_id)
            for passage in passages:
                passage_id = str(passage.get("passage_id") or uuid4())
                toc_node_id = passage.get("toc_node_id")
                if toc_node_id:
                    toc = connection.execute(
                        "SELECT version_id FROM toc_nodes WHERE toc_node_id = ?", (toc_node_id,)
                    ).fetchone()
                    if toc is None or toc["version_id"] != version_id:
                        raise IntegrityError("a passage TOC node must belong to the same version")
                content = self._required_text(passage, "content", allow_empty=True)
                supplied_hash = passage.get("content_sha256")
                calculated_hash = _sha256_text(content)
                if supplied_hash is not None and supplied_hash != calculated_hash:
                    raise IntegrityError("content_sha256 does not match UTF-8 source content")
                kind = str(passage.get("content_kind", "paragraph"))
                connection.execute(
                    """INSERT INTO passages(
                         passage_id, version_id, toc_node_id, source_href, source_fragment, spine_index,
                         ordinal, content_kind, content, content_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        passage_id,
                        version_id,
                        toc_node_id,
                        self._required_text(passage, "source_href"),
                        passage.get("source_fragment"),
                        self._nonnegative_int(passage, "spine_index"),
                        self._nonnegative_int(passage, "ordinal"),
                        kind,
                        content,
                        calculated_hash,
                    ),
                )
                ids.append(passage_id)
        return ids

    def get_passage(self, passage_id: str) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT * FROM passages WHERE passage_id = ?", (passage_id,)
        ).fetchone()
        return self._search_row_with_toc(dict(row)) if row is not None else None

    def list_passages(self, version_id: str) -> list[dict[str, Any]]:
        return [
            self._search_row_with_toc(dict(row))
            for row in self._connection()
            .execute(
                "SELECT * FROM passages WHERE version_id = ? ORDER BY spine_index, ordinal", (version_id,)
            )
            .fetchall()
        ]

    def add_retrieval_unit(
        self,
        passage_id: str,
        start_codepoint: int,
        end_codepoint: int,
        *,
        embedding_profile: str | None = None,
        vector_state: str = "PENDING",
        retrieval_unit_id: str | None = None,
    ) -> str:
        """Store a verified, continuous derived window of one source passage."""
        if vector_state not in {"PENDING", "READY", "FAILED"}:
            raise IntegrityError(f"invalid vector state: {vector_state}")
        if not isinstance(start_codepoint, int) or not isinstance(end_codepoint, int):
            raise IntegrityError("excerpt offsets must be integer Unicode code-point positions")
        with self._write() as connection:
            passage = connection.execute(
                "SELECT content FROM passages WHERE passage_id = ?", (passage_id,)
            ).fetchone()
            if passage is None:
                raise IntegrityError(f"unknown passage_id: {passage_id}")
            content = passage["content"]
            if start_codepoint < 0 or end_codepoint <= start_codepoint or end_codepoint > len(content):
                raise IntegrityError("retrieval-unit offsets must identify a non-empty source substring")
            excerpt = content[start_codepoint:end_codepoint]
            unit_id = retrieval_unit_id or str(uuid4())
            connection.execute(
                """INSERT INTO retrieval_units(
                     retrieval_unit_id, passage_id, start_codepoint, end_codepoint, content, content_sha256,
                     embedding_profile, vector_state
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    unit_id,
                    passage_id,
                    start_codepoint,
                    end_codepoint,
                    excerpt,
                    _sha256_text(excerpt),
                    embedding_profile,
                    vector_state,
                ),
            )
        return unit_id

    def get_retrieval_unit(self, retrieval_unit_id: str) -> dict[str, Any] | None:
        return self._row(
            self._connection()
            .execute("SELECT * FROM retrieval_units WHERE retrieval_unit_id = ?", (retrieval_unit_id,))
            .fetchone()
        )

    def list_concept_terms(self) -> list[dict[str, Any]]:
        """Return canonical names and aliases for the in-memory Tier-1 matcher."""
        return [
            dict(row)
            for row in self._connection()
            .execute(
                """SELECT c.concept_id, c.canonical_name, a.alias AS term
                   FROM concepts AS c
                   JOIN concept_aliases AS a ON a.concept_id = c.concept_id
                   WHERE c.status != 'REJECTED'
                   ORDER BY c.canonical_name, a.alias"""
            )
            .fetchall()
        ]

    def count_concept_occurrences(self, concept_ids: Sequence[str]) -> int:
        if not concept_ids:
            return 0
        placeholders = ", ".join("?" for _ in concept_ids)
        row = self._connection().execute(
            f"SELECT COUNT(*) AS count FROM concept_mentions WHERE concept_id IN ({placeholders})",
            tuple(concept_ids),
        ).fetchone()
        return int(row["count"])

    def list_concept_occurrences(
        self, concept_ids: Sequence[str], *, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        """Page all matching graph occurrences in a stable source order."""
        if not concept_ids:
            return []
        if offset < 0 or limit < 1:
            raise IntegrityError("concept occurrence pagination values are invalid")
        placeholders = ", ".join("?" for _ in concept_ids)
        rows = self._connection().execute(
            f"""SELECT m.mention_id, m.concept_id, m.passage_id, m.start_codepoint,
                       m.end_codepoint, c.canonical_name, p.content, p.content_sha256,
                       p.toc_node_id, b.title AS book_title
                FROM concept_mentions AS m
                JOIN concepts AS c ON c.concept_id = m.concept_id
                JOIN passages AS p ON p.passage_id = m.passage_id
                JOIN book_versions AS v ON v.version_id = p.version_id
                JOIN books AS b ON b.book_id = v.book_id
                WHERE m.concept_id IN ({placeholders})
                ORDER BY p.spine_index, p.ordinal, m.start_codepoint, m.mention_id
                LIMIT ? OFFSET ?""",
            (*concept_ids, limit, offset),
        ).fetchall()
        return [self._search_row_with_toc(dict(row)) for row in rows]

    def get_search_passage(self, passage_id: str) -> dict[str, Any] | None:
        row = self._connection().execute(
            """SELECT p.passage_id, p.content, p.content_sha256, p.toc_node_id,
                       b.title AS book_title
                FROM passages AS p
                JOIN book_versions AS v ON v.version_id = p.version_id
                JOIN books AS b ON b.book_id = v.book_id
                WHERE p.passage_id = ?""",
            (passage_id,),
        ).fetchone()
        return self._search_row_with_toc(dict(row)) if row is not None else None

    def matched_concept_names(self, passage_id: str, concept_ids: Sequence[str]) -> list[str]:
        if not concept_ids:
            return []
        placeholders = ", ".join("?" for _ in concept_ids)
        rows = self._connection().execute(
            f"""SELECT DISTINCT c.canonical_name
                FROM concept_mentions AS m
                JOIN concepts AS c ON c.concept_id = m.concept_id
                WHERE m.passage_id = ? AND m.concept_id IN ({placeholders})
                ORDER BY c.canonical_name""",
            (passage_id, *concept_ids),
        ).fetchall()
        return [str(row["canonical_name"]) for row in rows]

    def _search_row_with_toc(self, row: dict[str, Any]) -> dict[str, Any]:
        row["toc_path"] = self._toc_path(row.pop("toc_node_id", None))
        return row

    def _toc_path(self, toc_node_id: str | None) -> tuple[str, ...]:
        if not toc_node_id:
            return ()
        path: list[str] = []
        current_id: str | None = toc_node_id
        # The schema validates parent ownership.  The guard still protects a
        # read path from an accidental legacy cycle without ever looping.
        visited: set[str] = set()
        while current_id is not None:
            if current_id in visited:
                raise IntegrityError("TOC hierarchy contains a cycle")
            visited.add(current_id)
            row = self._connection().execute(
                "SELECT title, parent_toc_node_id FROM toc_nodes WHERE toc_node_id = ?", (current_id,)
            ).fetchone()
            if row is None:
                raise IntegrityError("passage refers to a missing TOC node")
            path.append(str(row["title"]))
            current_id = row["parent_toc_node_id"]
        path.reverse()
        return tuple(path)

    def upsert_concept(
        self,
        canonical_name: str,
        *,
        aliases: Iterable[str] = (),
        definition: str = "",
        status: str = "PROVISIONAL",
        concept_id: str | None = None,
        alias_source: str = "MODEL",
    ) -> str:
        """Create/update a concept without replacing its foreign-key parent row."""
        if status not in {"PROVISIONAL", "APPROVED", "REJECTED"}:
            raise IntegrityError(f"invalid concept status: {status}")
        if alias_source not in {"SEED", "MODEL", "ADMIN"}:
            raise IntegrityError(f"invalid alias source: {alias_source}")
        normalized_name = _normalize(canonical_name)
        requested_aliases: dict[str, str] = {normalized_name: canonical_name}
        for alias in aliases:
            requested_aliases[_normalize(alias)] = alias
        with self._write() as connection:
            existing = connection.execute(
                "SELECT concept_id FROM concepts WHERE normalized_name = ?", (normalized_name,)
            ).fetchone()
            resolved_id = concept_id or (existing["concept_id"] if existing else str(uuid4()))
            if existing is not None and concept_id is not None and existing["concept_id"] != concept_id:
                raise IntegrityError("canonical name already belongs to a different concept")
            canonical_alias_owner = connection.execute(
                "SELECT concept_id FROM concept_aliases WHERE normalized_alias = ?", (normalized_name,)
            ).fetchone()
            if canonical_alias_owner is not None and canonical_alias_owner["concept_id"] != resolved_id:
                raise IntegrityError("canonical name is already an alias of a different concept")
            if existing is None:
                connection.execute(
                    """INSERT INTO concepts(concept_id, canonical_name, normalized_name, definition, status)
                       VALUES (?, ?, ?, ?, ?)""",
                    (resolved_id, canonical_name, normalized_name, definition, status),
                )
            else:
                connection.execute(
                    """UPDATE concepts SET canonical_name = ?, definition = ?, status = ?,
                               updated_at = CURRENT_TIMESTAMP WHERE concept_id = ?""",
                    (canonical_name, definition, status, resolved_id),
                )
            for normalized_alias, alias in requested_aliases.items():
                owner = connection.execute(
                    "SELECT concept_id FROM concept_aliases WHERE normalized_alias = ?", (normalized_alias,)
                ).fetchone()
                if owner is not None and owner["concept_id"] != resolved_id:
                    raise IntegrityError(f"alias already belongs to another concept: {alias}")
                if owner is None:
                    connection.execute(
                        """INSERT INTO concept_aliases(alias_id, concept_id, alias, normalized_alias, source)
                           VALUES (?, ?, ?, ?, ?)""",
                        (str(uuid4()), resolved_id, alias, normalized_alias, alias_source),
                    )
        return resolved_id

    def add_concept_mention(
        self,
        concept_id: str,
        passage_id: str,
        *,
        start_codepoint: int | None = None,
        end_codepoint: int | None = None,
        evidence: str | None = None,
        source: str = "MODEL",
        mention_id: str | None = None,
    ) -> str:
        """Link an existing concept to an existing source passage in FK-safe order."""
        if source not in {"SEED", "MODEL", "ADMIN"}:
            raise IntegrityError(f"invalid mention source: {source}")
        if (start_codepoint is None) != (end_codepoint is None):
            raise IntegrityError("mention offsets must be supplied together")
        with self._write() as connection:
            if connection.execute("SELECT 1 FROM concepts WHERE concept_id = ?", (concept_id,)).fetchone() is None:
                raise IntegrityError(f"unknown concept_id: {concept_id}")
            passage = connection.execute(
                "SELECT content FROM passages WHERE passage_id = ?", (passage_id,)
            ).fetchone()
            if passage is None:
                raise IntegrityError(f"unknown passage_id: {passage_id}")
            if start_codepoint is not None:
                if start_codepoint < 0 or end_codepoint is None or end_codepoint <= start_codepoint or end_codepoint > len(passage["content"]):
                    raise IntegrityError("mention offsets must identify a non-empty source substring")
                expected = passage["content"][start_codepoint:end_codepoint]
                if evidence is not None and evidence != expected:
                    raise IntegrityError("mention evidence must equal the source substring")
                evidence = expected
            mention = mention_id or str(uuid4())
            connection.execute(
                """INSERT INTO concept_mentions(
                     mention_id, concept_id, passage_id, start_codepoint, end_codepoint, evidence, source
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (mention, concept_id, passage_id, start_codepoint, end_codepoint, evidence, source),
            )
        return mention

    def create_batch_job(
        self,
        version_id: str,
        *,
        provider: str,
        profile_name: str,
        is_sample: bool = False,
        batch_job_id: str | None = None,
    ) -> str:
        """Create a durable job record before any provider submission occurs."""
        if not provider.strip() or not profile_name.strip():
            raise IntegrityError("provider and profile_name cannot be empty")
        job_id = batch_job_id or str(uuid4())
        with self._write() as connection:
            self._require_version(connection, version_id)
            connection.execute(
                """INSERT INTO batch_jobs(batch_job_id, version_id, provider, profile_name, is_sample)
                   VALUES (?, ?, ?, ?, ?)""",
                (job_id, version_id, provider, profile_name, int(is_sample)),
            )
        return job_id

    def add_batch_item(
        self,
        batch_job_id: str,
        passage_id: str,
        *,
        custom_id: str,
        request: Mapping[str, Any],
        batch_item_id: str | None = None,
    ) -> str:
        """Persist a request item and verify that passage and job share a version."""
        if not custom_id:
            raise IntegrityError("batch custom_id cannot be empty")
        item_id = batch_item_id or str(uuid4())
        with self._write() as connection:
            job = connection.execute(
                "SELECT version_id FROM batch_jobs WHERE batch_job_id = ?", (batch_job_id,)
            ).fetchone()
            passage = connection.execute(
                "SELECT version_id FROM passages WHERE passage_id = ?", (passage_id,)
            ).fetchone()
            if job is None:
                raise IntegrityError(f"unknown batch_job_id: {batch_job_id}")
            if passage is None or passage["version_id"] != job["version_id"]:
                raise IntegrityError("a batch item passage must belong to the job's book version")
            connection.execute(
                """INSERT INTO batch_items(batch_item_id, batch_job_id, passage_id, custom_id, request_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (item_id, batch_job_id, passage_id, custom_id, json.dumps(request, ensure_ascii=False, sort_keys=True)),
            )
        return item_id

    @staticmethod
    def _required_text(
        values: Mapping[str, Any], key: str, *, allow_empty: bool = False
    ) -> str:
        value = values.get(key)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise IntegrityError(f"{key} must be {'a string' if allow_empty else 'a non-empty string'}")
        return value

    @staticmethod
    def _nonnegative_int(values: Mapping[str, Any], key: str) -> int:
        value = values.get(key)
        if not isinstance(value, int) or value < 0:
            raise IntegrityError(f"{key} must be a non-negative integer")
        return value

    @staticmethod
    def _require_version(connection: sqlite3.Connection, version_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM book_versions WHERE version_id = ?", (version_id,)
        ).fetchone() is None:
            raise IntegrityError(f"unknown version_id: {version_id}")
