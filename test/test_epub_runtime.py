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


if __name__ == "__main__":
    unittest.main()
