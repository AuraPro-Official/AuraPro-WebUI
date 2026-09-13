"""Startup wiring tests for the independent EPUB runtime."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from open_webui.retrieval.epub.inference import ModelAvailability  # noqa: E402
from open_webui.retrieval.epub.search import EpubSearchService  # noqa: E402
from open_webui.retrieval.epub.vector_index import DerivedVectorRecord  # noqa: E402
from open_webui.services.epub_runtime import (  # noqa: E402
    EpubRuntimeConfigurationError,
    _aurapro_rag_models,
    close_epub_concept_service,
    configure_epub_rag_inference_policy,
    initialize_epub_concept_service,
)


# The shipped defaults, as AuraPro persists them on a first boot: no engine is
# selected on either side, an embedding model is named, and the reranking model
# is the empty string.  Restated here rather than imported because importing
# open_webui.config at test time unlinks the static directory.
DEFAULT_RAG_CONFIG = {
    'rag.embedding_engine': '',
    'rag.embedding_model': 'sentence-transformers/all-MiniLM-L6-v2',
    'rag.ollama.base_url': '',
    'rag.reranking_engine': '',
    'rag.reranking_model': '',
}


def _digest(value: str) -> str:
    return sha256(value.encode('utf-8')).hexdigest()


class OneWindowSource:
    """The smallest source a search will read: one passage, one derived window.

    The vocabulary is empty on purpose.  This fixture exists to answer "does
    the vector channel run at all", and an empty vocabulary keeps the graph
    channel out of the answer, so a non-empty ``vector_results`` can only have
    come from the channel under test.
    """

    content = '潮位基准的复核在每个汛期开始之前完成。'

    def __init__(self) -> None:
        self.passage = {
            'passage_id': 'tide-1',
            'book_title': '潮汐观测手册',
            'toc_path': ('第一章',),
            'content': self.content,
            'content_sha256': _digest(self.content),
        }
        window = self.content[0:6]
        self.unit = {
            'retrieval_unit_id': 'tide-1-w1',
            'passage_id': 'tide-1',
            'start_codepoint': 0,
            'end_codepoint': 6,
            'content': window,
            'content_sha256': _digest(window),
        }

    def list_concept_terms(self):
        return []

    def concept_term_fingerprint(self):
        return (0,)

    def get_search_passage(self, passage_id):
        return self.passage if passage_id == self.passage['passage_id'] else None

    def get_retrieval_unit(self, retrieval_unit_id):
        return self.unit if retrieval_unit_id == self.unit['retrieval_unit_id'] else None

    def matched_concept_names(self, passage_id, concept_ids):
        return ()


class OneWindowVectorBackend:
    def __init__(self, source: OneWindowSource) -> None:
        self.record = DerivedVectorRecord(
            retrieval_unit_id=str(source.unit['retrieval_unit_id']),
            passage_id=str(source.unit['passage_id']),
            start_codepoint=int(source.unit['start_codepoint']),
            end_codepoint=int(source.unit['end_codepoint']),
            content_sha256=str(source.unit['content_sha256']),
            embedding_profile=LocalEmbeddings.profile,
            vector=(1.0, 0.0),
        )

    def search(self, query_vector, *, embedding_profile, limit):
        return [self.record]


class LocalEmbeddings:
    """A ready in-process embedding model, which the default config permits."""

    profile = 'private-embed-v1'

    def availability(self):
        return ModelAvailability.ready('local-embedding')

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class EpubRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = SimpleNamespace(state=SimpleNamespace())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_startup_uses_an_independent_persistent_store_and_attaches_service(self) -> None:
        service = initialize_epub_concept_service(self.app, data_dir=self.temporary.name, environment={})
        expected = Path(self.temporary.name, 'epub_concept_v1.db').resolve()
        self.assertEqual(Path(self.app.state.EPUB_CONCEPT_STORE.path), expected)
        self.assertIs(self.app.state.EPUB_CONCEPT_SERVICE, service)
        self.assertEqual(service.list_books(), [])
        runtime_status = self.app.state.EPUB_CONCEPT_RUNTIME_STATUS()
        self.assertFalse(runtime_status['vector_index']['available'])
        self.assertEqual(runtime_status['vector_index']['component'], 'sqlite-vec')
        self.assertFalse(runtime_status['concept_resolver']['available'])
        self.assertNotIn('database', runtime_status)
        close_epub_concept_service(self.app)

    def test_explicit_sqlite_path_is_used_but_memory_and_postgres_are_rejected(self) -> None:
        expected = Path(self.temporary.name, 'library.db')
        initialize_epub_concept_service(
            self.app,
            data_dir=self.temporary.name,
            environment={'EPUB_CONCEPT_DB_PATH': str(expected)},
        )
        self.assertTrue(expected.exists())
        close_epub_concept_service(self.app)

        for database_url in (':memory:', 'postgresql://epub-user@db.example/epub'):
            with self.assertRaises(EpubRuntimeConfigurationError):
                initialize_epub_concept_service(
                    SimpleNamespace(state=SimpleNamespace()),
                    data_dir=self.temporary.name,
                    environment={'EPUB_CONCEPT_DATABASE_URL': database_url},
                )

    def test_batch_key_is_explicit_to_the_epub_domain(self) -> None:
        initialize_epub_concept_service(
            self.app,
            data_dir=self.temporary.name,
            environment={'OPENAI_API_KEY': 'must-not-be-reused'},
        )
        self.assertEqual(self.app.state.EPUB_CONCEPT_SERVICE._providers, {})
        close_epub_concept_service(self.app)

    def test_epub_reuses_only_proven_private_or_in_process_rag_models(self) -> None:
        local = SimpleNamespace()
        configure_epub_rag_inference_policy(
            local,
            {
                'rag.embedding_engine': '',
                'rag.embedding_model': 'bge-m3',
                'rag.reranking_engine': '',
                'rag.reranking_model': 'bge-reranker',
            },
        )
        self.assertTrue(local.EPUB_RAG_EMBEDDING_LOCAL)
        self.assertTrue(local.EPUB_RAG_RERANKER_LOCAL)

        public_ollama = SimpleNamespace()
        configure_epub_rag_inference_policy(
            public_ollama,
            {
                'rag.embedding_engine': 'ollama',
                'rag.embedding_model': 'nomic-embed-text',
                'rag.ollama.base_url': 'https://public.example/v1',
                'rag.reranking_engine': 'external',
                'rag.reranking_model': 'remote-reranker',
            },
        )
        self.assertFalse(public_ollama.EPUB_RAG_EMBEDDING_LOCAL)
        self.assertFalse(public_ollama.EPUB_RAG_RERANKER_LOCAL)
        self.assertIn('not local/private', public_ollama.EPUB_RAG_EMBEDDING_POLICY.reason or '')
        self.assertIn('built-in local', public_ollama.EPUB_RAG_RERANKER_POLICY.reason or '')

        private_ollama = SimpleNamespace()
        configure_epub_rag_inference_policy(
            private_ollama,
            {
                'rag.embedding_engine': 'ollama',
                'rag.embedding_model': 'nomic-embed-text',
                'rag.ollama.base_url': 'http://127.0.0.1:11434',
                'rag.reranking_engine': '',
                'rag.reranking_model': 'bge-reranker',
            },
        )
        self.assertTrue(private_ollama.EPUB_RAG_EMBEDDING_LOCAL)

    def test_the_shipped_defaults_still_produce_a_working_vector_channel(self) -> None:
        """A default install must not be silently reduced to a graph-only search.

        This is the wiring the defect lived in, driven at the seam that decides
        it.  ``rag.reranking_model`` ships as the empty string and is persisted
        empty on a first boot — and a persisted row beats a module default, so
        changing the default would not have reached an existing install either
        — which means :func:`_aurapro_rag_models` builds an embedding adapter
        and no Cross-Encoder adapter, and the search service is constructed
        with ``reranker=None``.  On that entirely ordinary configuration two of
        the three channels used to return nothing at all.

        The embedding adapter the policy produces is replaced by an in-process
        double before the search runs, because the real one bridges to an
        application event loop and a loaded model that no unit test has.  What
        is *not* replaced is the value this test is about: the ``reranker`` the
        default configuration actually yields.
        """
        state = SimpleNamespace(EMBEDDING_FUNCTION=None, RERANKING_FUNCTION=None, main_loop=None)
        configure_epub_rag_inference_policy(state, DEFAULT_RAG_CONFIG)
        self.assertEqual(state.EPUB_RAG_EMBEDDING_PROFILE, DEFAULT_RAG_CONFIG['rag.embedding_model'])
        self.assertTrue(state.EPUB_RAG_EMBEDDING_LOCAL)
        # An empty reranking model is not a profile, so no adapter can be built
        # from it however permissive the policy is.
        self.assertIsNone(state.EPUB_RAG_RERANKER_PROFILE)
        self.assertTrue(state.EPUB_RAG_RERANKER_LOCAL)

        embeddings, reranker = _aurapro_rag_models(SimpleNamespace(state=state), {})
        self.assertEqual(getattr(embeddings, 'profile', None), DEFAULT_RAG_CONFIG['rag.embedding_model'])
        self.assertIsNone(reranker)

        source = OneWindowSource()
        response = EpubSearchService(
            source=source,
            vector_backend=OneWindowVectorBackend(source),
            embeddings=LocalEmbeddings(),
            reranker=reranker,
        ).search('汛期之前要复核什么')

        self.assertEqual([hit.passage_id for hit in response.vector_results], ['tide-1'])
        self.assertEqual([hit.passage_id for hit in response.fused_results], ['tide-1'])
        self.assertEqual(response.vector_results[0].excerpt.content, source.unit['content'])
        unreranked = [item for item in response.degraded if item.component == 'local-cross-encoder']
        self.assertEqual(len(unreranked), 1)
        self.assertIn('not reranked', unreranked[0].reason or '')
        self.assertEqual([item for item in response.degraded if item.component.endswith('-search')], [])

    def test_invalid_llama_cpp_configuration_is_degraded_without_startup_failure(self) -> None:
        initialize_epub_concept_service(
            self.app,
            data_dir=self.temporary.name,
            environment={
                'EPUB_CONCEPT_LOCAL_LLM_ENDPOINT': 'https://public.example/v1',
                'EPUB_CONCEPT_LOCAL_LLM_MODEL': 'local-model',
            },
        )
        status = self.app.state.EPUB_CONCEPT_RUNTIME_STATUS()
        self.assertFalse(status['concept_resolver']['available'])
        self.assertIn('neither local/private', status['concept_resolver']['reason'])
        close_epub_concept_service(self.app)

    def test_desktop_runtime_handoff_is_authoritative_over_static_development_settings(self) -> None:
        missing_descriptor = Path(self.temporary.name, 'desktop-llama-runtime.json')
        initialize_epub_concept_service(
            self.app,
            data_dir=self.temporary.name,
            environment={
                'AURAPRO_DESKTOP_LLM_RUNTIME_FILE': str(missing_descriptor),
                'EPUB_CONCEPT_LOCAL_LLM_ENDPOINT': 'http://127.0.0.1:18881',
                'EPUB_CONCEPT_LOCAL_LLM_MODEL': 'stale-development-model',
            },
        )
        status = self.app.state.EPUB_CONCEPT_RUNTIME_STATUS()
        self.assertFalse(status['concept_resolver']['available'])
        self.assertEqual(status['concept_resolver']['reason'], 'Desktop local runtime is not running')
        close_epub_concept_service(self.app)

    def test_invalid_desktop_runtime_descriptor_path_is_degraded_without_startup_failure(self) -> None:
        initialize_epub_concept_service(
            self.app,
            data_dir=self.temporary.name,
            environment={'AURAPRO_DESKTOP_LLM_RUNTIME_FILE': 'relative-runtime.json'},
        )
        status = self.app.state.EPUB_CONCEPT_RUNTIME_STATUS()
        self.assertFalse(status['concept_resolver']['available'])
        self.assertEqual(status['concept_resolver']['reason'], 'Desktop runtime descriptor path must be absolute')
        close_epub_concept_service(self.app)


if __name__ == '__main__':
    unittest.main()
