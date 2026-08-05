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

from open_webui.retrieval.epub.batch import BatchPayloadError, BatchServiceError  # noqa: E402
from open_webui.retrieval.epub.prompt_profiles import (  # noqa: E402
    DEFAULT_CONCEPT_PROMPT_PROFILE,
    available_prompt_profiles,
)
from open_webui.retrieval.epub.store import SQLiteEpubStore  # noqa: E402
from open_webui.routers.epub import get_epub_concept_service, router  # noqa: E402
from open_webui.services.epub_concept import EpubConceptService, EpubServiceError  # noqa: E402
from open_webui.services.epub_runtime import initialize_epub_concept_service  # noqa: E402
from open_webui.utils.auth import get_admin_user, get_verified_user  # noqa: E402


def _ordinary_user():
    return SimpleNamespace(id="ordinary", role="user")


def _admin_user():
    return SimpleNamespace(id="administrator", role="admin")


def _ordinary_user_cannot_administer():
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="access prohibited")


class FailingBatchJobService:
    """A BatchJobService double whose every read path violates an invariant.

    ``BatchServiceError`` belongs to the durable Batch layer; the router knows
    only ``EpubServiceError``.  The application service is the single place
    that translates one into the other, so a double that raises on every read
    path is what proves the translation exists for each of them.
    """

    failure = "batch job 'missing' does not exist"

    def list_job_summaries(self, *, version_id=None, offset=0, limit=50):
        raise BatchServiceError(self.failure)

    def get_job_summary(self, batch_job_id):
        raise BatchServiceError(self.failure)

    def review_sample_job(self, batch_job_id, *, status, reviewed_by):
        raise BatchServiceError(self.failure)

    def list_sample_reviews(self, *, version_id=None, job_kind=None):
        raise BatchServiceError(self.failure)

    def recover_all(self, providers):
        # The recovery path also catches BatchPayloadError, the subclass raised
        # when a provider result cannot become graph records.
        raise BatchPayloadError(self.failure)


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
        related_concept_id = store.upsert_concept("传输控制协议", status="APPROVED")
        store.add_concept_mention(
            related_concept_id,
            "passage-1",
            start_codepoint=5,
            end_codepoint=11,
            evidence="传输控制协议",
            source="ADMIN",
        )
        store.add_concept_relation(
            "version-1",
            concept_id,
            "ELABORATES",
            related_concept_id,
            evidence=[
                {
                    "passage_id": "passage-1",
                    "start_codepoint": 0,
                    "end_codepoint": 3,
                    "evidence": "TCP",
                }
            ],
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
        # The added unified rank channel is API-compatible with the existing
        # graph/vector fields and is empty when no local vector runtime exists.
        self.assertEqual(result.json()["fused_results"], [])

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
                "/api/v1/epub/admin/section-graph-batches",
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
            ("get", "/api/v1/epub/admin/prompt-profiles", None),
            ("get", "/api/v1/epub/admin/batches", None),
            ("get", "/api/v1/epub/admin/batches/missing", None),
            ("post", "/api/v1/epub/admin/batches/recover", None),
			("get", "/api/v1/epub/admin/sample-batch-reviews", None),
			("put", "/api/v1/epub/admin/sample-batches/missing/review", {"status": "APPROVED"}),
            ("get", "/api/v1/epub/admin/relation-assertions", None),
            ("put", "/api/v1/epub/admin/relation-assertions/missing", {"status": "APPROVED"}),
            ("post", "/api/v1/epub/admin/retrieval-units/missing/index", None),
            ("post", "/api/v1/epub/admin/versions/version-1/index", {"rebuild": False}),
        ]
        for method, url, payload in mutations:
            kwargs = {"json": payload} if payload is not None else {}
            response = getattr(self.client, method)(url, **kwargs)
            self.assertEqual(response.status_code, 401, url)

        upload = self.client.post(
            "/api/v1/epub/admin/import",
            files={"file": ("book.epub", b"not reached", "application/epub+zip")},
        )
        self.assertEqual(upload.status_code, 401)
        self.assertIsNone(self.service.get_book("new-book"))

    def test_admin_sees_every_implemented_prompt_profile_without_its_instructions(self) -> None:
        """The administrator UI must be able to select the server's own default.

        A client-side option list silently drops each new profile, so the server
        publishes the identifiers it actually implements.  Instruction text and
        output schemas stay server-owned and must not appear in the response.
        """
        self.app.dependency_overrides[get_admin_user] = _admin_user
        response = self.client.get("/api/v1/epub/admin/prompt-profiles")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"prompt_profiles", "default_prompt_profile"})
        self.assertEqual(
            payload["prompt_profiles"],
            list(available_prompt_profiles()),
        )
        self.assertEqual(payload["default_prompt_profile"], DEFAULT_CONCEPT_PROMPT_PROFILE)
        self.assertIn(DEFAULT_CONCEPT_PROMPT_PROFILE, payload["prompt_profiles"])
        self.assertNotIn("抽取器", response.text)
        self.assertNotIn("system_instruction", response.text)

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
        self.assertEqual(draft.json()["prompt_profile"], DEFAULT_CONCEPT_PROMPT_PROFILE)

        section_graph_draft = self.client.post(
            "/api/v1/epub/admin/section-graph-batches",
            json={"version_id": "version-1", "profile_name": "server-batch-profile", "is_sample": True},
        )
        self.assertEqual(section_graph_draft.status_code, 201)
        self.assertEqual(section_graph_draft.json()["item_count"], 1)
        self.assertEqual(section_graph_draft.json()["job_kind"], "SECTION_GRAPH")
        self.assertEqual(section_graph_draft.json()["prompt_profile"], "zh-section-graph-v1")

        history = self.client.get("/api/v1/epub/admin/batches?version_id=version-1")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["total"], 2)
        job = history.json()["items"][0]
        self.assertNotIn("request_json", job)
        self.assertNotIn("response_json", job)
        self.assertNotIn("last_error", job)

        detail = self.client.get(f"/api/v1/epub/admin/batches/{job['batch_job_id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["items"])
        self.assertNotIn("request_json", detail.json()["items"][0])

        recovery = self.client.post("/api/v1/epub/admin/batches/recover")
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(recovery.json(), {"recovered": [], "skipped": []})

    def test_admin_can_review_version_scoped_relation_assertions(self) -> None:
        self.app.dependency_overrides[get_admin_user] = _admin_user
        listed = self.client.get("/api/v1/epub/admin/relation-assertions?version_id=version-1")
        self.assertEqual(listed.status_code, 200)
        assertion = listed.json()["items"][0]
        self.assertEqual(assertion["predicate"], "ELABORATES")
        self.assertEqual(assertion["evidence"][0]["evidence"], "TCP")

        reviewed = self.client.put(
            f"/api/v1/epub/admin/relation-assertions/{assertion['assertion_id']}",
            json={"status": "APPROVED"},
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.json()["status"], "APPROVED")

    def test_admin_must_approve_a_fully_ingested_matching_sample_before_full_openai_batch(self) -> None:
        self.app.dependency_overrides[get_admin_user] = _admin_user
        sample = self.client.post(
            "/api/v1/epub/admin/batches",
            json={"version_id": "version-1", "profile_name": "cloud-model-snapshot", "is_sample": True},
        )
        self.assertEqual(sample.status_code, 201)
        sample_id = sample.json()["batch_job_id"]

        blocked = self.client.post(
            "/api/v1/epub/admin/batches",
            json={"version_id": "version-1", "profile_name": "cloud-model-snapshot", "is_sample": False},
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("administrator-approved sample", blocked.json()["detail"])

        repository = self.service._batch._repository
        repository.mark_submitted(sample_id, "remote-sample")
        repository.set_provider_state(sample_id, "SUCCEEDED", None)
        for item in repository.list_items(sample_id):
            self.assertTrue(repository.ingest_success(sample_id, item["custom_id"], {"concepts": []}))

        review = self.client.put(
            f"/api/v1/epub/admin/sample-batches/{sample_id}/review", json={"status": "APPROVED"}
        )
        self.assertEqual(review.status_code, 200)
        self.assertEqual(review.json()["version_id"], "version-1")
        self.assertEqual(review.json()["job_kind"], "CONCEPT_MENTIONS")
        self.assertEqual(review.json()["status"], "APPROVED")
        self.assertEqual(review.json()["reviewed_by"], "administrator")
        self.assertNotIn("TCP", review.text)

        wrong_kind = self.client.post(
            "/api/v1/epub/admin/section-graph-batches",
            json={"version_id": "version-1", "profile_name": "cloud-model-snapshot", "is_sample": False},
        )
        self.assertEqual(wrong_kind.status_code, 400)
        self.assertIn("same version and job kind", wrong_kind.json()["detail"])

        wrong_profile = self.client.post(
            "/api/v1/epub/admin/batches",
            json={
                "version_id": "version-1",
                "profile_name": "other-cloud-model-snapshot",
                "is_sample": False,
            },
        )
        self.assertEqual(wrong_profile.status_code, 400)
        self.assertIn("same model profile", wrong_profile.json()["detail"])

        full = self.client.post(
            "/api/v1/epub/admin/batches",
            json={"version_id": "version-1", "profile_name": "cloud-model-snapshot", "is_sample": False},
        )
        self.assertEqual(full.status_code, 201)
        self.assertFalse(full.json()["is_sample"])

        reviews = self.client.get("/api/v1/epub/admin/sample-batch-reviews?version_id=version-1")
        self.assertEqual(reviews.status_code, 200)
        self.assertEqual(reviews.json()["items"][0]["sample_batch_job_id"], sample_id)

    def test_admin_can_run_local_calibration_without_exposing_source_text(self) -> None:
        self.app.dependency_overrides[get_admin_user] = _admin_user
        test_case = self

        class FakeCalibrationRunner:
            def run(self, *, passages, prompt_profile, sample_limit):
                test_case.assertEqual(len(passages), 1)
                test_case.assertEqual(prompt_profile, DEFAULT_CONCEPT_PROMPT_PROFILE)
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

    def test_batch_lifecycle_failures_reach_the_administrator_as_actionable_errors(self) -> None:
        """Every Batch read path must degrade to 400/404, never to an opaque 500.

        ``EpubConceptService`` wraps five separate ``except BatchServiceError``
        clauses around the durable Batch service.  Each one is independent, so
        each is exercised here: a fix that restored the translation for only
        some of them would otherwise still look green.
        """
        self.app.dependency_overrides[get_admin_user] = _admin_user
        service = EpubConceptService(store=self.service._store, batch=FailingBatchJobService())
        self.app.dependency_overrides[get_epub_concept_service] = lambda: service

        service_calls = (
            lambda: service.list_batch_jobs(version_id="version-1"),
            lambda: service.get_batch_job("missing"),
            lambda: service.review_sample_batch(
                batch_job_id="missing", status="APPROVED", reviewed_by="administrator"
            ),
            lambda: service.list_sample_batch_reviews(version_id="version-1", job_kind=None),
            lambda: service.recover_batches(),
        )
        for call in service_calls:
            with self.assertRaises(EpubServiceError) as raised:
                call()
            self.assertIn("does not exist", str(raised.exception))

        requests = [
            ("get", "/api/v1/epub/admin/batches?version_id=version-1", None, 400),
            ("get", "/api/v1/epub/admin/batches/missing", None, 404),
            ("get", "/api/v1/epub/admin/sample-batch-reviews?version_id=version-1", None, 400),
            ("put", "/api/v1/epub/admin/sample-batches/missing/review", {"status": "APPROVED"}, 400),
            ("post", "/api/v1/epub/admin/batches/recover", None, 400),
        ]
        for method, url, payload, expected_status in requests:
            kwargs = {"json": payload} if payload is not None else {}
            response = getattr(self.client, method)(url, **kwargs)
            self.assertEqual(response.status_code, expected_status, url)
            self.assertEqual(response.json()["detail"], FailingBatchJobService.failure, url)

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
