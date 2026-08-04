"""Startup wiring tests for the independent EPUB runtime."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from open_webui.services.epub_runtime import (  # noqa: E402
    EpubRuntimeConfigurationError,
    close_epub_concept_service,
    configure_epub_rag_inference_policy,
    initialize_epub_concept_service,
)


class EpubRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.app = SimpleNamespace(state=SimpleNamespace())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_startup_uses_an_independent_persistent_store_and_attaches_service(self) -> None:
        service = initialize_epub_concept_service(self.app, data_dir=self.temporary.name, environment={})
        expected = Path(self.temporary.name, "epub_concept_v1.db").resolve()
        self.assertEqual(Path(self.app.state.EPUB_CONCEPT_STORE.path), expected)
        self.assertIs(self.app.state.EPUB_CONCEPT_SERVICE, service)
        self.assertEqual(service.list_books(), [])
        runtime_status = self.app.state.EPUB_CONCEPT_RUNTIME_STATUS()
        self.assertFalse(runtime_status["vector_index"]["available"])
        self.assertEqual(runtime_status["vector_index"]["component"], "sqlite-vec")
        self.assertFalse(runtime_status["concept_resolver"]["available"])
        self.assertNotIn("database", runtime_status)
        close_epub_concept_service(self.app)

    def test_explicit_sqlite_path_is_used_but_memory_and_postgres_are_rejected(self) -> None:
        expected = Path(self.temporary.name, "library.db")
        initialize_epub_concept_service(
            self.app,
            data_dir=self.temporary.name,
            environment={"EPUB_CONCEPT_DB_PATH": str(expected)},
        )
        self.assertTrue(expected.exists())
        close_epub_concept_service(self.app)

        for database_url in (":memory:", "postgresql://epub-user@db.example/epub"):
            with self.assertRaises(EpubRuntimeConfigurationError):
                initialize_epub_concept_service(
                    SimpleNamespace(state=SimpleNamespace()),
                    data_dir=self.temporary.name,
                    environment={"EPUB_CONCEPT_DATABASE_URL": database_url},
                )

    def test_batch_key_is_explicit_to_the_epub_domain(self) -> None:
        initialize_epub_concept_service(
            self.app,
            data_dir=self.temporary.name,
            environment={"OPENAI_API_KEY": "must-not-be-reused"},
        )
        self.assertEqual(self.app.state.EPUB_CONCEPT_SERVICE._providers, {})
        close_epub_concept_service(self.app)

    def test_epub_reuses_only_proven_private_or_in_process_rag_models(self) -> None:
        local = SimpleNamespace()
        configure_epub_rag_inference_policy(
            local,
            {
                "rag.embedding_engine": "",
                "rag.embedding_model": "bge-m3",
                "rag.reranking_engine": "",
                "rag.reranking_model": "bge-reranker",
            },
        )
        self.assertTrue(local.EPUB_RAG_EMBEDDING_LOCAL)
        self.assertTrue(local.EPUB_RAG_RERANKER_LOCAL)

        public_ollama = SimpleNamespace()
        configure_epub_rag_inference_policy(
            public_ollama,
            {
                "rag.embedding_engine": "ollama",
                "rag.embedding_model": "nomic-embed-text",
                "rag.ollama.base_url": "https://public.example/v1",
                "rag.reranking_engine": "external",
                "rag.reranking_model": "remote-reranker",
            },
        )
        self.assertFalse(public_ollama.EPUB_RAG_EMBEDDING_LOCAL)
        self.assertFalse(public_ollama.EPUB_RAG_RERANKER_LOCAL)
        self.assertIn("not local/private", public_ollama.EPUB_RAG_EMBEDDING_POLICY.reason or "")
        self.assertIn("built-in local", public_ollama.EPUB_RAG_RERANKER_POLICY.reason or "")

        private_ollama = SimpleNamespace()
        configure_epub_rag_inference_policy(
            private_ollama,
            {
                "rag.embedding_engine": "ollama",
                "rag.embedding_model": "nomic-embed-text",
                "rag.ollama.base_url": "http://127.0.0.1:11434",
                "rag.reranking_engine": "",
                "rag.reranking_model": "bge-reranker",
            },
        )
        self.assertTrue(private_ollama.EPUB_RAG_EMBEDDING_LOCAL)

    def test_invalid_llama_cpp_configuration_is_degraded_without_startup_failure(self) -> None:
        initialize_epub_concept_service(
            self.app,
            data_dir=self.temporary.name,
            environment={
                "EPUB_CONCEPT_LOCAL_LLM_ENDPOINT": "https://public.example/v1",
                "EPUB_CONCEPT_LOCAL_LLM_MODEL": "local-model",
            },
        )
        status = self.app.state.EPUB_CONCEPT_RUNTIME_STATUS()
        self.assertFalse(status["concept_resolver"]["available"])
        self.assertIn("neither local/private", status["concept_resolver"]["reason"])
        close_epub_concept_service(self.app)


if __name__ == "__main__":
    unittest.main()
