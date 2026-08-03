"""Acceptance tests for durable, provider-agnostic EPUB Batch orchestration."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


STORE = _load_module("epub_store_batch_test", "backend/open_webui/retrieval/epub/store.py")
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

    def submit(self, *, jsonl: str, idempotency_key: str) -> str:
        self.submit_calls += 1
        self.jsonl[idempotency_key] = jsonl
        provider_job_id = self.submissions.setdefault(idempotency_key, f"remote-{idempotency_key}")
        self.snapshots.setdefault(provider_job_id, ProviderSnapshot("queued"))
        return provider_job_id

    def poll(self, provider_job_id: str) -> ProviderSnapshot:
        return self.snapshots[provider_job_id]

    def fetch_results(self, provider_job_id: str):
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
                    "content": "TCP connects endpoints.",
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
            BatchItemInput("p1", "p1", {"model": "batch-model", "body": {"text": "TCP connects endpoints."}}),
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
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.repository.list_items("job")[0]["status"], "FAILED")
        self.assertEqual(
            self.store._connection().execute("SELECT COUNT(*) FROM concepts").fetchone()[0], 0
        )

    def test_failed_items_create_one_durable_retry_successor(self) -> None:
        self._draft()
        remote_id = self.service.submit("job", self.provider)
        self.provider.snapshots[remote_id] = ProviderSnapshot("failed", "provider timeout")
        self.provider.results[remote_id] = [ProviderItemResult("p2", error="temporary failure")]
        self.service.poll_and_ingest("job", self.provider)

        retry_job = self.service.retry_failed_items("job")
        self.assertEqual(retry_job, self.service.retry_failed_items("job"))
        retry_items = self.repository.list_items(retry_job)
        self.assertEqual(len(retry_items), 1)
        self.assertEqual(retry_items[0]["passage_id"], "p2")
        self.assertEqual(retry_items[0]["custom_id"], "retry:job:p2")
        self.assertEqual(retry_items[0]["attempt_count"], 1)

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


if __name__ == "__main__":
    unittest.main()
