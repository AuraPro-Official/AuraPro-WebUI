"""
Standalone SQLite persistence layer for the EPUB Concept Wiki & Grounded Search module.

Uses an independent database file (`epub_concept.db`) stored under `DATA_DIR`,
completely decoupled from the main `webui.db`.  This makes the concept graph
portable — it can be exported, backed up, or shared independently.

Tables:
  - books:      ingested EPUB book registry
  - passages:   100% faithful original text paragraphs with TOC breadcrumbs
  - concepts:   concept wiki entries with aliases and definitions
  - concept_occurrences:  many-to-many link between concepts and passages
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resolve database file location
# ---------------------------------------------------------------------------
_DATA_DIR = Path(os.getenv('DATA_DIR', Path(__file__).resolve().parent.parent / 'data'))
EPUB_CONCEPT_DB_PATH = _DATA_DIR / 'epub_concept.db'


def _get_db_path() -> str:
    """Return the resolved path string, creating parent dirs if needed."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return str(EPUB_CONCEPT_DB_PATH)


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS books (
    book_id       TEXT PRIMARY KEY,
    book_title    TEXT NOT NULL,
    file_hash     TEXT,
    total_passages INTEGER DEFAULT 0,
    created_at    REAL NOT NULL DEFAULT (julianday('now')),
    meta_json     TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS passages (
    passage_id     TEXT PRIMARY KEY,
    book_id        TEXT NOT NULL REFERENCES books(book_id),
    book_title     TEXT NOT NULL,
    toc_path_json  TEXT NOT NULL DEFAULT '[]',
    content        TEXT NOT NULL,
    parent_context TEXT DEFAULT '',
    char_count     INTEGER DEFAULT 0,
    created_at     REAL NOT NULL DEFAULT (julianday('now'))
);

CREATE INDEX IF NOT EXISTS idx_passages_book ON passages(book_id);

CREATE TABLE IF NOT EXISTS concepts (
    concept_id     TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    aliases_json   TEXT NOT NULL DEFAULT '[]',
    definition     TEXT DEFAULT '',
    related_json   TEXT DEFAULT '{}',
    created_at     REAL NOT NULL DEFAULT (julianday('now')),
    updated_at     REAL NOT NULL DEFAULT (julianday('now'))
);

CREATE INDEX IF NOT EXISTS idx_concepts_name ON concepts(canonical_name);

CREATE TABLE IF NOT EXISTS concept_occurrences (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id    TEXT NOT NULL REFERENCES concepts(concept_id),
    passage_id    TEXT NOT NULL REFERENCES passages(passage_id),
    book_title    TEXT DEFAULT '',
    UNIQUE(concept_id, passage_id)
);

CREATE INDEX IF NOT EXISTS idx_occ_concept ON concept_occurrences(concept_id);
CREATE INDEX IF NOT EXISTS idx_occ_passage ON concept_occurrences(passage_id);

CREATE TABLE IF NOT EXISTS alias_index (
    alias_lower   TEXT PRIMARY KEY,
    concept_id    TEXT NOT NULL REFERENCES concepts(concept_id)
);
"""


# ---------------------------------------------------------------------------
# Database connection manager (thread-safe singleton)
# ---------------------------------------------------------------------------
class EpubConceptDB:
    """
    Thread-safe SQLite manager for the epub_concept knowledge store.

    Usage:
        db = EpubConceptDB()
        db.save_book(...)
        db.save_passages([...])
        passages = db.get_passages_by_ids(["BookA_P00001"])
    """

    _instance: Optional['EpubConceptDB'] = None
    _lock = threading.Lock()

    def __new__(cls) -> 'EpubConceptDB':
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._db_path = _get_db_path()
        self._local = threading.local()
        self._ensure_schema()
        self._initialized = True
        log.info(f'EpubConceptDB initialized at {self._db_path}')

    # -- connection per thread --
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA foreign_keys=ON')
            self._local.conn = conn
        return conn

    def _ensure_schema(self):
        conn = self._conn()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

    # -----------------------------------------------------------------------
    # Books
    # -----------------------------------------------------------------------
    def save_book(
        self, book_id: str, book_title: str, total_passages: int = 0, file_hash: str = '', meta: Optional[Dict] = None
    ):
        conn = self._conn()
        conn.execute(
            """INSERT OR REPLACE INTO books (book_id, book_title, file_hash, total_passages, meta_json)
               VALUES (?, ?, ?, ?, ?)""",
            (book_id, book_title, file_hash, total_passages, json.dumps(meta or {}, ensure_ascii=False)),
        )
        conn.commit()

    def get_books(self) -> List[Dict[str, Any]]:
        rows = self._conn().execute('SELECT * FROM books ORDER BY created_at DESC').fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Passages
    # -----------------------------------------------------------------------
    def save_passages(self, passages: List[Dict[str, Any]]):
        """Batch insert passages.  Each dict must have: passage_id, book_id, book_title, toc_path, content."""
        conn = self._conn()
        for p in passages:
            content = p['content']
            conn.execute(
                """INSERT OR REPLACE INTO passages
                   (passage_id, book_id, book_title, toc_path_json, content, parent_context, char_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    p['passage_id'],
                    p.get('book_id', p['passage_id'].rsplit('_P', 1)[0]),
                    p['book_title'],
                    json.dumps(p.get('toc_path', []), ensure_ascii=False),
                    content,
                    p.get('parent_context', ''),
                    len(content),
                ),
            )
        conn.commit()

    def get_passages_by_ids(self, passage_ids: List[str]) -> List[Dict[str, Any]]:
        if not passage_ids:
            return []
        conn = self._conn()
        placeholders = ','.join('?' for _ in passage_ids)
        rows = conn.execute(f'SELECT * FROM passages WHERE passage_id IN ({placeholders})', passage_ids).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['toc_path'] = json.loads(d.pop('toc_path_json', '[]'))
            result.append(d)
        return result

    def get_all_passages(self, book_id: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._conn()
        if book_id:
            rows = conn.execute('SELECT * FROM passages WHERE book_id = ? ORDER BY passage_id', (book_id,)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM passages ORDER BY passage_id').fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['toc_path'] = json.loads(d.pop('toc_path_json', '[]'))
            result.append(d)
        return result

    def count_passages(self, book_id: Optional[str] = None) -> int:
        conn = self._conn()
        if book_id:
            row = conn.execute('SELECT COUNT(*) FROM passages WHERE book_id = ?', (book_id,)).fetchone()
        else:
            row = conn.execute('SELECT COUNT(*) FROM passages').fetchone()
        return row[0] if row else 0

    # -----------------------------------------------------------------------
    # Concepts
    # -----------------------------------------------------------------------
    def save_concept(
        self,
        concept_id: str,
        canonical_name: str,
        aliases: Optional[List[str]] = None,
        definition: str = '',
        related: Optional[Dict] = None,
    ):
        conn = self._conn()
        aliases_list = aliases or []
        conn.execute(
            """INSERT OR REPLACE INTO concepts
               (concept_id, canonical_name, aliases_json, definition, related_json, updated_at)
               VALUES (?, ?, ?, ?, ?, julianday('now'))""",
            (
                concept_id,
                canonical_name,
                json.dumps(aliases_list, ensure_ascii=False),
                definition,
                json.dumps(related or {}, ensure_ascii=False),
            ),
        )
        # Update alias_index
        for alias in aliases_list:
            alias_lower = alias.strip().lower()
            if alias_lower:
                conn.execute(
                    'INSERT OR REPLACE INTO alias_index (alias_lower, concept_id) VALUES (?, ?)',
                    (alias_lower, concept_id),
                )
        conn.commit()

    def save_concepts_bulk(self, concepts: List[Dict[str, Any]]):
        """Batch save multiple concepts at once."""
        conn = self._conn()
        for c in concepts:
            aliases_list = c.get('aliases', [])
            conn.execute(
                """INSERT OR REPLACE INTO concepts
                   (concept_id, canonical_name, aliases_json, definition, related_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, julianday('now'))""",
                (
                    c['concept_id'],
                    c['canonical_name'],
                    json.dumps(aliases_list, ensure_ascii=False),
                    c.get('definition', ''),
                    json.dumps(c.get('related', {}), ensure_ascii=False),
                ),
            )
            for alias in aliases_list:
                alias_lower = alias.strip().lower()
                if alias_lower:
                    conn.execute(
                        'INSERT OR REPLACE INTO alias_index (alias_lower, concept_id) VALUES (?, ?)',
                        (alias_lower, c['concept_id']),
                    )
        conn.commit()

    def get_all_concepts(self) -> List[Dict[str, Any]]:
        rows = self._conn().execute('SELECT * FROM concepts ORDER BY canonical_name').fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['aliases'] = json.loads(d.pop('aliases_json', '[]'))
            d['related'] = json.loads(d.pop('related_json', '{}'))
            result.append(d)
        return result

    def get_concept_by_id(self, concept_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute('SELECT * FROM concepts WHERE concept_id = ?', (concept_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d['aliases'] = json.loads(d.pop('aliases_json', '[]'))
        d['related'] = json.loads(d.pop('related_json', '{}'))
        return d

    def lookup_alias(self, alias: str) -> Optional[str]:
        """Returns concept_id for a given alias string (case-insensitive)."""
        row = (
            self._conn()
            .execute('SELECT concept_id FROM alias_index WHERE alias_lower = ?', (alias.strip().lower(),))
            .fetchone()
        )
        return row[0] if row else None

    def get_all_aliases(self) -> Dict[str, str]:
        """Returns full alias_lower -> concept_id mapping for building in-memory Trie."""
        rows = self._conn().execute('SELECT alias_lower, concept_id FROM alias_index').fetchall()
        return {r[0]: r[1] for r in rows}

    # -----------------------------------------------------------------------
    # Concept ↔ Passage Occurrences
    # -----------------------------------------------------------------------
    def save_occurrence(self, concept_id: str, passage_id: str, book_title: str = ''):
        conn = self._conn()
        conn.execute(
            'INSERT OR IGNORE INTO concept_occurrences (concept_id, passage_id, book_title) VALUES (?, ?, ?)',
            (concept_id, passage_id, book_title),
        )
        conn.commit()

    def save_occurrences_bulk(self, occurrences: List[Dict[str, str]]):
        """Each dict: {"concept_id": ..., "passage_id": ..., "book_title": ...}"""
        conn = self._conn()
        for occ in occurrences:
            conn.execute(
                'INSERT OR IGNORE INTO concept_occurrences (concept_id, passage_id, book_title) VALUES (?, ?, ?)',
                (occ['concept_id'], occ['passage_id'], occ.get('book_title', '')),
            )
        conn.commit()

    def get_passage_ids_for_concept(self, concept_id: str) -> List[str]:
        rows = (
            self._conn()
            .execute('SELECT passage_id FROM concept_occurrences WHERE concept_id = ?', (concept_id,))
            .fetchall()
        )
        return [r[0] for r in rows]

    def get_occurrences_for_concept(self, concept_id: str) -> List[Dict[str, str]]:
        rows = (
            self._conn()
            .execute('SELECT passage_id, book_title FROM concept_occurrences WHERE concept_id = ?', (concept_id,))
            .fetchall()
        )
        return [{'passage_id': r[0], 'book_title': r[1]} for r in rows]

    # -----------------------------------------------------------------------
    # Full-text keyword search fallback
    # -----------------------------------------------------------------------
    def search_passages_by_keyword(self, keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Simple LIKE-based keyword search on passage content."""
        rows = (
            self._conn()
            .execute('SELECT * FROM passages WHERE content LIKE ? LIMIT ?', (f'%{keyword}%', limit))
            .fetchall()
        )
        result = []
        for r in rows:
            d = dict(r)
            d['toc_path'] = json.loads(d.pop('toc_path_json', '[]'))
            result.append(d)
        return result

    # -----------------------------------------------------------------------
    # Stats / Utility
    # -----------------------------------------------------------------------
    def get_stats(self) -> Dict[str, int]:
        conn = self._conn()
        books = conn.execute('SELECT COUNT(*) FROM books').fetchone()[0]
        passages = conn.execute('SELECT COUNT(*) FROM passages').fetchone()[0]
        concepts = conn.execute('SELECT COUNT(*) FROM concepts').fetchone()[0]
        aliases = conn.execute('SELECT COUNT(*) FROM alias_index').fetchone()[0]
        occurrences = conn.execute('SELECT COUNT(*) FROM concept_occurrences').fetchone()[0]
        return {
            'books': books,
            'passages': passages,
            'concepts': concepts,
            'aliases': aliases,
            'occurrences': occurrences,
            'db_path': self._db_path,
        }

    def close(self):
        conn = getattr(self._local, 'conn', None)
        if conn:
            conn.close()
            self._local.conn = None
