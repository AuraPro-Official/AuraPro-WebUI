import ast
import asyncio
import copy
import hashlib
import logging
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


def load_retrieval_functions():
    # Isolate request orchestration from DB startup and other tests' sys.modules stubs.
    path = Path(__file__).parents[1] / 'open_webui' / 'retrieval' / 'utils.py'
    names = {'query_collection', 'query_collection_with_hybrid_search', 'merge_and_sort_query_results'}
    nodes = [
        node
        for node in ast.parse(path.read_text(encoding='utf-8')).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    if len(nodes) != len(names):
        raise RuntimeError('Missing production retrieval functions')
    module = ModuleType('retrieval_under_test')
    module.__dict__.update(
        asyncio=asyncio,
        hashlib=hashlib,
        log=logging.getLogger(__name__),
        Config=SimpleNamespace(get_many=AsyncMock()),
        ASYNC_VECTOR_DB_CLIENT=SimpleNamespace(get=AsyncMock()),
        RAG_EMBEDDING_QUERY_PREFIX='',
        query_doc=Mock(),
        query_doc_with_hybrid_search=AsyncMock(),
        query_doc_with_native_hybrid_search=AsyncMock(return_value=None),
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), module.__dict__)
    return module


retrieval = load_retrieval_functions()


def result_payload(document='matching text', metadata=None):
    return {
        'distances': [[0.8]],
        'documents': [[document]],
        'metadatas': [[metadata or {}]],
    }


class RetrievalSchedulingTest(unittest.IsolatedAsyncioTestCase):
    async def test_vector_query_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()
        payload = result_payload()

        def slow_query(**kwargs):
            started.set()
            release.wait(timeout=2)
            return SimpleNamespace(model_dump=lambda: copy.deepcopy(payload))

        with (
            patch.object(retrieval.Config, 'get_many', new=AsyncMock(return_value={})),
            patch.object(retrieval, 'query_doc', side_effect=slow_query),
        ):
            task = asyncio.create_task(
                retrieval.query_collection(None, ['collection'], ['query'], AsyncMock(return_value=[[0.1]]), 5)
            )
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                self.assertFalse(task.done(), 'The request loop must run while vector search is still waiting')
            finally:
                release.set()
                result = await task
        self.assertEqual(result, payload)

    async def test_vector_partial_failure_keeps_successful_results(self):
        payload = result_payload()

        def query(collection_name, **kwargs):
            if collection_name == 'broken':
                raise RuntimeError('unavailable collection')
            return SimpleNamespace(model_dump=lambda: copy.deepcopy(payload))

        with (
            patch.object(retrieval.Config, 'get_many', new=AsyncMock(return_value={})),
            patch.object(retrieval, 'query_doc', side_effect=query),
        ):
            result = await retrieval.query_collection(
                None, ['broken', 'available'], ['query'], AsyncMock(return_value=[[0.1]]), 5
            )
        self.assertEqual(result, payload)

    async def test_empty_queries_do_not_call_embedding_or_vector_search(self):
        embedding = AsyncMock()
        with (
            patch.object(retrieval.Config, 'get_many', new=AsyncMock(return_value={})),
            patch.object(retrieval, 'query_doc') as query,
        ):
            result = await retrieval.query_collection(None, ['collection'], [None, ''], embedding, 5)
        embedding.assert_not_awaited()
        query.assert_not_called()
        self.assertEqual(result['documents'], [[]])

    async def test_hybrid_search_preserves_parent_text_and_handles_missing_metadata(self):
        for metadata, expected in [
            ({'parent_content': 'full original paragraph'}, 'full original paragraph'),
            ({}, 'matching text'),
            (None, 'matching text'),
            ({'parent_content': ''}, 'matching text'),
            ({'parent_content': 123}, 'matching text'),
        ]:
            with self.subTest(metadata=metadata):
                payload = result_payload()
                payload['metadatas'] = [[metadata]]
                with (
                    patch.object(retrieval.ASYNC_VECTOR_DB_CLIENT, 'get', new=AsyncMock(return_value=object())),
                    patch.object(retrieval, 'query_doc_with_hybrid_search', new=AsyncMock(return_value=payload)),
                ):
                    result = await retrieval.query_collection_with_hybrid_search(
                        ['collection'], ['query'], AsyncMock(), 5, None, 5, 0, 0.5, True
                    )
                self.assertEqual(result['documents'], [[expected]])
                self.assertEqual(result['metadatas'], [[metadata]])


if __name__ == '__main__':
    unittest.main()
