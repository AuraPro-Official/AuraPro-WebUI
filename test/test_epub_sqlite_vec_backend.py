"""Real sqlite-vec persistence checks for EPUB derived-vector metadata."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import sys
import unittest

import pysqlite3


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from open_webui.retrieval.epub.sqlite_vec_backend import SQLiteVecDerivedVectorBackend  # noqa: E402
from open_webui.retrieval.epub.vector_index import DerivedVectorRecord, VectorIndexError  # noqa: E402


class PysqliteStore:
    def __init__(self) -> None:
        self.connection = pysqlite3.connect(':memory:')
        self.connection.row_factory = pysqlite3.Row
        self.connection.execute('PRAGMA foreign_keys = ON')
        self.connection.execute('CREATE TABLE retrieval_units (retrieval_unit_id TEXT PRIMARY KEY)')
        self.connection.executemany(
            'INSERT INTO retrieval_units(retrieval_unit_id) VALUES (?)', [('unit-a',), ('unit-b',)]
        )
        self.connection.commit()

    def _connection(self):
        return self.connection

    @contextmanager
    def _write(self):
        try:
            self.connection.execute('BEGIN')
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


def _record(unit_id: str, vector: tuple[float, ...], *, profile: str = 'local-embed-v1') -> DerivedVectorRecord:
    content = f'derived content for {unit_id}'
    return DerivedVectorRecord(
        retrieval_unit_id=unit_id,
        passage_id=f'passage-{unit_id}',
        start_codepoint=0,
        end_codepoint=len(content),
        content_sha256=sha256(content.encode('utf-8')).hexdigest(),
        embedding_profile=profile,
        vector=vector,
    )


class SQLiteVecDerivedVectorBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PysqliteStore()
        self.backend = SQLiteVecDerivedVectorBackend(self.store)

    def tearDown(self) -> None:
        self.store.connection.close()

    def test_persists_and_queries_profile_dimension_specific_vectors(self) -> None:
        first = _record('unit-a', (1.0, 0.0))
        second = _record('unit-b', (0.0, 1.0))
        self.backend.upsert(first)
        self.backend.upsert(second)

        rows = self.backend.search((1.0, 0.0), embedding_profile='local-embed-v1', limit=2)
        self.assertEqual([row.retrieval_unit_id for row in rows], ['unit-a', 'unit-b'])
        self.assertEqual(rows[0].vector, (1.0, 0.0))
        self.assertEqual(self.backend.search((1.0, 0.0), embedding_profile='other', limit=2), [])

    def test_rejects_rebinding_a_retrieval_unit_to_other_source_identity(self) -> None:
        self.backend.upsert(_record('unit-a', (1.0, 0.0)))
        changed = DerivedVectorRecord(
            retrieval_unit_id='unit-a',
            passage_id='different-passage',
            start_codepoint=0,
            end_codepoint=1,
            content_sha256='0' * 64,
            embedding_profile='local-embed-v1',
            vector=(1.0, 0.0),
        )
        with self.assertRaisesRegex(VectorIndexError, 'cannot be rebound'):
            self.backend.upsert(changed)

    def test_healthcheck_proves_the_active_connection_still_has_sqlite_vec(self) -> None:
        health = self.backend.healthcheck()
        self.assertTrue(health.version)


if __name__ == '__main__':
    unittest.main()
