"""Authorization and composition tests for the authenticated EPUB REST API."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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

# The real EPUB fixture builder already lives with the parser acceptance test.
# Reusing it is the point: the overlay round trip is only meaningful if both
# stores parse a genuine archive rather than hand-written passage rows.
from test_epub_parser_sdd import build_fixture_epub  # noqa: E402


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
            ("get", "/api/v1/epub/admin/concepts", None),
            (
                "post",
                "/api/v1/epub/admin/concepts/merge",
                {"target_concept_id": "one", "source_concept_id": "two"},
            ),
            ("get", "/api/v1/epub/admin/relation-assertions", None),
            ("put", "/api/v1/epub/admin/relation-assertions/missing", {"status": "APPROVED"}),
            ("post", "/api/v1/epub/admin/retrieval-units/missing/index", None),
            ("post", "/api/v1/epub/admin/versions/version-1/index", {"rebuild": False}),
            ("get", "/api/v1/epub/admin/versions/version-1/overlay", None),
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

        overlay_upload = self.client.post(
            "/api/v1/epub/admin/overlays",
            files={"file": ("overlay.json", b"{}", "application/json")},
        )
        self.assertEqual(overlay_upload.status_code, 401)

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
        # v2 is the default section-graph contract: v1 demands a code-point
        # offset on every span in a packet, and a packet holds many spans.
        self.assertEqual(section_graph_draft.json()["prompt_profile"], "zh-section-graph-v2")

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

    def test_admin_can_read_the_concept_graph_and_merge_a_duplicate(self) -> None:
        """Seeing the graph and merging a duplicate are one administrator workflow.

        Batch ingest refuses an item whose model suggestion matches two
        concepts exactly, and no other route could resolve that candidate.
        Neither response may carry passage text, prompts or model output.
        """
        self.app.dependency_overrides[get_admin_user] = _admin_user
        listed = self.client.get("/api/v1/epub/admin/concepts?limit=10")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 2)
        self.assertEqual(listed.json()["offset"], 0)
        by_name = {item["canonical_name"]: item for item in listed.json()["items"]}
        self.assertEqual(by_name["TCP"]["aliases"], ["TCP", "Transmission Control Protocol"])
        self.assertEqual(by_name["TCP"]["mention_count"], 1)
        self.assertEqual(by_name["TCP"]["status"], "APPROVED")
        self.assertNotIn("原文必须完整返回", listed.text)

        merged = self.client.post(
            "/api/v1/epub/admin/concepts/merge",
            json={
                "target_concept_id": by_name["TCP"]["concept_id"],
                "source_concept_id": by_name["传输控制协议"]["concept_id"],
                "canonical_name": "传输控制协议",
            },
        )
        self.assertEqual(merged.status_code, 200)
        self.assertEqual(merged.json()["canonical_name"], "传输控制协议")
        self.assertEqual(merged.json()["merged_by"], "administrator")
        self.assertEqual(merged.json()["moved_mentions"], 1)
        # The two concepts were related to each other, so that relation would
        # now point a concept at itself and is deliberately dropped.
        self.assertEqual(merged.json()["dropped_self_relations"], 1)
        self.assertNotIn("原文必须完整返回", merged.text)

        after = self.client.get("/api/v1/epub/admin/concepts")
        self.assertEqual(after.json()["total"], 1)
        surviving = after.json()["items"][0]
        self.assertEqual(surviving["canonical_name"], "传输控制协议")
        self.assertEqual(surviving["mention_count"], 2)
        self.assertEqual(
            surviving["aliases"], ["TCP", "Transmission Control Protocol", "传输控制协议"]
        )
        self.assertEqual(
            self.client.get("/api/v1/epub/admin/relation-assertions").json()["items"], []
        )

    def test_merge_refuses_an_unknown_or_identical_concept_with_an_actionable_status(self) -> None:
        self.app.dependency_overrides[get_admin_user] = _admin_user
        concept_id = self.client.get("/api/v1/epub/admin/concepts").json()["items"][0]["concept_id"]

        same = self.client.post(
            "/api/v1/epub/admin/concepts/merge",
            json={"target_concept_id": concept_id, "source_concept_id": concept_id},
        )
        self.assertEqual(same.status_code, 400)
        self.assertIn("merged into itself", same.json()["detail"])

        for payload in (
            {"target_concept_id": concept_id, "source_concept_id": "missing"},
            {"target_concept_id": "missing", "source_concept_id": concept_id},
        ):
            response = self.client.post("/api/v1/epub/admin/concepts/merge", json=payload)
            self.assertEqual(response.status_code, 404, payload)
            self.assertIn("unknown concept_id: missing", response.json()["detail"])

        self.assertEqual(self.client.get("/api/v1/epub/admin/concepts").json()["total"], 2)

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


class PortableAnalysisOverlayApiTest(unittest.TestCase):
    """T-170a acceptance: one paid analysis, applied to a second installation.

    The reader's store is built by importing the *same EPUB bytes* through the
    real parser, so its passages, ordinals and hashes are produced
    independently of anything the publisher sends.  Only the overlay travels.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        archive = directory / "fixture.epub"
        build_fixture_epub(archive)
        self.epub_bytes = archive.read_bytes()

        self.publisher_path = directory / "publisher.db"
        self.reader_path = directory / "reader.db"
        self.publisher_store = SQLiteEpubStore(str(self.publisher_path))
        self.reader_store = SQLiteEpubStore(str(self.reader_path))
        self.publisher = EpubConceptService(store=self.publisher_store)
        self.reader = EpubConceptService(store=self.reader_store)
        self.published_version = self._import(self.publisher)
        self.reader_version = self._import(self.reader)
        self._publish_analysis()

        self.publisher_client = self._client(self.publisher)
        self.client = self._client(self.reader)

    def tearDown(self) -> None:
        self.publisher_store.close()
        self.reader_store.close()
        self.temporary.cleanup()

    def _import(self, service: EpubConceptService) -> str:
        result = service.import_epub(filename="fixture.epub", epub_bytes=self.epub_bytes)
        self.assertTrue(result["created"])
        return str(result["version_id"])

    def _client(self, service: EpubConceptService) -> TestClient:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_epub_concept_service] = lambda: service
        app.dependency_overrides[get_verified_user] = _ordinary_user
        app.dependency_overrides[get_admin_user] = _admin_user
        return TestClient(app)

    def _publish_analysis(self) -> None:
        """Build the graph the publisher paid a cloud Batch run to produce."""
        passages = self.publisher_store.list_passages(self.published_version)
        self.english = next(item for item in passages if item["content"].startswith("Hello"))
        self.quote = next(item for item in passages if item["content"].startswith("引用"))
        hello = self.publisher_store.upsert_concept(
            "Hello", aliases=["招呼语"], definition="示例问候", status="APPROVED"
        )
        quotation = self.publisher_store.upsert_concept("引用文本", status="PROVISIONAL")
        self.publisher_store.add_concept_mention(
            hello, self.english["passage_id"], start_codepoint=0, end_codepoint=5, source="ADMIN"
        )
        self.publisher_store.add_concept_mention(
            quotation, self.quote["passage_id"], start_codepoint=0, end_codepoint=4
        )
        self.publisher_store.add_concept_relation(
            self.published_version,
            hello,
            "CONTRASTS",
            quotation,
            evidence=[
                {
                    "passage_id": self.english["passage_id"],
                    "start_codepoint": 0,
                    "end_codepoint": 11,
                    "evidence": self.english["content"][0:11],
                }
            ],
        )

    def _download(self) -> tuple[bytes, str]:
        response = self.publisher_client.get(
            f"/api/v1/epub/admin/versions/{self.published_version}/overlay"
        )
        self.assertEqual(response.status_code, 200)
        return response.content, response.headers["x-overlay-sha256"]

    def _spans(self, path: Path) -> list[tuple[str, int, int, str]]:
        connection = sqlite3.connect(str(path))
        try:
            return list(
                connection.execute(
                    """SELECT p.content, m.start_codepoint, m.end_codepoint, m.evidence
                         FROM concept_mentions AS m JOIN passages AS p ON p.passage_id = m.passage_id"""
                )
            ) + list(
                connection.execute(
                    """SELECT p.content, e.start_codepoint, e.end_codepoint, e.evidence
                         FROM concept_relation_evidence AS e
                         JOIN passages AS p ON p.passage_id = e.passage_id"""
                )
            )
        finally:
            connection.close()

    def test_admin_downloads_a_publishable_artifact_and_its_digest(self) -> None:
        body, digest = self._download()

        self.assertEqual(sha256(body).hexdigest(), digest)
        text = body.decode("utf-8")
        # Not one character of the book may leave the publisher's server.
        for passage in self.publisher_store.list_passages(self.published_version):
            self.assertNotIn(passage["content"], text)
        self.assertNotIn("Hello world", text)
        self.assertNotIn("引用文本。", text)
        # Concept labels and definitions are the analysis product and do ship.
        payload = json.loads(text)
        self.assertEqual([concept["key"] for concept in payload["concepts"]], ["hello", "引用文本"])
        self.assertEqual(payload["parser_version"], "1")
        self.assertEqual(payload["passage_fingerprint"]["count"], 6)
        self.assertEqual(payload["overlay_format_version"], 1)

    def test_a_second_installation_applies_the_overlay_to_its_own_book(self) -> None:
        body, digest = self._download()

        applied = self.client.post(
            "/api/v1/epub/admin/overlays",
            files={"file": ("overlay.json", body, "application/json")},
        )

        self.assertEqual(applied.status_code, 200)
        summary = applied.json()
        self.assertEqual(summary["version_id"], self.reader_version)
        self.assertNotEqual(self.reader_version, self.published_version)
        self.assertEqual(summary["uploaded_overlay_sha256"], digest)
        self.assertEqual(summary["canonical_overlay_sha256"], digest)
        self.assertEqual(summary["applied_detail"]["concepts_created"], 2)
        self.assertEqual(summary["applied_detail"]["mentions_created"], 2)
        self.assertEqual(summary["applied_detail"]["relations_created"], 1)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["rejection_reasons"], {})
        self.assertTrue(summary["vectors_require_reindex"])
        # A content-free summary: no passage text, no evidence, no labels.
        self.assertNotIn("Hello", applied.text)
        self.assertNotIn("引用", applied.text)

        # The reader's graph is the publisher's graph, re-derived locally.
        self.assertEqual(
            self.reader.export_concept_overlay(self.reader_version)["overlay_json"],
            body.decode("utf-8"),
        )
        spans = self._spans(self.reader_path)
        self.assertEqual(len(spans), 3)
        for content, start, end, evidence in spans:
            self.assertEqual(evidence, content[start:end])
        # An imported overlay has no vectors; the derived index is still empty.
        self.assertEqual(
            [
                unit["vector_state"]
                for unit in self.reader_store.list_retrieval_units_for_version(self.reader_version)
                if unit["vector_state"] == "READY"
            ],
            [],
        )

    def test_applying_the_same_artifact_twice_is_a_no_op(self) -> None:
        body, _ = self._download()
        files = {"file": ("overlay.json", body, "application/json")}

        self.client.post("/api/v1/epub/admin/overlays", files=files)
        second = self.client.post(
            "/api/v1/epub/admin/overlays",
            files={"file": ("overlay.json", body, "application/json")},
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["applied"], 0)
        self.assertEqual(second.json()["skipped_detail"]["mentions_existing"], 2)

    def test_an_overlay_for_a_book_this_library_lacks_is_a_404(self) -> None:
        body, _ = self._download()
        payload = json.loads(body)
        payload["epub_sha256"] = "a" * 64

        response = self.client.post(
            "/api/v1/epub/admin/overlays",
            files={"file": ("overlay.json", json.dumps(payload).encode(), "application/json")},
        )

        self.assertEqual(response.status_code, 404)
        self.assertIn("matches the overlay", response.json()["detail"])
        self.assertEqual(self.reader_store.count_concepts(), 0)

    def test_a_failed_fidelity_gate_reports_its_class_and_stores_nothing(self) -> None:
        body, _ = self._download()
        payload = json.loads(body)
        payload["passage_fingerprint"]["digest"] = "b" * 64

        response = self.client.post(
            "/api/v1/epub/admin/overlays",
            files={"file": ("overlay.json", json.dumps(payload).encode(), "application/json")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("passage_fingerprint_mismatch", response.json()["detail"])
        self.assertEqual(self.reader_store.count_concepts(), 0)
        self.assertEqual(self._spans(self.reader_path), [])

    def test_a_malformed_or_wrongly_named_upload_is_refused(self) -> None:
        wrong_extension = self.client.post(
            "/api/v1/epub/admin/overlays",
            files={"file": ("overlay.epub", b"{}", "application/epub+zip")},
        )
        self.assertEqual(wrong_extension.status_code, 400)
        self.assertIn(".json overlay", wrong_extension.json()["detail"])

        not_json = self.client.post(
            "/api/v1/epub/admin/overlays",
            files={"file": ("overlay.json", b"not json at all", "application/json")},
        )
        self.assertEqual(not_json.status_code, 400)
        self.assertIn("UTF-8 JSON", not_json.json()["detail"])

        smuggled = json.loads(self._download()[0])
        smuggled["mentions"][0]["evidence"] = "Hello"
        refused = self.client.post(
            "/api/v1/epub/admin/overlays",
            files={"file": ("overlay.json", json.dumps(smuggled).encode(), "application/json")},
        )
        self.assertEqual(refused.status_code, 400)
        self.assertIn("unsupported fields", refused.json()["detail"])
        self.assertEqual(self.reader_store.count_concepts(), 0)

    def test_exporting_an_unknown_version_is_a_404(self) -> None:
        response = self.publisher_client.get("/api/v1/epub/admin/versions/missing/overlay")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
