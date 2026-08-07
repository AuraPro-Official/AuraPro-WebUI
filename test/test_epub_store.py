"""Focused, dependency-free tests for the canonical EPUB SQLite store."""

from __future__ import annotations

import os
import importlib.util
import sys
import tempfile
import unittest


STORE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend/open_webui/retrieval/epub/store.py'))
STORE_SPEC = importlib.util.spec_from_file_location('epub_store_for_test', STORE_PATH)
assert STORE_SPEC is not None and STORE_SPEC.loader is not None
STORE_MODULE = importlib.util.module_from_spec(STORE_SPEC)
sys.modules[STORE_SPEC.name] = STORE_MODULE
STORE_SPEC.loader.exec_module(STORE_MODULE)
IntegrityError = STORE_MODULE.IntegrityError
SQLiteEpubStore = STORE_MODULE.SQLiteEpubStore


class SQLiteEpubStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteEpubStore(os.path.join(self.tempdir.name, 'epub.db'))
        self.book_id = self.store.create_book('A faithful book', book_id='book-a')
        creation = self.store.create_book_version(
            self.book_id, epub_bytes=b'a complete test epub', version_id='version-a'
        )
        self.assertTrue(creation.created)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _passage(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            'passage_id': 'passage-a',
            'source_href': 'chapter-1.xhtml',
            'source_fragment': 'opening',
            'spine_index': 0,
            'ordinal': 0,
            'content_kind': 'paragraph',
            'content': '原文，含标点。\nSecond line.',
        }
        base.update(overrides)
        return base

    def test_duplicate_hash_reuses_existing_canonical_version(self) -> None:
        other_book = self.store.create_book('Same title is irrelevant', book_id='book-b')
        duplicate = self.store.create_book_version(
            other_book, epub_bytes=b'a complete test epub', version_id='version-b'
        )

        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.version_id, 'version-a')
        self.assertEqual(duplicate.book_id, 'book-a')
        self.assertIsNone(self.store.get_version('version-b'))
        self.assertEqual(self.store.get_epub_bytes('version-a'), b'a complete test epub')

    def test_passage_and_derived_window_keep_exact_source_and_offsets(self) -> None:
        self.store.add_passages('version-a', [self._passage()])
        unit_id = self.store.add_retrieval_unit('passage-a', 4, 9, embedding_profile='local-v1')

        passage = self.store.get_passage('passage-a')
        unit = self.store.get_retrieval_unit(unit_id)
        self.assertIsNotNone(passage)
        self.assertIsNotNone(unit)
        assert passage is not None and unit is not None
        self.assertEqual(unit['content'], passage['content'][4:9])
        self.assertEqual(unit['content'], '标点。\nS')
        with self.assertRaises(IntegrityError):
            self.store.add_retrieval_unit('passage-a', 0, 999)

    def test_version_retrieval_units_have_stable_order_and_persist_index_state(self) -> None:
        self.store.add_passages('version-a', [self._passage()])
        unit_id = self.store.add_retrieval_unit('passage-a', 0, 3)

        rows = self.store.list_retrieval_units_for_version('version-a')
        self.assertEqual([row['retrieval_unit_id'] for row in rows], [unit_id])
        self.assertEqual(rows[0]['vector_state'], 'PENDING')
        self.store.set_retrieval_unit_vector_state(unit_id, 'READY')
        self.assertEqual(self.store.get_retrieval_unit(unit_id)['vector_state'], 'READY')
        with self.assertRaisesRegex(IntegrityError, 'invalid vector state'):
            self.store.set_retrieval_unit_vector_state(unit_id, 'UNKNOWN')

    def test_source_passages_are_immutable_and_foreign_keys_are_safe(self) -> None:
        self.store.add_passages('version-a', [self._passage()])
        concept_id = self.store.upsert_concept('TCP', aliases=['Transmission Control Protocol'])
        mention_id = self.store.add_concept_mention(
            concept_id, 'passage-a', start_codepoint=0, end_codepoint=2, evidence='原文'
        )
        self.assertTrue(mention_id)

        with self.assertRaises(IntegrityError):
            self.store.add_concept_mention('missing', 'passage-a')
        with self.assertRaises(Exception) as raised:
            self.store._connection().execute(
                'UPDATE passages SET content = ? WHERE passage_id = ?', ('rewritten', 'passage-a')
            )
        self.assertIn('immutable', str(raised.exception))
        self.store._connection().rollback()

        updated_concept_id = self.store.upsert_concept('TCP', aliases=['TCP/IP'], definition='updated')
        self.assertEqual(updated_concept_id, concept_id)
        mention = (
            self.store._connection()
            .execute('SELECT concept_id FROM concept_mentions WHERE mention_id = ?', (mention_id,))
            .fetchone()
        )
        self.assertEqual(mention['concept_id'], concept_id)

    def test_canonical_name_cannot_capture_another_concepts_alias(self) -> None:
        self.store.upsert_concept('TCP', aliases=['Transmission Control Protocol'])
        with self.assertRaisesRegex(IntegrityError, 'already an alias'):
            self.store.upsert_concept('Transmission Control Protocol')

    def test_concept_relations_are_version_scoped_and_evidence_bound(self) -> None:
        self.store.add_passages('version-a', [self._passage()])
        parent = self.store.upsert_concept('父概念')
        child = self.store.upsert_concept('子概念')
        self.store.add_concept_mention(parent, 'passage-a', start_codepoint=0, end_codepoint=2, evidence='原文')
        self.store.add_concept_mention(child, 'passage-a', start_codepoint=4, end_codepoint=6, evidence='标点')

        relation_id = self.store.add_concept_relation(
            'version-a',
            parent,
            'HAS_PART',
            child,
            evidence=[
                {
                    'passage_id': 'passage-a',
                    'start_codepoint': 0,
                    'end_codepoint': 2,
                    'evidence': '原文',
                }
            ],
        )
        relation = self.store.list_concept_relation_neighbors([parent])
        self.assertEqual(relation[0]['relation_id'], relation_id)
        self.assertEqual(relation[0]['object_concept_id'], child)

        self.store.create_book_version(self.book_id, epub_bytes=b'a second complete test epub', version_id='version-b')
        self.store.add_passages('version-b', [self._passage(passage_id='passage-b')])
        self.store.add_concept_mention(parent, 'passage-b', start_codepoint=0, end_codepoint=2, evidence='原文')
        self.store.add_concept_mention(child, 'passage-b', start_codepoint=4, end_codepoint=6, evidence='标点')
        same_relation = self.store.add_concept_relation(
            'version-b',
            parent,
            'HAS_PART',
            child,
            evidence=[
                {
                    'passage_id': 'passage-b',
                    'start_codepoint': 0,
                    'end_codepoint': 2,
                    'evidence': '原文',
                }
            ],
        )
        self.assertEqual(same_relation, relation_id)
        assertion_count = (
            self.store._connection()
            .execute('SELECT COUNT(*) FROM concept_relation_assertions WHERE relation_id = ?', (relation_id,))
            .fetchone()[0]
        )
        self.assertEqual(assertion_count, 2)

        with self.assertRaisesRegex(IntegrityError, 'immutable source substring'):
            self.store.add_concept_relation(
                'version-a',
                parent,
                'ELABORATES',
                child,
                evidence=[
                    {
                        'passage_id': 'passage-a',
                        'start_codepoint': 0,
                        'end_codepoint': 2,
                        'evidence': '改写',
                    }
                ],
            )

    def test_batch_item_cannot_cross_book_versions(self) -> None:
        self.store.add_passages('version-a', [self._passage()])
        job_id = self.store.create_batch_job('version-a', provider='provider', profile_name='concept-v1')
        self.store.add_batch_item(job_id, 'passage-a', custom_id='passage-a', request={'model': 'local'})

        other_book = self.store.create_book('Other', book_id='book-b')
        self.store.create_book_version(other_book, epub_bytes=b'other epub', version_id='version-b')
        self.store.add_passages('version-b', [self._passage(passage_id='passage-b')])
        with self.assertRaises(IntegrityError):
            self.store.add_batch_item(job_id, 'passage-b', custom_id='passage-b', request={})

    def test_search_read_surface_returns_stable_graph_rows_and_toc_hierarchy(self) -> None:
        self.store.add_toc_nodes(
            'version-a',
            [
                {
                    'toc_node_id': 'chapter',
                    'title': '第一章',
                    'href': 'chapter-1.xhtml',
                    'spine_index': 0,
                    'ordinal': 0,
                },
                {
                    'toc_node_id': 'section',
                    'parent_toc_node_id': 'chapter',
                    'title': '概述',
                    'href': 'chapter-1.xhtml',
                    'fragment': 'overview',
                    'spine_index': 0,
                    'ordinal': 1,
                },
            ],
        )
        self.store.add_passages('version-a', [self._passage(toc_node_id='section')])
        concept_id = self.store.upsert_concept('TCP', aliases=['Transmission Control Protocol'])
        self.store.add_concept_mention(concept_id, 'passage-a', start_codepoint=0, end_codepoint=2)

        self.assertEqual(self.store.count_concept_occurrences([concept_id]), 1)
        row = self.store.list_concept_occurrences([concept_id], offset=0, limit=20)[0]
        self.assertEqual(row['book_title'], 'A faithful book')
        self.assertEqual(row['toc_path'], ('第一章', '概述'))
        self.assertEqual(row['content'][0:2], '原文')
        self.assertEqual(self.store.matched_concept_names('passage-a', [concept_id]), ['TCP'])
        self.assertEqual(
            {entry['term'] for entry in self.store.list_concept_terms()},
            {'TCP', 'Transmission Control Protocol'},
        )


if __name__ == '__main__':
    unittest.main()
