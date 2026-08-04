"""Authorization and composition tests for the authenticated EPUB REST API."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The router needs only these two dependency identities.  Importing the full
# application auth module also bootstraps its database models, which is outside
# this isolated route/service acceptance test.  FastAPI still executes the
# router's real dependency wiring below via these overridden identities.
auth_dependencies = ModuleType("open_webui.utils.auth")
auth_dependencies.get_admin_user = lambda: None
auth_dependencies.get_verified_user = lambda: None
sys.modules.setdefault("open_webui.utils.auth", auth_dependencies)

from open_webui.retrieval.epub.store import SQLiteEpubStore  # noqa: E402
from open_webui.routers.epub import get_epub_concept_service, router  # noqa: E402
from open_webui.services.epub_concept import EpubConceptService  # noqa: E402
from open_webui.services.epub_runtime import initialize_epub_concept_service  # noqa: E402
from open_webui.utils.auth import get_admin_user, get_verified_user  # noqa: E402


def _ordinary_user():
    return SimpleNamespace(id="ordinary", role="user")


def _admin_user():
    return SimpleNamespace(id="administrator", role="admin")


def _ordinary_user_cannot_administer():
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="access prohibited")


class EpubAuthenticatedApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        store = SQLiteEpubStore(str(Path(self.temporary.name) / "epub.db"))
        book_id = store.create_book("共享图书", book_id="book-1")
        store.create_book_version(book_id, epub_bytes=b"api-test-epub", version_id="version-1")
        store.add_passages(
            "version-1",
            [
                {
                    "passage_id": "passage-1",
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": 0,
                    "content_kind": "paragraph",
                    "content": "TCP 是传输控制协议。原文必须完整返回。",
                }
            ],
        )
        store.set_version_status("version-1", "READY")
        concept_id = store.upsert_concept("TCP", aliases=["Transmission Control Protocol"], status="APPROVED")
        store.add_concept_mention(
            concept_id, "passage-1", start_codepoint=0, end_codepoint=3, evidence="TCP", source="ADMIN"
        )
        self.service = EpubConceptService(store=store)
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_epub_concept_service] = lambda: self.service
        self.app.dependency_overrides[get_verified_user] = _ordinary_user
        self.app.dependency_overrides[get_admin_user] = _ordinary_user_cannot_administer
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ordinary_verified_user_can_browse_and_search_shared_content(self) -> None:
        books = self.client.get("/api/v1/epub/books")
        self.assertEqual(books.status_code, 200)
        self.assertEqual(books.json()[0]["book_id"], "book-1")

        book = self.client.get("/api/v1/epub/books/book-1")
        self.assertEqual(book.status_code, 200)
        self.assertEqual(book.json()["versions"][0]["version_id"], "version-1")

        passages = self.client.get("/api/v1/epub/versions/version-1/passages")
        self.assertEqual(passages.status_code, 200)
        self.assertEqual(passages.json()["items"][0]["content"], "TCP 是传输控制协议。原文必须完整返回。")

        result = self.client.post("/api/v1/epub/search", json={"query": "TCP"})
        self.assertEqual(result.status_code, 200)
        hit = result.json()["graph_results"][0]
        self.assertEqual(hit["content"], "TCP 是传输控制协议。原文必须完整返回。")
        self.assertEqual(hit["excerpt"], {"content": "TCP", "start_codepoint": 0, "end_codepoint": 3})

    def test_ordinary_user_cannot_mutate_epub_domain(self) -> None:
        mutations = [
            ("put", "/api/v1/epub/admin/concepts", {"canonical_name": "HTTP"}),
            (
                "post",
                "/api/v1/epub/admin/batches",
                {"version_id": "version-1", "profile_name": "server-batch-profile"},
            ),
            (
                "post",
                "/api/v1/epub/admin/calibrations/local",
                {"version_id": "version-1"},
            ),
            ("post", "/api/v1/epub/admin/batches/missing/submit", None),
            ("post", "/api/v1/epub/admin/batches/missing/poll", None),
            ("post", "/api/v1/epub/admin/batches/missing/retry", None),
            ("post", "/api/v1/epub/admin/retrieval-units/missing/index", None),
            ("post", "/api/v1/epub/admin/versions/version-1/index", {"rebuild": False}),
        ]
        for method, url, payload in mutations:
            response = getattr(self.client, method)(url, json=payload)
            self.assertEqual(response.status_code, 401, url)

        upload = self.client.post(
            "/api/v1/epub/admin/import",
            files={"file": ("book.epub", b"not reached", "application/epub+zip")},
        )
        self.assertEqual(upload.status_code, 401)
        self.assertIsNone(self.service.get_book("new-book"))

    def test_admin_can_create_a_concept_and_batch_draft(self) -> None:
        self.app.dependency_overrides[get_admin_user] = _admin_user
        concept = self.client.put(
            "/api/v1/epub/admin/concepts",
            json={"canonical_name": "HTTP", "aliases": ["Hypertext Transfer Protocol"]},
        )
        self.assertEqual(concept.status_code, 200)
        self.assertTrue(concept.json()["concept_id"])

        draft = self.client.post(
            "/api/v1/epub/admin/batches",
            json={"version_id": "version-1", "profile_name": "server-batch-profile", "is_sample": True},
        )
        self.assertEqual(draft.status_code, 201)
        self.assertEqual(draft.json()["item_count"], 1)
        self.assertEqual(draft.json()["prompt_profile"], "zh-glossary-v3")

    def test_admin_can_run_local_calibration_without_exposing_source_text(self) -> None:
        self.app.dependency_overrides[get_admin_user] = _admin_user
        test_case = self

        class FakeCalibrationRunner:
            def run(self, *, passages, prompt_profile, sample_limit):
                test_case.assertEqual(len(passages), 1)
                test_case.assertEqual(prompt_profile, "zh-glossary-v3")
                test_case.assertEqual(sample_limit, 20)
                return {
                    "mode": "LOCAL_QWEN",
                    "prompt_profile": prompt_profile,
                    "model": "test-local-model",
                    "sample_count": 1,
                    "chapter_count": 1,
                    "valid_items": 1,
                    "invalid_items": 0,
                    "schema_valid_rate": 1.0,
                    "concept_count": 1,
                    "mention_count": 1,
                    "items": [
                        {
                            "passage_id": "passage-1",
                            "ordinal": 0,
                            "toc_path": [],
                            "valid": True,
                            "concept_count": 1,
                            "mention_count": 1,
                            "reason": None,
                        }
                    ],
                }

        self.service._calibration_runner = FakeCalibrationRunner()
        response = self.client.post(
            "/api/v1/epub/admin/calibrations/local", json={"version_id": "version-1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["valid_items"], 1)
        self.assertNotIn("完整返回", response.text)

    def test_admin_can_bulk_index_a_version_without_exposing_unit_ids(self) -> None:
        self.app.dependency_overrides[get_admin_user] = _admin_user
        unit_id = self.service._store.add_retrieval_unit(
            "passage-1", 0, len("TCP 是传输控制协议。原文必须完整返回。")
        )
        indexed: list[str] = []

        class ReadyIndexer:
            def index(self, retrieval_unit_id: str):
                indexed.append(retrieval_unit_id)
                return SimpleNamespace(state="READY", reason=None)

        self.service._vector_indexer = ReadyIndexer()

        response = self.client.post(
            "/api/v1/epub/admin/versions/version-1/index", json={"rebuild": False}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ready"], 1)
        self.assertEqual(indexed, [unit_id])
        self.assertEqual(self.service._store.get_retrieval_unit(unit_id)["vector_state"], "READY")

    def test_service_is_fail_closed_when_startup_did_not_configure_it(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_verified_user] = _ordinary_user
        response = TestClient(app).get("/api/v1/epub/books")
        self.assertEqual(response.status_code, 503)

    def test_initialized_runtime_makes_the_real_router_available(self) -> None:
        app = FastAPI()
        app.include_router(router)
        initialize_epub_concept_service(app, data_dir=self.temporary.name, environment={})
        app.dependency_overrides[get_verified_user] = _ordinary_user
        response = TestClient(app).get("/api/v1/epub/books")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        app.state.EPUB_CONCEPT_STORE.close()


if __name__ == "__main__":
    unittest.main()
