"""Acceptance tests for durable, provider-agnostic EPUB Batch orchestration."""

from __future__ import annotations

import hashlib
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

    def test_a_suggestion_matching_two_concepts_is_skipped_and_its_item_ingests(self) -> None:
        """What changed here, and why, since this test used to assert the opposite.

        ``_resolve_or_create_concept`` still refuses to guess when one
        suggestion exactly matches two concepts - that guard is untouched, and
        it must be, because linking would assert a merge no administrator
        decided and SDD 4.2 forbids a model performing a semantic merge.  What
        changed is what the refusal *costs*: it used to fail the whole Batch
        item, and now the concept alone is skipped and counted while the rest of
        the item ingests (SDD 4.2.2 point 6c).

        The old behaviour was justified as holding the item for administrator
        review, and this test used to assert the review remedy end to end: an
        administrator merges the two concepts, the same durable result is
        re-polled, and it ingests.  The full-book runs measured that the remedy
        almost never applies.  Of 33 held items, 32 collided on pairs an
        administrator had already adjudicated as *distinct* - 13 on
        ``全域潮汐枢纽``/``潮汐源`` alone - which no merge can resolve without
        reversing the adjudication, and the model will keep proposing them
        because the source genuinely uses both spellings.  Exactly one was a
        real merge candidate.  So the items were not being held; they were being
        discarded, along with every valid concept and mention beside the
        collision.

        The trade is stated plainly rather than hidden: the skipped concept's
        mentions link to nothing, and a later merge does not retroactively
        recover them, because the item is durably ``SUCCEEDED`` and re-ingest is
        idempotent.  Both halves are asserted below.
        """
        surviving = self.store.upsert_concept("UDP", concept_id="udp")
        duplicate = self.store.upsert_concept("datagram", concept_id="datagram")
        self._draft("merge-job")
        remote_id = self.service.submit("merge-job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        # The ambiguous suggestion travels beside a perfectly ordinary one, so
        # "the rest of the item ingests" is a real assertion rather than a
        # statement about an item with nothing else in it.
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
                        },
                        {
                            "name": "Datagram framing",
                            "aliases": [],
                            "definition": "How a datagram is delimited",
                            "mentions": [
                                {"start_codepoint": 16, "end_codepoint": 21, "evidence": "based"}
                            ],
                        },
                    ]
                },
            ),
        ]

        result = self.service.poll_and_ingest("merge-job", self.provider)
        item = next(
            item for item in self.repository.list_items("merge-job") if item["custom_id"] == "p2"
        )
        self.assertEqual((result["ingested"], result["failed"]), (2, 0))
        self.assertEqual(item["status"], "SUCCEEDED")
        self.assertIsNone(item["error_text"])
        self.assertEqual(item["skipped_ambiguous_concepts"], 1)

        # The concept that arrived beside the collision is written in full, and
        # the ambiguous one attached its mention to neither candidate.  Nothing
        # was invented to hang it on: that would be the merge the guard exists
        # to refuse.
        mentions = self.store._connection().execute(
            "SELECT concept_id, passage_id, start_codepoint, end_codepoint, evidence FROM concept_mentions"
        ).fetchall()
        self.assertEqual(len(mentions), 1)
        self.assertEqual(tuple(mentions[0])[1:], ("p2", 16, 21, "based"))
        self.assertNotIn(tuple(mentions[0])[0], {surviving, duplicate})

        # Visible without opening a response, per item and in the job aggregate
        # an administrator reads first.
        summary = self.service.get_job_summary("merge-job")
        self.assertEqual(summary["item_skipped_ambiguous_concepts"], 1)
        self.assertEqual(
            {row["custom_id"]: row["skipped_ambiguous_concepts"] for row in summary["items"]},
            {"p1": 0, "p2": 1},
        )

        # The skipped concept stays in the durable response verbatim.  Unlike a
        # sub-floor span, it is discovered at *write* time, so the read-only
        # grounding pass never removed it from the payload - the column records
        # what the write did, not what the payload contains.
        stored = json.loads(item["response_json"])
        self.assertEqual([concept["name"] for concept in stored["concepts"]], ["UDP", "Datagram framing"])
        self.assertEqual(stored["concepts"][0]["aliases"], ["datagram"])

        # The trade, asserted rather than described.  An administrator merge
        # would have resolved the collision had it happened first, but the item
        # is durably SUCCEEDED and re-ingest is idempotent, so the merge does
        # not retroactively recover the skipped mention.
        merged = self.store.merge_concepts(
            target_concept_id=surviving,
            source_concept_id=duplicate,
            merged_by="administrator",
        )
        self.assertEqual(merged["source_canonical_name"], "datagram")
        replay = self.service.poll_and_ingest("merge-job", self.provider)
        self.assertEqual((replay["ingested"], replay["failed"]), (0, 0))
        self.assertEqual(
            self.store._connection().execute("SELECT COUNT(*) FROM concept_mentions").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.service.get_job_summary("merge-job")["item_skipped_ambiguous_concepts"], 1
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
    def _graph_concept(
        local_id: str, name: str, mentions: list[dict], *, aliases: list[str] | None = None
    ) -> dict:
        """One packet concept.  ``aliases`` is what makes a suggestion collide."""
        return {
            "local_id": local_id,
            "name": name,
            "aliases": list(aliases or []),
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
        # Neither span is admitted, and that is the invariant.  What differs is
        # the unit destroyed.  A span naming a real passage that does not
        # contain it is one bad citation: it is dropped, the concept goes with
        # it for having no mention left, and the item succeeds contributing
        # nothing (SDD 4.2.2 point 6d).  A span naming a passage this EPUB
        # version does not have is not a claim about anything, so there is no
        # claim to drop and the packet still fails whole.
        for label, span, reason in (
            ("wrong-passage", self._v2_span("p1", "UDP is datagram based"), None),
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
                # Either way nothing unverified reaches the graph.
                self._assert_no_graph_rows()
                if reason is None:
                    self.assertEqual((result["ingested"], result["failed"]), (1, 0))
                    item = self.service.get_job_summary(job_id)["items"][0]
                    self.assertEqual(item["status"], "SUCCEEDED")
                    self.assertEqual(item["skipped_ungrounded_evidence"], 1)
                    self.assertEqual(item["skipped_short_evidence"], 0)
                    continue
                self.assertEqual((result["ingested"], result["failed"]), (0, 1))
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
        # The failure class that cost a whole diagnosis cycle.  Every span below
        # is equally unlocatable in the passage it names, so none of them is
        # ever admitted; what differs is the slug, and therefore what a prompt
        # author goes and fixes.  "quoted a field we sent" is repaired by not
        # sending it; invented prose is not.
        #
        # Since SDD 4.2.2 point 6d the slug also decides the unit destroyed, and
        # the two are deliberately not aligned.  EVIDENCE_ABSENT drops its own
        # citation and the packet ingests around it.  EVIDENCE_FROM_TOC_PATH
        # still fails the packet whole, although it is a strict subset of
        # "absent": it has never been measured under this rule, because
        # zh-section-graph-v3 took it to zero by removing the field, so
        # extending leniency to it would be generalizing past the evidence -
        # which is exactly what the two previous revisions of this rule did.
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

                if reason == "EVIDENCE_ABSENT":
                    # The claim-level case.  The bad concept's only citation is
                    # dropped, so the concept goes with it and the relation
                    # naming it goes with the concept - but the good concept and
                    # its exact span are written, which is the whole change.
                    self.assertEqual((result["ingested"], result["failed"]), (1, 0))
                    self.assertEqual(
                        self._stored_spans("concept_mentions"),
                        [("p2", *self._expected_span(self._P2, "UDP is datagram based"))],
                    )
                    self.assertEqual(self._graph_row_counts()["concept_relations"], 0)
                    item = self.service.get_job_summary(job_id)["items"][0]
                    self.assertEqual(item["skipped_ungrounded_evidence"], 1)
                    # Dropped by the grounding pass, so absent from the stored
                    # payload rather than edited out of it afterwards.
                    self.assertNotIn(
                        evidence, self.repository.list_items(job_id)[0]["response_json"]
                    )
                    continue
                self.assertEqual((result["ingested"], result["failed"]), (0, 1))
                # This slug is still all or nothing: the first concept was
                # perfectly good and must not survive either.
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
    # Endpoints an administrator has already merged (SDD 4.2.2 point 6a)
    #
    # Sample 965c2c11 sat at 15/16 on exactly this: the model asserted
    # 双轨校准法 --HAS_PART--> 观测规程2.4-2.11 and an administrator had since
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
            connection.execute("UPDATE batch_items SET skipped_self_relations = '双轨校准法'")
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
        # ungrounded output, not a merge artefact, and the hard-failure half of
        # point 6 still governs it: the whole packet fails and nothing it
        # touched changed.
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

        # ``skipped_ambiguous_concepts`` is the one counter that is measured on
        # both job kinds, because every success resolves concepts.  So a
        # CONCEPT_MENTIONS item records a real zero here where it records NULL
        # above, and NULL in this column means only "written before the column
        # existed".
        self.assertEqual(
            [
                item["skipped_ambiguous_concepts"]
                for item in self.repository.list_items("plain-job")
            ],
            [0, 0],
        )
        self.assertEqual(
            self.repository.list_items("section-graph-zero")[0]["skipped_ambiguous_concepts"], 0
        )

    # ------------------------------------------------------------------
    # A concept whose spellings match several existing ones (SDD 4.2.2 point 6c)
    #
    # The same shape as the merged-endpoint skip above, arriving one step
    # earlier.  ``_resolve_or_create_concept`` cannot link a suggestion whose
    # name and aliases match two concepts without asserting they are the same,
    # which SDD 4.2 forbids a model from doing - so it returns ``None`` and the
    # write skips the concept, its mentions, and any relation that named it.
    #
    # This used to fail the whole packet.  The full-book runs measured the cost:
    # 33 held items, 32 of them colliding on pairs an administrator had already
    # adjudicated as *distinct*, which no merge resolves.  The difference from
    # 6a is that the collision does not settle - the same pairs recur on every
    # future book, because the source genuinely uses both spellings - which is
    # why this counter is a separate column rather than folded into the other.
    # ------------------------------------------------------------------

    def _ambiguous_pair(self) -> tuple[str, str]:
        """Two concepts an administrator adjudicated as distinct, left distinct.

        Deliberately *not* built through ``merge_concepts``: that is the 6a
        fixture, and the whole point here is that these two stay separate, so a
        suggestion naming both matches two concept ids rather than one.
        """
        return (
            self.store.upsert_concept("UDP", concept_id="udp"),
            self.store.upsert_concept("datagram", concept_id="datagram"),
        )

    def test_an_ambiguous_concept_is_skipped_and_takes_only_its_own_relation(self) -> None:
        first, second = self._ambiguous_pair()
        unique_p1 = "connects TCP endpoints"
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept("parent", "TCP", [self._v2_span("p1", unique_p1)]),
                    # Matches both existing concepts, so it resolves to neither.
                    self._graph_concept(
                        "ambiguous",
                        "UDP",
                        [self._v2_span("p2", "UDP is datagram based")],
                        aliases=["datagram"],
                    ),
                    self._graph_concept("other", "Framing", [self._v2_span("p2", "based")]),
                ],
                "relations": [
                    # Names the skipped concept, so it is dropped with it.
                    self._graph_relation("parent", "ambiguous", [self._v2_span("p1", unique_p1)]),
                    # Names nothing skipped, so it survives untouched.
                    self._graph_relation(
                        "parent", "other", [self._v2_span("p1", unique_p1)], predicate="PRECEDES"
                    ),
                ],
            },
            job_id="section-graph-ambiguous",
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        item = self.repository.list_items("section-graph-ambiguous")[0]
        self.assertEqual(item["status"], "SUCCEEDED")
        self.assertEqual(item["skipped_ambiguous_concepts"], 1)
        # The cascade is *not* counted as a self-relation: the endpoint did not
        # resolve to the same concept, it did not resolve at all.  Conflating
        # the two would make an administrator read a merge artefact where there
        # is a standing collision.
        self.assertEqual(item["skipped_self_relations"], 0)

        # The valid relation survives, byte-exact, and the dropped one left no
        # partial row behind in any of the three relation tables.
        relations = self.store._connection().execute(
            "SELECT subject_concept_id, predicate, object_concept_id FROM concept_relations"
        ).fetchall()
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["predicate"], "PRECEDES")
        counts = self._graph_row_counts()
        self.assertEqual(counts["concept_relation_assertions"], 1)
        self.assertEqual(counts["concept_relation_evidence"], 1)
        self.assertEqual(
            self._stored_spans("concept_relation_evidence"),
            [("p1", *self._expected_span(self._P1, unique_p1))],
        )

        # The skipped concept's mention went nowhere - not to either candidate,
        # and not to a concept invented to hold it.
        self.assertEqual(
            self._stored_spans("concept_mentions"),
            [
                ("p1", *self._expected_span(self._P1, unique_p1)),
                ("p2", *self._expected_span(self._P2, "based")),
            ],
        )
        self.assertEqual(
            self.store._connection()
            .execute(
                "SELECT COUNT(*) FROM concept_mentions WHERE concept_id IN (?, ?)", (first, second)
            )
            .fetchone()[0],
            0,
        )

        # Visible per item and in the job aggregate.
        summary = self.service.get_job_summary("section-graph-ambiguous")
        self.assertEqual(summary["item_skipped_ambiguous_concepts"], 1)
        self.assertEqual(summary["items"][0]["skipped_ambiguous_concepts"], 1)

    def test_a_skipped_ambiguous_concept_stays_in_the_response_and_replays_identically(self) -> None:
        """The idempotency invariant the whole design rests on.

        Unlike a sub-floor span, which the read-only grounding pass removes
        before anything is stored, an ambiguous concept is discovered at *write*
        time and is therefore still in the stored response verbatim - exactly as
        a merged-away self-relation is.  The count records what the write did;
        the response records what the model returned.

        That separation is what keeps ``response_json`` byte-identical on
        replay, which is what makes re-ingest a no-op.  If the write ever
        rewrote the payload to reflect its own skips, a re-poll would serialize
        something different and every durable result would stop being a
        reproducibility record.
        """
        self._ambiguous_pair()
        self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "parent", "TCP", [self._v2_span("p1", "connects TCP endpoints")]
                    ),
                    self._graph_concept(
                        "ambiguous",
                        "UDP",
                        [self._v2_span("p2", "UDP is datagram based")],
                        aliases=["datagram"],
                    ),
                ],
                "relations": [
                    self._graph_relation(
                        "parent", "ambiguous", [self._v2_span("p1", "connects TCP endpoints")]
                    )
                ],
            },
            job_id="section-graph-ambiguous-replay",
        )

        item = self.repository.list_items("section-graph-ambiguous-replay")[0]
        stored = json.loads(item["response_json"])
        # Present verbatim: name, aliases, the mention that was never written,
        # and the relation that was dropped.
        ambiguous = next(c for c in stored["concepts"] if c["local_id"] == "ambiguous")
        self.assertEqual(ambiguous["name"], "UDP")
        self.assertEqual(ambiguous["aliases"], ["datagram"])
        self.assertEqual(len(ambiguous["mentions"]), 1)
        self.assertEqual(len(stored["relations"]), 1)

        before = self._graph_row_counts()
        replay = self.service.poll_and_ingest("section-graph-ambiguous-replay", self.provider)

        self.assertEqual((replay["ingested"], replay["failed"]), (0, 0))
        # Byte-identical, not merely equivalent: the assertion is on the stored
        # string, because that is what the idempotency guarantee is about.
        self.assertEqual(
            self.repository.list_items("section-graph-ambiguous-replay")[0]["response_json"],
            item["response_json"],
        )
        self.assertEqual(self._graph_row_counts(), before)
        self.assertEqual(
            self.repository.list_items("section-graph-ambiguous-replay")[0][
                "skipped_ambiguous_concepts"
            ],
            1,
        )

    def test_an_ambiguous_concept_does_not_make_an_undeclared_endpoint_lenient(self) -> None:
        # The boundary that must not move, restated for 6c.  A ``local_id`` the
        # response never declared is ungrounded output, and the presence of a
        # skipped concept in the same packet must not smuggle it through the
        # skip path: the whole packet fails and nothing it touched changed.
        self._ambiguous_pair()
        before = self._graph_row_counts()
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "parent", "TCP", [self._v2_span("p1", "connects TCP endpoints")]
                    ),
                    self._graph_concept(
                        "ambiguous",
                        "UDP",
                        [self._v2_span("p2", "UDP is datagram based")],
                        aliases=["datagram"],
                    ),
                ],
                "relations": [
                    self._graph_relation(
                        "parent", "ghost", [self._v2_span("p1", "connects TCP endpoints")]
                    )
                ],
            },
            job_id="section-graph-ambiguous-ghost",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(self._graph_row_counts(), before)
        diagnostics = self._item_diagnostics("section-graph-ambiguous-ghost")
        self.assertEqual(diagnostics["reason"], "RELATION_ENDPOINT_UNRESOLVED")
        # Rejected by the read-only pass, before any write ran, so neither
        # counter was ever measured for this item.
        item = self.repository.list_items("section-graph-ambiguous-ghost")[0]
        self.assertIsNone(item["skipped_ambiguous_concepts"])
        self.assertIsNone(item["skipped_self_relations"])

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
    """The per-profile minimum evidence span: enforced, and enforced narrowly.

    ``zh-glossary-v6``/``-v7`` and ``zh-section-graph-v2``/``-v3`` all instruct
    the model that evidence must be at least ten Unicode code points, with one
    escape hatch: a passage shorter than that is quoted whole.  Nothing verified
    it, so a model that ignored the clause was silently obeyed - on the
    completed full-book run, 345 of 2,619 stored mentions (13.2%) came in below
    the requested minimum and 45 of those were one to three code points.  A
    one-character citation is useless to a reader and, being ubiquitous,
    pollutes the graph channel.

    Two things about *how* it is enforced were wrong and are fixed here, and
    this class is where both are stated as behaviour.

    First, the level.  A sub-floor span is dropped from the payload during
    grounding; it does not fail its item.  Ingest is atomic per item, so
    rejecting one degenerate span discarded everything that arrived with it:
    measured on the full section-graph run, 13 of 43 packets died on this alone,
    taking 140 concepts, 140 mentions and 105 relations - more than a third of
    the potential relation graph - with them.  This is the same shape as the
    merged-away self-relation of SDD 4.2.2 point 6a, and is resolved the same
    way: skip the element, count it durably, keep the packet.

    Second, the number.  The instruction asks for 10 and ingest enforces 6, and
    the gap is deliberate.  10 was a proxy for "distinctive and locatable" and
    overshoots in Chinese: ``枢对测点的授时`` (7) and ``全网同步统一时基`` (8) are
    complete, distinctive citations, while the pathology the floor was aimed at
    is the bare term - ``枢``, ``扰动源``, ``潮位观测站`` (4).  Requesting more than is
    enforced encourages a substantive citation without discarding a usable one.

    The floor is a property of the profile, never a global constant: v1-v5 and
    ``zh-section-graph-v1`` never asked for one and their stored requests are
    still replayable, so enforcing anything on them would retroactively
    invalidate output that honoured the contract it was given.  ``batch.py``
    must not import ``prompt_profiles`` - cloud ingest recognises a payload
    shape without importing an extraction-policy module - so the numbers are
    injected into the repository, which is exactly what this fixture does.  That
    the numbers match the registries, and that the requested number still
    matches the instruction text, is pinned separately in
    test_epub_prompt_profiles.py and test_epub_section_graph.py.
    """

    # Both namespaces in one flat mapping, exactly as the service layer supplies
    # it, and carrying the *enforced* numbers.  Two floored profiles and two
    # unfloored ones, because the interesting property is the difference.
    _FLOORS = {
        "zh-glossary-v1": 0,
        "zh-glossary-v6": 6,
        "zh-section-graph-v1": 0,
        "zh-section-graph-v3": 6,
    }
    _FLOOR = 6

    # 27 code points: long enough that the floor is reachable, so a sub-floor
    # citation from it is the model's choice rather than the passage's.
    _LONG = "UDP is a datagram protocol."
    # 3 code points, below the floor.  Headings like this one are common - eight
    # of the twenty sampled passages are at or below the requested 10 and two
    # are 9 - so the escape hatch covers a real shape, not a hypothetical.
    _SHORT = "第一章"

    _GRAPH_TABLES = (
        "concepts",
        "concept_aliases",
        "concept_mentions",
        "concept_relations",
        "concept_relation_assertions",
        "concept_relation_evidence",
    )

    def setUp(self) -> None:
        self.assertLess(len(self._SHORT), self._FLOOR)
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
        """A payload whose every element was dropped writes nothing at all."""
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))

    def _stored_mentions(self) -> list[tuple[str, int, int, str]]:
        """Every stored mention, each verified as a byte-exact source slice.

        Admitting a span must never weaken source fidelity, and neither must
        dropping its neighbour: the surviving spans are still checked against
        the immutable passage they name, here rather than in each test.
        """
        rows = self.store._connection().execute(
            """SELECT m.passage_id, m.start_codepoint, m.end_codepoint, m.evidence, p.content
                 FROM concept_mentions AS m JOIN passages AS p ON p.passage_id = m.passage_id
                ORDER BY m.passage_id, m.start_codepoint"""
        ).fetchall()
        for row in rows:
            self.assertEqual(
                row["content"][row["start_codepoint"]:row["end_codepoint"]], row["evidence"]
            )
        return [
            (row["passage_id"], row["start_codepoint"], row["end_codepoint"], row["evidence"])
            for row in rows
        ]

    def _assert_stored_span_slices_the_source(self, expected_evidence: str) -> tuple[int, int]:
        mentions = self._stored_mentions()
        self.assertEqual(len(mentions), 1)
        _, start, end, evidence = mentions[0]
        self.assertEqual(evidence, expected_evidence)
        return start, end

    def _concept_names(self) -> set[str]:
        return {
            row["canonical_name"]
            for row in self.store._connection().execute("SELECT canonical_name FROM concepts")
        }

    def _item(self, job_id: str) -> dict:
        items = self.service.get_job_summary(job_id)["items"]
        self.assertEqual(len(items), 1)
        return items[0]

    def _assert_succeeded_with_drops(self, job_id: str, dropped: int) -> dict:
        """One item succeeded, and the drop count is durable and content-free.

        The count cannot be recovered from ``response_json``: that column holds
        the *grounded* payload, from which a dropped span is by construction
        absent.  So it has its own column, and this asserts the same three
        things the self-relation counter asserts - per item, per job, and
        integer-typed by schema rather than by a validator.
        """
        item = self._item(job_id)
        self.assertEqual(item["status"], "SUCCEEDED")
        self.assertEqual(item["skipped_short_evidence"], dropped)
        self.assertEqual(
            self.service.get_job_summary(job_id)["item_skipped_short_evidence"], dropped
        )
        self.assertEqual(
            tuple(
                self.store._connection().execute(
                    """SELECT skipped_short_evidence, typeof(skipped_short_evidence)
                       FROM batch_items WHERE batch_job_id = ?""",
                    (job_id,),
                ).fetchone()
            ),
            (dropped, "integer"),
        )
        return item

    @staticmethod
    def _mention(evidence: str, before: str = "", after: str = "") -> dict:
        """One offsets-free (v6/v7-shaped) concept mention."""
        return {"evidence": evidence, "context_before": before, "context_after": after}

    @staticmethod
    def _concept(name: str, mentions: list[dict]) -> dict:
        return {
            "name": name,
            "aliases": [],
            "definition": "A protocol unit",
            "mentions": mentions,
        }

    def _ingest_concepts(
        self,
        *,
        job_id: str,
        passage_id: str,
        prompt_profile: str | None,
        concepts: list[dict],
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
            ProviderItemResult(passage_id, payload={"concepts": concepts})
        ]
        return self.service.poll_and_ingest(job_id, provider)

    def _ingest_concept(
        self,
        *,
        job_id: str,
        passage_id: str,
        prompt_profile: str | None,
        mentions: list[dict],
    ) -> dict:
        return self._ingest_concepts(
            job_id=job_id,
            passage_id=passage_id,
            prompt_profile=prompt_profile,
            concepts=[self._concept("Datagram", mentions)],
        )

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

    @staticmethod
    def _graph_concept(local_id: str, name: str, mentions: list[dict]) -> dict:
        return {
            "local_id": local_id,
            "name": name,
            "aliases": [],
            "definition": "A protocol",
            "mentions": mentions,
        }

    def _item_diagnostics(self, job_id: str) -> dict:
        item = self._item(job_id)
        self.assertEqual(item["status"], "FAILED")
        return item["failure_diagnostics"]

    # Spans of the long passage, by length.  Every one occurs exactly once, so
    # the floor is the only gate any of them can fail.
    _VALID = "a datagram protocol."          # 20 code points
    _SECOND_VALID = "UDP is a datagram"      # 17 code points
    _AT_FLOOR = "a data"                     # 6 - admitted
    _BELOW_FLOOR = " data"                   # 5 - dropped
    _BARE = "UDP"                            # 3 - the bare term the floor is for

    # -- concept mentions ----------------------------------------------

    def test_a_sub_floor_mention_is_dropped_and_the_rest_of_the_item_ingests(self) -> None:
        # The core correction.  "UDP" is a byte-exact, uniquely occurring slice
        # of the source, so every other gate passes it and only the floor
        # objects.  It is dropped; the valid mention beside it is unaffected and
        # the item succeeds.  Previously this response ingested nothing at all.
        self.assertEqual(self._LONG.count(self._BARE), 1)
        self.assertLess(len(self._BARE), self._FLOOR)
        result = self._ingest_concept(
            job_id="floor-drops",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(self._BARE), self._mention(self._VALID)],
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self.assertEqual(
            self._assert_stored_span_slices_the_source(self._VALID),
            (self._LONG.index(self._VALID), len(self._LONG)),
        )
        self.assertEqual(self._concept_names(), {"Datagram"})
        item = self._assert_succeeded_with_drops("floor-drops", 1)
        # The drop is a fact about grounding, so the durable response is the
        # payload as written - the dropped span is simply not in it - and a
        # re-ingest of the identical provider output re-derives it byte for
        # byte, which is what keeps ingest idempotent.
        stored = json.loads(self.repository.list_items("floor-drops")[0]["response_json"])
        self.assertEqual(
            [mention["evidence"] for mention in stored["concepts"][0]["mentions"]], [self._VALID]
        )
        self.assertIsNone(item["failure_diagnostics"])

    def test_a_concept_whose_only_mention_is_sub_floor_is_dropped_with_it(self) -> None:
        # The first cascade.  The contract requires a concept to carry at least
        # one mention, so a concept that loses all of them cannot be stored
        # unanchored; it goes too.  Its sibling is untouched - that is the whole
        # point of dropping rather than failing.
        result = self._ingest_concepts(
            job_id="floor-cascade",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            concepts=[
                self._concept("Bare", [self._mention(self._BARE)]),
                self._concept("Datagram", [self._mention(self._VALID)]),
            ],
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self.assertEqual(self._concept_names(), {"Datagram"})
        self._assert_stored_span_slices_the_source(self._VALID)
        self._assert_succeeded_with_drops("floor-cascade", 1)
        stored = json.loads(self.repository.list_items("floor-cascade")[0]["response_json"])
        self.assertEqual([concept["name"] for concept in stored["concepts"]], ["Datagram"])

    def test_a_payload_reduced_to_no_concepts_is_still_a_success(self) -> None:
        # ``{"concepts": []}`` is what the instruction itself tells the model to
        # return when it finds nothing, so an item that grounds down to it has
        # produced a valid, if empty, result.  Calling that a failure would put
        # the item back in the retry population for no reason.
        result = self._ingest_concept(
            job_id="floor-empty",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(self._BARE)],
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self._assert_no_graph_rows()
        self._assert_succeeded_with_drops("floor-empty", 1)
        self.assertEqual(
            self.repository.list_items("floor-empty")[0]["response_json"], '{"concepts":[]}'
        )

    def test_the_floor_boundary_accepts_exactly_six_and_drops_five(self) -> None:
        self.assertEqual(
            (len(self._AT_FLOOR), len(self._BELOW_FLOOR)), (self._FLOOR, self._FLOOR - 1)
        )
        self.assertEqual(self._LONG.count(self._AT_FLOOR), 1)
        self.assertEqual(self._LONG.count(self._BELOW_FLOOR), 1)

        result = self._ingest_concept(
            job_id="floor-at",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(self._AT_FLOOR)],
        )
        self.assertEqual(result["ingested"], 1)
        start, end = self._assert_stored_span_slices_the_source(self._AT_FLOOR)
        self.assertEqual(
            (start, end),
            (self._LONG.index(self._AT_FLOOR), self._LONG.index(self._AT_FLOOR) + self._FLOOR),
        )
        self._assert_succeeded_with_drops("floor-at", 0)

        # One code point shorter, same passage, same profile: the floor is a
        # floor rather than an approximate preference.  It is now the boundary
        # of what is *dropped*, not of what fails.
        result = self._ingest_concept(
            job_id="floor-below",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(self._BELOW_FLOOR)],
        )
        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self._assert_succeeded_with_drops("floor-below", 1)
        # Nothing new was written: the only concept in that response lost its
        # only mention, and the first job's row is all that remains.
        self.assertEqual(len(self._stored_mentions()), 1)

    def test_a_short_passage_quoted_whole_clears_the_floor(self) -> None:
        # The escape hatch, and the reason it cannot be skipped: the instruction
        # itself tells the model to quote a sub-floor passage in full, so
        # dropping this would discard the exact output the prompt asks for.
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
        self._assert_succeeded_with_drops("floor-whole-passage", 0)

    def test_the_escape_hatch_is_the_whole_passage_and_not_merely_a_short_one(self) -> None:
        # A fragment of a short passage is not what the instruction asks for,
        # and is the ubiquitous bare term the floor exists to stop.
        fragment = self._SHORT[:2]
        self.assertLess(len(fragment), len(self._SHORT))
        result = self._ingest_concept(
            job_id="floor-short-fragment",
            passage_id="short",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(fragment)],
        )
        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self._assert_no_graph_rows()
        self._assert_succeeded_with_drops("floor-short-fragment", 1)

    def test_a_legacy_profile_keeps_ingesting_short_evidence(self) -> None:
        # The replay guarantee.  v1 never asked for a minimum, and neither did a
        # job created before ``batch_jobs.prompt_profile`` existed, so the
        # identical mention that v6 drops above must still be stored for both,
        # and must be counted as a measured zero rather than as a drop.
        for job_id, prompt_profile in (
            ("floor-legacy-v1", "zh-glossary-v1"),
            ("floor-legacy-null", None),
        ):
            with self.subTest(prompt_profile=prompt_profile):
                result = self._ingest_concept(
                    job_id=job_id,
                    passage_id="long",
                    prompt_profile=prompt_profile,
                    mentions=[self._mention(self._BARE)],
                )
                self.assertEqual(result["ingested"], 1)
                self.assertEqual(self._assert_stored_span_slices_the_source(self._BARE), (0, 3))
                self._assert_succeeded_with_drops(job_id, 0)
                with self.store._write() as connection:
                    connection.execute("DELETE FROM concept_mentions")
                    connection.execute("DELETE FROM concept_aliases")
                    connection.execute("DELETE FROM concepts")

    def test_a_sub_floor_citation_is_dropped_before_it_can_be_called_ambiguous(self) -> None:
        # Ordering, stated as a behaviour.  A one-character citation repeats
        # almost by definition, so a floor applied after the occurrence scan
        # would fail these items as EVIDENCE_AMBIGUOUS - the very population the
        # floor exists to remove quietly would instead destroy its own packet.
        # Under a profile with no floor the same span is still a hard failure,
        # because there nothing has judged it too short to be worth locating.
        self.assertGreater(self._LONG.count("a"), 1)
        result = self._ingest_concept(
            job_id="floor-repeat-v6",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention("a", before="never in the source ")],
        )
        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self._assert_no_graph_rows()
        self._assert_succeeded_with_drops("floor-repeat-v6", 1)

        result = self._ingest_concept(
            job_id="floor-repeat-v1",
            passage_id="long",
            prompt_profile="zh-glossary-v1",
            mentions=[self._mention("a", before="never in the source ")],
        )
        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(self._item_diagnostics("floor-repeat-v1")["reason"], "EVIDENCE_AMBIGUOUS")

    def test_an_ungrounded_span_still_fails_the_whole_item(self) -> None:
        # The floor is the only thing that became lenient.  Evidence that is not
        # in the immutable source at all is not a small citation, it is not a
        # citation, and it still costs the item - otherwise "drop what does not
        # fit" would quietly become "store whatever grounds and ignore the rest".
        result = self._ingest_concept(
            job_id="floor-not-lenient",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention("never in the source at all")],
        )
        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(self._item_diagnostics("floor-not-lenient")["reason"], "EVIDENCE_ABSENT")
        self._assert_no_graph_rows()

    # -- section graph packets -----------------------------------------

    def test_a_relation_whose_only_evidence_is_sub_floor_is_dropped_alone(self) -> None:
        # Relations are what this cost most.  Both mentions here are well over
        # the floor and both persist; only the relation's single evidence span
        # is short, so only the relation goes.  It used to take the concepts and
        # mentions with it - 105 relations and 140 concepts over the full run.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "subject", "UDP", [self._graph_span("long", self._SECOND_VALID)]
                    ),
                    self._graph_concept(
                        "object", "Datagram", [self._graph_span("long", self._VALID)]
                    ),
                ],
                "relations": [
                    {
                        "subject_local_id": "subject",
                        "predicate": "HAS_PART",
                        "object_local_id": "object",
                        "evidence": [self._graph_span("long", self._BARE)],
                    }
                ],
            },
            job_id="floor-relation",
            prompt_profile="zh-section-graph-v3",
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        counts = self._graph_row_counts()
        self.assertEqual(counts["concepts"], 2)
        self.assertEqual(counts["concept_mentions"], 2)
        self.assertEqual(counts["concept_relations"], 0)
        self.assertEqual(counts["concept_relation_assertions"], 0)
        self.assertEqual(counts["concept_relation_evidence"], 0)
        self._assert_succeeded_with_drops("floor-relation", 1)
        stored = json.loads(self.repository.list_items("floor-relation")[0]["response_json"])
        self.assertEqual(stored["relations"], [])
        self.assertEqual(len(stored["concepts"]), 2)

    def test_a_relation_keeps_the_valid_span_beside_a_sub_floor_one(self) -> None:
        # Per span, not per relation: an assertion needs at least one exact
        # span, and it has one, so it is asserted on exactly that one.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "subject", "UDP", [self._graph_span("long", self._SECOND_VALID)]
                    ),
                    self._graph_concept(
                        "object", "Datagram", [self._graph_span("long", self._VALID)]
                    ),
                ],
                "relations": [
                    {
                        "subject_local_id": "subject",
                        "predicate": "HAS_PART",
                        "object_local_id": "object",
                        "evidence": [
                            self._graph_span("long", self._BARE),
                            self._graph_span("long", self._VALID),
                        ],
                    }
                ],
            },
            job_id="floor-relation-mixed",
            prompt_profile="zh-section-graph-v3",
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self.assertEqual(self._graph_row_counts()["concept_relations"], 1)
        evidence = self.store._connection().execute(
            """SELECT e.passage_id, e.start_codepoint, e.end_codepoint, e.evidence, p.content
                 FROM concept_relation_evidence AS e
                 JOIN passages AS p ON p.passage_id = e.passage_id"""
        ).fetchall()
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["evidence"], self._VALID)
        # Source fidelity is untouched by the drop beside it.
        self.assertEqual(
            evidence[0]["content"][
                evidence[0]["start_codepoint"]:evidence[0]["end_codepoint"]
            ],
            self._VALID,
        )
        self._assert_succeeded_with_drops("floor-relation-mixed", 1)

    def test_a_dropped_concept_takes_its_relations_but_not_its_packet(self) -> None:
        # The second cascade.  ``bare`` loses its only mention and is dropped, so
        # a relation pointing at it has no endpoint left to resolve to and is
        # dropped as well.  That is emphatically not a hallucinated endpoint:
        # the model declared the concept and named it correctly, and ingest is
        # what removed it.  The valid concept and the valid relation beside them
        # both survive.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept("bare", "Bare", [self._graph_span("long", self._BARE)]),
                    self._graph_concept(
                        "subject", "UDP", [self._graph_span("long", self._SECOND_VALID)]
                    ),
                    self._graph_concept(
                        "object", "Datagram", [self._graph_span("long", self._VALID)]
                    ),
                ],
                "relations": [
                    {
                        "subject_local_id": "subject",
                        "predicate": "HAS_PART",
                        "object_local_id": "bare",
                        "evidence": [self._graph_span("long", self._VALID)],
                    },
                    {
                        "subject_local_id": "subject",
                        "predicate": "PRECEDES",
                        "object_local_id": "object",
                        "evidence": [self._graph_span("long", self._VALID)],
                    },
                ],
            },
            job_id="floor-endpoint",
            prompt_profile="zh-section-graph-v3",
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self.assertEqual(self._concept_names(), {"UDP", "Datagram"})
        relations = self.store._connection().execute(
            "SELECT predicate FROM concept_relations"
        ).fetchall()
        self.assertEqual([row["predicate"] for row in relations], ["PRECEDES"])
        self._assert_succeeded_with_drops("floor-endpoint", 1)

    def test_an_endpoint_the_response_never_defined_is_still_a_hard_failure(self) -> None:
        # The distinction the cascade above must not blur.  A ``local_id`` that
        # no concept declared is ungrounded output, not a floor artefact, and it
        # still costs the packet.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._graph_concept(
                        "subject", "UDP", [self._graph_span("long", self._SECOND_VALID)]
                    )
                ],
                "relations": [
                    {
                        "subject_local_id": "subject",
                        "predicate": "HAS_PART",
                        "object_local_id": "never-declared",
                        "evidence": [self._graph_span("long", self._VALID)],
                    }
                ],
            },
            job_id="floor-ghost-endpoint",
            prompt_profile="zh-section-graph-v3",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(
            self._item_diagnostics("floor-ghost-endpoint")["reason"],
            "RELATION_ENDPOINT_UNRESOLVED",
        )
        self._assert_no_graph_rows()

    def test_section_graph_mentions_and_legacy_packets_follow_the_same_rule(self) -> None:
        packet = {
            "concepts": [
                self._graph_concept("only", "UDP", [self._graph_span("long", self._BARE)])
            ],
            "relations": [],
        }

        # Under v3 the packet's one mention is dropped, so its one concept goes
        # with it and the item succeeds having contributed nothing.
        result = self._ingest_packet(
            packet, job_id="floor-graph-mention", prompt_profile="zh-section-graph-v3"
        )
        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self._assert_no_graph_rows()
        self._assert_succeeded_with_drops("floor-graph-mention", 1)
        self.assertEqual(
            json.loads(self.repository.list_items("floor-graph-mention")[0]["response_json"]),
            {"concepts": [], "relations": []},
        )

        # zh-section-graph-v1 never asked for a minimum, so its stored packets
        # still replay unchanged.
        result = self._ingest_packet(
            packet, job_id="floor-graph-legacy", prompt_profile="zh-section-graph-v1"
        )
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(self._assert_stored_span_slices_the_source(self._BARE), (0, 3))
        self._assert_succeeded_with_drops("floor-graph-legacy", 0)

    def test_the_drop_count_is_content_free_by_schema_and_survives_a_replay(self) -> None:
        # Same guarantee ``skipped_self_relations`` carries, for the same
        # reason: an administrator reads this number off a succeeded item, which
        # is exactly the item nobody opens, so it must be safe to display and
        # must not depend on loading a response.
        self._ingest_concept(
            job_id="floor-durable",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention(self._BARE), self._mention(self._VALID)],
        )
        self._assert_succeeded_with_drops("floor-durable", 1)

        connection = self.store._connection()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE batch_items SET skipped_short_evidence = 'UDP'")
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE batch_items SET skipped_short_evidence = -1")
        connection.rollback()

        # An item whose grounding pass never completed measures nothing, and
        # NULL says so rather than claiming a zero.  The job aggregate still
        # reads 0, because a SUM over no measurements is not a measurement.
        self._ingest_concept(
            job_id="floor-unmeasured",
            passage_id="long",
            prompt_profile="zh-glossary-v6",
            mentions=[self._mention("never in the source at all")],
        )
        failed = self._item("floor-unmeasured")
        self.assertEqual(failed["status"], "FAILED")
        self.assertIsNone(failed["skipped_short_evidence"])
        self.assertEqual(
            self.service.get_job_summary("floor-unmeasured")["item_skipped_short_evidence"], 0
        )


class EpubBatchUngroundedCitationTest(unittest.TestCase):
    """SDD 4.2.2 point 6d: a citation that does not verify drops its own claim.

    The rule this class pins replaced one under which a single unverifiable
    citation discarded its entire packet - around twenty-five passages of work.
    Measured across the ten failed section-graph packets of job ``31efbf3b``,
    that behaviour was discarding 78 concepts, 78 mentions and 51 relations over
    17 ungrounded spans out of 183, and two chapters held no relations at all
    solely because of it.

    The measurement also showed why the old rule read the failures wrongly.
    None of the six ``EVIDENCE_ABSENT`` spans was invented text: three were
    verbatim book text filed against a neighbouring passage the same packet had
    shown the model, one differed by a single punctuation mark, one by a single
    deleted character.  All seven ``EVIDENCE_AMBIGUOUS`` packets failed for one
    mechanical reason - the model returned a context window centred on and
    containing its own quote instead of the strictly preceding text, so exact
    anchoring can never match.  Both classes are the same phenomenon: the model
    reproduces real text reliably and is unreliable about the bookkeeping around
    it.  Discarding the bookkeeping is cheap; discarding the packet was not.

    What does **not** change is what a stored citation means.  A dropped span is
    removed by the read-only grounding pass, so nothing unverified is written,
    nothing unverified is stored, and every surviving span is still a byte-exact
    slice of the immutable source - asserted here on every test that writes.
    """

    # "TCP connects" occurs twice in p1, so a span quoting it is
    # locatable-but-ambiguous; nothing in either passage contains "QUIC", so a
    # span quoting it is absent.  Both defects come from the source text rather
    # than from a flag, exactly as they do on the real run.
    _P1 = "TCP connects endpoints. TCP connects nodes."
    _P2 = "UDP is a datagram protocol."
    _REPEATED = "TCP connects"
    _UNIQUE_P1 = "connects endpoints"
    _UNIQUE_P2 = "a datagram protocol"
    _ABSENT = "QUIC handshake framing"
    # 3 code points and uniquely locatable: the floor is the only gate it fails,
    # which is what lets one packet exercise both counters at once.
    _SUB_FLOOR = "UDP"

    _FLOORS = {"zh-glossary-v6": 6, "zh-section-graph-v3": 6}
    _FLOOR = 6

    _GRAPH_TABLES = (
        "concepts",
        "concept_aliases",
        "concept_mentions",
        "concept_relations",
        "concept_relation_assertions",
        "concept_relation_evidence",
    )

    def setUp(self) -> None:
        self.assertEqual(self._P1.count(self._REPEATED), 2)
        self.assertEqual(self._P1.count(self._UNIQUE_P1), 1)
        self.assertEqual(self._P2.count(self._UNIQUE_P2), 1)
        self.assertNotIn(self._ABSENT, self._P1 + self._P2)
        self.assertLess(len(self._SUB_FLOOR), self._FLOOR)
        self.assertGreaterEqual(len(self._REPEATED), self._FLOOR)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SQLiteEpubStore(os.path.join(self.tempdir.name, "epub.db"))
        self.addCleanup(self.store.close)
        book_id = self.store.create_book("Citation book", book_id="book")
        self.store.create_book_version(book_id, epub_bytes=b"citation epub", version_id="version")
        self.store.add_passages(
            "version",
            [
                {
                    "passage_id": passage_id,
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": ordinal,
                    "content_kind": "paragraph",
                    "content": content,
                }
                for ordinal, (passage_id, content) in enumerate(
                    (("p1", self._P1), ("p2", self._P2))
                )
            ],
        )
        self.store.set_version_status("version", "READY")
        self.repository = SQLiteBatchRepository(self.store, evidence_floors=self._FLOORS)
        self.service = BatchJobService(self.repository)

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _span(passage_id: str, evidence: str, *, before: str = "", after: str = "") -> dict:
        return {
            "passage_id": passage_id,
            "evidence": evidence,
            "context_before": before,
            "context_after": after,
        }

    def _absent_span(self) -> dict:
        return self._span("p1", self._ABSENT)

    def _ambiguous_span(self) -> dict:
        # The literal is in the passage, twice; the anchor selects neither
        # occurrence, which is the shape all seven live ambiguous failures have.
        return self._span("p1", self._REPEATED, before="Zz")

    @staticmethod
    def _concept(local_id: str, name: str, mentions: list[dict]) -> dict:
        return {
            "local_id": local_id,
            "name": name,
            "aliases": [],
            "definition": "A protocol",
            "mentions": mentions,
        }

    @staticmethod
    def _relation(subject: str, object_: str, evidence: list[dict]) -> dict:
        return {
            "subject_local_id": subject,
            "predicate": "HAS_PART",
            "object_local_id": object_,
            "evidence": evidence,
        }

    def _ingest_packet(
        self, payload: dict, *, job_id: str, prompt_profile: str = "zh-section-graph-v3"
    ) -> dict:
        self.service.create_draft(
            version_id="version",
            provider="fake-batch",
            profile_name="cloud-model-snapshot",
            job_kind="SECTION_GRAPH",
            prompt_profile=prompt_profile,
            items=[BatchItemInput("p1", "packet-1", {"body": {"packet": True}})],
            batch_job_id=job_id,
        )
        provider = FakeProvider()
        remote_id = self.service.submit(job_id, provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [ProviderItemResult("packet-1", payload=payload)]
        self._providers = getattr(self, "_providers", {})
        self._providers[job_id] = provider
        return self.service.poll_and_ingest(job_id, provider)

    def _graph_row_counts(self) -> dict[str, int]:
        connection = self.store._connection()
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in self._GRAPH_TABLES
        }

    def _stored_spans(self, table: str) -> list[tuple[str, str]]:
        """Every stored span, each re-verified as a byte-exact source slice.

        Dropping a neighbour must never weaken source fidelity, so the surviving
        spans are checked against the immutable passage they name here rather
        than in each test.
        """
        rows = self.store._connection().execute(
            f"""SELECT s.passage_id, s.start_codepoint, s.end_codepoint, s.evidence, p.content
                  FROM {table} AS s JOIN passages AS p ON p.passage_id = s.passage_id
                 ORDER BY s.passage_id, s.start_codepoint"""
        ).fetchall()
        for row in rows:
            self.assertEqual(
                row["content"][row["start_codepoint"]:row["end_codepoint"]], row["evidence"]
            )
        return [(row["passage_id"], row["evidence"]) for row in rows]

    def _concept_names(self) -> set[str]:
        return {
            row["canonical_name"]
            for row in self.store._connection().execute("SELECT canonical_name FROM concepts")
        }

    def _item(self, job_id: str) -> dict:
        items = self.service.get_job_summary(job_id)["items"]
        self.assertEqual(len(items), 1)
        return items[0]

    def _assert_succeeded_with_skips(
        self, job_id: str, *, ungrounded: int, short: int = 0
    ) -> dict:
        """One item succeeded, and both counters are durable, typed and apart.

        Neither count can be recovered from ``response_json``: that column holds
        the grounded payload, from which a dropped span is by construction
        absent.  So each has its own column, and each is asserted three ways -
        per item, per job, and integer-typed by the schema rather than by a
        validator.
        """
        item = self._item(job_id)
        self.assertEqual(item["status"], "SUCCEEDED")
        self.assertEqual(item["skipped_ungrounded_evidence"], ungrounded)
        self.assertEqual(item["skipped_short_evidence"], short)
        summary = self.service.get_job_summary(job_id)
        self.assertEqual(summary["item_skipped_ungrounded_evidence"], ungrounded)
        self.assertEqual(summary["item_skipped_short_evidence"], short)
        self.assertEqual(
            tuple(
                self.store._connection().execute(
                    """SELECT skipped_ungrounded_evidence,
                              typeof(skipped_ungrounded_evidence)
                         FROM batch_items WHERE batch_job_id = ?""",
                    (job_id,),
                ).fetchone()
            ),
            (ungrounded, "integer"),
        )
        return item

    def _item_diagnostics(self, job_id: str) -> dict:
        item = self._item(job_id)
        self.assertEqual(item["status"], "FAILED")
        return item["failure_diagnostics"]

    # -- the rule ------------------------------------------------------

    def test_an_unverifiable_citation_drops_its_claim_and_the_packet_ingests(self) -> None:
        # The core correction, in both claim-level classes at once. Each bad
        # concept loses its only citation and goes with it; the good concept
        # beside them is written exactly as if they had never been in the
        # packet. Previously this response ingested nothing at all.
        # The two cases run against one store, so each names a different good
        # concept and the set of survivors is asserted as it grows. Nothing is
        # deleted between them: an assertion about what is in the graph is worth
        # more when the graph has a history.
        expected: set[str] = set()
        for label, span, survivor in (
            ("absent", self._absent_span(), "UDP"),
            ("ambiguous", self._ambiguous_span(), "SCTP"),
        ):
            with self.subTest(case=label):
                job_id = f"claim-{label}"
                expected.add(survivor)
                result = self._ingest_packet(
                    {
                        "concepts": [
                            self._concept("bad", "QUIC", [span]),
                            self._concept(
                                "good", survivor, [self._span("p2", self._UNIQUE_P2)]
                            ),
                        ],
                        "relations": [],
                    },
                    job_id=job_id,
                )

                self.assertEqual((result["ingested"], result["failed"]), (1, 0))
                # QUIC is never written, under either class.
                self.assertEqual(self._concept_names(), expected)
                self.assertEqual(
                    self._stored_spans("concept_mentions"),
                    [("p2", self._UNIQUE_P2)] * len(expected),
                )
                item = self._assert_succeeded_with_skips(job_id, ungrounded=1)
                self.assertIsNone(item["failure_diagnostics"])

    def test_a_dropped_citation_takes_its_concept_and_then_its_relations(self) -> None:
        # The cascade, unchanged from the evidence floor's and deliberately so.
        # ``bad`` loses its only citation, so the concept is dropped for having
        # no mention; the relation naming it is dropped for having lost an
        # endpoint; and a third relation is dropped for having lost its own only
        # citation. That endpoint loss is emphatically not an unresolved
        # endpoint - the model declared the concept and named it correctly, and
        # ingest is what removed it. The valid concepts and the valid relation
        # survive, which is the entire point of dropping rather than failing.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._concept("bad", "QUIC", [self._absent_span()]),
                    self._concept("tcp", "TCP", [self._span("p1", self._UNIQUE_P1)]),
                    self._concept("udp", "UDP", [self._span("p2", self._UNIQUE_P2)]),
                ],
                "relations": [
                    self._relation("tcp", "udp", [self._span("p2", self._UNIQUE_P2)]),
                    self._relation("tcp", "bad", [self._span("p2", self._UNIQUE_P2)]),
                    self._relation("udp", "tcp", [self._ambiguous_span()]),
                ],
            },
            job_id="claim-cascade",
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self.assertEqual(self._concept_names(), {"TCP", "UDP"})
        self.assertEqual(self._graph_row_counts()["concept_relations"], 1)
        self.assertEqual(
            self._stored_spans("concept_relation_evidence"), [("p2", self._UNIQUE_P2)]
        )
        # Two spans dropped, three relations accounted for: one kept, one lost
        # to its endpoint, one lost to its own citation.
        self._assert_succeeded_with_skips("claim-cascade", ungrounded=2)

    def test_a_packet_reduced_to_nothing_is_still_a_success(self) -> None:
        # An empty result is what the instruction itself asks for when there is
        # nothing to report, so a packet that grounds down to one is a success
        # contributing nothing rather than a failure to retry.
        result = self._ingest_packet(
            {
                "concepts": [self._concept("bad", "QUIC", [self._absent_span()])],
                "relations": [],
            },
            job_id="claim-empty",
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))
        self._assert_succeeded_with_skips("claim-empty", ungrounded=1)
        self.assertEqual(
            self.repository.list_items("claim-empty")[0]["response_json"],
            '{"concepts":[],"relations":[]}',
        )

    def test_the_two_skip_counters_are_kept_apart_per_cause(self) -> None:
        # They must never be summed. A sub-floor span is our own threshold and
        # that number moves when we move the floor; an unverifiable citation is
        # the model's bookkeeping and moves only with a prompt or a model. In
        # one column a floor change and a model regression would be
        # indistinguishable, and either could mask the other.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._concept(
                        "udp",
                        "UDP",
                        [
                            self._span("p2", self._SUB_FLOOR),
                            self._span("p2", self._UNIQUE_P2),
                        ],
                    ),
                    self._concept("bad", "QUIC", [self._absent_span()]),
                ],
                "relations": [],
            },
            job_id="claim-counters",
        )

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        self.assertEqual(self._concept_names(), {"UDP"})
        self._assert_succeeded_with_skips("claim-counters", ungrounded=1, short=1)

    def test_a_dropped_citation_is_absent_from_the_response_and_replays_identically(self) -> None:
        # The invariant the design rests on. The drop happens in the read-only
        # grounding pass, so the stored payload is the graph as written and the
        # dropped span is simply not in it - never edited out of it afterwards,
        # which would destroy the byte-identity a replay depends on. Re-ingesting
        # the same provider output therefore re-derives the identical
        # serialization, and ingest stays idempotent.
        self._ingest_packet(
            {
                "concepts": [
                    self._concept("bad", "QUIC", [self._absent_span()]),
                    self._concept("udp", "UDP", [self._span("p2", self._UNIQUE_P2)]),
                ],
                "relations": [self._relation("udp", "bad", [self._ambiguous_span()])],
            },
            job_id="claim-replay",
        )
        stored = self.repository.list_items("claim-replay")[0]["response_json"]
        graph_before = self._graph_row_counts()

        self.assertNotIn(self._ABSENT, stored)
        self.assertNotIn(self._REPEATED, stored)
        self.assertEqual(json.loads(stored)["relations"], [])
        self.assertEqual(
            [concept["name"] for concept in json.loads(stored)["concepts"]], ["UDP"]
        )

        # The same provider output, polled again.
        result = self.service.poll_and_ingest("claim-replay", self._providers["claim-replay"])

        self.assertEqual((result["ingested"], result["failed"]), (0, 0))
        self.assertEqual(self.repository.list_items("claim-replay")[0]["response_json"], stored)
        self.assertEqual(self._graph_row_counts(), graph_before)
        self._assert_succeeded_with_skips("claim-replay", ungrounded=2)

    # -- what stays a hard failure ---------------------------------------

    def test_an_undeclared_relation_endpoint_is_still_a_hard_failure(self) -> None:
        # The distinction the cascade must never blur, tested with a dropped
        # citation in the same packet so leniency has every chance to swallow
        # it. A ``local_id`` no concept declared is not a citation that failed
        # to verify - there is no claim to localize the failure to - and it
        # still costs the packet, writing nothing at all.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._concept("bad", "QUIC", [self._absent_span()]),
                    self._concept("udp", "UDP", [self._span("p2", self._UNIQUE_P2)]),
                ],
                "relations": [
                    self._relation("udp", "never-declared", [self._span("p2", self._UNIQUE_P2)])
                ],
            },
            job_id="claim-ghost-endpoint",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(
            self._item_diagnostics("claim-ghost-endpoint")["reason"],
            "RELATION_ENDPOINT_UNRESOLVED",
        )
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))
        self.assertIsNone(self._item("claim-ghost-endpoint")["skipped_ungrounded_evidence"])

    def test_a_schema_violation_beside_a_dropped_citation_still_fails_the_packet(self) -> None:
        # A response that is not valid for its schema cannot be localized to a
        # claim either: there is no telling which claim it was going to make.
        result = self._ingest_packet(
            {
                "concepts": [
                    self._concept("bad", "QUIC", [self._absent_span()]),
                    {"local_id": "udp", "name": "UDP", "mentions": []},
                ],
                "relations": [],
            },
            job_id="claim-schema",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(self._item_diagnostics("claim-schema")["reason"], "INVALID_SCHEMA")
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))

    def test_a_missing_anchor_is_not_claim_level_and_still_fails_the_packet(self) -> None:
        # The sharpest edge of the change, stated rather than hidden. This is
        # the *same literal in the same passage* as the ambiguous span two tests
        # up; the only difference is that the model supplied no anchor at all
        # instead of a wrong one, and that one difference decides whether the
        # packet survives. So the line drawn by point 6d is not "is this one
        # citation" - it plainly is - it is "has this class been measured". The
        # ten failed packets held EVIDENCE_ABSENT and EVIDENCE_AMBIGUOUS and
        # nothing else, and this rule has already been restated twice for
        # generalizing past its evidence, so nothing joins them on reasoning
        # alone. Widening it is a decision for a future measurement, and this
        # test is what will fail first when that measurement is taken.
        result = self._ingest_packet(
            {
                "concepts": [self._concept("tcp", "TCP", [self._span("p1", self._REPEATED)])],
                "relations": [],
            },
            job_id="claim-anchor",
        )

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(self._item_diagnostics("claim-anchor")["reason"], "ANCHOR_MISSING")
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))

    def test_a_concept_mentions_item_still_fails_whole_on_an_ungrounded_span(self) -> None:
        # The scope of point 6d, asserted rather than left to a docstring. The
        # rule was measured on section-graph packets and runs only there, so a
        # CONCEPT_MENTIONS response keeps the old behaviour - and its counter is
        # NULL, not 0, because a zero would claim a measurement nobody made.
        self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            prompt_profile="zh-glossary-v6",
            items=[BatchItemInput("p2", "p2", {"body": {"passage": "p2"}})],
            is_sample=True,
            batch_job_id="claim-concept-job",
        )
        provider = FakeProvider()
        provider.name = "openai-batch"
        remote_id = self.service.submit("claim-concept-job", provider)
        provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        provider.results[remote_id] = [
            ProviderItemResult(
                "p2",
                payload={
                    "concepts": [
                        {
                            "name": "QUIC",
                            "aliases": [],
                            "definition": "A protocol",
                            "mentions": [
                                {
                                    "evidence": self._ABSENT,
                                    "context_before": "",
                                    "context_after": "",
                                }
                            ],
                        },
                        {
                            "name": "UDP",
                            "aliases": [],
                            "definition": "A protocol",
                            "mentions": [
                                {
                                    "evidence": self._UNIQUE_P2,
                                    "context_before": "",
                                    "context_after": "",
                                }
                            ],
                        },
                    ]
                },
            )
        ]

        result = self.service.poll_and_ingest("claim-concept-job", provider)

        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        self.assertEqual(
            self._item_diagnostics("claim-concept-job")["reason"], "EVIDENCE_ABSENT"
        )
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))
        self.assertIsNone(self._item("claim-concept-job")["skipped_ungrounded_evidence"])

    def test_a_succeeded_concept_item_records_the_counter_as_null(self) -> None:
        # And the NULL is not an artefact of failing: an ordinary concept item
        # that succeeds also records NULL, because the rule could not have run.
        self.service.create_draft(
            version_id="version",
            provider="openai-batch",
            profile_name="cloud-model-snapshot",
            prompt_profile="zh-glossary-v6",
            items=[BatchItemInput("p2", "p2", {"body": {"passage": "p2"}})],
            is_sample=True,
            batch_job_id="claim-concept-ok",
        )
        provider = FakeProvider()
        provider.name = "openai-batch"
        remote_id = self.service.submit("claim-concept-ok", provider)
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
                                {
                                    "evidence": self._UNIQUE_P2,
                                    "context_before": "",
                                    "context_after": "",
                                }
                            ],
                        }
                    ]
                },
            )
        ]

        result = self.service.poll_and_ingest("claim-concept-ok", provider)

        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        item = self._item("claim-concept-ok")
        self.assertEqual(item["status"], "SUCCEEDED")
        self.assertIsNone(item["skipped_ungrounded_evidence"])
        # A measured zero beside it, so the NULL reads as "not applicable"
        # rather than "this row measured nothing at all".
        self.assertEqual(item["skipped_short_evidence"], 0)
        self.assertEqual(
            self.service.get_job_summary("claim-concept-ok")[
                "item_skipped_ungrounded_evidence"
            ],
            0,
        )


class EpubBatchFailedPacketDryRunTest(unittest.TestCase):
    """Measuring a packet: what is inside it, at no cost and no risk.

    This was built when one ungrounded span still discarded a whole packet, to
    answer what those ten discarded packets contained - a question the store
    could not answer, because ``ingest_success`` is the only writer of
    ``batch_items.response_json`` and a FAILED item therefore stores nothing at
    all.  The measurement it produced is what SDD 4.2.2 point 6d was decided
    from, and the rule then absorbed the probe's central behaviour: dropping a
    claim-level rejection and carrying on is what ingest now does.

    So the class keeps its three properties and gains a fourth.

    **It measures the real thing.**  A classifier that disagreed with ingest
    would be worse than none, so there is no second implementation to disagree:
    the same ``_ground_section_graph_payload`` walks the packet, over the same
    read connection, with the same floor from the same recorded prompt profile,
    the same resolver, and the same ``_is_claim_level_failure`` deciding which
    rejections are dropped.

    **It agrees with the write, exactly.**  Now that ingest is lenient, the
    measurement is a *preview*, and the two tests that matter most compare it
    against the rows a real ingest of the same packet produced - not
    approximately, exactly.  Where the two can still part is named rather than
    hidden: a probe also classifies the rejections ingest refuses to drop, so
    ``spans_skipped_by_ingest`` is reported beside ``spans_failed`` and a packet
    that will still fail whole reads as one.

    **It writes nothing.**  Asserted, not claimed: the Batch ledger is
    checksummed byte-for-byte and every graph table row-counted before and
    after.

    **It costs nothing.**  Only ``poll`` and ``fetch_results`` are called -
    ``batches.retrieve`` plus an output-file download - and ``submit`` is never
    reached, which is asserted directly because the difference between a free
    re-fetch and a paid re-run is one method call.

    The two claim-level classes are reported apart throughout, because they are
    different findings.  ``EVIDENCE_ABSENT`` means the model quoted text that is
    not in the passage it named; ``EVIDENCE_AMBIGUOUS`` means the text is there,
    verbatim, more than once, and only the occurrence is unresolved.
    """

    # "TCP" occurs twice in p1, so it is locatable-but-ambiguous; nothing in
    # either passage contains "QUIC", so a span quoting it is absent.  Both
    # defects come from the source text rather than from a flag, exactly as they
    # do on the real run.
    _P1 = "TCP connects TCP endpoints."
    _P2 = "UDP is datagram based."
    _ABSENT = "QUIC"
    _ABSENT_LONGER = "QUIC handshake"
    _REPEATED = "TCP"
    _UNIQUE_P1 = "connects TCP endpoints"
    _UNIQUE_P2 = "UDP is datagram based"

    _GRAPH_TABLES = (
        "concepts",
        "concept_aliases",
        "concept_mentions",
        "concept_relations",
        "concept_relation_assertions",
        "concept_relation_evidence",
    )

    def setUp(self) -> None:
        self.assertEqual(self._P1.count(self._REPEATED), 2)
        self.assertNotIn(self._ABSENT, self._P1 + self._P2)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SQLiteEpubStore(os.path.join(self.tempdir.name, "epub.db"))
        self.addCleanup(self.store.close)
        book_id = self.store.create_book("Packet book", book_id="book")
        self.store.create_book_version(book_id, epub_bytes=b"packet epub", version_id="version")
        self.store.add_passages(
            "version",
            [
                {
                    "passage_id": "p1",
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": 0,
                    "content_kind": "paragraph",
                    "content": self._P1,
                },
                {
                    "passage_id": "p2",
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": 1,
                    "content_kind": "paragraph",
                    "content": self._P2,
                },
            ],
        )
        self.store.set_version_status("version", "READY")
        self.repository = SQLiteBatchRepository(self.store)
        self.service = BatchJobService(self.repository)
        self.provider = FakeProvider()

    # -- packet construction -------------------------------------------

    @staticmethod
    def _span(passage_id: str, evidence: str, *, before: str = "", after: str = "") -> dict:
        """One zh-section-graph-v2/v3 span: a literal, anchors, and no offsets."""
        return {
            "passage_id": passage_id,
            "evidence": evidence,
            "context_before": before,
            "context_after": after,
        }

    @staticmethod
    def _concept(local_id: str, name: str, mentions: list[dict]) -> dict:
        return {
            "local_id": local_id,
            "name": name,
            "aliases": [],
            "definition": "A protocol",
            "mentions": mentions,
        }

    @staticmethod
    def _relation(subject: str, object_: str, evidence: list[dict]) -> dict:
        return {
            "subject_local_id": subject,
            "predicate": "HAS_PART",
            "object_local_id": object_,
            "evidence": evidence,
        }

    def _mixed_packet(self) -> dict:
        """One packet carrying every outcome the measurement has to tell apart.

        Deliberately not a minimal fixture.  The whole question the owner is
        asking is what sits *beside* an ungrounded span, so a packet that
        contained only the defect would answer nothing:

        * ``absent`` quotes text no passage contains -> ``EVIDENCE_ABSENT``;
        * ``ambiguous`` quotes a literal that occurs twice, with an anchor that
          selects neither -> ``EVIDENCE_AMBIGUOUS``, which is the shape all
          seven live ambiguous failures have (``anchored_candidate_count`` 0);
        * ``tcp`` and ``udp`` are ordinary, uniquely locatable, and must survive;
        * one relation between the two survivors must survive with them;
        * one relation points at the concept the absent span removed -> lost to
          the endpoint cascade;
        * one relation's only evidence is itself absent -> lost for having no
          citation left.

        The two cascades are separated on purpose: "the model named a concept it
        could not evidence" and "the model asserted an edge it could not
        evidence" are different findings with different remedies.
        """
        return {
            "concepts": [
                self._concept("absent", "QUIC", [self._span("p1", self._ABSENT_LONGER)]),
                self._concept(
                    "ambiguous",
                    "Segment",
                    [self._span("p1", self._REPEATED, before="Zz")],
                ),
                self._concept("tcp", "TCP", [self._span("p1", self._UNIQUE_P1)]),
                self._concept("udp", "UDP", [self._span("p2", self._UNIQUE_P2)]),
            ],
            "relations": [
                self._relation("tcp", "udp", [self._span("p1", self._UNIQUE_P1)]),
                self._relation("tcp", "absent", [self._span("p2", "datagram")]),
                self._relation("udp", "tcp", [self._span("p1", self._ABSENT)]),
            ],
        }

    def _hard_packet(self) -> dict:
        """The mixed packet plus one rejection ingest still refuses whole.

        ``p9`` is not a passage of this EPUB version, so the span naming it is
        not a claim about the source that could be dropped - there is nothing to
        have cited.  ``_is_claim_level_failure`` says so, and the item therefore
        still fails whole, which is what keeps a *failed* packet in this class's
        fixtures now that the mixed packet on its own ingests.

        It is also the only shape in which a probe and ingest disagree, so the
        tests below can assert that the disagreement is reported rather than
        silent: four spans classified, three of them ones ingest would drop.
        """
        packet = self._mixed_packet()
        packet["concepts"] = [
            *packet["concepts"],
            self._concept("unknown", "SCTP", [self._span("p9", self._UNIQUE_P1)]),
        ]
        return packet

    def _clean_packet(self) -> dict:
        """A packet with no ungrounded span, so ingest accepts it whole."""
        return {
            "concepts": [
                self._concept("tcp", "TCP", [self._span("p1", self._UNIQUE_P1)]),
                self._concept("udp", "UDP", [self._span("p2", self._UNIQUE_P2)]),
            ],
            "relations": [
                self._relation("tcp", "udp", [self._span("p2", "datagram")]),
            ],
        }

    # -- job plumbing ---------------------------------------------------

    def _run_job(self, payloads: dict[str, dict], *, job_id: str = "packets") -> dict:
        """Create, submit and poll one SECTION_GRAPH job; return the poll result."""
        self.service.create_draft(
            version_id="version",
            provider="fake-batch",
            profile_name="cloud-model-snapshot",
            prompt_profile="zh-section-graph-v3",
            job_kind="SECTION_GRAPH",
            items=[
                BatchItemInput(passage_id, custom_id, {"body": {"packet": True}})
                for custom_id, passage_id in zip(payloads, ("p1", "p2", "p1", "p2"))
            ],
            batch_job_id=job_id,
        )
        remote_id = self.service.submit(job_id, self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        self.provider.results[remote_id] = [
            ProviderItemResult(custom_id, payload=payload)
            for custom_id, payload in payloads.items()
        ]
        return self.service.poll_and_ingest(job_id, self.provider)

    def _ledger(self) -> list[tuple]:
        """Every ``batch_items`` row, whole, so an in-place UPDATE cannot hide.

        Row counts alone would miss the failure mode that actually matters here
        - a status flipped, a ``response_json`` written, a diagnostic replaced -
        so the comparison is over the rows themselves.
        """
        return [
            tuple(row)
            for row in self.store._connection().execute(
                "SELECT * FROM batch_items ORDER BY batch_item_id"
            )
        ]

    def _ledger_checksum(self) -> str:
        return hashlib.sha256(repr(self._ledger()).encode("utf-8")).hexdigest()

    def _graph_row_counts(self) -> dict[str, int]:
        connection = self.store._connection()
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in self._GRAPH_TABLES
        }

    def _packet(self, report: dict, custom_id: str) -> dict:
        matches = [item for item in report["packets"] if item["custom_id"] == custom_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    # -- the measurement -------------------------------------------------

    def test_a_packet_is_measured_span_by_span_and_the_write_agrees(self) -> None:
        # The headline, and it now has two halves. The measurement reports the
        # packet span by span - which is what the ten failed packets were
        # measured with, and what SDD 4.2.2 point 6d was decided from. And
        # ingest, under that rule, writes exactly what the measurement says:
        # three ungrounded spans dropped, everything beside them kept.
        result = self._run_job({"packet-1": self._mixed_packet()})
        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        written = self._graph_row_counts()

        packet = self.repository.dry_run_section_graph_packet(
            "packets", "packet-1", self._mixed_packet()
        )

        self.assertTrue(packet["grounded"])
        self.assertIsNone(packet["unmeasurable_reason"])
        # Four mention spans and three relation-evidence spans; both kinds are
        # grounded by the same resolver and both are counted.
        self.assertEqual(packet["evidence_spans"], 7)
        self.assertEqual((packet["mention_spans"], packet["relation_evidence_spans"]), (4, 3))
        # Three ungrounded spans, split by the classes that were treated
        # differently rather than lumped into one "failed" number.
        self.assertEqual(packet["spans_failed"], 3)
        self.assertEqual(
            packet["spans_failed_by_reason"],
            {"EVIDENCE_ABSENT": 2, "EVIDENCE_AMBIGUOUS": 1},
        )
        self.assertEqual(
            packet["mention_spans_failed_by_reason"],
            {"EVIDENCE_ABSENT": 1, "EVIDENCE_AMBIGUOUS": 1},
        )
        self.assertEqual(
            packet["relation_evidence_spans_failed_by_reason"], {"EVIDENCE_ABSENT": 1}
        )
        # Every one of them is a class ingest drops, so nothing here is a
        # prediction the write will not honour.
        self.assertEqual(packet["spans_skipped_by_ingest"], packet["spans_failed"])
        # And the grounded counts are the rows that were actually written - not
        # approximately, exactly. This is the assertion the whole design rests
        # on: measurement and ingest are one code path, so they cannot part.
        self.assertEqual((packet["concepts"], packet["concepts_grounded"]), (4, 2))
        self.assertEqual(packet["concepts_grounded"], written["concepts"])
        self.assertEqual(packet["mentions_grounded"], written["concept_mentions"])
        self.assertEqual((packet["relations"], packet["relations_grounded"]), (3, 1))
        self.assertEqual(packet["relations_grounded"], written["concept_relations"])

        item = self.service.get_job_summary("packets")["items"][0]
        self.assertEqual(item["status"], "SUCCEEDED")
        self.assertEqual(item["skipped_ungrounded_evidence"], 3)
        self.assertEqual(packet["status"], "SUCCEEDED")
        self.assertIsNone(packet["stored_reason"])

    def test_a_packet_that_still_fails_whole_is_reported_as_such(self) -> None:
        # The measurement must not promise a recovery ingest will not perform.
        # This packet carries the same three claim-level spans plus one span
        # naming a passage the version does not have, which is not a claim about
        # anything and still fails the item. The probe classifies all four; only
        # three are ones ingest would drop, and the report says which is which
        # rather than leaving a reader to assume they are the same number.
        result = self._run_job({"packet-1": self._hard_packet()})
        self.assertEqual((result["ingested"], result["failed"]), (0, 1))
        item = self.repository.list_items("packets")[0]
        self.assertEqual(item["status"], "FAILED")
        self.assertIsNone(item["response_json"])
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))

        packet = self._packet(
            self.service.dry_run_failed_packets("packets", self.provider), "packet-1"
        )

        self.assertEqual(packet["spans_failed"], 4)
        self.assertEqual(packet["spans_skipped_by_ingest"], 3)
        self.assertEqual(
            packet["spans_failed_by_reason"],
            {"EVIDENCE_ABSENT": 2, "EVIDENCE_AMBIGUOUS": 1, "PASSAGE_UNAVAILABLE": 1},
        )
        # The stored failure class comes from the durable item, not from the
        # measurement, and it is the rejection that actually stopped the write -
        # the claim-level ones ahead of it in the walk no longer stop anything.
        self.assertEqual(packet["stored_reason"], "PASSAGE_UNAVAILABLE")
        self.assertEqual(packet["status"], "FAILED")

    def test_the_cascade_is_reported_by_cause_and_not_as_one_number(self) -> None:
        # A relation can be lost two ways and the difference is the argument.
        # ``tcp -> absent`` is lost because the concept it points at lost its
        # only mention: the model named the concept correctly and grounding is
        # what removed it. ``udp -> tcp`` is lost because its own only citation
        # was absent: both endpoints survive and the edge still cannot be
        # asserted. Folding them together would hide which fix each needs.
        self._run_job({"packet-1": self._mixed_packet()})
        packet = self.repository.dry_run_section_graph_packet(
            "packets", "packet-1", self._mixed_packet()
        )

        self.assertEqual(packet["relations_lost_to_dropped_endpoint"], 1)
        self.assertEqual(packet["relations_lost_without_evidence"], 1)
        # The three relations are exactly accounted for: one kept, two lost, by
        # named cause, with nothing unexplained.
        self.assertEqual(
            packet["relations_grounded"]
            + packet["relations_lost_to_dropped_endpoint"]
            + packet["relations_lost_without_evidence"],
            packet["relations"],
        )
        # And the one relation left is the one the write kept.
        self.assertEqual(self._graph_row_counts()["concept_relations"], 1)

    def test_a_clean_packet_measures_exactly_what_ingest_wrote(self) -> None:
        # The guard against the failure mode that would make this whole exercise
        # worthless: a classifier that quietly disagrees with production. On a
        # packet ingest accepted, the measurement's grounded counts must equal
        # the rows the write actually produced - not approximately, exactly.
        result = self._run_job({"packet-1": self._clean_packet()})
        self.assertEqual((result["ingested"], result["failed"]), (1, 0))
        written = self._graph_row_counts()

        measurement = self.repository.dry_run_section_graph_packet(
            "packets", "packet-1", self._clean_packet()
        )

        self.assertTrue(measurement["grounded"])
        self.assertEqual(measurement["spans_failed"], 0)
        self.assertEqual(measurement["spans_failed_by_reason"], {})
        self.assertEqual(measurement["concepts_grounded"], written["concepts"])
        self.assertEqual(measurement["mentions_grounded"], written["concept_mentions"])
        self.assertEqual(measurement["relations_grounded"], written["concept_relations"])
        # Measuring a succeeded item is allowed and reports it as succeeded; the
        # dry run is about packets, not about a status it is entitled to read.
        self.assertEqual(measurement["status"], "SUCCEEDED")

    # -- the safety properties -------------------------------------------

    def test_the_dry_run_leaves_the_ledger_and_the_graph_untouched(self) -> None:
        # The property that makes this safe to run at any time, including
        # against a live store before re-ingesting anything. A mixed job so
        # there is something of every kind to disturb: one succeeded item with a
        # stored response and a written graph, one failed item with a stored
        # failure class and diagnostics.
        self._run_job({"packet-1": self._hard_packet(), "packet-2": self._clean_packet()})
        ledger_before = self._ledger()
        checksum_before = self._ledger_checksum()
        graph_before = self._graph_row_counts()
        jobs_before = [
            tuple(row) for row in self.store._connection().execute("SELECT * FROM batch_jobs")
        ]
        self.assertNotEqual(graph_before, dict.fromkeys(self._GRAPH_TABLES, 0))

        report = self.service.dry_run_failed_packets("packets", self.provider)

        # It really did measure something; an inert no-op would pass the
        # assertions below for the wrong reason.
        self.assertEqual(report["failed_item_count"], 1)
        self.assertEqual(self._packet(report, "packet-1")["spans_failed"], 4)

        self.assertEqual(self._ledger(), ledger_before)
        self.assertEqual(self._ledger_checksum(), checksum_before)
        self.assertEqual(self._graph_row_counts(), graph_before)
        # The job row too: a dry run polls the provider but must never let a
        # provider snapshot move a durable job, so ``set_provider_state`` is not
        # on this path at all.
        self.assertEqual(
            [tuple(row) for row in self.store._connection().execute("SELECT * FROM batch_jobs")],
            jobs_before,
        )

    def test_measuring_never_submits_and_therefore_never_spends(self) -> None:
        # One method call is the whole difference between a free re-fetch of
        # durable output and paying for the batch again, so it is asserted
        # rather than reasoned about.
        self._run_job({"packet-1": self._hard_packet()})
        submits_before = self.provider.submit_calls

        self.service.dry_run_failed_packets("packets", self.provider)
        self.service.dry_run_failed_packets("packets", self.provider)

        self.assertEqual(self.provider.submit_calls, submits_before)

    def test_repeating_the_measurement_returns_the_identical_numbers(self) -> None:
        # Grounding is deterministic over an immutable source, and the dry run
        # changes nothing that could feed back into it, so a second run must be
        # indistinguishable from the first. If it were not, one of those two
        # properties would be false.
        self._run_job({"packet-1": self._hard_packet()})

        first = self.service.dry_run_failed_packets("packets", self.provider)
        second = self.service.dry_run_failed_packets("packets", self.provider)

        self.assertEqual(first, second)

    def test_a_measurement_carries_counts_and_slugs_and_never_packet_text(self) -> None:
        # Same discipline as the persisted diagnostics, for the same reason:
        # this record is printed, pasted into a decision note, and kept. It has
        # to be impossible for a passage, an evidence string, a concept name or
        # a local_id to be sitting in it.
        self._run_job({"packet-1": self._hard_packet()})
        report = self.service.dry_run_failed_packets("packets", self.provider)
        packet = self._packet(report, "packet-1")

        serialized = json.dumps(report, ensure_ascii=False)
        for secret in (
            self._P1,
            self._P2,
            self._ABSENT_LONGER,
            self._UNIQUE_P1,
            self._UNIQUE_P2,
            "Segment",
            "ambiguous",
        ):
            self.assertNotIn(secret, serialized)
        # Structurally, not only by inspection: every value is a count, a flag,
        # a whitelisted reason slug, or the durable custom_id.
        for name, value in packet.items():
            if name in {"custom_id", "status"}:
                continue
            if isinstance(value, dict):
                self.assertTrue(
                    all(key in BATCH._GROUNDING_FAILURE_REASONS for key in value),
                    f"{name} carries an unknown reason slug",
                )
                self.assertTrue(all(isinstance(count, int) for count in value.values()))
                continue
            if isinstance(value, str):
                self.assertIn(value, BATCH._GROUNDING_FAILURE_REASONS)
                continue
            self.assertTrue(
                value is None or isinstance(value, (bool, int)),
                f"{name} is neither a count, a flag, nor a slug",
            )

    # -- what cannot be measured is named, never estimated -----------------

    def test_an_item_the_provider_itself_rejected_is_named_rather_than_counted(self) -> None:
        # No packet came back for this item, so there is nothing to ground. The
        # honest report is the cause; an estimate here would be exactly the
        # extrapolation this tool exists to replace.
        self.service.create_draft(
            version_id="version",
            provider="fake-batch",
            profile_name="cloud-model-snapshot",
            prompt_profile="zh-section-graph-v3",
            job_kind="SECTION_GRAPH",
            items=[
                BatchItemInput("p1", "packet-1", {"body": {"packet": True}}),
                BatchItemInput("p2", "packet-2", {"body": {"packet": True}}),
            ],
            batch_job_id="packets",
        )
        remote_id = self.service.submit("packets", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("succeeded")
        # One item the provider rejected outright, one absent from a complete
        # output stream: two different causes, both reportable, neither guessed.
        self.provider.results[remote_id] = [
            ProviderItemResult("packet-1", error="model refused")
        ]
        self.service.poll_and_ingest("packets", self.provider)

        report = self.service.dry_run_failed_packets("packets", self.provider)

        self.assertEqual(report["failed_item_count"], 2)
        rejected = self._packet(report, "packet-1")
        self.assertFalse(rejected["grounded"])
        self.assertEqual(rejected["unmeasurable_reason"], "PROVIDER_ITEM_ERROR")
        self.assertNotIn("relations_grounded", rejected)
        missing = self._packet(report, "packet-2")
        self.assertFalse(missing["grounded"])
        self.assertEqual(missing["unmeasurable_reason"], "TERMINAL_WITHOUT_RESULT")

    def test_a_structurally_invalid_packet_is_reported_rather_than_measured(self) -> None:
        # A probe makes an *ungrounded span* recoverable, not a malformed
        # packet. An endpoint naming a local_id the response never defined is
        # not a groundability question at all, so the walk still rejects it and
        # the packet is reported by its slug with no counts attached.
        packet = self._clean_packet()
        packet["relations"] = [
            self._relation("tcp", "nowhere", [self._span("p1", self._UNIQUE_P1)])
        ]
        self._run_job({"packet-1": packet})

        report = self.service.dry_run_failed_packets("packets", self.provider)
        measured = self._packet(report, "packet-1")

        self.assertFalse(measured["grounded"])
        self.assertEqual(measured["unmeasurable_reason"], "RELATION_ENDPOINT_UNRESOLVED")
        self.assertEqual(measured["stored_reason"], "RELATION_ENDPOINT_UNRESOLVED")

    def test_a_job_whose_output_cannot_be_downloaded_measures_nothing_and_records_nothing(
        self,
    ) -> None:
        # A half-finished download must read as "not measured", never as "this
        # packet held nothing". Unlike the ingest path, which marks the job for
        # a safe re-poll, this records nothing at all: an unreadable download is
        # a fact about the measurement, not about the durable job.
        self._run_job({"packet-1": self._hard_packet()})
        ledger_before = self._ledger()
        jobs_before = [
            tuple(row) for row in self.store._connection().execute("SELECT * FROM batch_jobs")
        ]
        self.provider.fetch_error = RuntimeError("connection reset")

        report = self.service.dry_run_failed_packets("packets", self.provider)

        self.assertTrue(report["results_pending_retrieval"])
        self.assertEqual(report["packets"], [])
        self.assertEqual(self._ledger(), ledger_before)
        self.assertEqual(
            [tuple(row) for row in self.store._connection().execute("SELECT * FROM batch_jobs")],
            jobs_before,
        )

    def test_only_a_section_graph_job_has_packets_to_measure(self) -> None:
        # The measurement is defined in terms of concepts, mentions, relations
        # and the cascade between them; a CONCEPT_MENTIONS item has no relations
        # and no packet, so asking is a caller error rather than an empty answer.
        self.service.create_draft(
            version_id="version",
            provider="fake-batch",
            profile_name="cloud-model-snapshot",
            items=[BatchItemInput("p1", "mention-1", {"body": {"passage": "p1"}})],
            batch_job_id="mentions",
        )
        self.service.submit("mentions", self.provider)

        with self.assertRaises(BatchServiceError):
            self.service.dry_run_failed_packets("mentions", self.provider)

    def test_the_probe_still_never_widens_what_ingest_will_write(self) -> None:
        # The behavioural risk of measuring through the production pass, in the
        # form it takes now that ingest is itself lenient. A probe is still
        # lenient about *more* than ingest is, so a probe that leaked into the
        # write would turn a hard rejection into a partial graph. Measuring a
        # packet first must change nothing about what a re-poll then writes:
        # this one fails on its unavailable passage before and after, with no
        # graph rows and no stored response.
        self._run_job({"packet-1": self._hard_packet()})
        self.service.dry_run_failed_packets("packets", self.provider)

        result = self.service.poll_and_ingest("packets", self.provider)

        self.assertEqual(result["ingested"], 0)
        item = self.repository.list_items("packets")[0]
        self.assertEqual(item["status"], "FAILED")
        self.assertIsNone(item["response_json"])
        self.assertEqual(
            item["error_text"], "section graph evidence does not belong to this EPUB version"
        )
        self.assertEqual(self._graph_row_counts(), dict.fromkeys(self._GRAPH_TABLES, 0))


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
            {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11},
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
            {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11},
        )
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], STORE.SCHEMA_VERSION)
        self.assertEqual(STORE.SCHEMA_VERSION, 11)
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


class EpubBatchAmbiguousConceptMigrationTest(unittest.TestCase):
    """``batch_items.skipped_ambiguous_concepts`` arrives through the runner.

    The store this ships against already holds the completed full-book runs,
    whose items were written before the column existed.  Those rows must read
    back as NULL and not as a measured zero: a zero would claim the write
    checked for a collision and found none, which is exactly the opposite of
    the truth - the writes that produced those rows *failed* their items on
    collisions, which is the behaviour this column exists to record replacing.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = os.path.join(self.tempdir.name, "schema-9.db")
        self._create_previous_schema_version()

    def _create_previous_schema_version(self) -> None:
        """Build a database exactly as the schema stood at version 9."""
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        # Mirror the runner, which owns the bookkeeping table itself and
        # therefore skips the first statement of migration 1.
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
            (6, STORE._MIGRATION_6),
            (7, STORE._MIGRATION_7),
            (8, STORE._MIGRATION_8),
            (9, STORE._MIGRATION_9),
        )
        for version, statements in migrations:
            for statement in statements[1:] if version == 1 else statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        connection.execute("PRAGMA user_version = 9")
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
               VALUES ('legacy-job', 'version', 'openai-batch', 'zh-section-graph-v3', 'SUCCEEDED')"""
        )
        # One succeeded item that did measure the two older counters, so the
        # test can tell "this column is new" apart from "this row measured
        # nothing at all".
        connection.execute(
            """INSERT INTO batch_items(
                   batch_item_id, batch_job_id, passage_id, custom_id, status, request_json,
                   response_json, skipped_self_relations, skipped_short_evidence
               ) VALUES ('legacy-item', 'legacy-job', 'p1', 'p1', 'SUCCEEDED', '{}',
                         '{"concepts": [], "relations": []}', 1, 2)"""
        )
        connection.commit()
        connection.close()

    def test_previous_schema_version_gains_the_column_without_losing_data(self) -> None:
        self.assertNotIn(
            "skipped_ambiguous_concepts",
            {row[1] for row in sqlite3.connect(self.path).execute("PRAGMA table_info(batch_items)")},
        )

        store = SQLiteEpubStore(self.path)
        self.addCleanup(store.close)
        connection = store._connection()

        self.assertEqual(
            {row[0] for row in connection.execute("SELECT version FROM schema_migrations")},
            {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11},
        )
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], STORE.SCHEMA_VERSION)
        self.assertEqual(STORE.SCHEMA_VERSION, 11)

        item = connection.execute("SELECT * FROM batch_items").fetchone()
        # Not measured, not zero.  The counters the row *did* record are
        # untouched, which is what makes the NULL meaningful.
        self.assertIsNone(item["skipped_ambiguous_concepts"])
        self.assertEqual(item["skipped_self_relations"], 1)
        self.assertEqual(item["skipped_short_evidence"], 2)
        self.assertEqual(item["status"], "SUCCEEDED")
        self.assertEqual(item["response_json"], '{"concepts": [], "relations": []}')

        # A NULL reads through the summary as NULL per item, while the job
        # aggregate coalesces it to 0 - the aggregate is a sum, and an
        # unmeasured row contributes nothing to one.
        summary = SQLiteBatchRepository(store).get_job_summary("legacy-job")
        self.assertIsNone(summary["items"][0]["skipped_ambiguous_concepts"])
        self.assertEqual(summary["item_skipped_ambiguous_concepts"], 0)

        # Content-free by schema rather than by a validator, like the two
        # columns beside it.
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE batch_items SET skipped_ambiguous_concepts = '潮汐源'")
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE batch_items SET skipped_ambiguous_concepts = -1")
        connection.rollback()

    def test_migration_is_idempotent_across_reopens(self) -> None:
        first = SQLiteEpubStore(self.path)
        first.close()
        second = SQLiteEpubStore(self.path)
        self.addCleanup(second.close)
        columns = [
            row[1] for row in second._connection().execute("PRAGMA table_info(batch_items)")
        ]
        self.assertEqual(columns.count("skipped_ambiguous_concepts"), 1)


class EpubBatchUngroundedEvidenceMigrationTest(unittest.TestCase):
    """``batch_items.skipped_ungrounded_evidence`` arrives through the runner.

    The store this ships against holds the completed full-book runs, whose
    section-graph items were written while an ungrounded span still failed its
    whole item.  Those rows must read back NULL rather than 0, and the reason is
    sharper here than for any earlier counter: a 0 would assert the write looked
    for an unverifiable citation and found none, when in fact the items that
    carried one are not in this column's population at all - they are the ten
    FAILED rows this rule exists to recover.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = os.path.join(self.tempdir.name, "schema-10.db")
        self._create_previous_schema_version()

    def _create_previous_schema_version(self) -> None:
        """Build a database exactly as the schema stood at version 10."""
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
            (6, STORE._MIGRATION_6),
            (7, STORE._MIGRATION_7),
            (8, STORE._MIGRATION_8),
            (9, STORE._MIGRATION_9),
            (10, STORE._MIGRATION_10),
        )
        for version, statements in migrations:
            for statement in statements[1:] if version == 1 else statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        connection.execute("PRAGMA user_version = 10")
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
               VALUES ('legacy-job', 'version', 'openai-batch', 'zh-section-graph-v3', 'SUCCEEDED')"""
        )
        # One succeeded item that measured all three older counters, so a NULL
        # in the new one reads as "this column is new" and not as "this row
        # measured nothing".
        connection.execute(
            """INSERT INTO batch_items(
                   batch_item_id, batch_job_id, passage_id, custom_id, status, request_json,
                   response_json, skipped_self_relations, skipped_short_evidence,
                   skipped_ambiguous_concepts
               ) VALUES ('legacy-item', 'legacy-job', 'p1', 'p1', 'SUCCEEDED', '{}',
                         '{"concepts": [], "relations": []}', 1, 2, 3)"""
        )
        connection.commit()
        connection.close()

    def test_previous_schema_version_gains_the_column_without_losing_data(self) -> None:
        self.assertNotIn(
            "skipped_ungrounded_evidence",
            {row[1] for row in sqlite3.connect(self.path).execute("PRAGMA table_info(batch_items)")},
        )

        store = SQLiteEpubStore(self.path)
        self.addCleanup(store.close)
        connection = store._connection()

        self.assertEqual(
            {row[0] for row in connection.execute("SELECT version FROM schema_migrations")},
            {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11},
        )
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], STORE.SCHEMA_VERSION)
        self.assertEqual(STORE.SCHEMA_VERSION, 11)

        item = connection.execute("SELECT * FROM batch_items").fetchone()
        self.assertIsNone(item["skipped_ungrounded_evidence"])
        # The three counters the row did record are untouched, which is what
        # makes the NULL beside them meaningful.
        self.assertEqual(
            (
                item["skipped_self_relations"],
                item["skipped_short_evidence"],
                item["skipped_ambiguous_concepts"],
            ),
            (1, 2, 3),
        )
        self.assertEqual(item["status"], "SUCCEEDED")
        self.assertEqual(item["response_json"], '{"concepts": [], "relations": []}')

        summary = SQLiteBatchRepository(store).get_job_summary("legacy-job")
        self.assertIsNone(summary["items"][0]["skipped_ungrounded_evidence"])
        self.assertEqual(summary["item_skipped_ungrounded_evidence"], 0)

        # Content-free by schema rather than by a validator, like the three
        # columns beside it.  An evidence string is exactly what a column
        # counting dropped evidence must be unable to hold.
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE batch_items SET skipped_ungrounded_evidence = '全域潮汐枢纽'")
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE batch_items SET skipped_ungrounded_evidence = -1")
        connection.rollback()

    def test_migration_is_idempotent_across_reopens(self) -> None:
        first = SQLiteEpubStore(self.path)
        first.close()
        second = SQLiteEpubStore(self.path)
        self.addCleanup(second.close)
        columns = [
            row[1] for row in second._connection().execute("PRAGMA table_info(batch_items)")
        ]
        self.assertEqual(columns.count("skipped_ungrounded_evidence"), 1)


if __name__ == "__main__":
    unittest.main()
