"""Acceptance tests for durable, provider-agnostic EPUB Batch orchestration."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import types
from types import SimpleNamespace


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# ``store`` imports the pure ``overlay`` module for the concept-key folding
# rule it shares with the portable analysis artifact, so it is loaded as a
# member of a synthetic package instead of as a lone file.
PACKAGE_NAME = "epub_batch_sdd_test_package"
PACKAGE = types.ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [os.path.join(ROOT, "backend/open_webui/retrieval/epub")]
sys.modules[PACKAGE_NAME] = PACKAGE


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load_module(f"{PACKAGE_NAME}.overlay", "backend/open_webui/retrieval/epub/overlay.py")
STORE = _load_module(f"{PACKAGE_NAME}.store", "backend/open_webui/retrieval/epub/store.py")
BATCH = _load_module("epub_batch_test", "backend/open_webui/retrieval/epub/batch.py")
SQLiteEpubStore = STORE.SQLiteEpubStore
BatchItemInput = BATCH.BatchItemInput
BatchJobService = BATCH.BatchJobService
BatchPayloadError = BATCH.BatchPayloadError
BatchServiceError = BATCH.BatchServiceError
ProviderItemResult = BATCH.ProviderItemResult
ProviderSnapshot = BATCH.ProviderSnapshot
SQLiteBatchRepository = BATCH.SQLiteBatchRepository
OpenAIBatchProvider = BATCH.OpenAIBatchProvider


class FakeProvider:
    name = "fake-batch"

    def __init__(self) -> None:
        self.submissions: dict[str, str] = {}
        self.submit_calls = 0
        self.jsonl: dict[str, str] = {}
        self.snapshots: dict[str, ProviderSnapshot] = {}
        self.results: dict[str, list[ProviderItemResult]] = {}
        self.fetch_error: Exception | None = None

    def submit(self, *, jsonl: str, idempotency_key: str) -> str:
        self.submit_calls += 1
        self.jsonl[idempotency_key] = jsonl
        provider_job_id = self.submissions.setdefault(idempotency_key, f"remote-{idempotency_key}")
        self.snapshots.setdefault(provider_job_id, ProviderSnapshot("queued"))
        return provider_job_id

    def poll(self, provider_job_id: str) -> ProviderSnapshot:
        return self.snapshots[provider_job_id]

    def fetch_results(self, provider_job_id: str):
        if self.fetch_error is not None:
            raise self.fetch_error
        return list(self.results.get(provider_job_id, []))


class MockOpenAIBatchClient:
    """In-memory SDK-shaped fake; these tests must never call the network."""

    def __init__(self) -> None:
        self.file_creates: list[dict] = []
        self.batch_creates: list[dict] = []
        self.retrieved: dict[str, object] = {}
        self.file_contents: dict[str, object] = {}
        self.files = SimpleNamespace(create=self._create_file, content=self._file_content)
        self.batches = SimpleNamespace(create=self._create_batch, retrieve=self._retrieve_batch)

    def _create_file(self, **kwargs):
        self.file_creates.append(kwargs)
        return SimpleNamespace(id="file-input")

    def _create_batch(self, **kwargs):
        self.batch_creates.append(kwargs)
        return SimpleNamespace(id="batch-openai-1")

    def _retrieve_batch(self, batch_id: str):
        return self.retrieved[batch_id]

    def _file_content(self, file_id: str):
        return self.file_contents[file_id]


class OpenAIBatchProviderTest(unittest.TestCase):
    @staticmethod
    def _jsonl() -> str:
        return (
            '{"custom_id":"p1","method":"POST","url":"/v1/chat/completions",'
            '"body":{"model":"gpt-4o-mini","messages":[]}}\n'
        )

    def setUp(self) -> None:
        self.client = MockOpenAIBatchClient()
        self.provider = OpenAIBatchProvider(client=self.client)

    def test_submit_uploads_jsonl_and_uses_openai_idempotency_headers(self) -> None:
        configured = OpenAIBatchProvider(api_key="server-only-secret", client=self.client)
        self.assertNotIn("server-only-secret", repr(configured.__dict__))
        self.assertEqual(self.provider.submit(jsonl=self._jsonl(), idempotency_key="durable-job"), "batch-openai-1")
        self.assertEqual(len(self.client.file_creates), 1)
        upload = self.client.file_creates[0]
        self.assertEqual(upload["purpose"], "batch")
        self.assertEqual(upload["extra_headers"], {"Idempotency-Key": "epub-input:durable-job"})
        self.assertEqual(upload["file"][0], "epub-concepts-durable-job.jsonl")
        self.assertEqual(upload["file"][1], self._jsonl().encode("utf-8"))
        create = self.client.batch_creates[0]
        self.assertEqual(create["input_file_id"], "file-input")
        self.assertEqual(create["endpoint"], "/v1/chat/completions")
        self.assertEqual(create["completion_window"], "24h")
        self.assertEqual(create["metadata"], {"epub_batch_job_id": "durable-job"})
        self.assertEqual(create["extra_headers"], {"Idempotency-Key": "epub-batch:durable-job"})
        self.assertNotIn("api_key", repr(upload))

    def test_submit_rejects_non_openai_envelope_and_credentials(self) -> None:
        with self.assertRaisesRegex(BatchServiceError, "must POST"):
            self.provider.submit(jsonl='{"custom_id":"p1","body":{}}\n', idempotency_key="job")
        with self.assertRaisesRegex(BatchServiceError, "credentials"):
            self.provider.submit(
                jsonl=(
                    '{"custom_id":"p1","method":"POST","url":"/v1/chat/completions",'
                    '"body":{"api_key":"forbidden"}}\n'
                ),
                idempotency_key="job",
            )
        self.assertFalse(self.client.file_creates)

    def test_poll_normalizes_openai_lifecycle_statuses_and_errors(self) -> None:
        self.client.retrieved["queued"] = SimpleNamespace(status="validating", errors=None)
        self.client.retrieved["running"] = SimpleNamespace(status="finalizing", errors=None)
        self.client.retrieved["done"] = SimpleNamespace(status="completed", errors=None)
        self.client.retrieved["failed"] = SimpleNamespace(
            status="expired", errors=SimpleNamespace(data=[SimpleNamespace(code="too_long", message="expired")])
        )
        self.client.retrieved["cancelled"] = SimpleNamespace(status="cancelled", errors=None)
        self.assertEqual(self.provider.poll("queued"), ProviderSnapshot("queued"))
        self.assertEqual(self.provider.poll("running"), ProviderSnapshot("running"))
        self.assertEqual(self.provider.poll("done"), ProviderSnapshot("succeeded"))
        self.assertEqual(self.provider.poll("failed"), ProviderSnapshot("failed", "too_long: expired"))
        self.assertEqual(self.provider.poll("cancelled"), ProviderSnapshot("cancelled"))

    def test_fetch_results_translates_output_and_error_jsonl(self) -> None:
        self.client.retrieved["batch-openai-1"] = SimpleNamespace(
            output_file_id="file-output", error_file_id="file-errors"
        )
        self.client.file_contents["file-output"] = SimpleNamespace(text=json.dumps(
            {
                "custom_id": "p1",
                "response": {
                    "status_code": 200,
                    "body": {
                        "choices": [
                            {"message": {"content": json.dumps({"concepts": [{"name": "TCP", "mentions": []}]})}}
                        ]
                    },
                },
            }
        ) + "\n")
        self.client.file_contents["file-errors"] = SimpleNamespace(content=json.dumps(
            {
                "custom_id": "p2",
                "response": {"status_code": 429, "body": {"error": {"message": "rate limited"}}},
            }
        ).encode("utf-8"))
        results = list(self.provider.fetch_results("batch-openai-1"))
        self.assertEqual(
            results,
            [
                ProviderItemResult("p1", payload={"concepts": [{"name": "TCP", "mentions": []}]}),
                ProviderItemResult("p2", error="rate limited"),
            ],
        )

    def test_fetch_results_rejects_duplicate_or_malformed_provider_records(self) -> None:
        self.client.retrieved["batch-openai-1"] = SimpleNamespace(
            output_file_id="file-output", error_file_id="file-errors"
        )
        response = '{"custom_id":"p1","error":{"message":"bad"}}\n'
        self.client.file_contents["file-output"] = SimpleNamespace(text=response)
        self.client.file_contents["file-errors"] = SimpleNamespace(text=response)
        with self.assertRaisesRegex(BatchServiceError, "duplicate output"):
            list(self.provider.fetch_results("batch-openai-1"))


class EpubBatchServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteEpubStore(os.path.join(self.tempdir.name, "epub.db"))
        book_id = self.store.create_book("Batch book", book_id="book")
        self.store.create_book_version(book_id, epub_bytes=b"batch epub", version_id="version")
        self.store.add_passages(
            "version",
            [
                {
                    "passage_id": "p1",
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": 0,
                    "content_kind": "paragraph",
                    "content": "TCP connects TCP endpoints.",
                },
                {
                    "passage_id": "p2",
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": 1,
                    "content_kind": "paragraph",
                    "content": "UDP is datagram based.",
                },
            ],
        )
        self.store.set_version_status("version", "READY")
        self.repository = SQLiteBatchRepository(self.store)
        self.service = BatchJobService(self.repository)
        self.provider = FakeProvider()

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    @staticmethod
    def _items() -> list[BatchItemInput]:
        return [
            BatchItemInput("p1", "p1", {"model": "batch-model", "body": {"text": "TCP connects TCP endpoints."}}),
            BatchItemInput("p2", "p2", {"model": "batch-model", "body": {"text": "UDP is datagram based."}}),
        ]

    def _draft(self, job_id: str = "job") -> str:
        return self.service.create_draft(
            version_id="version",
            provider="fake-batch",
            profile_name="concept-v1",
            items=self._items(),
            batch_job_id=job_id,
        )

    def test_submit_is_durable_idempotent_and_keeps_credentials_out_of_jsonl(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.assertEqual(remote_id, "remote-job")
        self.assertEqual(self.provider.submit_calls, 1)
        self.assertIn('"custom_id":"p1"', self.provider.jsonl["job"])

        # A fresh repository/service instance represents process restart.  The
        # provider is not called again once local acknowledgement is durable.
        restarted = BatchJobService(SQLiteBatchRepository(self.store))
        self.assertEqual(restarted.submit("job", self.provider), remote_id)
        self.assertEqual(self.provider.submit_calls, 1)
        with self.assertRaisesRegex(BatchServiceError, "credentials"):
            self.service.create_draft(
                version_id="version",
                provider="fake-batch",
                profile_name="concept-v1",
                batch_job_id="secret-job",
                items=[BatchItemInput("p1", "secret", {"api_key": "never persist this"})],
            )

    def test_full_openai_job_requires_a_completed_and_approved_matching_sample(self) -> None:
        with self.assertRaisesRegex(BatchServiceError, "administrator-approved sample"):
            self.service.create_draft(
                version_id="version",
                provider="openai-batch",
                profile_name="cloud-model-snapshot",
                items=self._items(),
                batch_job_id="full-without-sample",
            )

        sample_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            items=self._items(),
            is_sample=True,
            batch_job_id="openai-sample",
        )
        openai_provider = FakeProvider()
        openai_provider.name = "openai-batch"
        remote_id = self.service.submit(sample_id, openai_provider)
        openai_provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        openai_provider.results[remote_id] = [
            ProviderItemResult("p1", payload={"concepts": []}),
            ProviderItemResult("p2", payload={"concepts": []}),
        ]
        self.assertEqual(self.service.poll_and_ingest(sample_id, openai_provider)["failed"], 0)

        approved = self.service.review_sample_job(
            sample_id, status="APPROVED", reviewed_by="administrator"
        )
        self.assertEqual(approved["version_id"], "version")
        self.assertEqual(approved["job_kind"], "CONCEPT_MENTIONS")
        self.assertEqual(approved["status"], "APPROVED")
        self.assertTrue(approved["reviewed_at"])

        with self.assertRaisesRegex(BatchServiceError, "same model profile"):
            self.service.create_draft(
                version_id="version",
                provider="openai-batch",
                profile_name="other-cloud-model-snapshot",
                items=self._items(),
                batch_job_id="full-after-other-model-sample",
            )

        full_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            items=self._items(),
            batch_job_id="full-after-sample",
        )
        self.assertEqual(full_id, "full-after-sample")
        reloaded = SQLiteBatchRepository(self.store).list_sample_reviews(version_id="version")
        self.assertEqual(reloaded[0]["sample_batch_job_id"], sample_id)
        self.assertEqual(reloaded[0]["reviewed_by"], "administrator")

    def test_draft_or_partial_sample_cannot_be_approved(self) -> None:
        sample_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            items=self._items(),
            is_sample=True,
            batch_job_id="unfinished-sample",
        )
        with self.assertRaisesRegex(BatchServiceError, "SUCCEEDED and every item was ingested"):
            self.service.review_sample_job(sample_id, status="APPROVED", reviewed_by="administrator")

        self.repository.mark_submitted(sample_id, "remote-unfinished-sample")
        self.repository.set_provider_state(sample_id, "SUCCEEDED", None)
        first_item = self.repository.list_items(sample_id)[0]
        self.assertTrue(
            self.repository.ingest_success(sample_id, first_item["custom_id"], {"concepts": []})
        )
        with self.assertRaisesRegex(BatchServiceError, "SUCCEEDED and every item was ingested"):
            self.service.review_sample_job(sample_id, status="APPROVED", reviewed_by="administrator")

    def test_operator_history_is_safe_and_recovery_never_submits_drafts(self) -> None:
        self._draft()
        history = self.service.list_job_summaries()
        self.assertEqual(history["total"], 1)
        summary = history["items"][0]
        self.assertEqual(summary["batch_job_id"], "job")
        self.assertEqual(summary["status"], "DRAFT")
        self.assertEqual(summary["item_status_counts"], {"PENDING": 2})
        self.assertNotIn("request_json", summary)
        self.assertNotIn("response_json", summary)
        self.assertNotIn("last_error", summary)

        detail = self.service.get_job_summary("job")
        self.assertEqual(len(detail["items"]), 2)
        self.assertNotIn("request_json", detail["items"][0])
        self.assertNotIn("error_text", detail["items"][0])

        # A restart-recovery action is deliberately unable to create remote
        # work from a durable DRAFT.
        self.assertEqual(self.service.recover_all({self.provider.name: self.provider}), {"recovered": [], "skipped": []})
        self.assertEqual(self.provider.submit_calls, 0)

        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("running")
        recovered = self.service.recover_all({self.provider.name: self.provider})
        self.assertEqual(recovered["recovered"][0]["state"], "RUNNING")
        self.assertEqual(self.provider.submit_calls, 1)

    def test_recovery_reports_unconfigured_provider_without_contacting_it(self) -> None:
        self._draft()
        self.service.submit("job", self.provider)
        result = self.service.recover_all({})
        self.assertEqual(result["recovered"], [])
        self.assertEqual(
            result["skipped"],
            [{"job_id": "job", "provider": "fake-batch", "reason": "provider is not configured"}],
        )

    def test_completed_output_ingest_is_idempotent_and_exact(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        self.provider.results[remote_id] = [
            ProviderItemResult(
                "p1",
                payload={
                    "concepts": [
                        {
                            "name": "TCP",
                            "aliases": ["Transmission Control Protocol"],
                            "definition": "A protocol",
                            "mentions": [{"start_codepoint": 0, "end_codepoint": 3, "evidence": "TCP"}],
                        }
                    ]
                },
            ),
            ProviderItemResult("p2", payload={"concepts": []}),
        ]

        first = self.service.poll_and_ingest("job", self.provider)
        second = self.service.poll_and_ingest("job", self.provider)
        self.assertEqual(first, {"job_id": "job", "state": "SUCCEEDED", "ingested": 2, "failed": 0})
        self.assertEqual(second, {"job_id": "job", "state": "SUCCEEDED", "ingested": 0, "failed": 0})
        self.assertEqual(
            self.store._connection().execute("SELECT COUNT(*) FROM concepts").fetchone()[0], 1
        )
        self.assertEqual(
            self.store._connection().execute("SELECT COUNT(*) FROM concept_mentions").fetchone()[0], 1
        )
        item = self.repository.list_items("job")[0]
        self.assertEqual(item["status"], "SUCCEEDED")

    def test_admin_merge_unblocks_a_suggestion_that_matched_two_concepts(self) -> None:
        """The end-to-end remedy for the only failure an operator cannot retry.

        ``_resolve_or_create_concept`` refuses to guess when one suggestion
        exactly matches two concepts, and that guard stays exactly as it is.
        What was missing was an administrator action that resolves the
        candidate, after which the very same durable result ingests cleanly.
        """
        surviving = self.store.upsert_concept("UDP", concept_id="udp")
        duplicate = self.store.upsert_concept("datagram", concept_id="datagram")
        self._draft("merge-job")
        remote_id = self.service.submit("merge-job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        self.provider.results[remote_id] = [
            ProviderItemResult("p1", payload={"concepts": []}),
            ProviderItemResult(
                "p2",
                payload={
                    "concepts": [
                        {
                            "name": "UDP",
                            "aliases": ["datagram"],
                            "definition": "A protocol",
                            "mentions": [
                                {"start_codepoint": 0, "end_codepoint": 3, "evidence": "UDP"}
                            ],
                        }
                    ]
                },
            ),
        ]

        blocked = self.service.poll_and_ingest("merge-job", self.provider)
        held = next(
            item for item in self.repository.list_items("merge-job") if item["custom_id"] == "p2"
        )
        self.assertEqual(blocked["failed"], 1)
        self.assertEqual(held["status"], "FAILED")
        self.assertIn("multiple concepts", held["error_text"])
        self.assertEqual(
            self.store._connection().execute("SELECT COUNT(*) FROM concept_mentions").fetchone()[0],
            0,
        )

        merged = self.store.merge_concepts(
            target_concept_id=surviving,
            source_concept_id=duplicate,
            merged_by="administrator",
        )
        self.assertEqual(merged["source_canonical_name"], "datagram")
        self.assertEqual(
            {
                row[0]
                for row in self.store._connection().execute(
                    "SELECT alias FROM concept_aliases WHERE concept_id = ?", (surviving,)
                )
            },
            {"UDP", "datagram"},
        )

        resolved = self.service.poll_and_ingest("merge-job", self.provider)
        self.assertEqual(resolved["ingested"], 1)
        self.assertEqual(
            [item["status"] for item in self.repository.list_items("merge-job")],
            ["SUCCEEDED", "SUCCEEDED"],
        )
        mention = self.store._connection().execute(
            "SELECT concept_id, passage_id, start_codepoint, end_codepoint, evidence FROM concept_mentions"
        ).fetchall()
        self.assertEqual([tuple(row) for row in mention], [(surviving, "p2", 0, 3, "UDP")])
        self.assertEqual(
            self.store._connection().execute("SELECT COUNT(*) FROM concepts").fetchone()[0], 1
        )

    def test_openai_cloud_ingest_repairs_only_uniquely_locatable_evidence(self) -> None:
        job_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            items=[self._items()[1]],
            is_sample=True,
            batch_job_id="openai-unique-evidence",
        )
        provider = FakeProvider()
        provider.name = "openai-batch"
        remote_id = self.service.submit(job_id, provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [
            ProviderItemResult(
                "p2",
                payload={
                    "concepts": [
                        {
                            "name": "UDP",
                            "aliases": [],
                            "definition": "A protocol",
                            "mentions": [
                                {"start_codepoint": 77, "end_codepoint": 80, "evidence": "UDP"}
                            ],
                        }
                    ]
                },
            )
        ]

        self.assertEqual(self.service.poll_and_ingest(job_id, provider)["ingested"], 1)
        mention = self.store._connection().execute(
            "SELECT start_codepoint, end_codepoint, evidence FROM concept_mentions"
        ).fetchone()
        self.assertEqual(tuple(mention), (0, 3, "UDP"))
        stored = json.loads(self.repository.list_items(job_id)[0]["response_json"])
        self.assertEqual(stored["concepts"][0]["mentions"][0]["start_codepoint"], 0)

    def test_openai_cloud_ingest_uses_bounded_context_only_to_disambiguate_repeated_evidence(self) -> None:
        job_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            items=[self._items()[0]],
            is_sample=True,
            batch_job_id="openai-anchored-evidence",
        )
        provider = FakeProvider()
        provider.name = "openai-batch"
        remote_id = self.service.submit(job_id, provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [
            ProviderItemResult(
                "p1",
                payload={
                    "concepts": [
                        {
                            "name": "TCP",
                            "aliases": [],
                            "definition": "A protocol",
                            "mentions": [
                                {"start_codepoint": 99, "end_codepoint": 102, "evidence": "TCP"}
                            ],
                        }
                    ]
                },
            )
        ]
        self.assertEqual(self.service.poll_and_ingest(job_id, provider)["failed"], 1)
        self.assertEqual(self.repository.list_items(job_id)[0]["status"], "FAILED")

        provider.results[remote_id] = [
            ProviderItemResult(
                "p1",
                payload={
                    "concepts": [
                        {
                            "name": "TCP",
                            "aliases": [],
                            "definition": "A protocol",
                            "mentions": [
                                {
                                    "start_codepoint": 99,
                                    "end_codepoint": 102,
                                    "evidence": "TCP",
                                    "context_before": "connects ",
                                    "context_after": " endpoints.",
                                }
                            ],
                        }
                    ]
                },
            )
        ]

        self.assertEqual(self.service.poll_and_ingest(job_id, provider)["ingested"], 1)
        mention = self.store._connection().execute(
            "SELECT start_codepoint, end_codepoint FROM concept_mentions"
        ).fetchone()
        self.assertEqual(tuple(mention), (13, 16))

    # ------------------------------------------------------------------
    # Grounding precedence between a unique literal and a supplied anchor
    #
    # Real cloud samples showed the anchor filter vetoing byte-exact evidence
    # that occurred exactly once.  The anchors are a disambiguation device for
    # repeated evidence, so they must not decide a case that has nothing to
    # disambiguate - while still deciding the cases they exist for.
    # ------------------------------------------------------------------

    def _ingest_openai_item(self, *, job_id: str, passage_id: str, payload: dict) -> None:
        """Poll a one-item OpenAI job whose single result must ground cleanly."""
        self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            items=[BatchItemInput(passage_id, passage_id, {"body": {"passage": passage_id}})],
            is_sample=True,
            batch_job_id=job_id,
        )
        provider = FakeProvider()
        provider.name = "openai-batch"
        remote_id = self.service.submit(job_id, provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [ProviderItemResult(passage_id, payload=payload)]
        self.assertEqual(self.service.poll_and_ingest(job_id, provider)["ingested"], 1)

    def _only_mention(self) -> tuple[str, int, int, str]:
        row = self.store._connection().execute(
            """SELECT passage_id, start_codepoint, end_codepoint, evidence
                 FROM concept_mentions"""
        ).fetchall()
        self.assertEqual(len(row), 1)
        return tuple(row[0])

    def _assert_mention_slices_the_immutable_source(self) -> tuple[int, int]:
        """A stored citation must still be a byte-exact slice of the passage."""
        passage_id, start, end, evidence = self._only_mention()
        content = self.store._connection().execute(
            "SELECT content FROM passages WHERE passage_id = ?", (passage_id,)
        ).fetchone()["content"]
        self.assertEqual(content[start:end], evidence)
        return start, end

    def test_unique_evidence_outranks_a_wrong_anchor_and_wrong_model_offsets(self) -> None:
        # The dominant v4 cloud failure: offsets out of range (the model counted
        # code points wrongly) and anchors that match nothing, over evidence
        # that occurs exactly once.  There is nothing to disambiguate, so the
        # single occurrence must win.
        self._ingest_openai_item(
            job_id="unique-vs-anchor",
            passage_id="p2",
            payload=self._concept_payload(
                [
                    {
                        "start_codepoint": 77,
                        "end_codepoint": 80,
                        "evidence": "UDP",
                        "context_before": "never in the source ",
                        "context_after": " nor is this",
                    }
                ]
            ),
        )
        self.assertEqual(self._only_mention(), ("p2", 0, 3, "UDP"))
        self.assertEqual(self._assert_mention_slices_the_immutable_source(), (0, 3))

    def test_repeated_evidence_with_a_wrong_anchor_is_still_ambiguous(self) -> None:
        # The filter must keep working where it is meant to: two occurrences and
        # anchors that select neither cannot identify a citation.
        passage_id = self._add_marker_passage()
        self._fail_openai_item(
            job_id="repeat-wrong-anchor",
            passage_id=passage_id,
            payload=self._concept_payload(
                [
                    {
                        "start_codepoint": 900,
                        "end_codepoint": 912,
                        "evidence": "ZORBLAX gate",
                        "context_before": "never in the source ",
                        "context_after": " nor is this",
                    }
                ]
            ),
        )
        diagnostics = self._item_diagnostics("repeat-wrong-anchor")
        self.assertEqual(diagnostics["reason"], "EVIDENCE_AMBIGUOUS")
        self.assertEqual(diagnostics["occurrence_count"], 2)
        self.assertEqual(diagnostics["anchored_candidate_count"], 0)
        self.assertEqual(
            self.repository.list_items("repeat-wrong-anchor")[0]["error_text"],
            "OpenAI evidence cannot be uniquely located in the immutable source",
        )
        self.assertEqual(
            self.store._connection().execute(
                "SELECT COUNT(*) FROM concept_mentions"
            ).fetchone()[0],
            0,
        )

    def test_repeated_evidence_with_a_correct_anchor_still_selects_one_occurrence(self) -> None:
        passage_id = self._add_marker_passage()
        self._ingest_openai_item(
            job_id="repeat-right-anchor",
            passage_id=passage_id,
            payload=self._concept_payload(
                [
                    {
                        "start_codepoint": 900,
                        "end_codepoint": 912,
                        "evidence": "ZORBLAX gate",
                        "context_before": "opens. The ",
                        "context_after": " closes.",
                    }
                ]
            ),
        )
        self.assertEqual(self._only_mention(), (passage_id, 28, 40, "ZORBLAX gate"))
        self._assert_mention_slices_the_immutable_source()

    def test_a_correct_direct_offset_is_still_verified_against_its_anchor(self) -> None:
        # Deliberate asymmetry with the unique-literal repair above: when the
        # model's own offsets already slice the evidence exactly, SDD 4.2.1
        # still verifies them against the anchors it supplied.  Same passage and
        # same unmatchable anchors as the repair test - only the offsets differ.
        self._fail_openai_item(
            job_id="exact-offset-wrong-anchor",
            passage_id="p2",
            payload=self._concept_payload(
                [
                    {
                        "start_codepoint": 0,
                        "end_codepoint": 3,
                        "evidence": "UDP",
                        "context_before": "",
                        "context_after": " nor is this",
                    }
                ]
            ),
        )
        diagnostics = self._item_diagnostics("exact-offset-wrong-anchor")
        self.assertEqual(diagnostics["reason"], "ANCHOR_MISMATCH")
        self.assertTrue(diagnostics["direct_is_exact"])
        self.assertEqual(diagnostics["occurrence_count"], 1)
        self.assertEqual(
            self.repository.list_items("exact-offset-wrong-anchor")[0]["error_text"],
            "OpenAI evidence context anchor does not match the immutable source",
        )

    def test_openai_repeat_poll_can_reingest_a_previously_failed_item_without_rewriting_success(self) -> None:
        job_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            items=self._items(),
            is_sample=True,
            batch_job_id="openai-reingest-failed",
        )
        provider = FakeProvider()
        provider.name = "openai-batch"
        remote_id = self.service.submit(job_id, provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [
            ProviderItemResult("p1", payload={"concepts": []}),
            ProviderItemResult(
                "p2",
                payload={
                    "concepts": [
                        {
                            "name": "UDP",
                            "aliases": [],
                            "definition": "A protocol",
                            "mentions": [
                                {"start_codepoint": 0, "end_codepoint": 1, "evidence": "absent"}
                            ],
                        }
                    ]
                },
            ),
        ]
        self.assertEqual(self.service.poll_and_ingest(job_id, provider)["failed"], 1)
        self.assertEqual([item["status"] for item in self.repository.list_items(job_id)], ["SUCCEEDED", "FAILED"])

        # The same durable remote job can be polled again after a grounding
        # protocol upgrade.  The known success is byte-for-byte identical and
        # remains immutable; the former failure is newly, safely normalizable.
        provider.results[remote_id] = [
            ProviderItemResult("p1", payload={"concepts": []}),
            ProviderItemResult(
                "p2",
                payload={
                    "concepts": [
                        {
                            "name": "UDP",
                            "aliases": [],
                            "definition": "A protocol",
                            "mentions": [
                                {"start_codepoint": 100, "end_codepoint": 103, "evidence": "UDP"}
                            ],
                        }
                    ]
                },
            ),
        ]
        self.assertEqual(self.service.poll_and_ingest(job_id, provider)["ingested"], 1)
        self.assertEqual([item["status"] for item in self.repository.list_items(job_id)], ["SUCCEEDED", "SUCCEEDED"])

        provider.results[remote_id][0] = ProviderItemResult("p1", payload={"concepts": [{"unexpected": True}]})
        with self.assertRaisesRegex(BatchPayloadError, "succeeded Batch item cannot be overwritten"):
            self.service.poll_and_ingest(job_id, provider)
        self.assertEqual([item["status"] for item in self.repository.list_items(job_id)], ["SUCCEEDED", "SUCCEEDED"])

    def test_terminal_batch_without_output_marks_only_missing_items_failed_for_retry(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("failed")

        result = self.service.poll_and_ingest("job", self.provider)

        self.assertEqual(result, {"job_id": "job", "state": "FAILED", "ingested": 0, "failed": 2})
        self.assertEqual(
            [item["status"] for item in self.repository.list_items("job")], ["FAILED", "FAILED"]
        )
        retry_id = self.service.retry_failed_items("job")
        self.assertEqual(len(self.repository.list_items(retry_id)), 2)

    def test_terminal_partial_output_retries_only_unreported_items(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        # FakeProvider already returns normalized lifecycle states.
        self.provider.snapshots[remote_id] = ProviderSnapshot("failed")
        self.provider.results[remote_id] = [ProviderItemResult("p1", payload={"concepts": []})]

        result = self.service.poll_and_ingest("job", self.provider)

        self.assertEqual(result, {"job_id": "job", "state": "FAILED", "ingested": 1, "failed": 1})
        items = self.repository.list_items("job")
        self.assertEqual([item["status"] for item in items], ["SUCCEEDED", "FAILED"])
        retry_id = self.service.retry_failed_items("job")
        retry_items = self.repository.list_items(retry_id)
        self.assertEqual(len(retry_items), 1)
        self.assertEqual(retry_items[0]["passage_id"], "p2")

    def test_fetch_failure_keeps_terminal_items_pending_until_a_safe_repoll(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("cancelled")
        self.provider.fetch_error = OSError("simulated provider output download failure")

        result = self.service.poll_and_ingest("job", self.provider)

        self.assertEqual(result["results_pending_retrieval"], True)
        self.assertEqual(
            [item["status"] for item in self.repository.list_items("job")], ["SUBMITTED", "SUBMITTED"]
        )
        summary = self.service.get_job_summary("job")
        self.assertTrue(summary["results_pending_retrieval"])
        self.assertNotIn("simulated", repr(summary))
        with self.assertRaisesRegex(BatchServiceError, "no failed items"):
            self.service.retry_failed_items("job")

        # A later readable terminal result makes p1 conclusive and leaves only
        # p2 eligible for a successor job.
        self.provider.fetch_error = None
        self.provider.results[remote_id] = [ProviderItemResult("p1", payload={"concepts": []})]
        recovered = self.service.poll_and_ingest("job", self.provider)
        self.assertEqual(recovered, {"job_id": "job", "state": "CANCELLED", "ingested": 1, "failed": 1})
        self.assertFalse(self.service.get_job_summary("job")["results_pending_retrieval"])

    def test_invalid_output_is_persisted_as_failed_without_graph_mutation(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        self.provider.results[remote_id] = [
            ProviderItemResult(
                "p1",
                payload={"concepts": [{"name": "TCP", "mentions": [{"start_codepoint": 0, "end_codepoint": 999}]}]},
            )
        ]
        result = self.service.poll_and_ingest("job", self.provider)
        # p1 failed schema validation and p2 was absent from a complete output
        # stream, so both are safe, known failures eligible for retry.
        self.assertEqual(result["failed"], 2)
        self.assertEqual(
            [item["status"] for item in self.repository.list_items("job")], ["FAILED", "FAILED"]
        )
        self.assertEqual(
            self.store._connection().execute("SELECT COUNT(*) FROM concepts").fetchone()[0], 0
        )

    # ------------------------------------------------------------------
    # Section graph packets
    #
    # A packet holds many spans across many passages, and ingest is atomic, so
    # what matters is not "how often is one offset right" but "how often is
    # every offset in the packet right".  Measured across four CONCEPT_MENTIONS
    # cloud samples the model supplied a correct code-point pair about one time
    # in thirty-seven, so a packet whose offsets had to be believed would
    # essentially never survive.  Nothing below writes a correct offset by
    # hand: every expected span is derived from the immutable passage, exactly
    # the way ingest has to derive it.  Tests that construct correct offsets
    # themselves are what let this path ship with no repair at all.
    # ------------------------------------------------------------------

    _P1 = "TCP connects TCP endpoints."
    _P2 = "UDP is datagram based."

    @staticmethod
    def _v2_span(passage_id: str, evidence: str, *, before: str = "", after: str = "") -> dict:
        """One zh-section-graph-v2 span: a literal, an anchor, and no offsets."""
        return {
            "passage_id": passage_id,
            "evidence": evidence,
            "context_before": before,
            "context_after": after,
        }

    @staticmethod
    def _v1_span(passage_id: str, evidence: str, *, start: int, end: int) -> dict:
        """One zh-section-graph-v1 span.  Callers pass wrong offsets on purpose."""
        return {
            "passage_id": passage_id,
            "start_codepoint": start,
            "end_codepoint": end,
            "evidence": evidence,
        }

    @staticmethod
    def _graph_concept(local_id: str, name: str, mentions: list[dict]) -> dict:
        return {
            "local_id": local_id,
            "name": name,
            "aliases": [],
            "definition": "A protocol",
            "mentions": mentions,
        }

    @staticmethod
    def _graph_relation(
        subject: str, object_: str, evidence: list[dict], predicate: str = "HAS_PART"
    ) -> dict:
        return {
            "subject_local_id": subject,
            "predicate": predicate,
            "object_local_id": object_,
            "evidence": evidence,
        }

    def _ingest_packet(self, payload: dict, *, job_id: str = "section-graph") -> dict:
        self.service.create_draft(
            version_id="version",
            provider="fake-batch",
            profile_name="zh-section-graph-v2",
            job_kind="SECTION_GRAPH",
            items=[BatchItemInput("p1", "section-1", {"body": {"packet": True}})],
            batch_job_id=job_id,
        )
        remote_id = self.service.submit(job_id, self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        self.provider.results[remote_id] = [ProviderItemResult("section-1", payload=payload)]
        return self.service.poll_and_ingest(job_id, self.provider)

    _GRAPH_TABLES = (
        "concepts",
        "concept_aliases",
        "concept_mentions",
        "concept_relations",
        "concept_relation_assertions",
        "concept_relation_evidence",
    )

    def _graph_row_counts(self) -> dict[str, int]:
        connection = self.store._connection()
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in self._GRAPH_TABLES
        }

    def _assert_no_graph_rows(self) -> None:
        """Every table one packet can touch, not only the one that failed."""
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))

    def _stored_spans(self, table: str) -> list[tuple]:
        rows = self.store._connection().execute(
            f"""SELECT s.passage_id, s.start_codepoint, s.end_codepoint, s.evidence, p.content
                FROM {table} AS s JOIN passages AS p ON p.passage_id = s.passage_id
                ORDER BY s.passage_id, s.start_codepoint"""
        ).fetchall()
        for row in rows:
            # The invariant the whole feature exists to protect.
            self.assertEqual(
                row["content"][row["start_codepoint"]:row["end_codepoint"]], row["evidence"]
            )
        return [(row["passage_id"], row["start_codepoint"], row["end_codepoint"]) for row in rows]

    @staticmethod
    def _expected_span(passage: str, evidence: str, *, occurrence: int = 0) -> tuple[int, int]:
        """Where the span has to land, derived from the source rather than typed."""
        start = -1
        for _ in range(occurrence + 1):
            start = passage.index(evidence, start + 1)
        return start, start + len(evidence)

    def test_section_graph_v2_packet_without_offsets_is_grounded_and_persisted(self) -> None:
        unique_p1 = "connects TCP endpoints"
        unique_p2 = "UDP is datagram based"
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept("parent", "TCP", [self._v2_span("p1", unique_p1)]),
                    self._graph_concept("child", "UDP", [self._v2_span("p2", unique_p2)]),
                ],
                "relations": [
                    self._graph_relation("parent", "child", [self._v2_span("p1", unique_p1)])
                ],
            }
        )

        self.assertEqual(result["ingested"], 1)
        self.assertEqual(
            self._stored_spans("concept_mentions"),
            [
                ("p1", *self._expected_span(self._P1, unique_p1)),
                ("p2", *self._expected_span(self._P2, unique_p2)),
            ],
        )
        self.assertEqual(
            self._stored_spans("concept_relation_evidence"),
            [("p1", *self._expected_span(self._P1, unique_p1))],
        )
        counts = self._graph_row_counts()
        self.assertEqual(counts["concepts"], 2)
        self.assertEqual(counts["concept_relations"], 1)
        self.assertEqual(counts["concept_relation_assertions"], 1)

        # The durable response is the graph that was written.  The anchors were
        # an input device for choosing an occurrence; once one is chosen the
        # derived offset is the fact, and that is what a replay compares.
        stored = json.loads(self.repository.list_items("section-graph")[0]["response_json"])
        mention = stored["concepts"][0]["mentions"][0]
        self.assertEqual(
            set(mention), {"passage_id", "start_codepoint", "end_codepoint", "evidence"}
        )
        self.assertEqual(
            (mention["start_codepoint"], mention["end_codepoint"]),
            self._expected_span(self._P1, unique_p1),
        )
        # Grounding is deterministic, so re-polling the same remote job is a
        # no-op rather than "different output for an already ingested item".
        self.assertEqual(
            self.service.poll_and_ingest("section-graph", self.provider)["ingested"], 0
        )

    def test_section_graph_v1_offsets_are_repaired_from_the_unique_literal(self) -> None:
        # A stored v1 request still replays, but its offsets are re-derived
        # rather than believed.  Both of these are wrong: one points outside
        # the passage, one points at the wrong text inside it.
        unique_p1 = "connects TCP endpoints"
        unique_p2 = "UDP is datagram based"
        self.assertNotEqual(self._P2[1:1 + len(unique_p2)], unique_p2)
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "parent",
                        "TCP",
                        [self._v1_span("p1", unique_p1, start=900, end=922)],
                    ),
                    self._graph_concept(
                        "child",
                        "UDP",
                        [self._v1_span("p2", unique_p2, start=1, end=1 + len(unique_p2))],
                    ),
                ],
                "relations": [
                    self._graph_relation(
                        "parent", "child", [self._v1_span("p1", unique_p1, start=0, end=22)]
                    )
                ],
            },
            job_id="section-graph-v1",
        )

        self.assertEqual(result["ingested"], 1)
        self.assertEqual(
            self._stored_spans("concept_mentions"),
            [
                ("p1", *self._expected_span(self._P1, unique_p1)),
                ("p2", *self._expected_span(self._P2, unique_p2)),
            ],
        )
        self.assertEqual(
            self._stored_spans("concept_relation_evidence"),
            [("p1", *self._expected_span(self._P1, unique_p1))],
        )

    def test_repeated_section_graph_literal_without_an_anchor_fails_atomically(self) -> None:
        # "TCP" occurs twice in p1, so nothing in this payload can choose an
        # occurrence.  The first concept is perfectly good and must not survive:
        # a packet is one item, and an item is all or nothing.
        self.assertEqual(self._P1.count("TCP"), 2)
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "good", "UDP", [self._v2_span("p2", "UDP is datagram based")]
                    ),
                    self._graph_concept("bad", "TCP", [self._v2_span("p1", "TCP")]),
                ],
                "relations": [
                    self._graph_relation(
                        "good", "bad", [self._v2_span("p2", "UDP is datagram based")]
                    )
                ],
            },
            job_id="section-graph-repeated",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self._assert_no_graph_rows()
        diagnostics = self._item_diagnostics("section-graph-repeated")
        self.assertEqual(diagnostics["reason"], "ANCHOR_MISSING")
        self.assertEqual(diagnostics["concept_index"], 1)
        self.assertEqual(diagnostics["concept_count"], 2)
        self.assertEqual(diagnostics["mention_index"], 0)
        self.assertEqual(diagnostics["occurrence_count"], 2)
        self.assertEqual(diagnostics["passage_codepoints"], len(self._P1))
        self.assertEqual(diagnostics["evidence_codepoints"], len("TCP"))
        self.assertTrue(
            all(isinstance(value, (bool, int)) or key == "reason" for key, value in diagnostics.items())
        )

    def test_repeated_section_graph_literal_with_an_anchor_selects_that_occurrence(self) -> None:
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "second", "TCP", [self._v2_span("p1", "TCP", before="connects ")]
                    )
                ],
                "relations": [],
            },
            job_id="section-graph-anchored",
        )

        self.assertEqual(result["ingested"], 1)
        # The anchor selects the *second* occurrence, not merely "an" occurrence.
        self.assertEqual(
            self._stored_spans("concept_mentions"),
            [("p1", *self._expected_span(self._P1, "TCP", occurrence=1))],
        )

    def test_section_graph_evidence_must_come_from_the_passage_it_names(self) -> None:
        for label, span, reason in (
            ("wrong-passage", self._v2_span("p1", "UDP is datagram based"), "EVIDENCE_ABSENT"),
            ("unknown-passage", self._v2_span("p9", "UDP is datagram based"), "PASSAGE_UNAVAILABLE"),
        ):
            with self.subTest(case=label):
                job_id = f"section-graph-{label}"
                result = self._ingest_packet(
                    {
                        "concepts": [self._graph_concept("only", "UDP", [span])],
                        "relations": [],
                    },
                    job_id=job_id,
                )
                self.assertEqual((result["ingested"], result["failed"]), (0, 1))
                self._assert_no_graph_rows()
                self.assertEqual(self._item_diagnostics(job_id)["reason"], reason)

    def test_relation_evidence_is_grounded_exactly_like_a_mention(self) -> None:
        # Same resolver, same repair.  A relation span is not a second, weaker
        # citation path: it is the same one.
        unique_p1 = "connects TCP endpoints"
        unique_p2 = "UDP is datagram based"
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept("parent", "TCP", [self._v2_span("p1", unique_p1)]),
                    self._graph_concept("child", "UDP", [self._v2_span("p2", unique_p2)]),
                ],
                "relations": [
                    self._graph_relation(
                        "parent",
                        "child",
                        [self._v1_span("p2", unique_p2, start=999, end=1_020)],
                    )
                ],
            },
            job_id="section-graph-relation-repair",
        )

        self.assertEqual(result["ingested"], 1)
        self.assertEqual(
            self._stored_spans("concept_relation_evidence"),
            [("p2", *self._expected_span(self._P2, unique_p2))],
        )

    def test_relation_evidence_that_cannot_be_located_fails_the_whole_packet(self) -> None:
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "parent", "TCP", [self._v2_span("p1", "connects TCP endpoints")]
                    ),
                    self._graph_concept(
                        "child", "UDP", [self._v2_span("p2", "UDP is datagram based")]
                    ),
                ],
                "relations": [
                    self._graph_relation("parent", "child", [self._v2_span("p1", "TCP")])
                ],
            },
            job_id="section-graph-relation-ambiguous",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self._assert_no_graph_rows()
        diagnostics = self._item_diagnostics("section-graph-relation-ambiguous")
        self.assertEqual(diagnostics["reason"], "ANCHOR_MISSING")
        # A relation failure is reported on its own axes, so an operator can
        # tell "the model cannot cite a relation" from "it cannot cite a term".
        self.assertEqual(diagnostics["relation_index"], 0)
        self.assertEqual(diagnostics["relation_count"], 1)
        self.assertEqual(diagnostics["evidence_index"], 0)
        self.assertEqual(diagnostics["evidence_count"], 1)
        self.assertEqual(diagnostics["local_concept_count"], 2)
        self.assertNotIn("concept_index", diagnostics)
        self.assertNotIn("mention_index", diagnostics)

    def test_unresolved_relation_local_id_fails_atomically(self) -> None:
        # The packet-local ID mechanism is the only thing that makes relations
        # expressible inside one response, so an endpoint that no concept
        # defined is its own failure class rather than a generic schema error.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "parent", "TCP", [self._v2_span("p1", "connects TCP endpoints")]
                    )
                ],
                "relations": [
                    self._graph_relation(
                        "parent", "ghost", [self._v2_span("p1", "connects TCP endpoints")]
                    )
                ],
            },
            job_id="section-graph-ghost",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self._assert_no_graph_rows()
        diagnostics = self._item_diagnostics("section-graph-ghost")
        self.assertEqual(diagnostics["reason"], "RELATION_ENDPOINT_UNRESOLVED")
        self.assertEqual(diagnostics["relation_index"], 0)
        self.assertEqual(diagnostics["local_concept_count"], 1)
        self.assertEqual(
            self.repository.list_items("section-graph-ghost")[0]["error_text"],
            "section graph relation endpoint is not a packet concept",
        )

    def test_duplicate_packet_local_id_is_its_own_failure_class(self) -> None:
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "same", "TCP", [self._v2_span("p1", "connects TCP endpoints")]
                    ),
                    self._graph_concept(
                        "same", "UDP", [self._v2_span("p2", "UDP is datagram based")]
                    ),
                ],
                "relations": [],
            },
            job_id="section-graph-duplicate-local-id",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self._assert_no_graph_rows()
        diagnostics = self._item_diagnostics("section-graph-duplicate-local-id")
        self.assertEqual(diagnostics["reason"], "LOCAL_ID_INVALID")
        self.assertEqual(diagnostics["concept_index"], 1)
        self.assertEqual(diagnostics["local_concept_count"], 1)

    def test_invalid_section_graph_rolls_back_all_graph_writes(self) -> None:
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "only", "TCP", [self._v2_span("p1", "connects TCP endpoints")]
                    )
                ],
                "relations": [],
                "unexpected": True,
            },
            job_id="section-graph-invalid",
        )

        self.assertEqual(result["failed"], 1)
        self._assert_no_graph_rows()
        self.assertEqual(
            self._item_diagnostics("section-graph-invalid")["reason"], "INVALID_SCHEMA"
        )

    def test_section_graph_diagnostics_never_carry_packet_text(self) -> None:
        self.store.add_passages(
            "version",
            [
                {
                    "passage_id": "p4",
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": 3,
                    "content_kind": "paragraph",
                    "content": "The ZORBLAX gate opens. The ZORBLAX gate closes.",
                }
            ],
        )
        # The marker repeats with identical surroundings, so it is unresolvable
        # and appears in the packet, in the evidence and nowhere downstream.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept("only", "ZORBLAX", [self._v2_span("p4", "ZORBLAX gate")])
                ],
                "relations": [],
            },
            job_id="section-graph-no-text",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        summary = self.service.get_job_summary("section-graph-no-text")
        self.assertEqual(summary["items"][0]["failure_diagnostics"]["reason"], "ANCHOR_MISSING")
        stored = self.store._connection().execute(
            "SELECT failure_diagnostics_json FROM batch_items WHERE batch_job_id = ?",
            ("section-graph-no-text",),
        ).fetchone()[0]
        for rendered in (json.dumps(summary["items"][0]["failure_diagnostics"]), repr(summary), stored):
            self.assertNotIn("ZORBLAX", rendered)
            self.assertNotIn("gate", rendered)

    def test_failed_items_create_one_durable_retry_successor(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("failed", "provider timeout")
        self.provider.results[remote_id] = [ProviderItemResult("p2", error="temporary failure")]
        self.service.poll_and_ingest("job", self.provider)

        retry_job = self.service.retry_failed_items("job")
        self.assertEqual(retry_job, self.service.retry_failed_items("job"))
        retry_items = self.repository.list_items(retry_job)
        self.assertEqual(len(retry_items), 2)
        self.assertEqual(
            [(item["passage_id"], item["custom_id"], item["attempt_count"]) for item in retry_items],
            [("p1", "retry:job:p1", 1), ("p2", "retry:job:p2", 1)],
        )

    # ------------------------------------------------------------------
    # Content-free failure diagnostics
    #
    # A failed item deliberately stores no result payload, so these numbers are
    # the only signal a prompt author has.  Every assertion below therefore
    # checks two things at once: that the measurement is useful, and that it is
    # made of counts and flags rather than anything the model or the source
    # said.
    # ------------------------------------------------------------------

    # This passage repeats one literal with identical surroundings, which is
    # exactly the real-world shape that context anchors cannot disambiguate.
    _MARKER_PASSAGE = "The ZORBLAX gate opens. The ZORBLAX gate closes."

    def _add_marker_passage(self) -> str:
        self.store.add_passages(
            "version",
            [
                {
                    "passage_id": "p3",
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": 2,
                    "content_kind": "paragraph",
                    "content": self._MARKER_PASSAGE,
                }
            ],
        )
        return "p3"

    @staticmethod
    def _concept_payload(mentions: list[dict]) -> dict:
        return {
            "concepts": [
                {"name": "Gate", "aliases": [], "definition": "A gate", "mentions": mentions}
            ]
        }

    def _fail_openai_item(self, *, job_id: str, passage_id: str, payload: dict) -> FakeProvider:
        """Poll a one-item OpenAI job whose single result fails grounding."""
        self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            items=[BatchItemInput(passage_id, passage_id, {"body": {"passage": passage_id}})],
            is_sample=True,
            batch_job_id=job_id,
        )
        provider = FakeProvider()
        provider.name = "openai-batch"
        remote_id = self.service.submit(job_id, provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [ProviderItemResult(passage_id, payload=payload)]
        self.assertEqual(self.service.poll_and_ingest(job_id, provider)["failed"], 1)
        return provider

    def _item_diagnostics(self, job_id: str) -> dict:
        items = self.service.get_job_summary(job_id)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "FAILED")
        return items[0]["failure_diagnostics"]

    def test_absent_evidence_failure_is_measured_without_changing_the_error_text(self) -> None:
        # The dominant real-world failure: the model paraphrased or normalized
        # instead of copying the source verbatim.
        self._fail_openai_item(
            job_id="diagnostics-absent",
            passage_id="p2",
            payload=self._concept_payload(
                [{"start_codepoint": 0, "end_codepoint": 5, "evidence": "paraphrase"}]
            ),
        )
        diagnostics = self._item_diagnostics("diagnostics-absent")
        self.assertEqual(
            diagnostics,
            {
                "reason": "EVIDENCE_ABSENT",
                "concept_index": 0,
                "concept_count": 1,
                "mention_index": 0,
                "mention_count": 1,
                "passage_codepoints": len("UDP is datagram based."),
                "evidence_codepoints": len("paraphrase"),
                "occurrence_count": 0,
                "has_anchors": False,
                "anchor_before_codepoints": 0,
                "anchor_after_codepoints": 0,
                "direct_offsets_in_range": True,
                "direct_is_exact": False,
            },
        )
        # Anchor filtering never ran, so the field is absent rather than zero.
        self.assertNotIn("anchored_candidate_count", diagnostics)
        # Instrumentation only: the durable failure class is unchanged.
        self.assertEqual(
            self.repository.list_items("diagnostics-absent")[0]["error_text"],
            "OpenAI evidence is absent from the immutable source",
        )

    def test_repeated_evidence_that_anchors_cannot_disambiguate_is_measured(self) -> None:
        # The other real-world failure: the literal is genuinely present, but
        # the anchors the model supplied select more than one occurrence.
        passage_id = self._add_marker_passage()
        self._fail_openai_item(
            job_id="diagnostics-ambiguous",
            passage_id=passage_id,
            payload=self._concept_payload(
                [
                    {
                        "start_codepoint": 900,
                        "end_codepoint": 912,
                        "evidence": "ZORBLAX gate",
                        "context_before": "The ",
                        "context_after": " ",
                    }
                ]
            ),
        )
        diagnostics = self._item_diagnostics("diagnostics-ambiguous")
        self.assertEqual(
            diagnostics,
            {
                "reason": "EVIDENCE_AMBIGUOUS",
                "concept_index": 0,
                "concept_count": 1,
                "mention_index": 0,
                "mention_count": 1,
                "passage_codepoints": len(self._MARKER_PASSAGE),
                "evidence_codepoints": len("ZORBLAX gate"),
                "occurrence_count": 2,
                "anchored_candidate_count": 2,
                "has_anchors": True,
                "anchor_before_codepoints": len("The "),
                "anchor_after_codepoints": 1,
                "direct_offsets_in_range": False,
                "direct_is_exact": False,
            },
        )
        self.assertEqual(
            self.repository.list_items("diagnostics-ambiguous")[0]["error_text"],
            "OpenAI evidence cannot be uniquely located in the immutable source",
        )

    def test_every_grounding_failure_class_has_its_own_reason_slug(self) -> None:
        anchored = {"start_codepoint": 0, "end_codepoint": 3, "evidence": "TCP"}
        cases = [
            ("INVALID_SCHEMA", "payload", {"concepts": [], "unexpected": True}),
            (
                "INVALID_SCHEMA",
                "concept",
                {"concepts": [{"name": "TCP", "mentions": []}]},
            ),
            ("MENTIONS_MISSING", "empty-mentions", self._concept_payload([])),
            (
                "INVALID_SCHEMA",
                "mention",
                self._concept_payload([{"start_codepoint": 0, "end_codepoint": 3}]),
            ),
            (
                "INVALID_OFFSETS",
                "string-offset",
                self._concept_payload(
                    [{"start_codepoint": "0", "end_codepoint": 3, "evidence": "TCP"}]
                ),
            ),
            (
                "ANCHOR_INVALID",
                "oversized-anchor",
                self._concept_payload(
                    [{**anchored, "context_before": "x" * 49, "context_after": ""}]
                ),
            ),
            (
                "ANCHOR_MISSING",
                "repeated-without-anchor",
                self._concept_payload(
                    [{**anchored, "context_before": "", "context_after": ""}]
                ),
            ),
            (
                "ANCHOR_MISMATCH",
                "anchor-not-in-source",
                self._concept_payload(
                    [{**anchored, "context_before": "", "context_after": "XX"}]
                ),
            ),
            (
                "EVIDENCE_ABSENT",
                "paraphrase",
                self._concept_payload(
                    [{"start_codepoint": 0, "end_codepoint": 3, "evidence": "absent"}]
                ),
            ),
            (
                "EVIDENCE_AMBIGUOUS",
                "legacy-repeat",
                self._concept_payload(
                    [{"start_codepoint": 99, "end_codepoint": 102, "evidence": "TCP"}]
                ),
            ),
        ]
        for reason, label, payload in cases:
            with self.subTest(reason=reason, case=label):
                job_id = f"diagnostics-{label}"
                self._fail_openai_item(job_id=job_id, passage_id="p1", payload=payload)
                diagnostics = self._item_diagnostics(job_id)
                self.assertEqual(diagnostics["reason"], reason)
                self.assertTrue(
                    all(isinstance(value, (bool, int, str)) for value in diagnostics.values())
                )

    def test_mention_position_and_evidence_size_are_reported_for_a_later_concept(self) -> None:
        # "the model degrades after the Nth concept" must be visible without
        # ever storing which concept it was.
        good = {
            "name": "TCP",
            "aliases": [],
            "definition": "A protocol",
            "mentions": [{"start_codepoint": 0, "end_codepoint": 3, "evidence": "TCP"}],
        }
        bad = {
            "name": "Endpoint",
            "aliases": [],
            "definition": "A protocol",
            "mentions": [
                {"start_codepoint": 0, "end_codepoint": 3, "evidence": "TCP"},
                {"start_codepoint": 0, "end_codepoint": 1, "evidence": "t"},
            ],
        }
        self._fail_openai_item(
            job_id="diagnostics-position",
            passage_id="p1",
            payload={"concepts": [good, bad]},
        )
        diagnostics = self._item_diagnostics("diagnostics-position")
        self.assertEqual(diagnostics["reason"], "EVIDENCE_AMBIGUOUS")
        self.assertEqual(diagnostics["concept_index"], 1)
        self.assertEqual(diagnostics["concept_count"], 2)
        self.assertEqual(diagnostics["mention_index"], 1)
        self.assertEqual(diagnostics["mention_count"], 2)
        # A one code point literal that occurs three times is a different
        # prompt fix from a paraphrase, and this is how they are told apart.
        self.assertEqual(diagnostics["evidence_codepoints"], 1)
        self.assertEqual(diagnostics["occurrence_count"], "TCP connects TCP endpoints.".count("t"))

    def test_failure_reason_aggregate_covers_every_failed_item(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("failed")
        self.provider.results[remote_id] = [
            ProviderItemResult("p2", error="provider said something unrepeatable")
        ]
        self.service.poll_and_ingest("job", self.provider)

        summary = self.service.get_job_summary("job")
        self.assertEqual(summary["item_status_counts"], {"FAILED": 2})
        self.assertEqual(
            summary["item_failure_reason_counts"],
            {"PROVIDER_ITEM_ERROR": 1, "TERMINAL_WITHOUT_RESULT": 1},
        )
        self.assertEqual(
            sum(summary["item_failure_reason_counts"].values()),
            summary["item_status_counts"]["FAILED"],
        )
        # The provider's own error text is classified, never copied.
        self.assertNotIn("unrepeatable", repr(summary))
        # The aggregate is also on the history list, which never loads items.
        listed = self.service.list_job_summaries()["items"][0]
        self.assertNotIn("items", listed)
        self.assertEqual(
            listed["item_failure_reason_counts"],
            {"PROVIDER_ITEM_ERROR": 1, "TERMINAL_WITHOUT_RESULT": 1},
        )

    def test_grounding_failures_are_grouped_by_reason_for_an_administrator(self) -> None:
        self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            items=self._items(),
            is_sample=True,
            batch_job_id="diagnostics-aggregate",
        )
        provider = FakeProvider()
        provider.name = "openai-batch"
        remote_id = self.service.submit("diagnostics-aggregate", provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [
            ProviderItemResult(
                "p1",
                payload=self._concept_payload(
                    [{"start_codepoint": 99, "end_codepoint": 102, "evidence": "TCP"}]
                ),
            ),
            ProviderItemResult(
                "p2",
                payload=self._concept_payload(
                    [{"start_codepoint": 0, "end_codepoint": 3, "evidence": "absent"}]
                ),
            ),
        ]
        self.assertEqual(self.service.poll_and_ingest("diagnostics-aggregate", provider)["failed"], 2)
        self.assertEqual(
            self.service.get_job_summary("diagnostics-aggregate")["item_failure_reason_counts"],
            {"EVIDENCE_AMBIGUOUS": 1, "EVIDENCE_ABSENT": 1},
        )

    def test_repeat_poll_backfills_diagnostics_without_recounting_the_failure(self) -> None:
        provider = self._fail_openai_item(
            job_id="diagnostics-repoll",
            passage_id="p2",
            payload=self._concept_payload(
                [{"start_codepoint": 0, "end_codepoint": 5, "evidence": "paraphrase"}]
            ),
        )
        # Represent an item that failed before this instrumentation existed:
        # the failure class is durable, the measurement was never taken.
        with self.store._write() as connection:
            connection.execute(
                "UPDATE batch_items SET failure_diagnostics_json = NULL WHERE batch_job_id = ?",
                ("diagnostics-repoll",),
            )
        self.assertIsNone(self._item_diagnostics("diagnostics-repoll"))
        self.assertEqual(
            self.service.get_job_summary("diagnostics-repoll")["item_failure_reason_counts"],
            {"UNDIAGNOSED": 1},
        )

        # Re-polling the same durable remote job is still not a new failure,
        # but it does re-derive the numbers from the same durable inputs.
        result = self.service.poll_and_ingest("diagnostics-repoll", provider)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(self._item_diagnostics("diagnostics-repoll")["reason"], "EVIDENCE_ABSENT")

    def test_a_later_success_clears_the_previous_failure_measurement(self) -> None:
        provider = self._fail_openai_item(
            job_id="diagnostics-recovered",
            passage_id="p2",
            payload=self._concept_payload(
                [{"start_codepoint": 0, "end_codepoint": 5, "evidence": "paraphrase"}]
            ),
        )
        provider.results["remote-diagnostics-recovered"] = [
            ProviderItemResult(
                "p2",
                payload=self._concept_payload(
                    [{"start_codepoint": 0, "end_codepoint": 3, "evidence": "UDP"}]
                ),
            )
        ]
        self.assertEqual(self.service.poll_and_ingest("diagnostics-recovered", provider)["ingested"], 1)
        summary = self.service.get_job_summary("diagnostics-recovered")
        self.assertIsNone(summary["items"][0]["failure_diagnostics"])
        self.assertEqual(summary["item_failure_reason_counts"], {})

    def test_diagnostics_can_never_carry_source_text_or_model_output(self) -> None:
        """The invariant that matters most: numbers only, nowhere any text."""
        passage_id = self._add_marker_passage()
        self._fail_openai_item(
            job_id="diagnostics-no-source-text",
            passage_id=passage_id,
            payload=self._concept_payload(
                [
                    {
                        "start_codepoint": 4,
                        "end_codepoint": 16,
                        "evidence": "ZORBLAX portal",
                        "context_before": "The ZORBLAX ",
                        "context_after": " opens.",
                    }
                ]
            ),
        )
        diagnostics = self._item_diagnostics("diagnostics-no-source-text")
        self.assertEqual(diagnostics["reason"], "EVIDENCE_ABSENT")
        summary = self.service.get_job_summary("diagnostics-no-source-text")
        stored = self.store._connection().execute(
            "SELECT failure_diagnostics_json FROM batch_items WHERE batch_job_id = ?",
            ("diagnostics-no-source-text",),
        ).fetchone()[0]

        # The marker occurs in the passage, in the evidence the model returned
        # and in its context anchors.  It must survive nowhere downstream.
        for rendered in (json.dumps(diagnostics), repr(summary), stored, repr(self.service.list_job_summaries())):
            self.assertNotIn("ZORBLAX", rendered)
            self.assertNotIn("portal", rendered)
        self.assertTrue(
            all(
                isinstance(value, (bool, int)) or key == "reason"
                for key, value in diagnostics.items()
            )
        )

        # The write path is the gate: a text value cannot be persisted even if
        # a future raise site tries to attach one.
        with self.assertRaisesRegex(BatchServiceError, "counts and flags"):
            BATCH._grounding_diagnostics("EVIDENCE_ABSENT", evidence_codepoints="ZORBLAX")
        with self.assertRaisesRegex(BatchServiceError, "diagnostic field"):
            BATCH._grounding_diagnostics("EVIDENCE_ABSENT", evidence_text=1)
        with self.assertRaisesRegex(BatchServiceError, "failure reason"):
            BATCH._grounding_diagnostics("SOMETHING_NEW")

    def test_operator_summary_drops_untrusted_persisted_diagnostics(self) -> None:
        # Defence in depth for a restored, hand-edited or downgraded database:
        # the read path re-validates instead of displaying what it finds.
        self._fail_openai_item(
            job_id="diagnostics-tampered",
            passage_id="p2",
            payload=self._concept_payload(
                [{"start_codepoint": 0, "end_codepoint": 5, "evidence": "paraphrase"}]
            ),
        )
        with self.store._write() as connection:
            connection.execute(
                "UPDATE batch_items SET failure_diagnostics_json = ? WHERE batch_job_id = ?",
                (
                    json.dumps({"reason": "SOMETHING_ELSE", "evidence": "ZORBLAX", "occurrence_count": 2}),
                    "diagnostics-tampered",
                ),
            )
        diagnostics = self._item_diagnostics("diagnostics-tampered")
        self.assertEqual(diagnostics, {"reason": "UNDIAGNOSED", "occurrence_count": 2})
        self.assertNotIn("ZORBLAX", repr(self.service.get_job_summary("diagnostics-tampered")))

    def test_terminal_job_state_cannot_regress_and_recovery_is_repeatable(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("running")
        self.assertEqual(self.service.recover(self.provider)[0]["state"], "RUNNING")
        self.provider.snapshots[remote_id] = ProviderSnapshot("cancelled", "admin cancelled")
        self.service.poll_and_ingest("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("running")
        with self.assertRaisesRegex(BatchServiceError, "terminal Batch state"):
            self.service.poll_and_ingest("job", self.provider)


class EpubBatchDiagnosticsMigrationTest(unittest.TestCase):
    """The diagnostics column must arrive through the versioned migration runner."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = os.path.join(self.tempdir.name, "legacy.db")

    def _create_previous_schema_version(self) -> None:
        """Build a database exactly as the schema stood before this change."""
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        # Mirror the runner, which owns the bookkeeping table itself and
        # therefore skips the first statement of migration 1.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        migrations = ((1, STORE._MIGRATION_1), (2, STORE._MIGRATION_2), (3, STORE._MIGRATION_3))
        for version, statements in migrations:
            for statement in statements[1:] if version == 1 else statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        connection.execute("PRAGMA user_version = 3")
        connection.execute("INSERT INTO books(book_id, title) VALUES ('book', 'Legacy book')")
        connection.execute(
            "INSERT INTO book_versions(version_id, book_id, epub_sha256, status) "
            "VALUES ('version', 'book', 'legacy-hash', 'READY')"
        )
        connection.execute(
            """INSERT INTO passages(
                   passage_id, version_id, source_href, spine_index, ordinal,
                   content_kind, content, content_sha256
               ) VALUES ('p1', 'version', 'chapter.xhtml', 0, 0, 'paragraph', 'TCP endpoints.', 'hash')"""
        )
        connection.execute(
            """INSERT INTO batch_jobs(batch_job_id, version_id, provider, profile_name, status)
               VALUES ('legacy-job', 'version', 'openai-batch', 'zh-glossary-v3', 'SUCCEEDED')"""
        )
        connection.execute(
            """INSERT INTO batch_items(
                   batch_item_id, batch_job_id, passage_id, custom_id, status, request_json, error_text
               ) VALUES ('legacy-item', 'legacy-job', 'p1', 'p1', 'FAILED', '{}',
                         'OpenAI evidence cannot be uniquely located in the immutable source')"""
        )
        connection.commit()
        connection.close()

    def test_previous_schema_version_gains_the_column_without_losing_data(self) -> None:
        self._create_previous_schema_version()
        self.assertNotIn(
            "failure_diagnostics_json",
            {row[1] for row in sqlite3.connect(self.path).execute("PRAGMA table_info(batch_items)")},
        )

        store = SQLiteEpubStore(self.path)
        self.addCleanup(store.close)
        connection = store._connection()

        self.assertEqual(
            {row[0] for row in connection.execute("SELECT version FROM schema_migrations")},
            {1, 2, 3, 4, 5},
        )
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], STORE.SCHEMA_VERSION)
        item = connection.execute("SELECT * FROM batch_items").fetchone()
        # The pre-existing failure keeps its durable class and simply has no
        # measurement, which is exactly what the aggregate reports it as.
        self.assertEqual(item["batch_item_id"], "legacy-item")
        self.assertEqual(item["status"], "FAILED")
        self.assertEqual(
            item["error_text"], "OpenAI evidence cannot be uniquely located in the immutable source"
        )
        self.assertIsNone(item["failure_diagnostics_json"])
        summary = SQLiteBatchRepository(store).get_job_summary("legacy-job")
        self.assertEqual(summary["item_failure_reason_counts"], {"UNDIAGNOSED": 1})
        self.assertIsNone(summary["items"][0]["failure_diagnostics"])

    def test_migration_is_idempotent_across_reopens(self) -> None:
        self._create_previous_schema_version()
        first = SQLiteEpubStore(self.path)
        first.close()
        second = SQLiteEpubStore(self.path)
        self.addCleanup(second.close)
        self.assertEqual(
            [
                row[1]
                for row in second._connection().execute("PRAGMA table_info(batch_items)")
            ].count("failure_diagnostics_json"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
