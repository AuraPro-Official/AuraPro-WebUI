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

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if 'open_webui' not in sys.modules:
    package = types.ModuleType('open_webui')
    package.__path__ = [str(BACKEND / 'open_webui')]
    sys.modules['open_webui'] = package

from open_webui.utils.knowledge_export_import import (  # noqa: E402
    KnowledgeImportError,
    create_knowledge_export_zip,
    import_knowledge_from_zip,
    is_bilingual_knowledge,
)


def _fake_knowledge(knowledge_id: str = 'kb-1', knowledge_type: str | None = None) -> mock.MagicMock:
    knowledge = mock.MagicMock()
    meta = {'knowledge_type': knowledge_type} if knowledge_type else None
    knowledge.model_dump.return_value = {
        'id': knowledge_id,
        'user_id': 'u-1',
        'name': 'Test KB',
        'description': '',
        'meta': meta,
        'created_at': 0,
        'updated_at': 0,
    }
    knowledge.meta = meta
    return knowledge


def _batch_result(ids, documents, metadatas, embeddings) -> mock.MagicMock:
    result = mock.MagicMock()
    result.ids = [ids]
    result.documents = [documents]
    result.metadatas = [metadatas]
    result.embeddings = [embeddings]
    return result


def _single_batch_get(collection_name, offset=0, limit=1000):
    return _batch_result(
        ids=['v-1', 'v-2'],
        documents=['hello zh', 'hello en'],
        metadatas=[{'lang': 'zh', 'type': 'sentence'}, {'lang': 'en', 'type': 'sentence'}],
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
    )


def _build_v2_zip(
    *,
    count: int = 2,
    manifest: bool = True,
    records: bool = True,
    embeddings: bool = True,
) -> io.BytesIO:
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('knowledge.json', json.dumps(_fake_knowledge().model_dump()))
        if manifest:
            zf.writestr(
                'vectors/manifest.json',
                json.dumps(
                    {
                        'format': 'knowledge-export-v2',
                        'count': count,
                        'dim': 2,
                        'dtype': 'float32',
                        'batch_size': 3000,
                    }
                ),
            )
        if embeddings:
            array = np.asarray(
                [[0.1 + 0.2 * i, 0.2 + 0.2 * i] for i in range(count)],
                dtype=np.float32,
            )
            npy_buffer = io.BytesIO()
            np.save(npy_buffer, array, allow_pickle=False)
            zf.writestr('vectors/embeddings.npy', npy_buffer.getvalue())
        if records:
            lines = [
                json.dumps(
                    {
                        'id': f'v-{i + 1}',
                        'document': f'document {i + 1}',
                        'metadata': {'lang': 'zh'},
                    }
                )
                for i in range(count)
            ]
            zf.writestr('vectors/records.jsonl', '\n'.join(lines))
    zip_buffer.seek(0)
    return zip_buffer


class TestExportKnowledgeWithVectors(unittest.TestCase):
    def test_export_zip_contains_knowledge_and_vectors_only(self) -> None:
        async def run() -> None:
            with (
                mock.patch('open_webui.utils.knowledge_export_import.Knowledges') as mock_knowledges,
                mock.patch(
                    'open_webui.utils.knowledge_export_import.ASYNC_VECTOR_DB_CLIENT'
                ) as mock_async_client,
            ):
                mock_knowledges.get_knowledge_by_id = mock.AsyncMock(return_value=_fake_knowledge())
                mock_async_client.get = mock.AsyncMock(side_effect=_single_batch_get)

                zip_buffer = await create_knowledge_export_zip('kb-1')

            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                names = sorted(zf.namelist())
                self.assertEqual(
                    names,
                    [
                        'knowledge.json',
                        'vectors/embeddings.npy',
                        'vectors/manifest.json',
                        'vectors/records.jsonl',
                    ],
                )
                knowledge = json.loads(zf.read('knowledge.json'))
                self.assertEqual(knowledge['id'], 'kb-1')

                manifest = json.loads(zf.read('vectors/manifest.json'))
                self.assertEqual(manifest['format'], 'knowledge-export-v2')
                self.assertEqual(manifest['count'], 2)
                self.assertEqual(manifest['dim'], 2)

                records = [json.loads(line) for line in zf.read('vectors/records.jsonl').split(b'\n') if line]
                self.assertEqual([r['id'] for r in records], ['v-1', 'v-2'])
                self.assertEqual([r['document'] for r in records], ['hello zh', 'hello en'])

                npy = np.load(io.BytesIO(zf.read('vectors/embeddings.npy')), allow_pickle=False)
                self.assertEqual(npy.shape, (2, 2))

        asyncio.run(run())

    def test_export_reports_progress_and_reaches_100(self) -> None:
        async def run() -> None:
            progress: list[tuple[int, str]] = []

            async def progress_callback(percent: int, message: str) -> None:
                progress.append((percent, message))

            with (
                mock.patch('open_webui.utils.knowledge_export_import.Knowledges') as mock_knowledges,
                mock.patch(
                    'open_webui.utils.knowledge_export_import.ASYNC_VECTOR_DB_CLIENT'
                ) as mock_async_client,
            ):
                mock_knowledges.get_knowledge_by_id = mock.AsyncMock(return_value=_fake_knowledge())
                mock_async_client.get = mock.AsyncMock(side_effect=_single_batch_get)

                zip_buffer = await create_knowledge_export_zip('kb-1', progress_callback=progress_callback)

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
                mock.patch('open_webui.utils.knowledge_export_import.Knowledges') as mock_knowledges,
                mock.patch(
                    'open_webui.utils.knowledge_export_import.ASYNC_VECTOR_DB_CLIENT'
                ) as mock_async_client,
            ):
                mock_knowledges.get_knowledge_by_id = mock.AsyncMock(return_value=_fake_knowledge())
                mock_async_client.get = mock.AsyncMock(side_effect=_single_batch_get)

                zip_buffer = await create_knowledge_export_zip('kb-1')

            self.assertIsNotNone(zip_buffer)

        asyncio.run(run())

    def test_export_module_has_no_file_dependencies(self) -> None:
        import open_webui.utils.knowledge_export_import as module

        self.assertFalse(hasattr(module, 'Files'))
        self.assertFalse(hasattr(module, 'Storage'))
        self.assertFalse(hasattr(module, 'FileForm'))


class TestBilingualRestriction(unittest.TestCase):
    def test_bilingual_knowledge_is_allowed(self) -> None:
        self.assertTrue(is_bilingual_knowledge(_fake_knowledge(knowledge_type='bilingual')))

    def test_general_knowledge_is_rejected(self) -> None:
        self.assertFalse(is_bilingual_knowledge(_fake_knowledge(knowledge_type='general')))

    def test_missing_meta_is_rejected(self) -> None:
        self.assertFalse(is_bilingual_knowledge(_fake_knowledge()))

    def test_missing_knowledge_type_is_rejected(self) -> None:
        knowledge = _fake_knowledge()
        knowledge.meta = {'name': 'x'}
        self.assertFalse(is_bilingual_knowledge(knowledge))

    def test_bilingual_knowledge_other_fields_in_meta(self) -> None:
        knowledge = _fake_knowledge()
        knowledge.meta = {'knowledge_type': 'bilingual', 'name': 'x'}
        self.assertTrue(is_bilingual_knowledge(knowledge))


class TestImportKnowledgeWithVectors(unittest.TestCase):
    def test_import_inserts_vectors_into_collection(self) -> None:
        async def run() -> None:
            zip_buffer = _build_v2_zip()
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
            self.assertEqual([item['id'] for item in items], ['v-1', 'v-2'])
            self.assertEqual([item['text'] for item in items], ['document 1', 'document 2'])
            for actual, expected in zip([item['vector'] for item in items], [[0.1, 0.2], [0.3, 0.4]]):
                for a, e in zip(actual, expected):
                    self.assertAlmostEqual(a, e, places=6)
            self.assertEqual([item['metadata'] for item in items], [{'lang': 'zh'}, {'lang': 'zh'}])

        asyncio.run(run())

    def test_import_reports_progress_sequence(self) -> None:
        async def run() -> None:
            progress: list[tuple[int, str]] = []

            async def progress_callback(percent: int, message: str) -> None:
                progress.append((percent, message))

            zip_buffer = _build_v2_zip()
            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                mock_vector_client.insert = mock.MagicMock()

                result = await import_knowledge_from_zip('kb-1', zip_buffer, progress_callback=progress_callback)

            self.assertEqual(result, 'kb-1')
            self.assertTrue(progress)
            self.assertEqual(progress[-1][0], 100)
            self.assertTrue(all(0 <= percent <= 100 for percent, _ in progress))
            self.assertEqual(progress[0][0], 15)
            self.assertTrue(all(message for _, message in progress))
            # The trailing (final) batch must also report progress, otherwise
            # small archives jump straight from "importing" to 100%.
            self.assertIn(100, [percent for percent, _ in progress])
            self.assertTrue(any('2/2' in message for _, message in progress))

        asyncio.run(run())

    def test_import_reports_progress_for_trailing_partial_batch(self) -> None:
        async def run() -> None:
            progress: list[tuple[int, str]] = []

            async def progress_callback(percent: int, message: str) -> None:
                progress.append((percent, message))

            zip_buffer = _build_v2_zip(count=3)
            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                mock_vector_client.insert = mock.MagicMock()

                result = await import_knowledge_from_zip('kb-1', zip_buffer, progress_callback=progress_callback)

            self.assertEqual(result, 'kb-1')
            messages = [message for _, message in progress]
            self.assertTrue(any('3/3' in message for message in messages))

        asyncio.run(run())

    def test_import_without_manifest_inserts_nothing(self) -> None:
        async def run() -> None:
            zip_buffer = _build_v2_zip(manifest=False)
            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                mock_vector_client.insert = mock.MagicMock()

                result = await import_knowledge_from_zip('kb-1', zip_buffer)

            self.assertEqual(result, 'kb-1')
            mock_vector_client.insert.assert_not_called()

        asyncio.run(run())

    def test_import_rejects_missing_embeddings(self) -> None:
        async def run() -> None:
            zip_buffer = _build_v2_zip(embeddings=False)
            with mock.patch(
                'open_webui.utils.knowledge_export_import.VECTOR_DB_CLIENT'
            ) as mock_vector_client:
                mock_vector_client.insert = mock.MagicMock()
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
                mock_vector_client.insert = mock.MagicMock()
                with self.assertRaises(KnowledgeImportError):
                    await import_knowledge_from_zip('kb-1', bad)
                mock_vector_client.insert.assert_not_called()

        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
