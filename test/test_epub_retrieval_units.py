"""Acceptance tests for faithful Chinese-first EPUB retrieval windows."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


BACKEND = Path(__file__).resolve().parents[1] / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Retrieval-window planning and the EPUB application service do not require
# Open WebUI's CLI bootstrap dependencies.  Keep this focused acceptance test
# runnable in the constrained Python environment used for parser/store tests.
if 'open_webui' not in sys.modules:
    package = types.ModuleType('open_webui')
    package.__path__ = [str(BACKEND / 'open_webui')]
    sys.modules['open_webui'] = package

from open_webui.retrieval.epub.retrieval_units import plan_retrieval_windows  # noqa: E402
from open_webui.retrieval.epub.store import SQLiteEpubStore  # noqa: E402
from open_webui.services.epub_concept import EpubConceptService  # noqa: E402


class RetrievalWindowPlanningTest(unittest.TestCase):
    def test_short_passage_is_one_complete_unicode_code_point_window(self) -> None:
        source = '简体中文与😀。'

        windows = plan_retrieval_windows(source)
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual((window.start_codepoint, window.end_codepoint), (0, len(source)))
        self.assertEqual(source[window.start_codepoint : window.end_codepoint], source)

    def test_long_chinese_passage_prefers_chinese_sentence_ends_and_overlaps(self) -> None:
        source = '甲' * 799 + '。' + '乙' * 650 + '！' + '丙' * 200

        windows = plan_retrieval_windows(source)

        self.assertEqual(
            [(item.start_codepoint, item.end_codepoint) for item in windows],
            [(0, 800), (650, 1451), (1301, len(source))],
        )
        self._assert_faithful_windows(source, windows)
        self.assertEqual(windows[0].end_codepoint - windows[1].start_codepoint, 150)
        self.assertEqual(windows[1].end_codepoint - windows[2].start_codepoint, 150)

    def test_english_sentence_end_is_used_when_no_chinese_boundary_is_near_target(self) -> None:
        source = 'a' * 799 + '.' + 'b' * 650 + ';' + 'c' * 200

        windows = plan_retrieval_windows(source)

        self.assertEqual(
            [(item.start_codepoint, item.end_codepoint) for item in windows],
            [(0, 800), (650, 1451), (1301, len(source))],
        )
        self._assert_faithful_windows(source, windows)

    def test_chinese_boundary_wins_when_both_language_boundaries_are_near_target(self) -> None:
        source = 'a' * 799 + '.' + '中' * 9 + '。' + '尾' * 800

        windows = plan_retrieval_windows(source)

        self.assertEqual((windows[0].start_codepoint, windows[0].end_codepoint), (0, 810))
        self._assert_faithful_windows(source, windows)

    def test_character_boundary_is_used_only_when_no_sentence_boundary_exists(self) -> None:
        source = '无' * 1601

        windows = plan_retrieval_windows(source)

        self.assertEqual(
            [(item.start_codepoint, item.end_codepoint) for item in windows],
            [(0, 800), (650, 1450), (1300, 1601)],
        )
        self._assert_faithful_windows(source, windows)

    def _assert_faithful_windows(self, source: str, windows: tuple[object, ...]) -> None:
        previous_end = 0
        for window in windows:
            start = window.start_codepoint  # type: ignore[attr-defined]
            end = window.end_codepoint  # type: ignore[attr-defined]
            self.assertLess(start, end)
            self.assertLessEqual(end, len(source))
            self.assertGreater(end, previous_end)
            self.assertEqual(source[start:end], source[start:end])
            previous_end = end


class RetrievalUnitPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEpubStore(str(Path(self.temporary.name) / 'epub.db'))
        book_id = self.store.create_book('中文测试书', book_id='book')
        self.store.create_book_version(book_id, epub_bytes=b'epub', version_id='version')
        self.source = '甲' * 799 + '。' + '乙' * 650 + '！' + '丙' * 200
        self.store.add_passages(
            'version',
            [
                {
                    'passage_id': 'passage',
                    'source_href': 'chapter.xhtml',
                    'spine_index': 0,
                    'ordinal': 0,
                    'content_kind': 'paragraph',
                    'content': self.source,
                }
            ],
        )
        self.service = EpubConceptService(
            store=self.store,
            retrieval_embedding_profile='local-embedding-v1',
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_service_persists_automatic_windows_and_retry_is_idempotent(self) -> None:
        first_count = self.service._create_retrieval_units(['passage'])
        first = self.store.list_retrieval_units('passage')
        second_count = self.service._create_retrieval_units(['passage'])
        second = self.store.list_retrieval_units('passage')

        self.assertEqual(first_count, 3)
        self.assertEqual(second_count, 3)
        self.assertEqual(len(first), 3)
        self.assertEqual(
            [(row['start_codepoint'], row['end_codepoint']) for row in first],
            [(0, 800), (650, 1451), (1301, len(self.source))],
        )
        self.assertEqual({row['embedding_profile'] for row in first}, {'local-embedding-v1'})
        self.assertEqual([row['retrieval_unit_id'] for row in second], [row['retrieval_unit_id'] for row in first])
        for row in second:
            self.assertEqual(row['content'], self.source[row['start_codepoint'] : row['end_codepoint']])

    def test_import_flow_generates_units_before_marking_version_ready(self) -> None:
        parsed = SimpleNamespace(
            book_title='导入测试书',
            passages=(
                SimpleNamespace(
                    toc_path=(),
                    source_path='chapter.xhtml',
                    source_fragment=None,
                    spine_index=0,
                    ordinal=0,
                    content_kind='paragraph',
                    content=self.source,
                ),
            ),
            warnings=(),
        )
        with patch('open_webui.services.epub_concept.EPUBParser') as parser:
            parser.return_value.parse_book.return_value = parsed
            result = self.service.import_epub(filename='import.epub', epub_bytes=b'distinct EPUB bytes')

        self.assertTrue(result['created'])
        self.assertEqual(result['total_retrieval_units'], 3)
        version = self.store.get_version(result['version_id'])
        self.assertIsNotNone(version)
        assert version is not None
        self.assertEqual(version['status'], 'READY')
        passage = self.store.list_passages(result['version_id'])[0]
        self.assertEqual(len(self.store.list_retrieval_units(passage['passage_id'])), 3)


class VersionBulkIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLiteEpubStore(str(Path(self.temporary.name) / 'epub.db'))
        book_id = self.store.create_book('批量索引测试书', book_id='book')
        self.store.create_book_version(book_id, epub_bytes=b'epub', version_id='version')
        self.store.add_passages(
            'version',
            [
                {
                    'passage_id': f'passage-{ordinal}',
                    'source_href': 'chapter.xhtml',
                    'spine_index': 0,
                    'ordinal': ordinal,
                    'content_kind': 'paragraph',
                    'content': f'第{ordinal + 1}个不可变原文段落。',
                }
                for ordinal in range(3)
            ],
        )
        self.pending_id = self.store.add_retrieval_unit('passage-0', 0, len('第1个不可变原文段落。'))
        self.ready_id = self.store.add_retrieval_unit(
            'passage-1', 0, len('第2个不可变原文段落。'), vector_state='READY'
        )
        self.failed_id = self.store.add_retrieval_unit(
            'passage-2', 0, len('第3个不可变原文段落。'), vector_state='FAILED'
        )
        self.outcomes: dict[str, object] = {
            self.pending_id: SimpleNamespace(state='READY', reason=None),
            self.failed_id: ValueError('embedding output is invalid'),
        }

        class FakeIndexer:
            def __init__(inner_self) -> None:
                inner_self.calls: list[str] = []

            def index(inner_self, retrieval_unit_id: str):
                inner_self.calls.append(retrieval_unit_id)
                outcome = self.outcomes[retrieval_unit_id]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        self.indexer = FakeIndexer()
        self.service = EpubConceptService(store=self.store, vector_indexer=self.indexer)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_pending_run_isolated_failures_and_persists_outcomes(self) -> None:
        result = self.service.index_version_retrieval_units('version')

        self.assertEqual(result['mode'], 'PENDING')
        self.assertEqual(result['total_retrieval_units'], 3)
        self.assertEqual(result['selected_retrieval_units'], 2)
        self.assertEqual(result['skipped_ready'], 1)
        self.assertEqual(result['ready'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['error_count'], 1)
        self.assertEqual(self.indexer.calls, [self.pending_id, self.failed_id])
        self.assertEqual(self.store.get_retrieval_unit(self.pending_id)['vector_state'], 'READY')
        self.assertEqual(self.store.get_retrieval_unit(self.ready_id)['vector_state'], 'READY')
        self.assertEqual(self.store.get_retrieval_unit(self.failed_id)['vector_state'], 'FAILED')
        self.assertIn('embedding output is invalid', result['errors'][0]['reason'])

    def test_rebuild_retries_ready_units_and_preserves_ready_state_when_degraded(self) -> None:
        self.store.set_retrieval_unit_vector_state(self.pending_id, 'READY')
        self.store.set_retrieval_unit_vector_state(self.failed_id, 'READY')
        self.outcomes = {
            self.pending_id: SimpleNamespace(state='READY', reason=None),
            self.ready_id: SimpleNamespace(state='DEGRADED', reason='private model is stopped'),
            self.failed_id: SimpleNamespace(state='READY', reason=None),
        }

        result = self.service.index_version_retrieval_units('version', rebuild=True)

        self.assertEqual(result['mode'], 'REBUILD')
        self.assertEqual(result['selected_retrieval_units'], 3)
        self.assertEqual(result['ready'], 2)
        self.assertEqual(result['degraded'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(self.store.get_retrieval_unit(self.ready_id)['vector_state'], 'READY')
        self.assertEqual(result['errors'], [{'retrieval_unit_id': self.ready_id, 'reason': 'private model is stopped'}])

    def test_unknown_version_is_rejected_before_any_indexing(self) -> None:
        with self.assertRaisesRegex(ValueError, 'unknown EPUB version'):
            self.service.index_version_retrieval_units('missing')
        self.assertEqual(self.indexer.calls, [])


if __name__ == '__main__':
    unittest.main()
