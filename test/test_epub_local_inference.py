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
select_resident_llama_cpp_model = INFERENCE.select_resident_llama_cpp_model
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


def inventory(*entries: tuple[str, str]) -> dict[str, object]:
    """A *router* ``GET /v1/models`` snapshot from ``(model id, status)`` pairs.

    llama.cpp's router lists every *servable* model and stamps each one
    ``loaded`` or ``unloaded``, so an entry appearing here is not on its own
    permission to request it.  The real response also carries an ollama-style
    ``models`` array with a different shape and no load state at all, so it is
    included here -- listing every id, resident or not -- to keep any reader
    that reached for it visibly wrong.
    """
    return {
        'object': 'list',
        'data': [{'id': identifier, 'object': 'model', 'status': {'value': status}} for identifier, status in entries],
        'models': [{'name': identifier, 'model': identifier, 'size': 0} for identifier, _status in entries],
    }


def plain_server_inventory(*identifiers: str) -> dict[str, object]:
    """A non-router ``llama-server`` snapshot, as observed from build b10106.

    One model named on the command line, no model router.  Entry keys are
    exactly ``aliases``/``created``/``id``/``meta``/``object``/``owned_by``/
    ``tags`` -- there is no ``status`` member to read, and ``id`` is the model's
    full filesystem path rather than a short name.  This is what the documented
    static-configuration route points at.
    """
    return {
        'object': 'list',
        'data': [
            {
                'aliases': [],
                'created': 1757000000,
                'id': identifier,
                'meta': {'n_ctx_train': 32768, 'n_params': 3000000000},
                'object': 'model',
                'owned_by': 'llamacpp',
                'tags': [],
            }
            for identifier in identifiers
        ],
        'models': [{'name': identifier, 'model': identifier, 'size': 0} for identifier in identifiers],
    }


class FakeLlamaCppTransport:
    def __init__(self, *, models: object, completions: list[object] | None = None):
        self.models = models
        self.completions = list(completions or [])
        self.calls: list[tuple[str, str, object | None]] = []
        self.thread_ids: list[int] = []

    @property
    def posted_models(self) -> list[object]:
        return [payload['model'] for method, _url, payload in self.calls if method == 'POST']

    def get_json(self, url: str):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(('GET', url, None))
        if isinstance(self.models, Exception):
            raise self.models
        return self.models

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
            models=inventory(('resident.gguf', 'loaded')),
            completions=[{'choices': [{'message': {'content': '{"concept":"拥塞控制"}'}}]}],
        )
        resolver = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881/v1'),
            transport=transport,
            profile='qwen-local.gguf',
        )
        self.assertTrue(resolver.availability().available)
        self.assertEqual(resolver.resolve('网络为什么变慢', ['拥塞控制', '流量整形']), '拥塞控制')
        self.assertEqual(transport.calls[0][:2], ('GET', 'http://127.0.0.1:18881/v1/models'))
        method, url, payload = transport.calls[-1]
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
                models=inventory(('resident.gguf', 'loaded')),
                completions=[{'choices': [{'message': {'content': 'null'}}]}],
            ),
            profile='qwen-local.gguf',
        )
        self.assertIsNone(abstained.resolve('这本书跟潮汐完全无关的一个问题', ['已有概念']))

        chatty = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881'),
            transport=FakeLlamaCppTransport(
                models=inventory(('resident.gguf', 'loaded')),
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
                models=inventory(('resident.gguf', 'loaded')),
                completions=[{'choices': [{'message': {'content': 'not json'}}]}],
            ),
            profile='qwen-local.gguf',
        )
        with self.assertRaisesRegex(LocalInferenceUnavailable, 'invalid concept JSON'):
            invalid.resolve('查询', ['已有概念'])
        unknown = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881'),
            transport=FakeLlamaCppTransport(
                models=inventory(('resident.gguf', 'loaded')),
                completions=[{'choices': [{'message': {'content': '{"concept":"新概念"}'}}]}],
            ),
            profile='qwen-local.gguf',
        )
        with self.assertRaisesRegex(LocalInferenceUnavailable, 'outside'):
            unknown.resolve('查询', ['已有概念'])
        unavailable = LlamaCppConceptResolver(
            endpoint=PrivateModelEndpoint('http://127.0.0.1:18881'),
            transport=FakeLlamaCppTransport(models=ConnectionError('offline')),
            profile='qwen-local.gguf',
        )
        self.assertFalse(unavailable.availability().available)

    def test_llama_cpp_resolve_async_runs_blocking_transport_off_the_event_loop(self) -> None:
        transport = FakeLlamaCppTransport(
            models=inventory(('resident.gguf', 'loaded')),
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


class ResidentModelBorrowingTest(unittest.TestCase):
    """Tier-2 names the model llama.cpp already holds, or it does not run.

    llama.cpp is launched with ``--models-max 1``, which bounds how many models
    can be resident at once but leaves *every* preset entry servable.  A
    completion request naming an entry that is not resident therefore evicts the
    one that is -- the model the user is chatting with -- and the user's next
    message evicts ours to load theirs back.  Two multi-gigabyte loads to answer
    one concept lookup is strictly worse than not answering it, so this tier
    borrows whatever is already in memory and otherwise degrades.

    The configured model name survives only as a tie-breaker.  It cannot be
    trusted as an instruction: Desktop writes its runtime descriptor the moment
    llama.cpp reports healthy, which -- with ``load-on-startup = false`` -- is
    exactly the moment nothing is loaded.
    """

    ENDPOINT = PrivateModelEndpoint('http://127.0.0.1:18881')
    HINT = 'descriptor-hint.gguf'

    def _resolver(self, transport, *, hint: str | None = None):
        return LlamaCppConceptResolver(
            endpoint=self.ENDPOINT,
            transport=transport,
            profile=self.HINT if hint is None else hint,
        )

    @staticmethod
    def _answer() -> list[object]:
        return [{'choices': [{'message': {'content': '{"concept":"已有概念"}'}}]}]

    def test_the_one_resident_model_is_borrowed_even_when_the_hint_names_another(self) -> None:
        transport = FakeLlamaCppTransport(
            models=inventory(
                (self.HINT, 'unloaded'),
                ('what-the-deployer-loaded.gguf', 'loaded'),
                ('another-preset-entry.gguf', 'unloaded'),
            ),
            completions=self._answer(),
        )
        resolver = self._resolver(transport)

        self.assertTrue(resolver.availability().available)
        # Availability is the inventory read itself.  `GET /health` is not
        # consulted at all: a router holding nothing still answers it `ok`.
        self.assertEqual(transport.calls, [('GET', 'http://127.0.0.1:18881/v1/models', None)])

        self.assertEqual(resolver.resolve('查询', ['已有概念']), '已有概念')
        self.assertEqual(transport.posted_models, ['what-the-deployer-loaded.gguf'])

    def test_nothing_resident_degrades_with_that_reason_and_asks_for_no_completion(self) -> None:
        transport = FakeLlamaCppTransport(
            models=inventory((self.HINT, 'unloaded'), ('the-users-chat-model.gguf', 'unloaded')),
            completions=self._answer(),
        )
        resolver = self._resolver(transport)

        availability = resolver.availability()
        self.assertFalse(availability.available)
        self.assertEqual(availability.component, 'llama.cpp-concept-resolver')
        # The reason has to say what is wrong.  Before discovery, availability
        # reported ready here and the tier degraded one step later with a raw
        # HTTP error from the completion request.
        self.assertIn('no model is loaded', availability.reason or '')
        self.assertIn('never triggers a load', availability.reason or '')

        with self.assertRaisesRegex(LocalInferenceUnavailable, 'no model is loaded'):
            resolver.resolve('查询', ['已有概念'])
        self.assertEqual(transport.posted_models, [])

    def test_a_hint_breaks_a_tie_only_among_models_that_are_already_resident(self) -> None:
        transport = FakeLlamaCppTransport(
            models=inventory((self.HINT, 'loaded'), ('the-users-chat-model.gguf', 'loaded')),
            completions=self._answer(),
        )
        self.assertEqual(self._resolver(transport).resolve('查询', ['已有概念']), '已有概念')
        self.assertEqual(transport.posted_models, [self.HINT])

    def test_several_resident_models_without_a_hint_match_degrade_rather_than_guess(self) -> None:
        for hint in (self.HINT, ''):
            with self.subTest(hint=hint or '<no hint configured>'):
                transport = FakeLlamaCppTransport(
                    models=inventory(('the-users-chat-model.gguf', 'loaded'), ('a-second-model.gguf', 'loaded')),
                    completions=self._answer(),
                )
                resolver = self._resolver(transport, hint=hint)
                availability = resolver.availability()
                self.assertFalse(availability.available)
                self.assertIn('refusing to guess', availability.reason or '')
                with self.assertRaisesRegex(LocalInferenceUnavailable, 'refusing to guess'):
                    resolver.resolve('查询', ['已有概念'])
                self.assertEqual(transport.posted_models, [])

    def test_an_unreachable_or_malformed_inventory_fails_closed(self) -> None:
        for label, models in (
            ('transport error', ConnectionError('connection refused')),
            ('not a JSON object', 'a bare string'),
            ('no data member', {'object': 'list'}),
            ('data is not a list', {'object': 'list', 'data': {'id': 'the-users-chat-model.gguf'}}),
            ('entries are not objects', {'object': 'list', 'data': ['the-users-chat-model.gguf']}),
            # An ollama-style `models` array is never a substitute for `data`.
            ('only the ollama-style array', {'models': [{'name': 'the-users-chat-model.gguf'}]}),
        ):
            with self.subTest(inventory=label):
                transport = FakeLlamaCppTransport(models=models, completions=self._answer())
                resolver = self._resolver(transport)
                availability = resolver.availability()
                self.assertFalse(availability.available)
                self.assertTrue(availability.reason)
                with self.assertRaises(LocalInferenceUnavailable):
                    resolver.resolve('查询', ['已有概念'])
                self.assertEqual(transport.posted_models, [])

    def test_no_inventory_shape_can_select_an_entry_that_is_not_reported_loaded(self) -> None:
        """On a runtime that reports load state, only ``loaded`` is usable.

        Every entry below carries a ``status`` member, which is what puts the
        inventory on the router path.  Anything but the exact string ``loaded``
        -- and a malformed status is "anything but" -- keeps it out of a
        request, because on a router an entry it names may well be the user's
        chat model sitting in memory.
        """
        not_resident = (
            {'id': 'the-users-chat-model.gguf', 'status': {'value': 'unloaded'}},
            {'id': 'the-users-chat-model.gguf', 'status': {'value': 'LOADED'}},
            {'id': 'the-users-chat-model.gguf', 'status': {'value': 'loading'}},
            {'id': 'the-users-chat-model.gguf', 'status': {'value': True}},
            {'id': 'the-users-chat-model.gguf', 'status': {'value': None}},
            {'id': 'the-users-chat-model.gguf', 'status': {}},
            {'id': 'the-users-chat-model.gguf', 'status': 'loaded'},
            {'id': 'the-users-chat-model.gguf', 'status': None},
            # A resident entry with no usable id is still not requestable.
            {'status': {'value': 'loaded'}},
            {'id': '   ', 'status': {'value': 'loaded'}},
        )
        for entry in not_resident:
            with self.subTest(entry=entry):
                transport = FakeLlamaCppTransport(
                    models={'object': 'list', 'data': [entry]}, completions=self._answer()
                )
                # The hint names this very entry, so nothing but the reported
                # status is keeping it out of the completion request.
                resolver = self._resolver(transport, hint='the-users-chat-model.gguf')
                self.assertFalse(resolver.availability().available)
                with self.assertRaisesRegex(LocalInferenceUnavailable, 'no model is loaded'):
                    resolver.resolve('查询', ['已有概念'])
                self.assertEqual(transport.posted_models, [])

    def test_selection_is_a_pure_function_of_one_inventory_snapshot(self) -> None:
        self.assertEqual(
            select_resident_llama_cpp_model(inventory(('only.gguf', 'loaded'), ('other.gguf', 'unloaded'))),
            'only.gguf',
        )
        # A duplicated entry is one resident model, not an ambiguous pair.
        self.assertEqual(
            select_resident_llama_cpp_model(inventory(('only.gguf', 'loaded'), ('only.gguf', 'loaded'))),
            'only.gguf',
        )
        with self.assertRaisesRegex(LocalInferenceUnavailable, 'no model is loaded'):
            select_resident_llama_cpp_model(inventory(('only.gguf', 'unloaded')), hint='only.gguf')

    def test_a_plain_single_model_server_reports_no_load_state_and_is_borrowed(self) -> None:
        """A non-router ``llama-server`` has one model and nothing to evict.

        Observed on build b10106, the build Desktop pins: a plain
        ``llama-server -m <model>.gguf`` answers ``/v1/models`` with one entry
        whose keys are aliases/created/id/meta/object/owned_by/tags.  There is
        no ``status`` member -- not ``unloaded``, absent.  Demanding one would
        permanently degrade the documented static-configuration route and tell
        its operator "no model is loaded" about a model that is loaded and
        serving.  The eviction hazard needs a router with several servable
        entries, and a router stamps a status on every one of them, so reading
        load state only where it is reported gives up none of the guarantee.
        """
        path_id = '/opt/models/an-instruct-model-q4_k_m.gguf'
        transport = FakeLlamaCppTransport(models=plain_server_inventory(path_id), completions=self._answer())
        resolver = self._resolver(transport)

        self.assertTrue(resolver.availability().available)
        self.assertEqual(resolver.resolve('查询', ['已有概念']), '已有概念')
        # `id` is the full filesystem path, which is also why a configured model
        # name could never have selected anything on a runtime like this one.
        self.assertEqual(transport.posted_models, [path_id])

    def test_several_models_with_no_reported_load_state_degrade_rather_than_guess(self) -> None:
        transport = FakeLlamaCppTransport(
            models=plain_server_inventory('/opt/models/first.gguf', '/opt/models/second.gguf'),
            completions=self._answer(),
        )
        resolver = self._resolver(transport, hint='/opt/models/first.gguf')
        availability = resolver.availability()

        self.assertFalse(availability.available)
        self.assertIn('ambiguous', availability.reason or '')
        self.assertIn('refusing to guess', availability.reason or '')
        with self.assertRaises(LocalInferenceUnavailable):
            resolver.resolve('查询', ['已有概念'])
        self.assertEqual(transport.posted_models, [])

    def test_one_entry_reporting_load_state_puts_the_whole_inventory_on_the_router_path(self) -> None:
        """A status anywhere means a router, and a router is never inferred from.

        The status-less entries in a mixed inventory are exactly the ones that
        must not be borrowed: on a router they are entries whose load state was
        not reported, not entries from a server that has no load state.
        """
        stamped = {'id': '/opt/models/stamped.gguf', 'object': 'model', 'status': {'value': 'unloaded'}}
        bare = {'id': '/opt/models/bare.gguf', 'object': 'model'}

        nothing_loaded = FakeLlamaCppTransport(
            models={'object': 'list', 'data': [stamped, bare]}, completions=self._answer()
        )
        resolver = self._resolver(nothing_loaded, hint='/opt/models/bare.gguf')
        self.assertFalse(resolver.availability().available)
        with self.assertRaisesRegex(LocalInferenceUnavailable, 'no model is loaded'):
            resolver.resolve('查询', ['已有概念'])
        self.assertEqual(nothing_loaded.posted_models, [])

        # The stamped-and-loaded entry wins outright; the bare one is not a
        # second candidate, so this is not even an ambiguous inventory.
        one_loaded = FakeLlamaCppTransport(
            models={
                'object': 'list',
                'data': [{**stamped, 'status': {'value': 'loaded'}}, bare],
            },
            completions=self._answer(),
        )
        self.assertEqual(self._resolver(one_loaded).resolve('查询', ['已有概念']), '已有概念')
        self.assertEqual(one_loaded.posted_models, ['/opt/models/stamped.gguf'])

    def test_a_cold_runtime_and_a_misconfigured_one_report_different_reasons(self) -> None:
        """An operator has to be able to tell these apart.

        Holding nothing is the ordinary state after llama.cpp starts and fixes
        itself at the user's first chat message.  An inventory that cannot be
        read is a misconfiguration and will not fix itself.
        """
        cold = self._resolver(FakeLlamaCppTransport(models=inventory(('a.gguf', 'unloaded')))).availability()
        empty = self._resolver(FakeLlamaCppTransport(models={'object': 'list', 'data': []})).availability()
        unreadable = self._resolver(FakeLlamaCppTransport(models={'object': 'list'})).availability()
        unreachable = self._resolver(FakeLlamaCppTransport(models=ConnectionError('refused'))).availability()
        ambiguous = self._resolver(
            FakeLlamaCppTransport(models=inventory(('a.gguf', 'loaded'), ('b.gguf', 'loaded')))
        ).availability()

        for availability in (cold, empty, unreadable, unreachable, ambiguous):
            self.assertFalse(availability.available)
        for holds_nothing in (cold, empty):
            self.assertIn('no model is loaded', holds_nothing.reason or '')
        self.assertIn('could not be read', unreadable.reason or '')
        self.assertIn('unreachable', unreachable.reason or '')
        self.assertIn('ambiguous', ambiguous.reason or '')
        # The cold-start reason must not read as a misconfiguration, or an
        # operator goes looking for a broken endpoint that is working.
        for misread in ('could not be read', 'unreachable', 'ambiguous'):
            self.assertNotIn(misread, cold.reason or '')


if __name__ == '__main__':
    unittest.main()
