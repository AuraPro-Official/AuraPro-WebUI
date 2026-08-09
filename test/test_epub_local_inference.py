"""Acceptance tests for EPUB's local-only inference and vector boundary."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest


EPUB_DIR = Path(__file__).resolve().parents[1] / 'backend/open_webui/retrieval/epub'
PACKAGE_NAME = 'epub_sdd_test_package'
PACKAGE = types.ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(EPUB_DIR)]  # type: ignore[attr-defined]
sys.modules[PACKAGE_NAME] = PACKAGE


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f'{PACKAGE_NAME}.{name}', EPUB_DIR / f'{name}.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INFERENCE = _load('inference')
VECTOR_INDEX = _load('vector_index')

LocalConceptResolverAdapter = INFERENCE.LocalConceptResolverAdapter
LlamaCppConceptResolver = INFERENCE.LlamaCppConceptResolver
LocalEmbeddingAdapter = INFERENCE.LocalEmbeddingAdapter
LocalEndpointRejected = INFERENCE.LocalEndpointRejected
LocalRerankerAdapter = INFERENCE.LocalRerankerAdapter
AuraProEmbeddingAdapter = INFERENCE.AuraProEmbeddingAdapter
AuraProRerankDocument = INFERENCE.AuraProRerankDocument
AuraProRerankerAdapter = INFERENCE.AuraProRerankerAdapter
LocalInferenceUnavailable = INFERENCE.LocalInferenceUnavailable
ModelAvailability = INFERENCE.ModelAvailability
PrivateModelEndpoint = INFERENCE.PrivateModelEndpoint
DerivedVectorIndexer = VECTOR_INDEX.DerivedVectorIndexer
InMemoryDerivedVectorBackend = VECTOR_INDEX.InMemoryDerivedVectorBackend
VectorIndexError = VECTOR_INDEX.VectorIndexError


class FakeTransport:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[tuple[str, object]] = []

    def post_json(self, url: str, payload: object):
        self.calls.append((url, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeLlamaCppTransport:
    def __init__(self, *, health: object, completions: list[object] | None = None):
        self.health = health
        self.completions = list(completions or [])
        self.calls: list[tuple[str, str, object | None]] = []
        self.thread_ids: list[int] = []

    def get_json(self, url: str):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(('GET', url, None))
        if isinstance(self.health, Exception):
            raise self.health
        return self.health

    def post_json(self, url: str, payload: object):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(('POST', url, payload))
        response = self.completions.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeEmbeddings:
    profile = 'private-embed-v1'

    def __init__(self, *, available: bool = True, vectors: list[list[float]] | None = None):
        self.available = available
        self.vectors = vectors or [[0.1, 0.2, 0.3]]
        self.calls: list[list[str]] = []

    def availability(self):
        if self.available:
            return ModelAvailability.ready('local-embedding')
        return ModelAvailability.degraded('local-embedding', 'private runtime stopped')

    def embed(self, texts):
        self.calls.append(list(texts))
        return self.vectors


class FakeSource:
    def __init__(self, *, unit_content: str = '文，A', profile: str = 'private-embed-v1'):
        self.passage = {'passage_id': 'passage-1', 'content': '原文，A。第二句。'}
        from hashlib import sha256

        self.unit = {
            'retrieval_unit_id': 'unit-1',
            'passage_id': 'passage-1',
            'start_codepoint': 1,
            'end_codepoint': 4,
            'content': unit_content,
            'content_sha256': sha256(unit_content.encode('utf-8')).hexdigest(),
            'embedding_profile': profile,
        }

    def get_retrieval_unit(self, retrieval_unit_id: str):
        return self.unit if retrieval_unit_id == 'unit-1' else None

    def get_passage(self, passage_id: str):
        return self.passage if passage_id == 'passage-1' else None


class RunningLoop:
    """A dedicated application-loop stand-in for synchronous bridge tests."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.started.set()
        self.loop.run_forever()

    def __enter__(self):
        self.thread.start()
        if not self.started.wait(timeout=2):
            raise RuntimeError('test application loop did not start')
        return self

    def __exit__(self, *_unused) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


class LocalOnlyInferenceTest(unittest.TestCase):
    def test_public_endpoints_are_rejected_and_private_dns_is_explicitly_allowlisted(self) -> None:
        for endpoint in (
            'https://api.openai.com/v1/embeddings',
            'https://8.8.8.8/embed',
            'http://127.0.0.1:99999/embed',
        ):
            with self.assertRaises(LocalEndpointRejected):
                PrivateModelEndpoint(endpoint)
        self.assertEqual(PrivateModelEndpoint('http://127.0.0.1:11434/embed').url, 'http://127.0.0.1:11434/embed')
        with self.assertRaises(LocalEndpointRejected):
            PrivateModelEndpoint('http://models.internal/embed')
        accepted = PrivateModelEndpoint(
            'http://models.internal/embed', trusted_hostnames=frozenset({'models.internal'})
        )
        self.assertEqual(accepted.url, 'http://models.internal/embed')

    def test_local_embedding_adapter_returns_only_valid_private_model_vectors(self) -> None:
        transport = FakeTransport(
            [
                {'available': True},
                {'data': [{'embedding': [0.1, 0.2]}, {'embedding': [0.3, 0.4]}]},
            ]
        )
        adapter = LocalEmbeddingAdapter(
            endpoint=PrivateModelEndpoint('http://localhost:11434/epub'),
            transport=transport,
            profile='private-embed-v1',
        )
        self.assertTrue(adapter.availability().available)
        self.assertEqual(adapter.embed(['第一段', 'Second paragraph']), [[0.1, 0.2], [0.3, 0.4]])
        self.assertEqual(transport.calls[1][1]['op'], 'embed')

    def test_unavailable_private_runtime_is_degraded_without_any_cloud_fallback(self) -> None:
        transport = FakeTransport([ConnectionError('local service refused connection')])
        adapter = LocalEmbeddingAdapter(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:11434/epub'),
            transport=transport,
            profile='private-embed-v1',
        )
        availability = adapter.availability()
        self.assertFalse(availability.available)
        self.assertIn('refused', availability.reason or '')
        self.assertEqual(len(transport.calls), 1)

    def test_local_llm_resolver_availability_is_explicitly_degraded(self) -> None:
        transport = FakeTransport([{'available': False, 'reason': 'model is loading'}])
        resolver = LocalConceptResolverAdapter(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:11434/epub'),
            transport=transport,
            profile='private-llm-v1',
        )
        availability = resolver.availability()
        self.assertFalse(availability.available)
        self.assertEqual(availability.component, 'local-concept-resolver')
        self.assertEqual(availability.reason, 'model is loading')

    def test_reranker_and_tier_two_resolver_use_the_same_private_boundary(self) -> None:
        transport = FakeTransport([{'scores': [0.9, 0.1]}, {'concept': None}])
        endpoint = PrivateModelEndpoint('http://10.1.2.3:8080/infer')
        reranker = LocalRerankerAdapter(endpoint=endpoint, transport=transport, profile='private-rerank-v1')
        resolver = LocalConceptResolverAdapter(endpoint=endpoint, transport=transport, profile='private-llm-v1')
        self.assertEqual(reranker.score('TCP 是什么', ['TCP', 'HTTP']), [0.9, 0.1])
        self.assertIsNone(resolver.resolve('tcp', ['TCP', 'HTTP']))
        self.assertEqual([call[1]['op'] for call in transport.calls], ['rerank', 'resolve_concept'])

    def test_llama_cpp_resolver_uses_desktop_openai_endpoint_and_strict_json(self) -> None:
        transport = FakeLlamaCppTransport(
            health={'status': 'ok'},
            completions=[{'choices': [{'message': {'content': '{"concept":"拥塞控制"}'}}]}],
        )
        resolver = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881/v1'),
            transport=transport,
            profile='qwen-local.gguf',
        )
        self.assertTrue(resolver.availability().available)
        self.assertEqual(resolver.resolve('网络为什么变慢', ['拥塞控制', '流量整形']), '拥塞控制')
        self.assertEqual(transport.calls[0][:2], ('GET', 'http://127.0.0.1:18881/health'))
        method, url, payload = transport.calls[1]
        self.assertEqual((method, url), ('POST', 'http://127.0.0.1:18881/v1/chat/completions'))
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['temperature'], 0)

    def test_llama_cpp_resolver_reads_a_bare_null_as_the_abstention_it_is(self) -> None:
        """Declining to answer is a result, not a malformed response.

        A local Qwen2.5-3B answers a query it cannot place with the bare JSON
        document ``null`` rather than with ``{"concept": null}``.  Both are the
        model saying "none of these", and only the second used to be understood:
        the first raised, so search reported ``local-concept-resolver`` degraded
        and a clean abstention became indistinguishable from a stopped runtime.
        Abstaining well is the behaviour that makes this tier safe to enable at
        all, so it is pinned here rather than left to the object form.

        Strictness is unchanged wherever an answer can actually arrive: an
        object that is not exactly ``{"concept": ...}`` is still refused, so the
        second spelling admits no concept the first would not have.
        """
        abstained = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881'),
            transport=FakeLlamaCppTransport(
                health={'status': 'ok'}, completions=[{'choices': [{'message': {'content': 'null'}}]}]
            ),
            profile='qwen-local.gguf',
        )
        self.assertIsNone(abstained.resolve('这本书跟潮汐完全无关的一个问题', ['已有概念']))

        chatty = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881'),
            transport=FakeLlamaCppTransport(
                health={'status': 'ok'},
                completions=[{'choices': [{'message': {'content': '{"concept":"已有概念","why":"因为"}'}}]}],
            ),
            profile='qwen-local.gguf',
        )
        with self.assertRaisesRegex(LocalInferenceUnavailable, 'invalid schema'):
            chatty.resolve('查询', ['已有概念'])

    def test_llama_cpp_resolver_fails_closed_for_bad_json_unknown_concept_and_transport_error(self) -> None:
        invalid = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881'),
            transport=FakeLlamaCppTransport(
                health={'status': 'ok'}, completions=[{'choices': [{'message': {'content': 'not json'}}]}]
            ),
            profile='qwen-local.gguf',
        )
        with self.assertRaisesRegex(LocalInferenceUnavailable, 'invalid concept JSON'):
            invalid.resolve('查询', ['已有概念'])
        unknown = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881'),
            transport=FakeLlamaCppTransport(
                health={'status': 'ok'}, completions=[{'choices': [{'message': {'content': '{"concept":"新概念"}'}}]}]
            ),
            profile='qwen-local.gguf',
        )
        with self.assertRaisesRegex(LocalInferenceUnavailable, 'outside'):
            unknown.resolve('查询', ['已有概念'])
        unavailable = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881'),
            transport=FakeLlamaCppTransport(health=ConnectionError('offline')),
            profile='qwen-local.gguf',
        )
        self.assertFalse(unavailable.availability().available)

    def test_llama_cpp_resolve_async_runs_blocking_transport_off_the_event_loop(self) -> None:
        transport = FakeLlamaCppTransport(
            health={'status': 'ok'},
            completions=[{'choices': [{'message': {'content': '{"concept":null}'}}]}],
        )
        resolver = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881'), transport=transport, profile='qwen-local.gguf'
        )
        self.assertIsNone(asyncio.run(resolver.resolve_async('没有对应概念', ['已有概念'])))
        self.assertNotEqual(transport.thread_ids, [threading.get_ident()])

    def test_index_binds_embedding_to_exact_parent_window_and_never_cites_a_vector_text(self) -> None:
        source = FakeSource()
        embeddings = FakeEmbeddings()
        backend = InMemoryDerivedVectorBackend()
        result = DerivedVectorIndexer(source=source, embeddings=embeddings, backend=backend).index('unit-1')

        self.assertEqual(result.state, 'READY')
        self.assertEqual(embeddings.calls, [['文，A']])
        record = backend.records['unit-1']
        self.assertEqual(record.passage_id, 'passage-1')
        self.assertEqual((record.start_codepoint, record.end_codepoint), (1, 4))
        self.assertFalse(hasattr(record, 'content'))

    def test_index_rejects_tampered_parent_window_or_profile(self) -> None:
        backend = InMemoryDerivedVectorBackend()
        with self.assertRaisesRegex(VectorIndexError, 'exact parent source window'):
            DerivedVectorIndexer(
                source=FakeSource(unit_content='篡改'), embeddings=FakeEmbeddings(), backend=backend
            ).index('unit-1')
        with self.assertRaisesRegex(VectorIndexError, 'embedding_profile'):
            DerivedVectorIndexer(
                source=FakeSource(profile='other-embed-v2'), embeddings=FakeEmbeddings(), backend=backend
            ).index('unit-1')

    def test_unavailable_embedding_leaves_window_unindexed_and_reports_degraded(self) -> None:
        embeddings = FakeEmbeddings(available=False)
        backend = InMemoryDerivedVectorBackend()
        result = DerivedVectorIndexer(source=FakeSource(), embeddings=embeddings, backend=backend).index('unit-1')
        self.assertEqual(result.state, 'DEGRADED')
        self.assertEqual(embeddings.calls, [])
        self.assertEqual(backend.records, {})

    def test_aurapro_embedding_bridge_runs_the_async_rag_function_on_its_application_loop(self) -> None:
        called_on: list[int] = []

        async def embeddings(texts):
            called_on.append(threading.get_ident())
            self.assertEqual(texts, ['第一段', '第二段'])
            return [[0.1, 0.2], [0.3, 0.4]]

        with RunningLoop() as running:
            adapter = AuraProEmbeddingAdapter(
                embedding_function=embeddings,
                event_loop=running.loop,
                profile='bge-m3-local',
                local_permitted=True,
            )
            self.assertTrue(adapter.availability().available)
            self.assertEqual(adapter.embed(['第一段', '第二段']), [[0.1, 0.2], [0.3, 0.4]])
            self.assertEqual(called_on, [running.thread.ident])

    def test_aurapro_embedding_fails_closed_without_explicit_local_permission(self) -> None:
        calls: list[list[str]] = []

        async def embeddings(texts):
            calls.append(list(texts))
            return [[0.1, 0.2]]

        with RunningLoop() as running:
            adapter = AuraProEmbeddingAdapter(
                embedding_function=embeddings,
                event_loop=running.loop,
                profile='configured-but-not-proven-local',
                local_permitted=False,
            )
            availability = adapter.availability()
            self.assertFalse(availability.available)
            self.assertIn('not explicitly', availability.reason or '')
            with self.assertRaises(LocalInferenceUnavailable):
                adapter.embed(['不会发送'])
        self.assertEqual(calls, [])

    def test_aurapro_embedding_rejects_sync_bridge_from_its_own_event_loop(self) -> None:
        async def embeddings(_texts):
            return [[0.1, 0.2]]

        with RunningLoop() as running:
            adapter = AuraProEmbeddingAdapter(
                embedding_function=embeddings,
                event_loop=running.loop,
                profile='bge-m3-local',
                local_permitted=True,
            )

            async def invoke_from_loop():
                with self.assertRaisesRegex(LocalInferenceUnavailable, 'cannot synchronously bridge'):
                    adapter.embed(['同一事件循环'])

            asyncio.run_coroutine_threadsafe(invoke_from_loop(), running.loop).result(timeout=2)

    def test_aurapro_reranker_wraps_immutable_strings_as_page_content_documents(self) -> None:
        received: list[object] = []

        def rerank(query, documents):
            self.assertEqual(query, '什么是红楼梦')
            received.extend(documents)
            return (0.9, 0.2)

        adapter = AuraProRerankerAdapter(
            reranking_function=rerank,
            profile='bge-reranker-local',
            local_permitted=True,
        )
        self.assertEqual(adapter.score('什么是红楼梦', ['贾宝玉', '林黛玉']), [0.9, 0.2])
        self.assertEqual([document.page_content for document in received], ['贾宝玉', '林黛玉'])
        self.assertTrue(all(isinstance(document, AuraProRerankDocument) for document in received))

    def test_aurapro_reranker_fails_closed_without_local_permission(self) -> None:
        calls: list[object] = []

        def rerank(_query, _documents):
            calls.append(True)
            return [1.0]

        adapter = AuraProRerankerAdapter(
            reranking_function=rerank,
            profile='configured-but-not-proven-local',
            local_permitted=False,
        )
        self.assertFalse(adapter.availability().available)
        with self.assertRaises(LocalInferenceUnavailable):
            adapter.score('查询', ['不会发送'])
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
