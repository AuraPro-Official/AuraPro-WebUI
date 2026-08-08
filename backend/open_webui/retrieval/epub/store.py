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

from .overlay import (
    OVERLAY_FORMAT_VERSION,
    ConceptOverlay,
    OverlayConcept,
    OverlayMention,
    OverlayRelation,
    OverlaySpan,
    build_overlay,
    normalize_concept_key,
    passage_fingerprint,
)


SCHEMA_VERSION = 9


class IntegrityError(ValueError):
    """Raised before a would-be write violates EPUB source invariants."""


class DuplicateEpubError(IntegrityError):
    """The complete EPUB hash already identifies an existing book version."""


class UnknownConceptError(IntegrityError):
    """A referenced concept identifier does not exist in the graph."""


class OverlayRejected(IntegrityError):
    """An analysis overlay failed a source-fidelity gate and was not applied.

    ``reason`` is a stable, content-free class name so an operator surface can
    report *why* an artifact was refused without echoing passage text, a
    concept label, or an offset back to the caller.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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

    def list_retrieval_units(self, passage_id: str) -> list[dict[str, Any]]: ...

    def list_retrieval_units_for_version(self, version_id: str) -> list[dict[str, Any]]: ...

    def set_retrieval_unit_vector_state(self, retrieval_unit_id: str, vector_state: str) -> None: ...

    def add_concept_relation(
        self,
        version_id: str,
        subject_concept_id: str,
        predicate: str,
        object_concept_id: str,
        *,
        evidence: Sequence[Mapping[str, Any]],
        status: str = "PROVISIONAL",
        source: str = "MODEL",
    ) -> str: ...

    def list_concept_relation_neighbors(
        self, concept_ids: Sequence[str], *, predicates: Sequence[str] = ("HAS_PART",)
    ) -> list[dict[str, Any]]: ...

    def list_concept_relation_assertions(
        self,
        *,
        status: str | None = None,
        version_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    def count_concept_relation_assertions(
        self, *, status: str | None = None, version_id: str | None = None
    ) -> int: ...

    def set_concept_relation_assertion_status(self, assertion_id: str, status: str) -> None: ...

    def export_concept_overlay(self, version_id: str) -> ConceptOverlay: ...

    def apply_overlay(
        self, overlay: ConceptOverlay, *, version_id: str | None = None
    ) -> dict[str, Any]: ...


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


_RELATION_PREDICATES = (
    "HAS_PART",
    "PRECEDES",
    "PREREQUISITE",
    "CAUSES",
    "CONTRASTS",
    "ELABORATES",
)

_MIGRATION_2: tuple[str, ...] = (
    f"""
    CREATE TABLE concept_relations (
        relation_id TEXT PRIMARY KEY,
        subject_concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE RESTRICT,
        predicate TEXT NOT NULL CHECK (predicate IN ({", ".join(repr(value) for value in _RELATION_PREDICATES)})),
        object_concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (subject_concept_id <> object_concept_id),
        UNIQUE(subject_concept_id, predicate, object_concept_id)
    )
    """,
    "CREATE INDEX idx_concept_relations_subject ON concept_relations(subject_concept_id, predicate)",
    "CREATE INDEX idx_concept_relations_object ON concept_relations(object_concept_id, predicate)",
    """
    CREATE TABLE concept_relation_assertions (
        assertion_id TEXT PRIMARY KEY,
        relation_id TEXT NOT NULL REFERENCES concept_relations(relation_id) ON DELETE CASCADE,
        version_id TEXT NOT NULL REFERENCES book_versions(version_id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'PROVISIONAL'
            CHECK (status IN ('PROVISIONAL', 'APPROVED', 'REJECTED')),
        source TEXT NOT NULL DEFAULT 'MODEL'
            CHECK (source IN ('MODEL', 'ADMIN')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(relation_id, version_id, source)
    )
    """,
    "CREATE INDEX idx_relation_assertions_relation ON concept_relation_assertions(relation_id, status)",
    "CREATE INDEX idx_relation_assertions_version ON concept_relation_assertions(version_id, status)",
    """
    CREATE TABLE concept_relation_evidence (
        relation_evidence_id TEXT PRIMARY KEY,
        assertion_id TEXT NOT NULL REFERENCES concept_relation_assertions(assertion_id) ON DELETE CASCADE,
        passage_id TEXT NOT NULL REFERENCES passages(passage_id) ON DELETE RESTRICT,
        start_codepoint INTEGER NOT NULL CHECK (start_codepoint >= 0),
        end_codepoint INTEGER NOT NULL CHECK (end_codepoint > start_codepoint),
        evidence TEXT NOT NULL,
        UNIQUE(assertion_id, passage_id, start_codepoint, end_codepoint)
    )
    """,
    "CREATE INDEX idx_concept_relation_evidence_assertion ON concept_relation_evidence(assertion_id)",
)


_MIGRATION_3: tuple[str, ...] = (
    """
    ALTER TABLE batch_jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'CONCEPT_MENTIONS'
        CHECK (job_kind IN ('CONCEPT_MENTIONS', 'SECTION_GRAPH'))
    """,
    "CREATE INDEX idx_batch_jobs_kind ON batch_jobs(job_kind, status)",
)


_MIGRATION_4: tuple[str, ...] = (
    # A failed Batch item deliberately keeps ``response_json`` NULL, so the
    # durable record retains only a failure class string.  This column holds a
    # content-free numeric record of *why* grounding rejected the result:
    # counts, code point lengths and booleans only, never source text,
    # evidence, anchors, prompts, model output, or raw provider errors.  It is
    # nullable because most failures (provider transport errors, missing
    # terminal results) have no such measurement, and because rows written
    # before this migration cannot gain one retroactively.
    "ALTER TABLE batch_items ADD COLUMN failure_diagnostics_json TEXT",
)


_MIGRATION_5: tuple[str, ...] = (
    # An administrator merge folds one concept into another and deletes the
    # source row, so the graph itself can no longer answer "what was merged
    # here, by whom".  This audit table deliberately holds identifiers, the
    # source's own concept label, the acting administrator and the time.  A
    # canonical name is a concept label, never source passage text, evidence,
    # a prompt or model output, and nothing else from the merge is copied.
    # ``source_concept_id`` cannot be a foreign key: its row is gone by design.
    """
    CREATE TABLE concept_merges (
        concept_merge_id TEXT PRIMARY KEY,
        target_concept_id TEXT NOT NULL REFERENCES concepts(concept_id) ON DELETE RESTRICT,
        source_concept_id TEXT NOT NULL,
        source_canonical_name TEXT NOT NULL,
        merged_by TEXT NOT NULL,
        merged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX idx_concept_merges_target ON concept_merges(target_concept_id, merged_at)",
    "CREATE INDEX idx_concept_merges_source ON concept_merges(source_concept_id)",
)


_MIGRATION_6: tuple[str, ...] = (
    # ``profile_name`` pins the *model* snapshot; the extraction *instruction*
    # lived only inside each item's request envelope.  The durable sample
    # review gate could therefore let an approval of one prompt profile unlock
    # a full run on a different one, which is exactly what the review exists
    # to prevent.  This column records the requested prompt profile identifier
    # on the job itself so the gate can bind to it.
    #
    # It is an identifier only: never instruction text, never model output.
    # It is nullable because rows written before this migration cannot gain a
    # value inside a SQL migration -- deriving one requires the registered
    # extraction policy, which this module must not import.  A NULL is read as
    # "unknown", and the gate treats unknown as "does not match", so a
    # legacy row unlocks nothing until an administrator runs the service-level
    # backfill.
    "ALTER TABLE batch_jobs ADD COLUMN prompt_profile TEXT",
)


_MIGRATION_7: tuple[str, ...] = (
    # A section-graph relation whose two endpoints resolve to one concept is
    # skipped rather than failing its packet, because it is what an
    # administrator merge looks like from the far side of an offline Batch, not
    # a defect in the output.  ``response_json`` cannot carry that count: it is
    # the grounded model output, and a replay of the same result has to
    # serialize byte-identically for ingest to stay idempotent.  The count is
    # therefore a property of the *write*, and lives on the item row.
    #
    # It is an integer or nothing, enforced by the schema rather than by a
    # validator, so this column cannot carry a concept name, an evidence string
    # or any other source text even from a hand-edited or restored database.
    # NULL means "not measured": every item written before this migration, and
    # every CONCEPT_MENTIONS item, which has no relations to skip.  A
    # SECTION_GRAPH success always stores a number, so a genuine zero is
    # distinguishable from an absent measurement.
    """
    ALTER TABLE batch_items ADD COLUMN skipped_self_relations INTEGER
        CHECK (skipped_self_relations IS NULL
               OR (typeof(skipped_self_relations) = 'integer' AND skipped_self_relations >= 0))
    """,
)


_MIGRATION_8: tuple[str, ...] = (
    # A span shorter than its profile's enforced evidence floor is dropped from
    # the payload during grounding rather than failing its item, exactly as a
    # merged-away self-relation is skipped rather than failing its packet
    # (SDD 4.2.2 points 6 and 7).  One unusable citation must not discard the
    # valid concepts, mentions and relations around it -- measured on the
    # full-book section-graph run, that behaviour cost 140 concepts, 140
    # mentions and 105 relations across 13 of 43 packets.
    #
    # The count cannot live in ``response_json``: that column stores the
    # grounded payload, from which the dropped spans are by definition absent,
    # and it must serialize byte-identically on replay for ingest to stay
    # idempotent.  How many spans the grounding pass removed is a fact about
    # that pass, so it lives on the item row beside ``skipped_self_relations``.
    #
    # One counter, not one per span kind: a dropped concept mention and a
    # dropped relation-evidence span are the same defect with the same fix, and
    # both are removed by the same resolver.  It is an integer or nothing,
    # enforced by the schema rather than by a validator, so the column cannot
    # carry an evidence string even in a hand-edited or restored database.
    # NULL means "not measured": every item written before this migration, and
    # every item whose payload was never put through a grounding pass.
    """
    ALTER TABLE batch_items ADD COLUMN skipped_short_evidence INTEGER
        CHECK (skipped_short_evidence IS NULL
               OR (typeof(skipped_short_evidence) = 'integer' AND skipped_short_evidence >= 0))
    """,
)


_MIGRATION_9: tuple[str, ...] = (
    # ``merge_concepts`` is one-way, and an administrator merge is a fallible
    # judgement: two have already had to be undone after review.  The only
    # recovery was restoring a backup and replaying, which stops working the
    # moment a later job postdates the backup.  ``split_concept`` is the
    # correction path, and like a merge it deletes nothing from the graph that
    # would let the graph answer "who decided this, and when" afterwards -- the
    # new concept looks exactly like any other concept once it exists.
    #
    # This is a separate table rather than a typed row in ``concept_merges``
    # because the two records are not the same shape read in opposite
    # directions.  A merge audit names one surviving concept and one identifier
    # whose row is *gone*; a split audit names two concepts that both exist.
    # ``concept_merges.target_concept_id`` is a real foreign key that means "the
    # survivor", and ``merge_concepts`` repoints it when that survivor is itself
    # folded onward -- a rule that is correct for merge lineage and simply false
    # for a split, whose source is not lineage but the concept an administrator
    # chose to divide.  Discriminating the two inside one table would make every
    # column's meaning depend on the discriminator and would put split rows in
    # the path of that UPDATE.
    #
    # Neither identifier is a foreign key, for the reason the audit exists at
    # all.  A ``RESTRICT`` reference to ``concepts`` would let an audit row veto
    # the very operations it records: a split concept could never afterwards be
    # merged, and a merge could never absorb a concept that had been split off.
    # ``concept_merges.source_concept_id`` already establishes that an audit row
    # may name an identifier the schema does not police; here both do, so the
    # record survives whatever the administrator decides next.
    #
    # It holds identifiers, the new concept's own label, the acting
    # administrator and the time.  A canonical name is a concept label, never
    # source passage text, evidence, a prompt or model output, and nothing else
    # from the split is copied.  Which aliases and mentions moved is not
    # recorded: reconstructing them is exactly the derivation this feature
    # refuses to fake, and the counts an operator needs are returned by the call.
    """
    CREATE TABLE concept_splits (
        concept_split_id TEXT PRIMARY KEY,
        source_concept_id TEXT NOT NULL,
        new_concept_id TEXT NOT NULL,
        new_canonical_name TEXT NOT NULL,
        split_by TEXT NOT NULL,
        split_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (source_concept_id <> new_concept_id)
    )
    """,
    "CREATE INDEX idx_concept_splits_source ON concept_splits(source_concept_id, split_at)",
    "CREATE INDEX idx_concept_splits_new ON concept_splits(new_concept_id)",
)


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    # The folding rule is shared with the portable overlay artifact, whose
    # concept key *is* ``normalized_name``.  A second copy of the rule here
    # would eventually disagree and reattach an imported analysis to the wrong
    # concept, so the pure module owns it and this wrapper only adds the
    # store's own emptiness invariant.
    if not isinstance(value, str):
        raise IntegrityError("a concept name or alias must be a string")
    normalized = normalize_concept_key(value)
    if not normalized:
        raise IntegrityError("a concept name or alias cannot be empty")
    return normalized


def _span_contains(
    start: Any, end: Any, other_start: Any, other_end: Any
) -> bool:
    """Report whether span ``[start, end)`` covers span ``[other_start, other_end)``.

    Equal spans count as containment: this is the attribution rule that puts
    every concept anchored on a span onto the one row that survives the
    de-duplication in :meth:`SQLiteEpubStore._occurrence_span_source`.  An
    unanchored (``NULL``) mention is not a span, so it is attributed only to
    the unanchored row for the same passage.
    """
    if start is None or end is None:
        return other_start is None or other_end is None
    if other_start is None or other_end is None:
        return False
    return int(other_start) >= int(start) and int(other_end) <= int(end)


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
            migrations = (
                (1, _MIGRATION_1),
                (2, _MIGRATION_2),
                (3, _MIGRATION_3),
                (4, _MIGRATION_4),
                (5, _MIGRATION_5),
                (6, _MIGRATION_6),
                (7, _MIGRATION_7),
                (8, _MIGRATION_8),
                (9, _MIGRATION_9),
            )
            try:
                connection.execute("BEGIN")
                for version, statements in migrations:
                    if version in applied:
                        continue
                    for statement in statements[1:] if version == 1 else statements:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
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
            # Window generation is retry-safe.  SQLite UNIQUE considers two
            # NULL embedding profiles distinct, so use ``IS`` explicitly
            # rather than depending only on the schema constraint.
            existing = connection.execute(
                """SELECT retrieval_unit_id FROM retrieval_units
                   WHERE passage_id = ? AND start_codepoint = ? AND end_codepoint = ?
                     AND embedding_profile IS ?""",
                (passage_id, start_codepoint, end_codepoint, embedding_profile),
            ).fetchone()
            if existing is not None:
                return str(existing["retrieval_unit_id"])
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

    def list_retrieval_units(self, passage_id: str) -> list[dict[str, Any]]:
        """Return stable derived windows for one immutable source passage."""
        return [
            dict(row)
            for row in self._connection()
            .execute(
                """SELECT * FROM retrieval_units WHERE passage_id = ?
                   ORDER BY start_codepoint, end_codepoint, retrieval_unit_id""",
                (passage_id,),
            )
            .fetchall()
        ]

    def list_retrieval_units_for_version(self, version_id: str) -> list[dict[str, Any]]:
        """Return a version's derived windows in stable source order.

        This is intentionally a derived-index read surface: it joins through
        immutable passages but never exposes a way to replace their source
        content.  Administrators use it to run a version-level indexing job
        without having to discover opaque retrieval-unit identifiers.
        """
        return [
            dict(row)
            for row in self._connection()
            .execute(
                """SELECT units.*
                   FROM retrieval_units AS units
                   JOIN passages AS passages ON passages.passage_id = units.passage_id
                   WHERE passages.version_id = ?
                   ORDER BY passages.spine_index, passages.ordinal,
                            units.start_codepoint, units.end_codepoint, units.retrieval_unit_id""",
                (version_id,),
            )
            .fetchall()
        ]

    def set_retrieval_unit_vector_state(self, retrieval_unit_id: str, vector_state: str) -> None:
        """Persist an indexing outcome without changing source-window fields."""
        if vector_state not in {"PENDING", "READY", "FAILED"}:
            raise IntegrityError(f"invalid vector state: {vector_state}")
        with self._write() as connection:
            changed = connection.execute(
                "UPDATE retrieval_units SET vector_state = ? WHERE retrieval_unit_id = ?",
                (vector_state, retrieval_unit_id),
            ).rowcount
            if changed != 1:
                raise IntegrityError(f"unknown retrieval_unit_id: {retrieval_unit_id}")

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

    def add_concept_relation(
        self,
        version_id: str,
        subject_concept_id: str,
        predicate: str,
        object_concept_id: str,
        *,
        evidence: Sequence[Mapping[str, Any]],
        status: str = "PROVISIONAL",
        source: str = "MODEL",
        relation_id: str | None = None,
    ) -> str:
        """Persist a global relation assertion with version-scoped evidence.

        The relation's endpoints must already be concepts mentioned in this
        immutable EPUB version.  This prevents a model result from introducing
        a free-floating node or connecting concepts from unrelated books.
        """
        if predicate not in _RELATION_PREDICATES:
            raise IntegrityError(f"unsupported concept relation predicate: {predicate}")
        if status not in {"PROVISIONAL", "APPROVED", "REJECTED"}:
            raise IntegrityError(f"invalid concept relation status: {status}")
        if source not in {"MODEL", "ADMIN"}:
            raise IntegrityError(f"invalid concept relation source: {source}")
        if not subject_concept_id or not object_concept_id or subject_concept_id == object_concept_id:
            raise IntegrityError("concept relation needs two distinct concept endpoints")
        if not evidence:
            raise IntegrityError("concept relation needs at least one source evidence span")
        with self._write() as connection:
            return self._add_concept_relation(
                connection,
                version_id=version_id,
                subject_concept_id=subject_concept_id,
                predicate=predicate,
                object_concept_id=object_concept_id,
                evidence=evidence,
                status=status,
                source=source,
                relation_id=relation_id,
            )

    def _add_concept_relation(
        self,
        connection: sqlite3.Connection,
        *,
        version_id: str,
        subject_concept_id: str,
        predicate: str,
        object_concept_id: str,
        evidence: Sequence[Mapping[str, Any]],
        status: str = "PROVISIONAL",
        source: str = "MODEL",
        relation_id: str | None = None,
    ) -> str:
        """Write one grounded relation using an existing store transaction.

        ``SQLiteBatchRepository`` uses this deliberately private primitive so a
        section-graph result, its mentions, relations, and item status commit
        (or roll back) together.  Public callers should use
        :meth:`add_concept_relation` instead.
        """
        if predicate not in _RELATION_PREDICATES:
            raise IntegrityError(f"unsupported concept relation predicate: {predicate}")
        if status not in {"PROVISIONAL", "APPROVED", "REJECTED"}:
            raise IntegrityError(f"invalid concept relation status: {status}")
        if source not in {"MODEL", "ADMIN"}:
            raise IntegrityError(f"invalid concept relation source: {source}")
        if not subject_concept_id or not object_concept_id or subject_concept_id == object_concept_id:
            raise IntegrityError("concept relation needs two distinct concept endpoints")
        if not evidence:
            raise IntegrityError("concept relation needs at least one source evidence span")
        if connection.execute(
            "SELECT 1 FROM book_versions WHERE version_id = ?", (version_id,)
        ).fetchone() is None:
            raise IntegrityError(f"unknown version_id: {version_id}")
        for concept_id in (subject_concept_id, object_concept_id):
            mentioned = connection.execute(
                """SELECT 1 FROM concept_mentions AS m
                   JOIN passages AS p ON p.passage_id = m.passage_id
                   WHERE m.concept_id = ? AND p.version_id = ?""",
                (concept_id, version_id),
            ).fetchone()
            if mentioned is None:
                raise IntegrityError("relation endpoint has no mention in this EPUB version")
        existing = connection.execute(
                """SELECT relation_id FROM concept_relations
                   WHERE subject_concept_id = ? AND predicate = ? AND object_concept_id = ?""",
                (subject_concept_id, predicate, object_concept_id),
        ).fetchone()
        resolved_id = str(existing["relation_id"]) if existing is not None else (relation_id or str(uuid4()))
        if existing is None:
            connection.execute(
                    """INSERT INTO concept_relations(
                           relation_id, subject_concept_id, predicate, object_concept_id
                       ) VALUES (?, ?, ?, ?)""",
                    (resolved_id, subject_concept_id, predicate, object_concept_id),
            )
        assertion = connection.execute(
                """SELECT assertion_id FROM concept_relation_assertions
                   WHERE relation_id = ? AND version_id = ? AND source = ?""",
                (resolved_id, version_id, source),
        ).fetchone()
        assertion_id = str(assertion["assertion_id"]) if assertion is not None else str(uuid4())
        if assertion is None:
            connection.execute(
                    """INSERT INTO concept_relation_assertions(
                           assertion_id, relation_id, version_id, status, source
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (assertion_id, resolved_id, version_id, status, source),
            )
        for item in evidence:
            if not isinstance(item, Mapping):
                raise IntegrityError("concept relation evidence must be an object")
            passage_id = item.get("passage_id")
            start = item.get("start_codepoint")
            end = item.get("end_codepoint")
            supplied = item.get("evidence")
            if not isinstance(passage_id, str) or not isinstance(start, int) or not isinstance(end, int):
                raise IntegrityError("concept relation evidence needs passage_id and integer offsets")
            passage = connection.execute(
                "SELECT content FROM passages WHERE passage_id = ? AND version_id = ?",
                (passage_id, version_id),
            ).fetchone()
            if passage is None or start < 0 or end <= start or end > len(passage["content"]):
                raise IntegrityError("concept relation evidence does not belong to this EPUB version")
            expected = passage["content"][start:end]
            if not isinstance(supplied, str) or supplied != expected:
                raise IntegrityError("concept relation evidence must equal the immutable source substring")
            exists = connection.execute(
                    """SELECT 1 FROM concept_relation_evidence
                       WHERE assertion_id = ? AND passage_id = ? AND start_codepoint = ? AND end_codepoint = ?""",
                    (assertion_id, passage_id, start, end),
            ).fetchone()
            if exists is None:
                connection.execute(
                        """INSERT INTO concept_relation_evidence(
                               relation_evidence_id, assertion_id, passage_id, start_codepoint, end_codepoint, evidence
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (str(uuid4()), assertion_id, passage_id, start, end, expected),
                )
        return resolved_id

    def list_concept_relation_neighbors(
        self, concept_ids: Sequence[str], *, predicates: Sequence[str] = ("HAS_PART",)
    ) -> list[dict[str, Any]]:
        """Return edges with at least one non-rejected grounded assertion."""
        if not concept_ids or not predicates:
            return []
        if any(predicate not in _RELATION_PREDICATES for predicate in predicates):
            raise IntegrityError("unsupported concept relation predicate")
        concept_placeholders = ", ".join("?" for _ in concept_ids)
        predicate_placeholders = ", ".join("?" for _ in predicates)
        return [
            dict(row)
            for row in self._connection()
            .execute(
                f"""SELECT DISTINCT r.relation_id, r.subject_concept_id, r.predicate, r.object_concept_id
                    FROM concept_relations AS r
                    JOIN concept_relation_assertions AS a ON a.relation_id = r.relation_id
                    WHERE r.subject_concept_id IN ({concept_placeholders})
                      AND r.predicate IN ({predicate_placeholders})
                      AND a.status != 'REJECTED'
                    ORDER BY r.subject_concept_id, r.predicate, r.object_concept_id, r.relation_id""",
                (*concept_ids, *predicates),
            )
            .fetchall()
        ]

    def list_concept_relation_assertions(
        self,
        *,
        status: str | None = None,
        version_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Page administrator-review rows together with immutable evidence."""
        if status is not None and status not in {"PROVISIONAL", "APPROVED", "REJECTED"}:
            raise IntegrityError("invalid concept relation assertion status")
        if offset < 0 or not 1 <= limit <= 200:
            raise IntegrityError("concept relation assertion pagination values are invalid")
        conditions: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            conditions.append("a.status = ?")
            parameters.append(status)
        if version_id is not None:
            conditions.append("a.version_id = ?")
            parameters.append(version_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._connection().execute(
            f"""SELECT a.assertion_id, a.relation_id, a.version_id, a.status, a.source, a.created_at,
                       r.predicate, subject.canonical_name AS subject_name,
                       object.canonical_name AS object_name
                FROM concept_relation_assertions AS a
                JOIN concept_relations AS r ON r.relation_id = a.relation_id
                JOIN concepts AS subject ON subject.concept_id = r.subject_concept_id
                JOIN concepts AS object ON object.concept_id = r.object_concept_id
                {where}
                ORDER BY a.created_at DESC, a.assertion_id DESC
                LIMIT ? OFFSET ?""",
            (*parameters, limit, offset),
        ).fetchall()
        result = [dict(row) for row in rows]
        for assertion in result:
            evidence_rows = self._connection().execute(
                """SELECT passage_id, start_codepoint, end_codepoint, evidence
                   FROM concept_relation_evidence
                   WHERE assertion_id = ?
                   ORDER BY passage_id, start_codepoint, end_codepoint""",
                (assertion["assertion_id"],),
            ).fetchall()
            assertion["evidence"] = [dict(row) for row in evidence_rows]
        return result

    def count_concept_relation_assertions(
        self, *, status: str | None = None, version_id: str | None = None
    ) -> int:
        if status is not None and status not in {"PROVISIONAL", "APPROVED", "REJECTED"}:
            raise IntegrityError("invalid concept relation assertion status")
        conditions: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status)
        if version_id is not None:
            conditions.append("version_id = ?")
            parameters.append(version_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self._connection().execute(
            f"SELECT COUNT(*) AS count FROM concept_relation_assertions {where}", parameters
        ).fetchone()
        return int(row["count"])

    def set_concept_relation_assertion_status(self, assertion_id: str, status: str) -> None:
        if status not in {"PROVISIONAL", "APPROVED", "REJECTED"}:
            raise IntegrityError("invalid concept relation assertion status")
        with self._write() as connection:
            changed = connection.execute(
                """UPDATE concept_relation_assertions
                   SET status = ? WHERE assertion_id = ?""",
                (status, assertion_id),
            ).rowcount
            if changed != 1:
                raise IntegrityError("unknown concept relation assertion")

    def _occurrence_span_source(self, concept_ids: Sequence[str]) -> tuple[str, tuple[str, ...]]:
        """Return the one FROM/WHERE/GROUP BY clause both occurrence queries use.

        The unit of enumeration is a **distinct source span**, not a mention
        row.  A reader asking for the graph occurrences of a query wants each
        piece of source text once; two concepts anchored on the very same
        characters, or one concept anchored inside another concept's span, are
        still one piece of source text.  So the clause below

        * collapses exact duplicates with ``GROUP BY`` on
          ``(passage_id, start_codepoint, end_codepoint)``, and
        * drops a span that some *other* span in the same passage wholly
          contains, keeping the maximal one.

        Only containment collapses.  A partial overlap keeps both spans,
        because widening a span to their union would render a citation that
        no concept actually anchored.  Unanchored (``NULL``) mentions are not
        spans, so they neither contain nor are contained; they group together
        per passage and are enumerated once.

        ``count_concept_occurrences`` and ``list_concept_occurrences`` share
        this text verbatim.  If the two ever applied different predicates the
        total would disagree with the pages, and pagination would silently
        drop or repeat source.
        """
        placeholders = ", ".join("?" for _ in concept_ids)
        clause = f"""
            FROM concept_mentions AS m
            JOIN passages AS p ON p.passage_id = m.passage_id
            JOIN book_versions AS v ON v.version_id = p.version_id
            JOIN books AS b ON b.book_id = v.book_id
            WHERE m.concept_id IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1
                  FROM concept_mentions AS wider
                  WHERE wider.passage_id = m.passage_id
                    AND wider.concept_id IN ({placeholders})
                    AND wider.start_codepoint IS NOT NULL
                    AND m.start_codepoint IS NOT NULL
                    AND wider.start_codepoint <= m.start_codepoint
                    AND wider.end_codepoint >= m.end_codepoint
                    AND (wider.start_codepoint < m.start_codepoint
                         OR wider.end_codepoint > m.end_codepoint)
              )
            GROUP BY m.passage_id, m.start_codepoint, m.end_codepoint
        """
        return clause, (*concept_ids, *concept_ids)

    def count_concept_occurrences(self, concept_ids: Sequence[str]) -> int:
        """Count the distinct source spans the graph channel will enumerate.

        See :meth:`_occurrence_span_source` for why a span, and not a mention
        row, is the unit that is counted and paged.
        """
        if not concept_ids:
            return 0
        clause, parameters = self._occurrence_span_source(concept_ids)
        row = self._connection().execute(
            f"SELECT COUNT(*) AS count FROM (SELECT 1 {clause})", parameters
        ).fetchone()
        return int(row["count"])

    def list_concept_occurrences(
        self, concept_ids: Sequence[str], *, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        """Page the distinct graph source spans in a stable source order.

        Each returned row carries ``concept_ids``/``canonical_names`` for every
        queried concept anchored on the span — including the concepts of the
        spans this one absorbed — so collapsing a duplicate never drops an
        attribution.  See :meth:`_occurrence_span_source` for the unit of
        enumeration and for why the count agrees with these pages.
        """
        if not concept_ids:
            return []
        if offset < 0 or limit < 1:
            raise IntegrityError("concept occurrence pagination values are invalid")
        clause, parameters = self._occurrence_span_source(concept_ids)
        rows = self._connection().execute(
            f"""SELECT m.passage_id, m.start_codepoint, m.end_codepoint,
                       p.content, p.content_sha256, p.toc_node_id,
                       b.title AS book_title
                {clause}
                ORDER BY p.spine_index, p.ordinal, m.start_codepoint, MIN(m.mention_id)
                LIMIT ? OFFSET ?""",
            (*parameters, limit, offset),
        ).fetchall()
        spans = [self._search_row_with_toc(dict(row)) for row in rows]
        self._attribute_span_concepts(spans, concept_ids)
        return spans

    def _attribute_span_concepts(
        self, spans: list[dict[str, Any]], concept_ids: Sequence[str]
    ) -> None:
        """Attach every queried concept anchored inside each surviving span."""
        if not spans:
            return
        passage_ids = tuple(dict.fromkeys(str(span["passage_id"]) for span in spans))
        concept_placeholders = ", ".join("?" for _ in concept_ids)
        passage_placeholders = ", ".join("?" for _ in passage_ids)
        rows = self._connection().execute(
            f"""SELECT DISTINCT m.passage_id, m.start_codepoint, m.end_codepoint,
                       m.concept_id, c.canonical_name
                FROM concept_mentions AS m
                JOIN concepts AS c ON c.concept_id = m.concept_id
                WHERE m.concept_id IN ({concept_placeholders})
                  AND m.passage_id IN ({passage_placeholders})""",
            (*concept_ids, *passage_ids),
        ).fetchall()
        mentions_by_passage: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            mentions_by_passage.setdefault(str(row["passage_id"]), []).append(row)
        for span in spans:
            start, end = span["start_codepoint"], span["end_codepoint"]
            attributed = [
                row
                for row in mentions_by_passage.get(str(span["passage_id"]), ())
                if _span_contains(start, end, row["start_codepoint"], row["end_codepoint"])
            ]
            span["concept_ids"] = tuple(sorted({str(row["concept_id"]) for row in attributed}))
            span["canonical_names"] = tuple(
                sorted({str(row["canonical_name"]) for row in attributed})
            )

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
        with self._write() as connection:
            return self._upsert_concept(
                connection,
                canonical_name,
                aliases=aliases,
                definition=definition,
                status=status,
                concept_id=concept_id,
                alias_source=alias_source,
            )

    def _upsert_concept(
        self,
        connection: sqlite3.Connection,
        canonical_name: str,
        *,
        aliases: Iterable[str] = (),
        definition: str = "",
        status: str = "PROVISIONAL",
        concept_id: str | None = None,
        alias_source: str = "MODEL",
    ) -> str:
        """Create/update a concept using an existing store transaction.

        :meth:`apply_overlay` uses this private primitive so a whole imported
        analysis — concepts, mentions, relations and evidence — commits or
        rolls back together.  Public callers should use :meth:`upsert_concept`.
        """
        if status not in {"PROVISIONAL", "APPROVED", "REJECTED"}:
            raise IntegrityError(f"invalid concept status: {status}")
        if alias_source not in {"SEED", "MODEL", "ADMIN"}:
            raise IntegrityError(f"invalid alias source: {alias_source}")
        normalized_name = _normalize(canonical_name)
        requested_aliases: dict[str, str] = {normalized_name: canonical_name}
        for alias in aliases:
            requested_aliases[_normalize(alias)] = alias
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

    def count_concepts(self, *, status: str | None = None) -> int:
        if status is not None and status not in {"PROVISIONAL", "APPROVED", "REJECTED"}:
            raise IntegrityError(f"invalid concept status: {status}")
        where = "WHERE status = ?" if status is not None else ""
        parameters = (status,) if status is not None else ()
        row = self._connection().execute(
            f"SELECT COUNT(*) AS count FROM concepts {where}", parameters
        ).fetchone()
        return int(row["count"])

    def list_concepts(
        self, *, status: str | None = None, offset: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Page the concept graph itself for administrator review.

        Merge candidates are only visible to an administrator who can see the
        graph, so this returns every concept's aliases and mention count in a
        stable order.  It deliberately carries no passage text or evidence:
        alias and canonical spellings are concept labels, not source material.
        """
        if status is not None and status not in {"PROVISIONAL", "APPROVED", "REJECTED"}:
            raise IntegrityError(f"invalid concept status: {status}")
        if offset < 0 or not 1 <= limit <= 200:
            raise IntegrityError("concept pagination values are invalid")
        where = "WHERE c.status = ?" if status is not None else ""
        parameters: tuple[Any, ...] = (status,) if status is not None else ()
        rows = self._connection().execute(
            f"""SELECT c.concept_id, c.canonical_name, c.definition, c.status,
                       c.created_at, c.updated_at,
                       (SELECT COUNT(*) FROM concept_mentions AS m
                         WHERE m.concept_id = c.concept_id) AS mention_count
                  FROM concepts AS c
                  {where}
                 ORDER BY c.canonical_name COLLATE NOCASE, c.concept_id
                 LIMIT ? OFFSET ?""",
            (*parameters, limit, offset),
        ).fetchall()
        concepts = [dict(row) for row in rows]
        if not concepts:
            return []
        placeholders = ", ".join("?" for _ in concepts)
        aliases: dict[str, list[str]] = {concept["concept_id"]: [] for concept in concepts}
        for alias_row in self._connection().execute(
            f"""SELECT concept_id, alias FROM concept_aliases
                 WHERE concept_id IN ({placeholders})
                 ORDER BY alias COLLATE NOCASE, alias_id""",
            tuple(concept["concept_id"] for concept in concepts),
        ):
            aliases[str(alias_row["concept_id"])].append(str(alias_row["alias"]))
        for concept in concepts:
            concept["aliases"] = aliases[concept["concept_id"]]
        return concepts

    def merge_concepts(
        self,
        *,
        target_concept_id: str,
        source_concept_id: str,
        merged_by: str,
        canonical_name: str | None = None,
    ) -> dict[str, Any]:
        """Fold ``source`` into ``target`` in one transaction, or change nothing.

        A model may suggest a semantic merge but can never perform one: when a
        suggestion exactly matches two existing concepts, ingest refuses the
        item and an administrator resolves it here.  Everything below happens
        inside a single ``_write()`` transaction because a partially merged
        graph is worse than an unmerged one.

        The source's canonical spelling becomes an alias of the target.  That
        spelling is exactly what a future model response will match on, so
        losing it would immediately reintroduce the failure this resolves.

        Mentions keep their exact passage, offsets, evidence and source; only
        ``concept_id`` is repointed.  A source mention that would duplicate one
        the target already holds for the same passage and span is dropped
        rather than duplicated.

        Relations are repointed, with two deliberate degenerate cases:

        * A relation *between* the two merged concepts would become a
          self-loop, which ``concept_relations`` forbids by CHECK.  It is
          deleted together with its assertions and evidence, because after the
          merge it asserts a relation from a concept to itself.  The count is
          reported so an operator sees it happened.
        * A relation whose repointed endpoints duplicate an existing relation
          is folded into that surviving relation instead of being dropped: its
          version-scoped assertions move across, and evidence spans that the
          surviving assertion already holds are deduplicated.  No grounded
          evidence span is discarded this way.

        Audit rows from earlier merges *into* the source are repointed onto the
        target before the source row is deleted, so merges chain: a concept
        that has already absorbed another can itself be folded onward, and the
        recorded lineage follows the mentions it describes.
        """
        if not target_concept_id or not source_concept_id:
            raise IntegrityError("a concept merge needs both a target and a source concept")
        if target_concept_id == source_concept_id:
            raise IntegrityError("a concept cannot be merged into itself")
        merger = merged_by.strip()
        if not merger or len(merger) > 200:
            raise IntegrityError(
                "concept merge operator identity must be a non-empty value of at most 200 characters"
            )
        # ``_normalize`` refuses an empty or whitespace-only override rather
        # than letting it read as "keep the target's current name".
        requested_canonical = canonical_name.strip() if canonical_name is not None else None
        requested_normalized = _normalize(canonical_name) if canonical_name is not None else None

        with self._write() as connection:
            target = connection.execute(
                "SELECT * FROM concepts WHERE concept_id = ?", (target_concept_id,)
            ).fetchone()
            if target is None:
                raise UnknownConceptError(f"unknown concept_id: {target_concept_id}")
            source = connection.execute(
                "SELECT * FROM concepts WHERE concept_id = ?", (source_concept_id,)
            ).fetchone()
            if source is None:
                raise UnknownConceptError(f"unknown concept_id: {source_concept_id}")
            source_canonical_name = str(source["canonical_name"])

            if requested_normalized is not None:
                owner = connection.execute(
                    """SELECT concept_id FROM concepts
                        WHERE normalized_name = ? AND concept_id NOT IN (?, ?)""",
                    (requested_normalized, target_concept_id, source_concept_id),
                ).fetchone()
                if owner is not None:
                    raise IntegrityError(
                        "the requested canonical name already belongs to a different concept"
                    )
                alias_owner = connection.execute(
                    """SELECT concept_id FROM concept_aliases
                        WHERE normalized_alias = ? AND concept_id NOT IN (?, ?)""",
                    (requested_normalized, target_concept_id, source_concept_id),
                ).fetchone()
                if alias_owner is not None:
                    raise IntegrityError(
                        "the requested canonical name is already an alias of a different concept"
                    )

            # ``normalized_alias`` is globally unique, so a row owned by the
            # source can never collide with one owned by the target.
            moved_aliases = connection.execute(
                "UPDATE concept_aliases SET concept_id = ? WHERE concept_id = ?",
                (target_concept_id, source_concept_id),
            ).rowcount
            moved_aliases += self._ensure_alias(
                connection,
                concept_id=target_concept_id,
                alias=source_canonical_name,
                normalized_alias=str(source["normalized_name"]),
            )
            # The target's own canonical spelling must survive an override as
            # an alias; usually ``upsert_concept`` already stored it.
            moved_aliases += self._ensure_alias(
                connection,
                concept_id=target_concept_id,
                alias=str(target["canonical_name"]),
                normalized_alias=str(target["normalized_name"]),
            )

            # ``IS`` is SQLite's NULL-safe comparison: a mention without
            # offsets must still deduplicate against the target's own
            # offset-less mention of the same passage.
            duplicate_mentions = connection.execute(
                """DELETE FROM concept_mentions
                    WHERE concept_id = ?
                      AND EXISTS (
                          SELECT 1 FROM concept_mentions AS kept
                           WHERE kept.concept_id = ?
                             AND kept.passage_id = concept_mentions.passage_id
                             AND kept.start_codepoint IS concept_mentions.start_codepoint
                             AND kept.end_codepoint IS concept_mentions.end_codepoint
                      )""",
                (source_concept_id, target_concept_id),
            ).rowcount
            moved_mentions = connection.execute(
                "UPDATE concept_mentions SET concept_id = ? WHERE concept_id = ?",
                (target_concept_id, source_concept_id),
            ).rowcount

            dropped_self_relations = connection.execute(
                """DELETE FROM concept_relations
                    WHERE (subject_concept_id = ? AND object_concept_id = ?)
                       OR (subject_concept_id = ? AND object_concept_id = ?)""",
                (source_concept_id, target_concept_id, target_concept_id, source_concept_id),
            ).rowcount
            repointed_relations = folded_relations = 0
            for relation in connection.execute(
                """SELECT relation_id, subject_concept_id, predicate, object_concept_id
                     FROM concept_relations
                    WHERE subject_concept_id = ? OR object_concept_id = ?
                    ORDER BY relation_id""",
                (source_concept_id, source_concept_id),
            ).fetchall():
                subject = (
                    target_concept_id
                    if relation["subject_concept_id"] == source_concept_id
                    else str(relation["subject_concept_id"])
                )
                object_ = (
                    target_concept_id
                    if relation["object_concept_id"] == source_concept_id
                    else str(relation["object_concept_id"])
                )
                survivor = connection.execute(
                    """SELECT relation_id FROM concept_relations
                        WHERE subject_concept_id = ? AND predicate = ? AND object_concept_id = ?
                          AND relation_id <> ?""",
                    (subject, relation["predicate"], object_, relation["relation_id"]),
                ).fetchone()
                if survivor is None:
                    connection.execute(
                        """UPDATE concept_relations
                              SET subject_concept_id = ?, object_concept_id = ?
                            WHERE relation_id = ?""",
                        (subject, object_, relation["relation_id"]),
                    )
                    repointed_relations += 1
                    continue
                self._fold_relation_assertions(
                    connection,
                    from_relation_id=str(relation["relation_id"]),
                    into_relation_id=str(survivor["relation_id"]),
                )
                connection.execute(
                    "DELETE FROM concept_relations WHERE relation_id = ?",
                    (relation["relation_id"],),
                )
                folded_relations += 1

            # An earlier merge *into* this source left audit rows naming it as
            # their target, and ``concept_merges.target_concept_id`` is a
            # RESTRICT reference: with those rows in place the DELETE below
            # fails the foreign key, so a concept that has ever absorbed
            # another could never itself be merged.  Chained consolidation is
            # exactly how an administrator reviews a large graph, so the rows
            # are repointed onto the surviving target rather than the
            # constraint being dropped.  The lineage they record genuinely
            # lives in the target now, and an audit table that nothing can
            # verify is not worth keeping as one.
            #
            # The ordering is load-bearing in both directions and deliberate:
            # this statement runs *before* the DELETE, which is what makes the
            # delete legal at all, and *before* the INSERT of this merge's own
            # audit row further down.  That row names the target, not the
            # source, so it could not match this WHERE clause even if it
            # already existed -- but writing it afterwards means the
            # arrangement does not depend on that argument staying true.
            #
            # A repointed row can only end up naming the target as its own
            # source if some caller resurrected a deleted identifier through
            # the explicit ``concept_id`` argument of :meth:`upsert_concept`;
            # the store never recycles one itself, since a new concept gets a
            # fresh uuid4 and the overlay reuses an id only for a concept that
            # still exists.  Such a row is still true -- those mentions did end
            # up here -- and is kept, because deleting it is the one outcome
            # that would lose history.
            repointed_merge_audits = connection.execute(
                "UPDATE concept_merges SET target_concept_id = ? WHERE target_concept_id = ?",
                (target_concept_id, source_concept_id),
            ).rowcount

            deleted = connection.execute(
                "DELETE FROM concepts WHERE concept_id = ?", (source_concept_id,)
            ).rowcount
            if deleted != 1:
                raise UnknownConceptError(f"unknown concept_id: {source_concept_id}")

            if requested_normalized is not None and requested_normalized != target["normalized_name"]:
                connection.execute(
                    """UPDATE concepts SET canonical_name = ?, normalized_name = ?,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE concept_id = ?""",
                    (requested_canonical, requested_normalized, target_concept_id),
                )
                moved_aliases += self._ensure_alias(
                    connection,
                    concept_id=target_concept_id,
                    alias=str(requested_canonical),
                    normalized_alias=requested_normalized,
                )
            else:
                connection.execute(
                    "UPDATE concepts SET updated_at = CURRENT_TIMESTAMP WHERE concept_id = ?",
                    (target_concept_id,),
                )

            merge_id = str(uuid4())
            connection.execute(
                """INSERT INTO concept_merges(
                       concept_merge_id, target_concept_id, source_concept_id,
                       source_canonical_name, merged_by
                   ) VALUES (?, ?, ?, ?, ?)""",
                (merge_id, target_concept_id, source_concept_id, source_canonical_name, merger),
            )
            merged = connection.execute(
                """SELECT c.canonical_name, c.status, m.merged_at
                     FROM concept_merges AS m
                     JOIN concepts AS c ON c.concept_id = m.target_concept_id
                    WHERE m.concept_merge_id = ?""",
                (merge_id,),
            ).fetchone()
        assert merged is not None
        return {
            "concept_merge_id": merge_id,
            "target_concept_id": target_concept_id,
            "source_concept_id": source_concept_id,
            "source_canonical_name": source_canonical_name,
            "canonical_name": str(merged["canonical_name"]),
            "status": str(merged["status"]),
            "merged_by": merger,
            "merged_at": merged["merged_at"],
            "moved_aliases": moved_aliases,
            "moved_mentions": moved_mentions,
            "duplicate_mentions": duplicate_mentions,
            "repointed_relations": repointed_relations,
            "folded_relations": folded_relations,
            "dropped_self_relations": dropped_self_relations,
            "repointed_merge_audits": repointed_merge_audits,
        }

    def split_concept(
        self,
        *,
        source_concept_id: str,
        canonical_name: str,
        aliases: Sequence[str] = (),
        mentions: Sequence[Mapping[str, Any]] = (),
        split_by: str,
    ) -> dict[str, Any]:
        """Carve part of one concept out into a new one, or change nothing.

        :meth:`merge_concepts` is one-way, and an administrator merge is a
        fallible judgement -- a context-specific designation folded into a
        generic one, a teaching folded into the document locator that names
        it.  Restoring a backup and replaying stops being possible as soon as a
        later job postdates the backup, so this is the correction path.

        It is deliberately **not** an undo.  ``concept_merges`` records the
        source's canonical name but not which aliases or mentions moved, so no
        faithful reverse is derivable from an audit row, and pretending
        otherwise would silently invent an administrator's decision.  The
        caller therefore states the whole decision explicitly: which concept
        survives, what the new one is called, which aliases go with it, and
        which mentions go with it.  Everything below happens inside a single
        ``_write()`` transaction because a half-split graph is worse than an
        unsplit one.

        A mention is named by ``{"passage_id", "start_codepoint",
        "end_codepoint"}`` -- the natural key of ``concept_mentions`` and the
        only vocabulary the rest of this API uses for a mention.
        ``add_concept_mention`` takes a passage and offsets, ``merge_concepts``
        deduplicates on exactly that triple, and the portable overlay travels
        mentions as locations rather than as surrogate keys; ``mention_id`` is
        an internal uuid that no read path in this store, service or API ever
        returns, so an administrator could not supply one.  Offsets are omitted
        together to name an unanchored mention, matching
        ``add_concept_mention``.

        ``canonical_name`` must either be one of the moving aliases or be a
        spelling no concept owns as a name or alias.  These are
        ``upsert_concept``'s own collision rules, reused so that a split cannot
        manufacture the very ambiguity a merge exists to resolve.  The source's
        own canonical spelling can never move: it would leave the surviving
        concept named by an alias belonging to a different concept.

        Moving *every* mention is refused.  That is a rename, and
        ``upsert_concept`` already renames a concept without inventing a second
        one.

        Mentions keep their exact passage, offsets, evidence and source; only
        ``concept_id`` changes, and each moved row is re-read and re-sliced from
        its passage afterwards, so a split that disturbed a citation by even one
        code point rolls back rather than committing.

        **Relations are deliberately not repointed.**  Which endpoint a
        relation belongs on after a split is a semantic judgement about what the
        relation asserts, and this store has no basis to make it: the row
        records two concept identifiers and a predicate, not which sense of the
        subject was meant.  Moving one automatically would assert something no
        administrator decided, and silently moving the wrong one is precisely
        the class of error this method exists to correct.  Every relation
        therefore stays on the source, and the result reports how many of them
        are grounded on evidence that literally names one of the split-off
        spellings, so an administrator can review that shortlist by hand.
        """
        if not source_concept_id:
            raise IntegrityError("a concept split needs a source concept")
        splitter = split_by.strip()
        if not splitter or len(splitter) > 200:
            raise IntegrityError(
                "concept split operator identity must be a non-empty value of at most 200 characters"
            )
        new_canonical = canonical_name.strip()
        normalized_new = _normalize(canonical_name)
        requested_aliases: dict[str, str] = {}
        for alias in aliases:
            requested_aliases[_normalize(alias)] = alias
        requested_mentions = self._mention_keys(mentions)

        with self._write() as connection:
            source = connection.execute(
                "SELECT * FROM concepts WHERE concept_id = ?", (source_concept_id,)
            ).fetchone()
            if source is None:
                raise UnknownConceptError(f"unknown concept_id: {source_concept_id}")
            source_canonical_name = str(source["canonical_name"])
            source_normalized_name = str(source["normalized_name"])

            if source_normalized_name in requested_aliases:
                raise IntegrityError(
                    "a split cannot move the source concept's own canonical spelling"
                )
            moving_alias_ids: list[str] = []
            moving_alias_spellings: list[str] = []
            for normalized_alias, alias in requested_aliases.items():
                owner = connection.execute(
                    "SELECT alias_id, concept_id, alias FROM concept_aliases WHERE normalized_alias = ?",
                    (normalized_alias,),
                ).fetchone()
                if owner is None or owner["concept_id"] != source_concept_id:
                    raise IntegrityError(
                        f"the source concept does not own this alias: {alias}"
                    )
                moving_alias_ids.append(str(owner["alias_id"]))
                moving_alias_spellings.append(str(owner["alias"]))

            # ``upsert_concept``'s collision rules, restated for a name that has
            # to be free *after* the requested aliases have moved.
            name_owner = connection.execute(
                "SELECT concept_id FROM concepts WHERE normalized_name = ?", (normalized_new,)
            ).fetchone()
            if name_owner is not None:
                raise IntegrityError(
                    "the new canonical name already belongs to an existing concept"
                )
            alias_owner = connection.execute(
                "SELECT concept_id FROM concept_aliases WHERE normalized_alias = ?",
                (normalized_new,),
            ).fetchone()
            if alias_owner is not None and normalized_new not in requested_aliases:
                raise IntegrityError(
                    "the new canonical name is already an alias of an existing concept"
                )

            moving_mentions: list[dict[str, Any]] = []
            for passage_id, start, end in requested_mentions:
                candidates = connection.execute(
                    """SELECT mention_id, concept_id, passage_id, start_codepoint,
                              end_codepoint, evidence, source
                         FROM concept_mentions
                        WHERE passage_id = ?
                          AND start_codepoint IS ?
                          AND end_codepoint IS ?""",
                    (passage_id, start, end),
                ).fetchall()
                owned = [row for row in candidates if row["concept_id"] == source_concept_id]
                if not owned:
                    if candidates:
                        raise IntegrityError(
                            "a mention named for the split belongs to a different concept"
                        )
                    raise IntegrityError("a mention named for the split does not exist")
                moving_mentions.append(dict(owned[0]))

            total_mentions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM concept_mentions WHERE concept_id = ?",
                    (source_concept_id,),
                ).fetchone()[0]
            )
            if total_mentions and len(moving_mentions) == total_mentions:
                raise IntegrityError(
                    "a split cannot move every mention of the source concept; "
                    "renaming a concept is upsert_concept's job"
                )

            new_concept_id = str(uuid4())
            connection.execute(
                """INSERT INTO concepts(concept_id, canonical_name, normalized_name, definition, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (new_concept_id, new_canonical, normalized_new, "", str(source["status"])),
            )
            moved_aliases = 0
            for alias_id in moving_alias_ids:
                moved_aliases += connection.execute(
                    "UPDATE concept_aliases SET concept_id = ? WHERE alias_id = ?",
                    (new_concept_id, alias_id),
                ).rowcount
            # A canonical spelling that was not among the moving aliases owns no
            # row yet; ``upsert_concept`` always stores one, so a split must too.
            moved_aliases += self._ensure_alias(
                connection,
                concept_id=new_concept_id,
                alias=new_canonical,
                normalized_alias=normalized_new,
            )

            moved_mentions = 0
            for mention in moving_mentions:
                moved_mentions += connection.execute(
                    "UPDATE concept_mentions SET concept_id = ? WHERE mention_id = ?",
                    (new_concept_id, mention["mention_id"]),
                ).rowcount
            self._verify_moved_mentions(
                connection, concept_id=new_concept_id, before=moving_mentions
            )

            split_names = {*moving_alias_spellings, new_canonical}
            relation_ids: set[str] = set()
            naming_relation_ids: set[str] = set()
            for row in connection.execute(
                """SELECT r.relation_id, e.evidence
                     FROM concept_relations AS r
                     LEFT JOIN concept_relation_assertions AS a ON a.relation_id = r.relation_id
                     LEFT JOIN concept_relation_evidence AS e ON e.assertion_id = a.assertion_id
                    WHERE r.subject_concept_id = ? OR r.object_concept_id = ?""",
                (source_concept_id, source_concept_id),
            ):
                relation_id = str(row["relation_id"])
                relation_ids.add(relation_id)
                evidence = row["evidence"]
                if evidence is not None and any(name in str(evidence) for name in split_names):
                    naming_relation_ids.add(relation_id)

            connection.execute(
                "UPDATE concepts SET updated_at = CURRENT_TIMESTAMP WHERE concept_id = ?",
                (source_concept_id,),
            )
            split_id = str(uuid4())
            connection.execute(
                """INSERT INTO concept_splits(
                       concept_split_id, source_concept_id, new_concept_id,
                       new_canonical_name, split_by
                   ) VALUES (?, ?, ?, ?, ?)""",
                (split_id, source_concept_id, new_concept_id, new_canonical, splitter),
            )
            recorded = connection.execute(
                "SELECT split_at FROM concept_splits WHERE concept_split_id = ?", (split_id,)
            ).fetchone()
        assert recorded is not None
        return {
            "concept_split_id": split_id,
            "source_concept_id": source_concept_id,
            "source_canonical_name": source_canonical_name,
            "new_concept_id": new_concept_id,
            "canonical_name": new_canonical,
            "status": str(source["status"]),
            "split_by": splitter,
            "split_at": recorded["split_at"],
            "moved_aliases": moved_aliases,
            "moved_mentions": moved_mentions,
            # Deliberately left where they were; see this method's docstring.
            "relations_on_source": len(relation_ids),
            "relations_naming_split_aliases": len(naming_relation_ids),
        }

    @staticmethod
    def _mention_keys(
        mentions: Sequence[Mapping[str, Any]]
    ) -> list[tuple[str, int | None, int | None]]:
        """Read the caller's mention list as ``concept_mentions``' natural key.

        Offsets are supplied together or not at all, exactly as in
        :meth:`add_concept_mention`; omitting both names an unanchored mention.
        """
        keys: list[tuple[str, int | None, int | None]] = []
        for spec in mentions:
            passage_id = str(spec.get("passage_id") or "").strip()
            if not passage_id:
                raise IntegrityError("a mention named for a split must name a passage")
            start = spec.get("start_codepoint")
            end = spec.get("end_codepoint")
            if (start is None) != (end is None):
                raise IntegrityError("mention offsets must be supplied together")
            key = (
                passage_id,
                None if start is None else int(start),
                None if end is None else int(end),
            )
            if key in keys:
                raise IntegrityError("the same mention was named twice for one split")
            keys.append(key)
        return keys

    @staticmethod
    def _verify_moved_mentions(
        connection: sqlite3.Connection,
        *,
        concept_id: str,
        before: Sequence[Mapping[str, Any]],
    ) -> None:
        """Re-read every moved mention and re-slice it from its own passage.

        A split must be a pure change of ``concept_id``.  This reads each row
        back after the write and compares its passage, offsets, evidence and
        source against the values it had before, then re-derives the evidence
        from the immutable passage the same way :meth:`_add_concept_mention`
        does.  Any disagreement raises, which rolls the whole split back.
        """
        for original in before:
            row = connection.execute(
                """SELECT m.concept_id, m.passage_id, m.start_codepoint, m.end_codepoint,
                          m.evidence, m.source, p.content
                     FROM concept_mentions AS m
                     JOIN passages AS p ON p.passage_id = m.passage_id
                    WHERE m.mention_id = ?""",
                (original["mention_id"],),
            ).fetchone()
            if row is None or row["concept_id"] != concept_id:
                raise IntegrityError("a mention named for the split did not move")
            if tuple(
                row[column]
                for column in ("passage_id", "start_codepoint", "end_codepoint", "evidence", "source")
            ) != tuple(
                original[column]
                for column in ("passage_id", "start_codepoint", "end_codepoint", "evidence", "source")
            ):
                raise IntegrityError("a split may change nothing about a mention but its concept")
            start, end = row["start_codepoint"], row["end_codepoint"]
            if start is None:
                continue
            content = str(row["content"])
            if start < 0 or end is None or end <= start or end > len(content):
                raise IntegrityError("a moved mention must identify a non-empty source substring")
            if row["evidence"] != content[start:end]:
                raise IntegrityError("a moved mention's evidence must equal the source substring")

    @staticmethod
    def _ensure_alias(
        connection: sqlite3.Connection,
        *,
        concept_id: str,
        alias: str,
        normalized_alias: str,
    ) -> int:
        """Give ``concept_id`` this alias unless the spelling is already taken.

        Returns 1 when a row was written.  A spelling owned by a third concept
        is an invariant violation the caller must not silently paper over: the
        whole merge is refused instead.
        """
        owner = connection.execute(
            "SELECT concept_id FROM concept_aliases WHERE normalized_alias = ?", (normalized_alias,)
        ).fetchone()
        if owner is not None:
            if owner["concept_id"] != concept_id:
                raise IntegrityError(f"alias already belongs to another concept: {alias}")
            return 0
        connection.execute(
            """INSERT INTO concept_aliases(alias_id, concept_id, alias, normalized_alias, source)
               VALUES (?, ?, ?, ?, 'ADMIN')""",
            (str(uuid4()), concept_id, alias, normalized_alias),
        )
        return 1

    @staticmethod
    def _fold_relation_assertions(
        connection: sqlite3.Connection, *, from_relation_id: str, into_relation_id: str
    ) -> None:
        """Move version-scoped assertions onto a surviving duplicate relation.

        ``concept_relation_assertions`` is unique per (relation, version,
        source) and its evidence is unique per (assertion, passage, span), so
        both levels deduplicate rather than fail.  Evidence rows keep their
        exact passage and offsets; only their parent identifier changes.
        """
        for assertion in connection.execute(
            "SELECT assertion_id, version_id, source FROM concept_relation_assertions WHERE relation_id = ?",
            (from_relation_id,),
        ).fetchall():
            survivor = connection.execute(
                """SELECT assertion_id FROM concept_relation_assertions
                    WHERE relation_id = ? AND version_id = ? AND source = ?""",
                (into_relation_id, assertion["version_id"], assertion["source"]),
            ).fetchone()
            if survivor is None:
                connection.execute(
                    "UPDATE concept_relation_assertions SET relation_id = ? WHERE assertion_id = ?",
                    (into_relation_id, assertion["assertion_id"]),
                )
                continue
            connection.execute(
                """DELETE FROM concept_relation_evidence
                    WHERE assertion_id = ?
                      AND EXISTS (
                          SELECT 1 FROM concept_relation_evidence AS kept
                           WHERE kept.assertion_id = ?
                             AND kept.passage_id = concept_relation_evidence.passage_id
                             AND kept.start_codepoint = concept_relation_evidence.start_codepoint
                             AND kept.end_codepoint = concept_relation_evidence.end_codepoint
                      )""",
                (assertion["assertion_id"], survivor["assertion_id"]),
            )
            connection.execute(
                "UPDATE concept_relation_evidence SET assertion_id = ? WHERE assertion_id = ?",
                (survivor["assertion_id"], assertion["assertion_id"]),
            )
            connection.execute(
                "DELETE FROM concept_relation_assertions WHERE assertion_id = ?",
                (assertion["assertion_id"],),
            )

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
        with self._write() as connection:
            return self._add_concept_mention(
                connection,
                concept_id,
                passage_id,
                start_codepoint=start_codepoint,
                end_codepoint=end_codepoint,
                evidence=evidence,
                source=source,
                mention_id=mention_id,
            )

    def _add_concept_mention(
        self,
        connection: sqlite3.Connection,
        concept_id: str,
        passage_id: str,
        *,
        start_codepoint: int | None = None,
        end_codepoint: int | None = None,
        evidence: str | None = None,
        source: str = "MODEL",
        mention_id: str | None = None,
    ) -> str:
        """Link a concept to a passage using an existing store transaction.

        The evidence string is always *derived* from the immutable passage;
        a caller may supply one only to have it checked.  :meth:`apply_overlay`
        relies on that: a portable overlay ships no text at all, so the
        receiving store reconstructs each mention from its own copy of the book.
        """
        if source not in {"SEED", "MODEL", "ADMIN"}:
            raise IntegrityError(f"invalid mention source: {source}")
        if (start_codepoint is None) != (end_codepoint is None):
            raise IntegrityError("mention offsets must be supplied together")
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

    def export_concept_overlay(self, version_id: str) -> ConceptOverlay:
        """Read one version's analysis as a portable, text-free artifact.

        Concept labels, aliases and definitions are the analysis product and
        do travel.  Everything that points *into* the book travels as a
        location — ``(ordinal, content_sha256, start, end)`` — so a receiving
        store can verify it against its own copy and derive the evidence
        string itself.  No passage text, evidence string, EPUB blob or vector
        is read here, let alone written to the artifact.

        A mention without code-point offsets cannot be located in another copy
        of the book and is therefore not exportable; the grounded ingest paths
        always record offsets, so this omits nothing they produced.
        """
        connection = self._connection()
        version = connection.execute(
            """SELECT v.version_id, v.epub_sha256, v.parser_version, b.title AS book_title
                 FROM book_versions AS v JOIN books AS b ON b.book_id = v.book_id
                WHERE v.version_id = ?""",
            (version_id,),
        ).fetchone()
        if version is None:
            raise IntegrityError(f"unknown version_id: {version_id}")

        passages = connection.execute(
            "SELECT passage_id, ordinal, content_sha256 FROM passages WHERE version_id = ? ORDER BY ordinal",
            (version_id,),
        ).fetchall()
        located = {
            str(row["passage_id"]): (int(row["ordinal"]), str(row["content_sha256"]))
            for row in passages
        }
        fingerprint = passage_fingerprint(
            (int(row["ordinal"]), str(row["content_sha256"])) for row in passages
        )

        aliases: dict[str, list[str]] = {}
        for row in connection.execute(
            """SELECT DISTINCT a.concept_id, a.alias
                 FROM concept_aliases AS a
                 JOIN concept_mentions AS m ON m.concept_id = a.concept_id
                 JOIN passages AS p ON p.passage_id = m.passage_id
                WHERE p.version_id = ?""",
            (version_id,),
        ):
            aliases.setdefault(str(row["concept_id"]), []).append(str(row["alias"]))

        concept_keys: dict[str, str] = {}
        concepts: list[OverlayConcept] = []
        for row in connection.execute(
            """SELECT DISTINCT c.concept_id, c.canonical_name, c.normalized_name, c.definition, c.status
                 FROM concepts AS c
                 JOIN concept_mentions AS m ON m.concept_id = c.concept_id
                 JOIN passages AS p ON p.passage_id = m.passage_id
                WHERE p.version_id = ?""",
            (version_id,),
        ):
            concept_id = str(row["concept_id"])
            concept_keys[concept_id] = str(row["normalized_name"])
            concepts.append(
                OverlayConcept(
                    key=str(row["normalized_name"]),
                    canonical_name=str(row["canonical_name"]),
                    aliases=tuple(aliases.get(concept_id, ())),
                    definition=str(row["definition"]),
                    status=str(row["status"]),
                )
            )

        mentions = [
            OverlayMention(
                concept_key=concept_keys[str(row["concept_id"])],
                ordinal=located[str(row["passage_id"])][0],
                content_sha256=located[str(row["passage_id"])][1],
                start_codepoint=int(row["start_codepoint"]),
                end_codepoint=int(row["end_codepoint"]),
            )
            for row in connection.execute(
                """SELECT m.concept_id, m.passage_id, m.start_codepoint, m.end_codepoint
                     FROM concept_mentions AS m
                     JOIN passages AS p ON p.passage_id = m.passage_id
                    WHERE p.version_id = ? AND m.start_codepoint IS NOT NULL""",
                (version_id,),
            )
        ]

        evidence: dict[str, list[OverlaySpan]] = {}
        for row in connection.execute(
            """SELECT e.assertion_id, e.passage_id, e.start_codepoint, e.end_codepoint
                 FROM concept_relation_evidence AS e
                 JOIN concept_relation_assertions AS a ON a.assertion_id = e.assertion_id
                WHERE a.version_id = ?""",
            (version_id,),
        ):
            ordinal, digest = located[str(row["passage_id"])]
            evidence.setdefault(str(row["assertion_id"]), []).append(
                OverlaySpan(ordinal, digest, int(row["start_codepoint"]), int(row["end_codepoint"]))
            )

        relations: list[OverlayRelation] = []
        for row in connection.execute(
            """SELECT a.assertion_id, a.status, r.subject_concept_id, r.predicate, r.object_concept_id
                 FROM concept_relation_assertions AS a
                 JOIN concept_relations AS r ON r.relation_id = a.relation_id
                WHERE a.version_id = ?""",
            (version_id,),
        ):
            subject = concept_keys.get(str(row["subject_concept_id"]))
            object_ = concept_keys.get(str(row["object_concept_id"]))
            spans = evidence.get(str(row["assertion_id"]), [])
            # An endpoint with no mention in this version cannot be named by a
            # key the artifact declares, and an assertion with no surviving
            # evidence is not verifiable against another copy of the book.
            if subject is None or object_ is None or subject == object_ or not spans:
                continue
            relations.append(
                OverlayRelation(
                    subject_key=subject,
                    predicate=str(row["predicate"]),
                    object_key=object_,
                    status=str(row["status"]),
                    evidence=tuple(spans),
                )
            )

        return build_overlay(
            epub_sha256=str(version["epub_sha256"]),
            parser_version=str(version["parser_version"]),
            book_title=str(version["book_title"]),
            fingerprint=fingerprint,
            concepts=concepts,
            mentions=mentions,
            relations=relations,
        )

    def apply_overlay(
        self, overlay: ConceptOverlay, *, version_id: str | None = None
    ) -> dict[str, Any]:
        """Attach a portable analysis to this store's own copy of the book.

        Everything below happens in one transaction: a half-applied graph is
        worse than none, so a single failed gate leaves the store untouched.
        The gates run in order, and nothing can be stored that was not already
        in the importer's own passages:

        1. ``epub_sha256`` must identify the target version.
        2. ``parser_version`` must equal the format that produced the target
           version's passages — a parser change can shift every offset.
        3. The artifact's fingerprint, recomputed over this store's complete
           ordered passage set, must match.
        4. Every mention and evidence location must name a passage that exists
           at that ordinal *and* whose ``content_sha256`` still matches.
        5. Offsets must satisfy ``0 <= start < end <= len(content)``; the
           evidence string is then derived from this store's own passage.

        Conflict policy — an overlay is other people's analysis arriving in a
        store whose administrator may already have curated the same graph, so
        it is strictly additive and never overrides a local decision:

        * A concept's status is adopted only while the local one is still
          ``PROVISIONAL``, the sole undecided state.  A local ``APPROVED`` is
          never downgraded and a local ``REJECTED`` is never resurrected.
        * A local canonical spelling and a non-empty local definition win; the
          overlay's spelling survives as an alias and its definition fills an
          empty one.  Aliases are unioned, never removed.
        * Rows are written with source ``MODEL``: an overlay is published
          model output, and must never masquerade as this operator's own
          ``ADMIN`` decision.  An existing mention for the same concept,
          passage and span is left exactly as it is, so an ``ADMIN`` mention
          is never overwritten by ``PROVISIONAL`` model output.
        * A relation assertion is scoped to (relation, version, source), so an
          ``ADMIN`` assertion is a different row and is untouched; an existing
          ``MODEL`` assertion keeps its reviewed status and only gains
          evidence spans it did not already hold.

        Applying the same artifact twice therefore changes nothing the second
        time.
        """
        if overlay.overlay_format_version != OVERLAY_FORMAT_VERSION:
            raise OverlayRejected(
                "overlay_format_version_unsupported",
                f"this server applies overlay format version {OVERLAY_FORMAT_VERSION}",
            )
        applied = {
            "concepts_created": 0,
            "concepts_updated": 0,
            "mentions_created": 0,
            "relations_created": 0,
            "relation_evidence_created": 0,
        }
        skipped = {
            "concepts_unchanged": 0,
            "mentions_existing": 0,
            "relations_existing": 0,
        }
        reasons: dict[str, int] = {}

        def note(reason: str) -> None:
            reasons[reason] = reasons.get(reason, 0) + 1

        with self._write() as connection:
            version = self._overlay_target(connection, overlay, version_id)
            resolved_version_id = str(version["version_id"])
            if overlay.parser_version != str(version["parser_version"]):
                raise OverlayRejected(
                    "parser_version_mismatch",
                    "the overlay was produced by a different EPUB parser format version",
                )
            passages = {
                int(row["ordinal"]): row
                for row in connection.execute(
                    "SELECT passage_id, ordinal, content, content_sha256 FROM passages WHERE version_id = ?",
                    (resolved_version_id,),
                )
            }
            local = passage_fingerprint(
                (ordinal, str(row["content_sha256"])) for ordinal, row in passages.items()
            )
            if local != overlay.fingerprint:
                raise OverlayRejected(
                    "passage_fingerprint_mismatch",
                    "this store's passages are not the ones the overlay was built against",
                )

            concept_ids: dict[str, str] = {}
            for concept in overlay.concepts:
                existing = connection.execute(
                    "SELECT concept_id, canonical_name, definition, status FROM concepts WHERE normalized_name = ?",
                    (concept.key,),
                ).fetchone()
                if existing is None:
                    # A key with no concept row can still be an alias of a
                    # local concept, which is a genuine duplicate an
                    # administrator has to merge rather than something an
                    # import may quietly decide.
                    concept_ids[concept.key] = self._overlay_concept(
                        connection,
                        concept.canonical_name,
                        aliases=concept.aliases,
                        definition=concept.definition,
                        status=concept.status,
                    )
                    applied["concepts_created"] += 1
                    continue
                concept_id = str(existing["concept_id"])
                before = (
                    str(existing["canonical_name"]),
                    str(existing["definition"]),
                    str(existing["status"]),
                    self._alias_count(connection, concept_id),
                )
                status = concept.status if before[2] == "PROVISIONAL" else before[2]
                if status != concept.status:
                    note("concept_status_locked")
                definition = before[1] or concept.definition
                if concept.definition and definition != concept.definition:
                    note("concept_definition_locked")
                concept_ids[concept.key] = self._overlay_concept(
                    connection,
                    before[0],
                    aliases=(*concept.aliases, concept.canonical_name),
                    definition=definition,
                    status=status,
                    concept_id=concept_id,
                )
                after = (before[0], definition, status, self._alias_count(connection, concept_id))
                if after == before:
                    skipped["concepts_unchanged"] += 1
                else:
                    applied["concepts_updated"] += 1

            for mention in overlay.mentions:
                passage = self._overlay_passage(passages, mention.span)
                existing = connection.execute(
                    """SELECT source FROM concept_mentions
                        WHERE concept_id = ? AND passage_id = ? AND start_codepoint = ? AND end_codepoint = ?""",
                    (
                        concept_ids[mention.concept_key],
                        passage["passage_id"],
                        mention.start_codepoint,
                        mention.end_codepoint,
                    ),
                ).fetchone()
                if existing is not None:
                    skipped["mentions_existing"] += 1
                    note(
                        "mention_admin_owned"
                        if str(existing["source"]) == "ADMIN"
                        else "mention_already_present"
                    )
                    continue
                self._add_concept_mention(
                    connection,
                    concept_ids[mention.concept_key],
                    str(passage["passage_id"]),
                    start_codepoint=mention.start_codepoint,
                    end_codepoint=mention.end_codepoint,
                    source="MODEL",
                )
                applied["mentions_created"] += 1

            assertions_before, evidence_before = self._relation_counts(connection, resolved_version_id)
            for relation in overlay.relations:
                if relation.predicate not in _RELATION_PREDICATES:
                    raise OverlayRejected(
                        "unsupported_predicate",
                        "the overlay uses a relation predicate this server does not implement",
                    )
                subject_id = concept_ids[relation.subject_key]
                object_id = concept_ids[relation.object_key]
                if not self._has_mention_in_version(
                    connection, resolved_version_id, (subject_id, object_id)
                ):
                    note("relation_endpoint_unmentioned")
                    continue
                existing = connection.execute(
                    """SELECT a.status FROM concept_relation_assertions AS a
                         JOIN concept_relations AS r ON r.relation_id = a.relation_id
                        WHERE r.subject_concept_id = ? AND r.predicate = ? AND r.object_concept_id = ?
                          AND a.version_id = ? AND a.source = 'MODEL'""",
                    (subject_id, relation.predicate, object_id, resolved_version_id),
                ).fetchone()
                if existing is not None:
                    skipped["relations_existing"] += 1
                    if str(existing["status"]) != relation.status:
                        note("relation_status_locked")
                self._add_concept_relation(
                    connection,
                    version_id=resolved_version_id,
                    subject_concept_id=subject_id,
                    predicate=relation.predicate,
                    object_concept_id=object_id,
                    evidence=[
                        self._overlay_evidence(passages, span) for span in relation.evidence
                    ],
                    status=relation.status,
                    source="MODEL",
                )
            assertions_after, evidence_after = self._relation_counts(connection, resolved_version_id)
            applied["relations_created"] = assertions_after - assertions_before
            applied["relation_evidence_created"] = evidence_after - evidence_before

        return {
            "version_id": resolved_version_id,
            "epub_sha256": overlay.epub_sha256,
            "overlay_format_version": overlay.overlay_format_version,
            "applied": sum(applied.values()),
            "skipped": sum(skipped.values()),
            "rejected": 0,
            "applied_detail": applied,
            "skipped_detail": skipped,
            "skipped_reasons": reasons,
            "rejection_reasons": {},
        }

    def _overlay_concept(
        self,
        connection: sqlite3.Connection,
        canonical_name: str,
        *,
        aliases: Iterable[str],
        definition: str,
        status: str,
        concept_id: str | None = None,
    ) -> str:
        """Write one overlay concept, reporting a spelling clash as its class.

        Overlay concepts are written with alias source ``MODEL``: a published
        analysis is model output and must never be recorded as this operator's
        own ``ADMIN`` decision.
        """
        try:
            return self._upsert_concept(
                connection,
                canonical_name,
                aliases=aliases,
                definition=definition,
                status=status,
                concept_id=concept_id,
                alias_source="MODEL",
            )
        except IntegrityError as error:
            raise OverlayRejected(
                "alias_conflict",
                "an overlay spelling already belongs to a different local concept",
            ) from error

    @staticmethod
    def _overlay_target(
        connection: sqlite3.Connection, overlay: ConceptOverlay, version_id: str | None
    ) -> sqlite3.Row:
        """Resolve and verify the version an overlay claims to describe."""
        if version_id is not None:
            version = connection.execute(
                "SELECT version_id, epub_sha256, parser_version FROM book_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
            if version is None:
                raise IntegrityError(f"unknown version_id: {version_id}")
            if str(version["epub_sha256"]) != overlay.epub_sha256:
                raise OverlayRejected(
                    "epub_sha256_mismatch",
                    "the overlay describes a different EPUB archive than the target version",
                )
            return version
        version = connection.execute(
            "SELECT version_id, epub_sha256, parser_version FROM book_versions WHERE epub_sha256 = ?",
            (overlay.epub_sha256,),
        ).fetchone()
        if version is None:
            raise OverlayRejected(
                "epub_sha256_mismatch",
                "no stored EPUB version has the archive hash this overlay describes",
            )
        return version

    @staticmethod
    def _overlay_passage(
        passages: Mapping[int, sqlite3.Row], span: OverlaySpan
    ) -> sqlite3.Row:
        """Resolve one artifact location against this store's own passages."""
        passage = passages.get(span.ordinal)
        if passage is None:
            raise OverlayRejected(
                "passage_missing", "the overlay points at a passage ordinal this store does not have"
            )
        if str(passage["content_sha256"]) != span.content_sha256:
            raise OverlayRejected(
                "passage_content_drift",
                "a passage this overlay points at differs from the one it was built against",
            )
        if (
            span.start_codepoint < 0
            or span.end_codepoint <= span.start_codepoint
            or span.end_codepoint > len(passage["content"])
        ):
            raise OverlayRejected(
                "offsets_out_of_range",
                "an overlay span falls outside the passage it points at",
            )
        return passage

    @classmethod
    def _overlay_evidence(
        cls, passages: Mapping[int, sqlite3.Row], span: OverlaySpan
    ) -> dict[str, Any]:
        """Derive one relation evidence row from the importer's own passage."""
        passage = cls._overlay_passage(passages, span)
        return {
            "passage_id": str(passage["passage_id"]),
            "start_codepoint": span.start_codepoint,
            "end_codepoint": span.end_codepoint,
            "evidence": str(passage["content"])[span.start_codepoint : span.end_codepoint],
        }

    @staticmethod
    def _alias_count(connection: sqlite3.Connection, concept_id: str) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM concept_aliases WHERE concept_id = ?", (concept_id,)
            ).fetchone()["count"]
        )

    @staticmethod
    def _has_mention_in_version(
        connection: sqlite3.Connection, version_id: str, concept_ids: Sequence[str]
    ) -> bool:
        return all(
            connection.execute(
                """SELECT 1 FROM concept_mentions AS m
                     JOIN passages AS p ON p.passage_id = m.passage_id
                    WHERE m.concept_id = ? AND p.version_id = ?""",
                (concept_id, version_id),
            ).fetchone()
            is not None
            for concept_id in concept_ids
        )

    @staticmethod
    def _relation_counts(connection: sqlite3.Connection, version_id: str) -> tuple[int, int]:
        assertions = connection.execute(
            "SELECT COUNT(*) AS count FROM concept_relation_assertions WHERE version_id = ?",
            (version_id,),
        ).fetchone()["count"]
        evidence = connection.execute(
            """SELECT COUNT(*) AS count FROM concept_relation_evidence AS e
                 JOIN concept_relation_assertions AS a ON a.assertion_id = e.assertion_id
                WHERE a.version_id = ?""",
            (version_id,),
        ).fetchone()["count"]
        return int(assertions), int(evidence)

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
