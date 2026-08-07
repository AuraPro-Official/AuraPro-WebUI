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
SECTION_GRAPH = _load_module(
    "epub_section_graph_batch_test", "backend/open_webui/retrieval/epub/section_graph.py"
)
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
        # Every full run below names its prompt profile explicitly.  The gate
        # binds to it, so a full job that omitted it would be refused for that
        # reason alone and this test would stop proving what it is about.
        with self.assertRaisesRegex(BatchServiceError, "administrator-approved sample"):
            self.service.create_draft(
                version_id="version",
                provider="openai-batch",
                profile_name="cloud-model-snapshot",
                prompt_profile="zh-glossary-v6",
                items=self._items(),
                batch_job_id="full-without-sample",
            )

        sample_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            prompt_profile="zh-glossary-v6",
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
        # The audit row states which extraction instruction was approved, not
        # only which model snapshot ran it.
        self.assertEqual(approved["prompt_profile"], "zh-glossary-v6")
        self.assertTrue(approved["reviewed_at"])

        with self.assertRaisesRegex(BatchServiceError, "same model profile"):
            self.service.create_draft(
                version_id="version",
                provider="openai-batch",
                profile_name="other-cloud-model-snapshot",
                prompt_profile="zh-glossary-v6",
                items=self._items(),
                batch_job_id="full-after-other-model-sample",
            )

        full_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            prompt_profile="zh-glossary-v6",
            items=self._items(),
            batch_job_id="full-after-sample",
        )
        self.assertEqual(full_id, "full-after-sample")
        reloaded = SQLiteBatchRepository(self.store).list_sample_reviews(version_id="version")
        self.assertEqual(reloaded[0]["sample_batch_job_id"], sample_id)
        self.assertEqual(reloaded[0]["reviewed_by"], "administrator")
        self.assertEqual(reloaded[0]["prompt_profile"], "zh-glossary-v6")

    def _approved_sample(self, prompt_profile: str, *, job_id: str = "openai-sample") -> str:
        """Run one cloud sample to a fully ingested, approved state."""
        sample_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            prompt_profile=prompt_profile,
            items=self._items(),
            is_sample=True,
            batch_job_id=job_id,
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
        self.service.review_sample_job(sample_id, status="APPROVED", reviewed_by="administrator")
        return sample_id

    def test_approving_one_prompt_profile_does_not_unlock_another(self) -> None:
        """The reviewed quality belongs to one instruction, not to the model.

        Same version, same job kind, same pinned model snapshot: only the
        extraction instruction differs.  Before the job recorded its prompt
        profile this was indistinguishable from an approved configuration, so
        promoting a new default silently sent a whole book on unreviewed
        prompt quality.
        """
        self._approved_sample("zh-glossary-v6")

        with self.assertRaisesRegex(BatchServiceError, "same prompt profile"):
            self.service.create_draft(
                version_id="version",
                provider="openai-batch",
                profile_name="cloud-model-snapshot",
                prompt_profile="zh-glossary-v7",
                items=self._items(),
                batch_job_id="full-on-unreviewed-prompt",
            )

        # The same prompt profile is exactly what was approved, and unlocks.
        self.assertEqual(
            self.service.create_draft(
                version_id="version",
                provider="openai-batch",
                profile_name="cloud-model-snapshot",
                prompt_profile="zh-glossary-v6",
                items=self._items(),
                batch_job_id="full-on-reviewed-prompt",
            ),
            "full-on-reviewed-prompt",
        )
        self.assertEqual(
            self.repository.get_job("full-on-reviewed-prompt")["prompt_profile"],
            "zh-glossary-v6",
        )

    def test_a_full_run_without_a_prompt_profile_is_refused(self) -> None:
        """Unknown must never read as "matches"."""
        self._approved_sample("zh-glossary-v6")

        with self.assertRaisesRegex(BatchServiceError, "administrator-approved sample"):
            self.service.create_draft(
                version_id="version",
                provider="openai-batch",
                profile_name="cloud-model-snapshot",
                items=self._items(),
                batch_job_id="full-without-a-prompt-profile",
            )
        # A blank string is not a second spelling of "unknown" either: it
        # would compare equal to itself and could satisfy the gate.
        with self.assertRaisesRegex(BatchServiceError, "prompt_profile cannot be blank"):
            self.service.create_draft(
                version_id="version",
                provider="openai-batch",
                profile_name="cloud-model-snapshot",
                prompt_profile="   ",
                items=self._items(),
                batch_job_id="full-with-a-blank-prompt-profile",
            )

    def test_an_unbackfilled_approved_sample_unlocks_nothing(self) -> None:
        """A legacy approval whose prompt profile is unknown stays inert.

        SQL equality against NULL is NULL rather than true, so the stored side
        cannot match; the test exists because "unknown silently matches" is
        the exact defect, and it must be proven absent, not assumed.
        """
        sample_id = self._approved_sample("zh-glossary-v6")
        connection = self.store._connection()
        connection.execute(
            "UPDATE batch_jobs SET prompt_profile = NULL WHERE batch_job_id = ?", (sample_id,)
        )
        connection.execute(
            "UPDATE epub_batch_sample_reviews SET prompt_profile = NULL "
            "WHERE sample_batch_job_id = ?",
            (sample_id,),
        )
        connection.commit()
        self.assertEqual(
            self.repository.get_job(sample_id)["status"], "SUCCEEDED"
        )

        for requested in ("zh-glossary-v6", "zh-glossary-v7"):
            with self.assertRaisesRegex(BatchServiceError, "administrator-approved sample"):
                self.service.create_draft(
                    version_id="version",
                    provider="openai-batch",
                    profile_name="cloud-model-snapshot",
                    prompt_profile=requested,
                    items=self._items(),
                    batch_job_id=f"full-after-legacy-{requested}",
                )

    def test_a_retry_successor_keeps_the_prompt_profile_it_replays(self) -> None:
        sample_id = self._approved_sample("zh-glossary-v6")
        full_id = self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            prompt_profile="zh-glossary-v6",
            items=self._items(),
            batch_job_id="full-to-retry",
        )
        self.assertNotEqual(full_id, sample_id)
        openai_provider = FakeProvider()
        openai_provider.name = "openai-batch"
        remote_id = self.service.submit(full_id, openai_provider)
        openai_provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        openai_provider.results[remote_id] = [
            ProviderItemResult("p1", payload={"concepts": []}),
            ProviderItemResult("p2", error="provider rejected the item"),
        ]
        self.service.poll_and_ingest(full_id, openai_provider)
        child_id = self.repository.create_retry_child(full_id)
        self.assertEqual(
            self.repository.get_job(child_id)["prompt_profile"], "zh-glossary-v6"
        )

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
        self.assertEqual(first, {"job_id": "job", "state": "SUCCEEDED", "ingested": 2, "failed": 0, "retained": 0})
        self.assertEqual(second, {"job_id": "job", "state": "SUCCEEDED", "ingested": 0, "failed": 0, "retained": 0})
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

    # ------------------------------------------------------------------
    # The offsets-free concept shape (``zh-glossary-v7``)
    #
    # A mention now arrives as evidence plus two anchors and no numbers at all.
    # Nothing about resolution changes - the offset was always derived from the
    # literal - so these tests exist to prove the shape is recognised, that the
    # older shapes still are, and that the stored citation is still a byte-exact
    # slice of the immutable passage.
    # ------------------------------------------------------------------

    @staticmethod
    def _v7_mention(evidence: str, before: str = "", after: str = "") -> dict:
        return {"evidence": evidence, "context_before": before, "context_after": after}

    def test_a_v7_mention_carrying_no_offsets_ingests_as_an_exact_source_slice(self) -> None:
        self._ingest_openai_item(
            job_id="v7-no-offsets",
            passage_id="p2",
            payload=self._concept_payload([self._v7_mention("datagram based")]),
        )
        self.assertEqual(self._only_mention(), ("p2", 7, 21, "datagram based"))
        self.assertEqual(self._assert_mention_slices_the_immutable_source(), (7, 21))

    def test_a_v7_repeated_literal_is_resolved_by_the_anchor_the_model_supplied(self) -> None:
        # "TCP" occurs twice in p1; the anchor is now the only thing the model
        # supplies that can choose between them.
        self._ingest_openai_item(
            job_id="v7-anchored-repeat",
            passage_id="p1",
            payload=self._concept_payload([self._v7_mention("TCP", "connects ", " endpoints")]),
        )
        self.assertEqual(self._only_mention(), ("p1", 13, 16, "TCP"))
        self._assert_mention_slices_the_immutable_source()

    def test_a_v7_repeated_literal_without_an_anchor_fails_anchor_missing(self) -> None:
        self._fail_openai_item(
            job_id="v7-unanchored-repeat",
            passage_id="p1",
            payload=self._concept_payload([self._v7_mention("TCP")]),
        )
        diagnostics = self._item_diagnostics("v7-unanchored-repeat")
        self.assertEqual(
            diagnostics,
            {
                "reason": "ANCHOR_MISSING",
                "concept_index": 0,
                "concept_count": 1,
                "mention_index": 0,
                "mention_count": 1,
                "passage_codepoints": len("TCP connects TCP endpoints."),
                "evidence_codepoints": 3,
                "occurrence_count": 2,
                "has_anchors": True,
                "anchor_before_codepoints": 0,
                "anchor_after_codepoints": 0,
            },
        )
        # ``has_anchors`` is reported truthfully, but this shape supplies no
        # offsets, so the two direct-offset flags are absent rather than false:
        # "not measurable here" must not aggregate as "measured wrong".
        self.assertNotIn("direct_offsets_in_range", diagnostics)
        self.assertNotIn("direct_is_exact", diagnostics)
        self.assertEqual(
            self.repository.list_items("v7-unanchored-repeat")[0]["error_text"],
            "repeated OpenAI evidence needs a non-empty context anchor",
        )

    def test_a_v7_mention_can_never_reach_the_anchor_mismatch_branch(self) -> None:
        # ANCHOR_MISMATCH lives on the direct-exact-offset branch.  With no
        # offsets in the payload that branch is unreachable by construction, so
        # anchors that contradict the source cost nothing on a unique literal -
        # while the same anchors on the v6 shape with exact offsets still fail.
        self._ingest_openai_item(
            job_id="v7-wrong-anchor-unique",
            passage_id="p2",
            payload=self._concept_payload(
                [self._v7_mention("datagram based", "never in the source ", " nor is this")]
            ),
        )
        self.assertEqual(self._only_mention(), ("p2", 7, 21, "datagram based"))
        self._assert_mention_slices_the_immutable_source()

    def test_older_concept_shapes_still_ingest_after_v7_becomes_the_default(self) -> None:
        # The replay guarantee.  v1-v6 requests are already persisted and the
        # approved v6 sample must stay re-ingestable, so the shape is read from
        # the mention rather than from whichever profile is current.  Both
        # legacy shapes carry deliberately wrong offsets, which unique-literal
        # repair is expected to overrule.
        for job_id, mention, expected in (
            (
                "replay-v3-shape",
                {"start_codepoint": 99, "end_codepoint": 113, "evidence": "datagram based"},
                ("p2", 7, 21, "datagram based"),
            ),
            (
                "replay-v6-shape",
                {
                    "start_codepoint": 0,
                    "end_codepoint": 14,
                    "evidence": "datagram based",
                    "context_before": "",
                    "context_after": "",
                },
                ("p2", 7, 21, "datagram based"),
            ),
        ):
            with self.subTest(shape=job_id):
                self._ingest_openai_item(
                    job_id=job_id, passage_id="p2", payload=self._concept_payload([mention])
                )
                self.assertEqual(self._only_mention(), expected)
                self._assert_mention_slices_the_immutable_source()
                with self.store._write() as connection:
                    connection.execute("DELETE FROM concept_mentions")
                    connection.execute("DELETE FROM concept_aliases")
                    connection.execute("DELETE FROM concepts")

    def test_a_half_offset_concept_mention_is_still_an_invalid_shape(self) -> None:
        # Three shapes are recognised and nothing between them: dropping only
        # one of the two offsets, or keeping offsets without anchors while also
        # sending anchors, is a schema failure rather than a resolvable mention.
        for label, mention in (
            ("one-offset", {"end_codepoint": 21, "evidence": "datagram based"}),
            (
                "offsets-and-one-anchor",
                {
                    "start_codepoint": 7,
                    "end_codepoint": 21,
                    "evidence": "datagram based",
                    "context_before": "",
                },
            ),
            ("evidence-only", {"evidence": "datagram based"}),
        ):
            with self.subTest(shape=label):
                job_id = f"v7-invalid-{label}"
                self._fail_openai_item(
                    job_id=job_id, passage_id="p2", payload=self._concept_payload([mention])
                )
                self.assertEqual(self._item_diagnostics(job_id)["reason"], "INVALID_SCHEMA")

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

        # A divergent output for an item that already succeeded must never
        # replace it -- but it must not abort the poll either, or one such item
        # blocks every item after it in the same pass.  The same refusal fires
        # when our own contract tightens after an item was accepted (an evidence
        # floor added later), which is not a provider anomaly at all.  So the
        # stored success stands, the poll completes, and the retention is
        # counted rather than raised.
        before = self.repository.list_items(job_id)
        provider.results[remote_id][0] = ProviderItemResult("p1", payload={"concepts": [{"unexpected": True}]})
        outcome = self.service.poll_and_ingest(job_id, provider)
        self.assertEqual(outcome["retained"], 1)
        self.assertEqual(outcome["failed"], 0)
        after = self.repository.list_items(job_id)
        self.assertEqual([item["status"] for item in after], ["SUCCEEDED", "SUCCEEDED"])
        # The guarantee itself: the stored result is byte-for-byte what it was.
        self.assertEqual(
            [item["response_json"] for item in after],
            [item["response_json"] for item in before],
        )
        self.assertEqual([item["status"] for item in self.repository.list_items(job_id)], ["SUCCEEDED", "SUCCEEDED"])

    def test_terminal_batch_without_output_marks_only_missing_items_failed_for_retry(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("failed")

        result = self.service.poll_and_ingest("job", self.provider)

        self.assertEqual(result, {"job_id": "job", "state": "FAILED", "ingested": 0, "failed": 2, "retained": 0})
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

        self.assertEqual(result, {"job_id": "job", "state": "FAILED", "ingested": 1, "failed": 1, "retained": 0})
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
        self.assertEqual(recovered, {"job_id": "job", "state": "CANCELLED", "ingested": 1, "failed": 1, "retained": 0})
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

    def _ingest_packet(
        self, payload: dict, *, job_id: str = "section-graph", request: dict | None = None
    ) -> dict:
        self.service.create_draft(
            version_id="version",
            provider="fake-batch",
            profile_name="zh-section-graph-v2",
            job_kind="SECTION_GRAPH",
            items=[
                BatchItemInput("p1", "section-1", request or {"body": {"packet": True}})
            ],
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

    _ROOT_TITLE = "Networking"
    _LEAF_TITLE = "Transport protocols"

    def _packet_request(self, *, legacy_per_passage_toc_path: bool = False) -> dict:
        """The durable request row for one packet, as the service stores it."""
        packet = SECTION_GRAPH.build_section_graph_packets(
            [
                {
                    "passage_id": "p1",
                    "ordinal": 0,
                    "toc_path": [self._ROOT_TITLE, self._LEAF_TITLE],
                    "content": self._P1,
                },
                {
                    "passage_id": "p2",
                    "ordinal": 1,
                    "toc_path": [self._ROOT_TITLE, self._LEAF_TITLE],
                    "content": self._P2,
                },
            ]
        )[0]
        body = SECTION_GRAPH.build_section_graph_completion_request(
            model="batch-model", packet=packet
        )
        if legacy_per_passage_toc_path:
            # A request submitted before the per-passage field was removed.  It
            # is durable and replays as it was sent, so the reader has to
            # recognise the titles it actually carried, not the ones the current
            # builder would emit.
            message = json.loads(body["messages"][1]["content"])
            for passage in message["passages"]:
                passage["toc_path"] = [self._ROOT_TITLE, self._LEAF_TITLE]
            body = {
                **body,
                "messages": [
                    body["messages"][0],
                    {"role": "user", "content": json.dumps(message, ensure_ascii=False)},
                ],
            }
        return {"method": "POST", "url": "/v1/chat/completions", "body": body}

    def test_evidence_quoted_from_a_toc_title_is_named_rather_than_called_absent(self) -> None:
        # The failure class that cost a whole diagnosis cycle.  Both spans below
        # are equally unlocatable in the passage they name, so both are rejected
        # and neither writes anything; what differs is the slug, and therefore
        # what a prompt author goes and fixes.  "quoted a field we sent" is
        # repaired by not sending it; invented prose is not.
        for label, evidence, request, reason in (
            (
                "packet-level-title",
                self._ROOT_TITLE,
                self._packet_request(),
                "EVIDENCE_FROM_TOC_PATH",
            ),
            (
                "legacy-per-passage-title",
                self._LEAF_TITLE,
                self._packet_request(legacy_per_passage_toc_path=True),
                "EVIDENCE_FROM_TOC_PATH",
            ),
            (
                "hallucinated",
                "TCP was standardised in Geneva",
                self._packet_request(),
                "EVIDENCE_ABSENT",
            ),
        ):
            with self.subTest(case=label):
                self.assertNotIn(evidence, self._P1)
                job_id = f"section-graph-toc-{label}"
                result = self._ingest_packet(
                    {
                        "concepts": [
                            self._graph_concept(
                                "good", "UDP", [self._v2_span("p2", "UDP is datagram based")]
                            ),
                            self._graph_concept("bad", "TCP", [self._v2_span("p1", evidence)]),
                        ],
                        "relations": [
                            self._graph_relation(
                                "good", "bad", [self._v2_span("p2", "UDP is datagram based")]
                            )
                        ],
                    },
                    job_id=job_id,
                    request=request,
                )

                self.assertEqual((result["ingested"], result["failed"]), (0, 1))
                # A packet is one item and an item is all or nothing: the first
                # concept was perfectly good and must not survive either.
                self._assert_no_graph_rows()
                diagnostics = self._item_diagnostics(job_id)
                self.assertEqual(diagnostics["reason"], reason)
                self.assertEqual(diagnostics["concept_index"], 1)
                self.assertEqual(diagnostics["occurrence_count"], 0)
                self.assertEqual(diagnostics["evidence_codepoints"], len(evidence))
                self.assertEqual(diagnostics["passage_codepoints"], len(self._P1))
                # Still content-free: the slug and numbers, never the title.
                self.assertTrue(
                    all(
                        key == "reason" or isinstance(value, (bool, int))
                        for key, value in diagnostics.items()
                    )
                )
                self.assertNotIn(evidence, json.dumps(diagnostics))

    def test_a_toc_title_that_also_occurs_in_the_passage_is_still_a_normal_span(self) -> None:
        # The slug only ever splits an already-failing span.  A title that is
        # genuinely quotable from the passage it names grounds exactly as
        # before, so naming this class cannot cost a legitimate citation.
        request = self._packet_request()
        self.assertIn("UDP is datagram based", json.loads(
            request["body"]["messages"][1]["content"]
        )["passages"][1]["content"])
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "only", "UDP", [self._v2_span("p2", "UDP is datagram based")]
                    )
                ],
                "relations": [],
            },
            job_id="section-graph-toc-locatable",
            request=request,
        )

        self.assertEqual(result["ingested"], 1)
        self.assertEqual(
            self._stored_spans("concept_mentions"),
            [("p2", *self._expected_span(self._P2, "UDP is datagram based"))],
        )

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

    # ------------------------------------------------------------------
    # Endpoints an administrator has already merged (SDD 4.2.2 point 6)
    #
    # Sample 965c2c11 sat at 15/16 on exactly this: the model asserted
    # 稗子的比喻 --HAS_PART--> 马太福音13:36-43 and an administrator had since
    # merged the parable with its scripture citation, so the relation resolved
    # to a self-loop that ``concept_relations`` forbids by CHECK.  The packet's
    # concepts and mentions were valid and were being discarded over an edge
    # the administrator themselves collapsed.  ``merge_concepts`` already drops
    # such a relation rather than refusing the merge; ingest now matches it.
    # ------------------------------------------------------------------

    def _merged_endpoints(self) -> str:
        """Two concepts an administrator merged after the response was produced.

        Built through the real ``merge_concepts`` rather than by hand, so the
        packet meets exactly the alias graph an administrator leaves behind:
        both names now resolve, through ``_resolve_or_create_concept``, to one
        surviving concept.
        """
        target = self.store.upsert_concept("TCP", concept_id="tcp")
        source = self.store.upsert_concept("UDP", concept_id="udp")
        self.store.merge_concepts(
            target_concept_id=target, source_concept_id=source, merged_by="admin"
        )
        return target

    def test_relation_between_merged_endpoints_is_skipped_and_the_packet_ingests(self) -> None:
        concept_id = self._merged_endpoints()
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
            },
            job_id="section-graph-merged",
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        # The valid part of the packet survives, byte-exact, on the one
        # surviving concept.  This is the whole point: the mentions are not
        # collateral damage of an edge that no longer exists.
        self.assertEqual(
            self._stored_spans("concept_mentions"),
            [
                ("p1", *self._expected_span(self._P1, unique_p1)),
                ("p2", *self._expected_span(self._P2, unique_p2)),
            ],
        )
        self.assertEqual(
            {
                row["concept_id"]
                for row in self.store._connection().execute(
                    "SELECT DISTINCT concept_id FROM concept_mentions"
                )
            },
            {concept_id},
        )
        counts = self._graph_row_counts()
        self.assertEqual(counts["concepts"], 1)
        self.assertEqual(counts["concept_relations"], 0)
        self.assertEqual(counts["concept_relation_assertions"], 0)
        self.assertEqual(counts["concept_relation_evidence"], 0)

    def test_a_skipped_self_relation_is_counted_durably_and_content_free(self) -> None:
        self._merged_endpoints()
        unique_p1 = "connects TCP endpoints"
        unique_p2 = "UDP is datagram based"
        self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept("parent", "TCP", [self._v2_span("p1", unique_p1)]),
                    self._graph_concept("child", "UDP", [self._v2_span("p2", unique_p2)]),
                ],
                "relations": [
                    self._graph_relation("parent", "child", [self._v2_span("p1", unique_p1)])
                ],
            },
            job_id="section-graph-counted",
        )

        item = self.repository.list_items("section-graph-counted")[0]
        self.assertEqual(item["skipped_self_relations"], 1)
        # Visible without opening a response: the item, and the job aggregate an
        # administrator sees first.
        summary = self.service.get_job_summary("section-graph-counted")
        self.assertEqual(summary["items"][0]["skipped_self_relations"], 1)
        self.assertEqual(summary["item_skipped_self_relations"], 1)

        # Content-free by schema, not by a validator: the column holds an
        # integer or nothing, so it cannot carry a concept name or source text
        # even from a hand-edited database.
        connection = self.store._connection()
        self.assertEqual(
            tuple(
                connection.execute(
                    """SELECT skipped_self_relations, typeof(skipped_self_relations)
                       FROM batch_items WHERE batch_job_id = ?""",
                    ("section-graph-counted",),
                ).fetchone()
            ),
            (1, "integer"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE batch_items SET skipped_self_relations = '稗子的比喻'")
        connection.rollback()

        # The durable response stays the grounded model output, relation and
        # all: it is the reproducibility record of what the model returned,
        # while the count records what the write did with it.  That is also what
        # keeps a replay byte-identical, so re-ingest is still a no-op.
        stored = json.loads(item["response_json"])
        self.assertEqual(len(stored["relations"]), 1)
        self.assertEqual(
            self.service.poll_and_ingest("section-graph-counted", self.provider)["ingested"], 0
        )
        self.assertEqual(
            self.service.get_job_summary("section-graph-counted")["item_skipped_self_relations"], 1
        )

    def test_packet_with_one_merged_and_one_valid_relation_keeps_the_valid_one(self) -> None:
        merged = self._merged_endpoints()
        unique_p1 = "connects TCP endpoints"
        unique_p2 = "UDP is datagram based"
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept("parent", "TCP", [self._v2_span("p1", unique_p1)]),
                    self._graph_concept("child", "UDP", [self._v2_span("p2", unique_p2)]),
                    self._graph_concept("other", "IP", [self._v2_span("p2", "datagram")]),
                ],
                "relations": [
                    self._graph_relation("parent", "child", [self._v2_span("p1", unique_p1)]),
                    self._graph_relation(
                        "parent", "other", [self._v2_span("p1", unique_p1)], predicate="PRECEDES"
                    ),
                ],
            },
            job_id="section-graph-mixed",
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self.assertEqual(self.repository.list_items("section-graph-mixed")[0]["skipped_self_relations"], 1)
        relations = self.store._connection().execute(
            "SELECT subject_concept_id, predicate, object_concept_id FROM concept_relations"
        ).fetchall()
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["subject_concept_id"], merged)
        self.assertEqual(relations[0]["predicate"], "PRECEDES")
        self.assertNotEqual(relations[0]["object_concept_id"], merged)
        # The surviving relation's evidence is still an exact slice of source.
        self.assertEqual(
            self._stored_spans("concept_relation_evidence"),
            [("p1", *self._expected_span(self._P1, unique_p1))],
        )

    def test_a_merged_endpoint_does_not_rescue_an_ungrounded_one(self) -> None:
        # The boundary that must not move.  A ``local_id`` no concept defined is
        # ungrounded output, not a merge artefact, and point 5 still governs it:
        # the whole packet fails and nothing it touched changed.
        self._merged_endpoints()
        before = self._graph_row_counts()
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
                    self._graph_relation(
                        "parent", "child", [self._v2_span("p1", "connects TCP endpoints")]
                    ),
                    self._graph_relation(
                        "parent", "ghost", [self._v2_span("p1", "connects TCP endpoints")]
                    ),
                ],
            },
            job_id="section-graph-merged-ghost",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(self._graph_row_counts(), before)
        self.assertEqual(before["concept_mentions"], 0)
        diagnostics = self._item_diagnostics("section-graph-merged-ghost")
        self.assertEqual(diagnostics["reason"], "RELATION_ENDPOINT_UNRESOLVED")
        self.assertEqual(diagnostics["relation_index"], 1)
        self.assertIsNone(
            self.repository.list_items("section-graph-merged-ghost")[0]["skipped_self_relations"]
        )

    def test_nothing_to_skip_records_a_measured_zero_rather_than_nothing(self) -> None:
        unique_p1 = "connects TCP endpoints"
        unique_p2 = "UDP is datagram based"
        self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept("parent", "TCP", [self._v2_span("p1", unique_p1)]),
                    self._graph_concept("child", "UDP", [self._v2_span("p2", unique_p2)]),
                ],
                "relations": [
                    self._graph_relation("parent", "child", [self._v2_span("p1", unique_p1)])
                ],
            },
            job_id="section-graph-zero",
        )

        self.assertEqual(self._graph_row_counts()["concept_relations"], 1)
        self.assertEqual(self.repository.list_items("section-graph-zero")[0]["skipped_self_relations"], 0)
        self.assertEqual(
            self.service.get_job_summary("section-graph-zero")["items"][0]["skipped_self_relations"], 0
        )

        # A CONCEPT_MENTIONS item has no relations to skip, so it measures
        # nothing at all and must stay distinguishable from a genuine zero.
        self._draft("plain-job")
        remote_id = self.service.submit("plain-job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        self.provider.results[remote_id] = [
            ProviderItemResult("p1", payload={"concepts": []}),
            ProviderItemResult("p2", payload={"concepts": []}),
        ]
        self.service.poll_and_ingest("plain-job", self.provider)
        self.assertEqual(
            [item["skipped_self_relations"] for item in self.repository.list_items("plain-job")],
            [None, None],
        )
        self.assertEqual(
            self.service.get_job_summary("plain-job")["item_skipped_self_relations"], 0
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


class EpubBatchEvidenceFloorTest(unittest.TestCase):
    """The per-profile minimum evidence span, enforced rather than requested.

    ``zh-glossary-v6``/``-v7`` and ``zh-section-graph-v2``/``-v3`` all instruct
    the model that evidence must be at least ten Unicode code points, with one
    escape hatch: a passage shorter than the floor is quoted whole.  Nothing
    verified it, so a model that ignored the clause was silently obeyed - on the
    completed full-book run, 345 of 2,619 stored mentions (13.2%) came in below
    the floor and 45 of those were one to three code points.  A single-character
    citation is useless to a reader and, being ubiquitous, pollutes the graph
    channel, so the decision is to reject it at ingest.

    The floor is a property of the profile, never a global constant: v1-v5 and
    ``zh-section-graph-v1`` never asked for one and their stored requests are
    still replayable, so enforcing ten everywhere would retroactively invalidate
    output that honoured the contract it was given.  ``batch.py`` must not
    import ``prompt_profiles`` - cloud ingest recognises a payload shape without
    importing an extraction-policy module - so the numbers are injected into the
    repository, which is exactly what this fixture does.  That the numbers match
    the registries is pinned separately, in test_epub_prompt_profiles.py and
    test_epub_section_graph.py.
    """

    # Both namespaces in one flat mapping, exactly as the service layer supplies
    # it.  Two floored profiles and two unfloored ones, because the interesting
    # property is the difference between them.
    _FLOORS = {
        "zh-glossary-v1": 0,
        "zh-glossary-v6": 10,
        "zh-section-graph-v1": 0,
        "zh-section-graph-v3": 10,
    }
    _FLOOR = 10

    # 27 code points: long enough that the floor is reachable, so a sub-floor
    # citation from it is the model's choice rather than the passage's.
    _LONG = "UDP is a datagram protocol."
    # 6 code points.  Eight of the twenty sampled passages are at or below the
    # floor and two are 9, mostly headings like this one, so the escape hatch
    # covers a real and common shape rather than a hypothetical.
    _SHORT = "第一章 引言"

    _GRAPH_TABLES = (
        "concepts",
        "concept_aliases",
        "concept_mentions",
        "concept_relations",
        "concept_relation_assertions",
        "concept_relation_evidence",
    )

    def setUp(self) -> None:
        self.assertEqual(len(self._SHORT), 6)
        self.assertGreater(len(self._LONG), self._FLOOR)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SQLiteEpubStore(os.path.join(self.tempdir.name, "epub.db"))
        self.addCleanup(self.store.close)
        book_id = self.store.create_book("Floor book", book_id="book")
        self.store.create_book_version(book_id, epub_bytes=b"floor epub", version_id="version")
        self.store.add_passages(
            "version",
            [
                {
                    "passage_id": "long",
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": 0,
                    "content_kind": "paragraph",
                    "content": self._LONG,
                },
                {
                    "passage_id": "short",
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": 1,
                    "content_kind": "heading",
                    "content": self._SHORT,
                },
            ],
        )
        self.store.set_version_status("version", "READY")
        self.repository = SQLiteBatchRepository(self.store, evidence_floors=self._FLOORS)
        self.service = BatchJobService(self.repository)

    # -- helpers -------------------------------------------------------

    def _graph_row_counts(self) -> dict[str, int]:
        connection = self.store._connection()
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in self._GRAPH_TABLES
        }

    def _assert_no_graph_rows(self) -> None:
        """Atomicity: a rejected span writes nothing to any graph table."""
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))

    def _assert_stored_span_slices_the_source(self, expected_evidence: str) -> tuple[int, int]:
        rows = self.store._connection().execute(
            """SELECT m.passage_id, m.start_codepoint, m.end_codepoint, m.evidence, p.content
                 FROM concept_mentions AS m JOIN passages AS p ON p.passage_id = m.passage_id"""
        ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Accepting a mention must never weaken source fidelity.
        self.assertEqual(row["evidence"], expected_evidence)
        self.assertEqual(
            row["content"][row["start_codepoint"]:row["end_codepoint"]], row["evidence"]
        )
        return row["start_codepoint"], row["end_codepoint"]

    @staticmethod
    def _mention(evidence: str, before: str = "", after: str = "") -> dict:
        """One offsets-free (v6/v7-shaped) concept mention."""
        return {"evidence": evidence, "context_before": before, "context_after": after}

    def _ingest_concept(
        self,
        *,
        job_id: str,
        passage_id: str,
        prompt_profile: str | None,
        mentions: list[dict],
    ) -> dict:
        self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            prompt_profile=prompt_profile,
            items=[BatchItemInput(passage_id, passage_id, {"body": {"passage": passage_id}})],
            is_sample=True,
            batch_job_id=job_id,
        )
        provider = FakeProvider()
        provider.name = "openai-batch"
        remote_id = self.service.submit(job_id, provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [
            ProviderItemResult(
                passage_id,
                payload={
                    "concepts": [
                        {
                            "name": "Datagram",
                            "aliases": [],
                            "definition": "A protocol unit",
                            "mentions": mentions,
                        }
                    ]
                },
            )
        ]
        return self.service.poll_and_ingest(job_id, provider)

    def _ingest_packet(self, payload: dict, *, job_id: str, prompt_profile: str) -> dict:
        self.service.create_draft(
            version_id="version",
            provider="fake-batch",
            profile_name="cloud-model-snapshot",
            job_kind="SECTION_GRAPH",
            prompt_profile=prompt_profile,
            items=[BatchItemInput("long", "packet-1", {"body": {"packet": True}})],
            batch_job_id=job_id,
        )
        provider = FakeProvider()
        remote_id = self.service.submit(job_id, provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [ProviderItemResult("packet-1", payload=payload)]
        return self.service.poll_and_ingest(job_id, provider)

    @staticmethod
    def _graph_span(passage_id: str, evidence: str, *, before: str = "", after: str = "") -> dict:
        return {
            "passage_id": passage_id,
            "evidence": evidence,
            "context_before": before,
            "context_after": after,
        }

    def _item_diagnostics(self, job_id: str) -> dict:
        items = self.service.get_job_summary(job_id)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "FAILED")
        return items[0]["failure_diagnostics"]

    _TOO_SHORT_TEXT = "OpenAI evidence is shorter than the minimum span this prompt profile requires"

    # -- concept mentions ----------------------------------------------

    def test_a_sub_floor_mention_is_rejected_and_writes_no_graph_row(self) -> None:
        # "UDP" occurs exactly once and is a byte-exact slice of the source, so
        # every other gate would pass it: the floor is the only thing rejecting
        # it, and this is the 13.2% the run measured.
        self.assertEqual(self._LONG.count("UDP"), 1)
        result = self._ingest_concept(
            job_id="floor-rejects",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention("UDP")],
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self._assert_no_graph_rows()
        diagnostics = self._item_diagnostics("floor-rejects")
        self.assertEqual(diagnostics["reason"], "EVIDENCE_TOO_SHORT")
        # The two numbers that make the rate measurable, and that tell a floor
        # violation apart from the escape hatch without storing any text.
        self.assertEqual(diagnostics["evidence_codepoints"], len("UDP"))
        self.assertEqual(diagnostics["passage_codepoints"], len(self._LONG))
        self.assertTrue(
            all(isinstance(value, (bool, int)) for key, value in diagnostics.items() if key != "reason")
        )
        self.assertEqual(
            self.repository.list_items("floor-rejects")[0]["error_text"], self._TOO_SHORT_TEXT
        )

    def test_the_floor_boundary_accepts_exactly_ten_and_rejects_nine(self) -> None:
        at_floor = "a datagram"
        below_floor = " datagram"
        self.assertEqual((len(at_floor), len(below_floor)), (self._FLOOR, self._FLOOR - 1))
        self.assertIn(at_floor, self._LONG)
        self.assertIn(below_floor, self._LONG)

        result = self._ingest_concept(
            job_id="floor-at",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(at_floor)],
        )
        self.assertEqual(result["ingested"], 1)
        start, end = self._assert_stored_span_slices_the_source(at_floor)
        self.assertEqual((start, end), (self._LONG.index(at_floor), self._LONG.index(at_floor) + self._FLOOR))

        # One code point shorter, same passage, same profile: the floor is a
        # floor rather than an approximate preference.
        result = self._ingest_concept(
            job_id="floor-below",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(below_floor)],
        )
        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(self._item_diagnostics("floor-below")["reason"], "EVIDENCE_TOO_SHORT")

    def test_a_short_passage_quoted_whole_clears_the_floor(self) -> None:
        # The escape hatch, and the reason it cannot be skipped: the instruction
        # itself tells the model to quote a sub-floor passage in full, so
        # rejecting this would reject the exact output the prompt asks for.
        self.assertLess(len(self._SHORT), self._FLOOR)
        result = self._ingest_concept(
            job_id="floor-whole-passage",
            passage_id="short",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(self._SHORT)],
        )
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(
            self._assert_stored_span_slices_the_source(self._SHORT), (0, len(self._SHORT))
        )

    def test_the_escape_hatch_is_the_whole_passage_and_not_merely_a_short_one(self) -> None:
        # A fragment of a short passage is not what the instruction asks for,
        # and is the ubiquitous-single-term citation the floor exists to stop.
        fragment = self._SHORT[:3]
        self.assertLess(len(fragment), len(self._SHORT))
        result = self._ingest_concept(
            job_id="floor-short-fragment",
            passage_id="short",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(fragment)],
        )
        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self._assert_no_graph_rows()
        diagnostics = self._item_diagnostics("floor-short-fragment")
        self.assertEqual(diagnostics["reason"], "EVIDENCE_TOO_SHORT")
        self.assertEqual(diagnostics["evidence_codepoints"], len(fragment))
        self.assertEqual(diagnostics["passage_codepoints"], len(self._SHORT))

    def test_a_legacy_profile_keeps_ingesting_short_evidence(self) -> None:
        # The replay guarantee.  v1 never asked for a minimum, and neither did a
        # job created before ``batch_jobs.prompt_profile`` existed, so the
        # identical mention that v6 rejects above must still ingest for both.
        for job_id, prompt_profile in (
            ("floor-legacy-v1", "zh-glossary-v1"),
            ("floor-legacy-null", None),
        ):
            with self.subTest(prompt_profile=prompt_profile):
                result = self._ingest_concept(
                    job_id=job_id,
                    passage_id="long",
                    prompt_profile=prompt_profile,
                    mentions=[self._mention("UDP")],
                )
                self.assertEqual(result["ingested"], 1)
                self.assertEqual(self._assert_stored_span_slices_the_source("UDP"), (0, 3))
                with self.store._write() as connection:
                    connection.execute("DELETE FROM concept_mentions")
                    connection.execute("DELETE FROM concept_aliases")
                    connection.execute("DELETE FROM concepts")

    def test_a_sub_floor_citation_is_measured_as_short_rather_than_ambiguous(self) -> None:
        # Ordering, stated as a behaviour.  A one-character citation repeats
        # almost by definition, so a floor checked after the occurrence scan
        # would report EVIDENCE_AMBIGUOUS for most of the population it exists
        # to measure and the rate would stay hidden inside another slug.
        self.assertGreater(self._LONG.count("a"), 1)
        for job_id, prompt_profile, reason in (
            ("floor-repeat-v6", "zh-glossary-v6", "EVIDENCE_TOO_SHORT"),
            ("floor-repeat-v1", "zh-glossary-v1", "EVIDENCE_AMBIGUOUS"),
        ):
            with self.subTest(prompt_profile=prompt_profile):
                result = self._ingest_concept(
                    job_id=job_id,
                    passage_id="long",
                    prompt_profile=prompt_profile,
                    mentions=[self._mention("a", before="never in the source ")],
                )
                self.assertEqual((result["ingested"], result["failed"]), (0, 1))
                self.assertEqual(self._item_diagnostics(job_id)["reason"], reason)

    # -- section graph packets -----------------------------------------

    def test_section_graph_relation_evidence_is_held_to_the_same_floor(self) -> None:
        # Relations are the point of this case.  Both mentions here are well
        # over the floor and would persist on their own; only the relation's
        # evidence span is short, and it still costs the whole packet, because
        # every span reaches the source through the one shared resolver.
        subject = "UDP is a datagram"
        object_ = "datagram protocol."
        self.assertGreater(min(len(subject), len(object_)), self._FLOOR)
        result = self._ingest_packet(
            {
                "concepts": [
                    {
                        "local_id": "subject",
                        "name": "UDP",
                        "aliases": [],
                        "definition": "A protocol",
                        "mentions": [self._graph_span("long", subject)],
                    },
                    {
                        "local_id": "object",
                        "name": "Datagram",
                        "aliases": [],
                        "definition": "A unit",
                        "mentions": [self._graph_span("long", object_)],
                    },
                ],
                "relations": [
                    {
                        "subject_local_id": "subject",
                        "predicate": "HAS_PART",
                        "object_local_id": "object",
                        "evidence": [self._graph_span("long", "UDP")],
                    }
                ],
            },
            job_id="floor-relation",
            prompt_profile="zh-section-graph-v3",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self._assert_no_graph_rows()
        diagnostics = self._item_diagnostics("floor-relation")
        self.assertEqual(diagnostics["reason"], "EVIDENCE_TOO_SHORT")
        self.assertEqual(diagnostics["evidence_codepoints"], len("UDP"))
        self.assertEqual(diagnostics["passage_codepoints"], len(self._LONG))
        # The rejection is located as a relation, not as a mention, so an
        # operator can tell which half of the packet needs the prompt fix.
        self.assertEqual(diagnostics["relation_index"], 0)
        self.assertEqual(diagnostics["evidence_index"], 0)
        self.assertNotIn("mention_index", diagnostics)
        self.assertEqual(
            self.repository.list_items("floor-relation")[0]["error_text"], self._TOO_SHORT_TEXT
        )

    def test_section_graph_mentions_and_legacy_packets_follow_the_same_rule(self) -> None:
        packet = {
            "concepts": [
                {
                    "local_id": "only",
                    "name": "UDP",
                    "aliases": [],
                    "definition": "A protocol",
                    "mentions": [self._graph_span("long", "UDP")],
                }
            ],
            "relations": [],
        }

        result = self._ingest_packet(
            packet, job_id="floor-graph-mention", prompt_profile="zh-section-graph-v3"
        )
        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self._assert_no_graph_rows()
        diagnostics = self._item_diagnostics("floor-graph-mention")
        self.assertEqual(diagnostics["reason"], "EVIDENCE_TOO_SHORT")
        self.assertEqual(diagnostics["mention_index"], 0)
        self.assertNotIn("relation_index", diagnostics)

        # zh-section-graph-v1 never asked for a minimum, so its stored packets
        # still replay unchanged.
        result = self._ingest_packet(
            packet, job_id="floor-graph-legacy", prompt_profile="zh-section-graph-v1"
        )
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(self._assert_stored_span_slices_the_source("UDP"), (0, 3))


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
            {1, 2, 3, 4, 5, 6, 7},
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


class EpubBatchPromptProfileMigrationTest(unittest.TestCase):
    """``batch_jobs.prompt_profile`` must arrive through the same runner.

    A store that already holds a submitted cloud run and an approved sample
    must gain the column without losing either, and the pre-existing rows must
    read as NULL rather than as any default that could satisfy the gate.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = os.path.join(self.tempdir.name, "schema-5.db")
        self._create_previous_schema_version()

    def _create_previous_schema_version(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        migrations = (
            (1, STORE._MIGRATION_1),
            (2, STORE._MIGRATION_2),
            (3, STORE._MIGRATION_3),
            (4, STORE._MIGRATION_4),
            (5, STORE._MIGRATION_5),
        )
        for version, statements in migrations:
            for statement in statements[1:] if version == 1 else statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        connection.execute("PRAGMA user_version = 5")
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
        for job_id, is_sample, state in (
            ("legacy-sample", 1, "SUCCEEDED"),
            ("legacy-full", 0, "SUBMITTED"),
        ):
            connection.execute(
                """INSERT INTO batch_jobs(
                       batch_job_id, version_id, provider, profile_name, status, is_sample
                   ) VALUES (?, 'version', 'openai-batch', 'gpt-4.1-2025-04-14', ?, ?)""",
                (job_id, state, is_sample),
            )
        connection.execute(
            """INSERT INTO batch_items(
                   batch_item_id, batch_job_id, passage_id, custom_id, status, request_json
               ) VALUES ('legacy-item', 'legacy-sample', 'p1', 'p1', 'SUCCEEDED', '{}')"""
        )
        connection.commit()
        connection.close()

    def test_previous_schema_version_gains_the_column_without_losing_data(self) -> None:
        self.assertNotIn(
            "prompt_profile",
            {row[1] for row in sqlite3.connect(self.path).execute("PRAGMA table_info(batch_jobs)")},
        )

        store = SQLiteEpubStore(self.path)
        self.addCleanup(store.close)
        connection = store._connection()

        self.assertEqual(
            {row[0] for row in connection.execute("SELECT version FROM schema_migrations")},
            {1, 2, 3, 4, 5, 6, 7},
        )
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], STORE.SCHEMA_VERSION)
        self.assertEqual(STORE.SCHEMA_VERSION, 7)
        jobs = {
            row["batch_job_id"]: row
            for row in connection.execute("SELECT * FROM batch_jobs")
        }
        self.assertEqual(set(jobs), {"legacy-sample", "legacy-full"})
        # The submitted full run keeps every field it had; it simply has no
        # recorded prompt profile until the service-level backfill derives one.
        self.assertEqual(jobs["legacy-full"]["status"], "SUBMITTED")
        self.assertEqual(jobs["legacy-full"]["profile_name"], "gpt-4.1-2025-04-14")
        self.assertIsNone(jobs["legacy-full"]["prompt_profile"])
        self.assertIsNone(jobs["legacy-sample"]["prompt_profile"])
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0], 1
        )

    def test_migration_is_idempotent_across_reopens(self) -> None:
        first = SQLiteEpubStore(self.path)
        first.close()
        second = SQLiteEpubStore(self.path)
        self.addCleanup(second.close)
        columns = [
            row[1] for row in second._connection().execute("PRAGMA table_info(batch_jobs)")
        ]
        self.assertEqual(columns.count("prompt_profile"), 1)
        # The namespaced service table adds its audit column outside the
        # versioned runner, so its idempotency is checked the same way.
        repository = SQLiteBatchRepository(second)
        SQLiteBatchRepository(second)
        review_columns = [
            row[1]
            for row in second._connection().execute(
                "PRAGMA table_info(epub_batch_sample_reviews)"
            )
        ]
        self.assertEqual(review_columns.count("prompt_profile"), 1)
        self.assertEqual(
            {job["batch_job_id"] for job in repository.list_jobs_without_prompt_profile()},
            {"legacy-sample", "legacy-full"},
        )


if __name__ == "__main__":
    unittest.main()
