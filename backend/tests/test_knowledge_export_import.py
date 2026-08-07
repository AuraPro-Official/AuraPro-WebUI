from __future__ import annotations

import asyncio
import io
import json
import sys
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if 'open_webui' not in sys.modules:
    package = types.ModuleType('open_webui')
    package.__path__ = [str(BACKEND / 'open_webui')]
    sys.modules['open_webui'] = package

from open_webui.retrieval.vector.dbs.chroma import ChromaClient  # noqa: E402
from open_webui.retrieval.vector.main import GetResult  # noqa: E402
from open_webui.utils.knowledge_export_import import (  # noqa: E402
    KnowledgeImportError,
    create_knowledge_export_zip,
    import_knowledge_from_zip,
)


def _fake_knowledge(knowledge_id: str = 'kb-1') -> mock.MagicMock:
    knowledge = mock.MagicMock()
    knowledge.model_dump.return_value = {
        'id': knowledge_id,
        'user_id': 'u-1',
        'name': 'Test KB',
        'description': '',
        'meta': None,
        'created_at': 0,
        'updated_at': 0,
    }
    return knowledge


def _batch_result(ids, documents, metadatas, embeddings) -> GetResult:
    return GetResult(
        ids=[ids],
        documents=[documents],
        metadatas=[metadatas],
        embeddings=[embeddings],
    )


def _single_batch_get(collection_name, offset=0, limit=1000):
    # Two items in one batch; nothing beyond offset 0.
    if offset >= 2:
        return None
    return _batch_result(
        ids=['v-1', 'v-2'],
        documents=['hello zh', 'hello en'],
        metadatas=[{'lang': 'zh', 'type': 'sentence'}, {'lang': 'en', 'type': 'sentence'}],
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
    )


def _multi_batch_get(collection_name, offset=0, limit=1000):
    # Three items over two pages: page 1 has 2 items, page 2 has 1 item.
    if offset >= 3:
        return None
    if offset == 0:
        return _batch_result(
            ids=['v-1', 'v-2'],
            documents=['hello zh', 'hello en'],
            metadatas=[{'lang': 'zh'}, {'lang': 'en'}],
            embeddings=[[0.1, 0.2], [0.3, 0.4]],
        )
    return _batch_result(
        ids=['v-3'],
        documents=['hello de'],
        metadatas=[{'lang': 'de'}],
        embeddings=[[0.5, 0.6]],
    )


class TestExportKnowledgeWithVectors(unittest.TestCase):
    def test_export_zip_contains_knowledge_and_vectors_only(self) -> None:
        async def run() -> None:
            with (
                mock.patch(
                    'open_webui.utils.knowledge_export_import.Knowledges'
                ) as mock_knowledges,
                mock.patch(
                    'open_webui.utils.knowledge_export_import.ASYNC_VECTOR_DB_CLIENT'
                ) as mock_async_client,
            ):
                mock_knowledges.get_knowledge_by_id = mock.AsyncMock(return_value=_fake_knowledge())
                mock_async_client.count = mock.AsyncMock(return_value=2)
                mock_async_client.get = mock.AsyncMock(side_effect=_single_batch_get)

                zip_buffer = await create_knowledge_export_zip('kb-1')

            mock_async_client.get.assert_awaited()
            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                names = zf.namelist()
                self.assertEqual(
                    sorted(names),
                    ['knowledge.json', 'metadata.json'],
                )
                self.assertNotIn('files.json', names)
                self.assertFalse(any(name.startswith('file_bodies/') for name in names))

                knowledge = json.loads(zf.read('knowledge.json'))
                self.assertEqual(knowledge['id'], 'kb-1')
                self.assertEqual(knowledge['name'], 'Test KB')

                metadata = json.loads(zf.read('metadata.json'))
                self.assertEqual(metadata['ids'], [['v-1', 'v-2']])
                self.assertEqual(metadata['documents'], [['hello zh', 'hello en']])
                self.assertEqual(metadata['embeddings'], [[[0.1, 0.2], [0.3, 0.4]]])

        asyncio.run(run())

    def test_export_zip_excludes_vectors_when_disabled(self) -> None:
        async def run() -> None:
            with (
                mock.patch(
                    'open_webui.utils.knowledge_export_import.Knowledges'
                ) as mock_knowledges,
                mock.patch(
                    'open_webui.utils.knowledge_export_import.ASYNC_VECTOR_DB_CLIENT'
                ) as mock_async_client,
            ):
                mock_knowledges.get_knowledge_by_id = mock.AsyncMock(return_value=_fake_knowledge())

                zip_buffer = await create_knowledge_export_zip('kb-1', include_vectors=False)

            mock_async_client.count.assert_not_called()
            mock_async_client.get.assert_not_called()
            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                self.assertEqual(zf.namelist(), ['knowledge.json'])

        asyncio.run(run())

    def test_export_reads_vectors_in_batches(self) -> None:
        async def run() -> None:
            with (
                mock.patch(
                    'open_webui.utils.knowledge_export_import.Knowledges'
                ) as mock_knowledges,
                mock.patch(
                    'open_webui.utils.knowledge_export_import.ASYNC_VECTOR_DB_CLIENT'
                ) as mock_async_client,
            ):
                mock_knowledges.get_knowledge_by_id = mock.AsyncMock(return_value=_fake_knowledge())
                mock_async_client.count = mock.AsyncMock(return_value=3)
                mock_async_client.get = mock.AsyncMock(side_effect=_multi_batch_get)

                zip_buffer = await create_knowledge_export_zip('kb-1')

            offsets = [call.kwargs['offset'] for call in mock_async_client.get.await_args_list]
            self.assertEqual(offsets, [0, 2, 3, 0, 2, 3, 0, 2, 3, 0, 2, 3])

            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                metadata = json.loads(zf.read('metadata.json'))
                self.assertEqual(metadata['ids'], [['v-1', 'v-2'], ['v-3']])
                self.assertEqual(metadata['documents'], [['hello zh', 'hello en'], ['hello de']])
                self.assertEqual(metadata['embeddings'], [[[0.1, 0.2], [0.3, 0.4]], [[0.5, 0.6]]])

        asyncio.run(run())

    def test_export_module_has_no_file_dependencies(self) -> None:
        # The reworked export/import must not depend on File records or the
        # storage provider at all — bilingual vectors live entirely in the
        # vector collection.
        import open_webui.utils.knowledge_export_import as module

        self.assertFalse(hasattr(module, 'Files'))
        self.assertFalse(hasattr(module, 'Storage'))
        self.assertFalse(hasattr(module, 'FileForm'))

    def test_export_reports_progress_and_reaches_100(self) -> None:
        async def run() -> None:
            progress: list[tuple[int, str]] = []

            async def progress_callback(percent: int, message: str) -> None:
                progress.append((percent, message))

            with (
                mock.patch(
                    'open_webui.utils.knowledge_export_import.Knowledges'
                ) as mock_knowledges,
                mock.patch(
                    'open_webui.utils.knowledge_export_import.ASYNC_VECTOR_DB_CLIENT'
                ) as mock_async_client,
            ):
                mock_knowledges.get_knowledge_by_id = mock.AsyncMock(return_value=_fake_knowledge())
                mock_async_client.count = mock.AsyncMock(return_value=3)
                mock_async_client.get = mock.AsyncMock(side_effect=_multi_batch_get)

                zip_buffer = await create_knowledge_export_zip(
                    'kb-1',
                    progress_callback=progress_callback,
                )

            self.assertIsNotNone(zip_buffer)
            self.assertTrue(progress)
            self.assertEqual(progress[-1][0], 100)
            self.assertTrue(all(0 <= percent <= 100 for percent, _ in progress))
            self.assertTrue(all(message for _, message in progress))
            self.assertEqual(progress[0][0], 5)

        asyncio.run(run())

    def test_export_progress_callback_optional(self) -> None:
        async def run() -> None:
            with (
                mock.patch(
                    'open_webui.utils.knowledge_export_import.Knowledges'
                ) as mock_knowledges,
                mock.patch(
                    'open_webui.utils.knowledge_export_import.ASYNC_VECTOR_DB_CLIENT'
                ) as mock_async_client,
            ):
                mock_knowledges.get_knowledge_by_id = mock.AsyncMock(return_value=_fake_knowledge())
                mock_async_client.count = mock.AsyncMock(return_value=2)
                mock_async_client.get = mock.AsyncMock(side_effect=_single_batch_get)

                zip_buffer = await create_knowledge_export_zip('kb-1')

            self.assertIsNotNone(zip_buffer)

        asyncio.run(run())


class TestImportKnowledgeWithVectors(unittest.TestCase):
    def _build_zip(self, *, metadata: bool = True, legacy_files: bool = False) -> io.BytesIO:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('knowledge.json', json.dumps(_fake_knowledge().model_dump()))
            if metadata:
                zf.writestr(
                    'metadata.json',
                    json.dumps(
                        {
                            'ids': [['v-1', 'v-2']],
                            'documents': [['hello zh', 'hello en']],
                            'metadatas': [[{'lang': 'zh'}, {'lang': 'en'}]],
                            'embeddings': [[[0.1, 0.2], [0.3, 0.4]]],
                        }
                    ),
                )
            if legacy_files:
                zf.writestr('files.json', json.dumps([]))
        zip_buffer.seek(0)
        return zip_buffer

    def test_import_inserts_vectors_into_collection(self) -> None:
        async def run() -> None:
            zip_buffer = self._build_zip()
            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                mock_vector_client.insert = mock.MagicMock()

                result = await import_knowledge_from_zip('kb-1', zip_buffer)

            self.assertEqual(result, 'kb-1')
            mock_vector_client.insert.assert_called_once()
            kwargs = mock_vector_client.insert.call_args.kwargs
            self.assertEqual(kwargs['collection_name'], 'kb-1')
            items = kwargs['items']
            self.assertEqual(
                [item['id'] for item in items],
                ['v-1', 'v-2'],
            )
            self.assertEqual([item['text'] for item in items], ['hello zh', 'hello en'])
            self.assertEqual([item['vector'] for item in items], [[0.1, 0.2], [0.3, 0.4]])
            self.assertEqual([item['metadata'] for item in items], [{'lang': 'zh'}, {'lang': 'en'}])

        asyncio.run(run())

    def test_import_ignores_legacy_files_json(self) -> None:
        async def run() -> None:
            zip_buffer = self._build_zip(legacy_files=True)
            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                mock_vector_client.insert = mock.MagicMock()

                result = await import_knowledge_from_zip('kb-1', zip_buffer)

            self.assertEqual(result, 'kb-1')
            mock_vector_client.insert.assert_called_once()

        asyncio.run(run())

    def test_import_without_metadata_inserts_nothing(self) -> None:
        async def run() -> None:
            zip_buffer = self._build_zip(metadata=False)
            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                mock_vector_client.insert = mock.MagicMock()

                result = await import_knowledge_from_zip('kb-1', zip_buffer)

            self.assertEqual(result, 'kb-1')
            mock_vector_client.insert.assert_not_called()

        asyncio.run(run())

    def test_import_rejects_archive_missing_knowledge_json(self) -> None:
        async def run() -> None:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('metadata.json', '{}')
            zip_buffer.seek(0)

            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                with self.assertRaises(KnowledgeImportError):
                    await import_knowledge_from_zip('kb-1', zip_buffer)
                mock_vector_client.insert.assert_not_called()

        asyncio.run(run())

    def test_import_rejects_non_dict_knowledge_json(self) -> None:
        async def run() -> None:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('knowledge.json', json.dumps([]))
            zip_buffer.seek(0)

            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                with self.assertRaises(KnowledgeImportError):
                    await import_knowledge_from_zip('kb-1', zip_buffer)
                mock_vector_client.insert.assert_not_called()

        asyncio.run(run())

    def test_import_rejects_non_dict_metadata_json(self) -> None:
        async def run() -> None:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('knowledge.json', json.dumps(_fake_knowledge().model_dump()))
                zf.writestr('metadata.json', json.dumps([]))
            zip_buffer.seek(0)

            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                with self.assertRaises(KnowledgeImportError):
                    await import_knowledge_from_zip('kb-1', zip_buffer)
                mock_vector_client.insert.assert_not_called()

        asyncio.run(run())

    def test_import_rejects_bad_zip(self) -> None:
        async def run() -> None:
            bad = io.BytesIO(b'not a zip file')
            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                with self.assertRaises(KnowledgeImportError):
                    await import_knowledge_from_zip('kb-1', bad)
                mock_vector_client.insert.assert_not_called()

        asyncio.run(run())


class TestChromaPagedRead(unittest.TestCase):
    """Chroma get(offset, limit)/count must page without materialising the whole collection."""

    def _chroma_client_with_fake(self) -> tuple[object, mock.MagicMock]:
        client = ChromaClient.__new__(ChromaClient)

        fake_chroma = mock.MagicMock()
        fake_collection = mock.MagicMock()
        fake_chroma.get_collection.return_value = fake_collection
        client.client = fake_chroma

        fake_collection.get.return_value = {
            'ids': ['v-1', 'v-2'],
            'documents': ['hello zh', 'hello en'],
            'metadatas': [{'lang': 'zh'}, {'lang': 'en'}],
            'embeddings': [[0.1, 0.2], [0.3, 0.4]],
        }
        fake_collection.count.return_value = 2
        return client, fake_collection

    def test_get_passes_offset_and_limit(self) -> None:
        client, fake_collection = self._chroma_client_with_fake()

        result = client.get('kb-1', offset=2, limit=1000)

        fake_collection.get.assert_called_once_with(
            include=['embeddings', 'documents', 'metadatas'],
            limit=1000,
            offset=2,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.ids, [['v-1', 'v-2']])
        self.assertEqual(result.documents, [['hello zh', 'hello en']])
        self.assertEqual(result.embeddings, [[[0.1, 0.2], [0.3, 0.4]]])

    def test_get_returns_none_when_page_empty(self) -> None:
        client, fake_collection = self._chroma_client_with_fake()
        fake_collection.get.return_value = {
            'ids': [],
            'documents': [],
            'metadatas': [],
            'embeddings': [],
        }

        result = client.get('kb-1', offset=0, limit=1000)

        self.assertIsNone(result)

    def test_get_supports_paging_flag(self) -> None:
        client, _ = self._chroma_client_with_fake()
        self.assertTrue(client.supports_paged_get)

    def test_count_delegates_to_chroma(self) -> None:
        client, fake_collection = self._chroma_client_with_fake()

        result = client.count('kb-1')

        fake_collection.count.assert_called_once_with()
        self.assertEqual(result, 2)


if __name__ == '__main__':
    unittest.main()
