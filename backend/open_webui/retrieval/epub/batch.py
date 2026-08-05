"""Durable, provider-neutral offline Batch orchestration for EPUB concepts.

The provider adapter deliberately has no credentials field.  Credentials belong
to the server-side provider implementation, while this service persists only
the immutable request body, a provider name, and opaque provider job IDs.  A
provider *must* honour the supplied idempotency key: it is what makes the small
crash window between remote submission and local acknowledgement recoverable.

This module owns the Batch-specific repository adapter.  Domain code talks to
``BatchRepository``; only ``SQLiteBatchRepository`` knows that the current
canonical store happens to be SQLite.  PostgreSQL can implement the same
protocol without changing ``BatchJobService``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping, Protocol, Sequence
from uuid import uuid4


class BatchServiceError(ValueError):
    """A durable Batch lifecycle or provider-contract invariant was violated."""


class BatchPayloadError(BatchServiceError):
    """A provider result cannot safely be turned into concept graph records.

    ``diagnostics`` optionally carries the content-free measurement of the
    rejection (see :func:`_grounding_diagnostics`).  It is an attribute on the
    exception rather than a mutable out-parameter because the raise site is the
    only place that still holds the numbers, while the durable failure is
    recorded several frames higher; passing it along the existing failure path
    keeps every intermediate frame unaware of it.  ``str(exc)`` remains exactly
    the human failure class string, because that string is what is persisted as
    ``error_text`` and is compared for idempotency.
    """

    def __init__(self, message: str, *, diagnostics: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics: dict[str, Any] | None = (
            dict(diagnostics) if diagnostics is not None else None
        )


_JOB_KINDS = {"CONCEPT_MENTIONS", "SECTION_GRAPH"}
_RELATION_PREDICATES = {
    "HAS_PART",
    "PRECEDES",
    "PREREQUISITE",
    "CAUSES",
    "CONTRASTS",
    "ELABORATES",
}


@dataclass(frozen=True)
class BatchItemInput:
    """One persisted request, already tied to an immutable source passage."""

    passage_id: str
    custom_id: str
    request: Mapping[str, Any]


@dataclass(frozen=True)
class ProviderSnapshot:
    """Normalized provider lifecycle state; no provider SDK type leaks upward."""

    state: str
    error: str | None = None


@dataclass(frozen=True)
class ProviderItemResult:
    """One provider result.  Exactly one of ``payload`` and ``error`` is required."""

    custom_id: str
    payload: Mapping[str, Any] | None = None
    error: str | None = None


class BatchProvider(Protocol):
    """Provider boundary used by the service.

    ``submit`` must return the same remote job for a repeated idempotency key.
    This requirement is intentional: a service process can die after a remote
    request succeeds but before its local transaction records ``provider_job_id``.
    """

    name: str

    def submit(self, *, jsonl: str, idempotency_key: str) -> str: ...

    def poll(self, provider_job_id: str) -> ProviderSnapshot: ...

    def fetch_results(self, provider_job_id: str) -> Iterable[ProviderItemResult]: ...


class OpenAIBatchProvider:
    """Synchronous adapter for OpenAI's offline Batch API.

    The constructor is intentionally the only place this adapter accepts an
    API key.  It is used to initialise the SDK client and is never copied into
    a durable job, request JSONL, metadata, or an exception.  ``client`` is an
    injection seam for application wiring and unit tests; production callers
    normally pass ``api_key`` from server-side administrator configuration.

    The durable request JSONL must already use OpenAI's Batch format: each line
    has a ``custom_id``, ``method``, ``url``, and request ``body``.  Keeping that
    envelope in the durable request makes each cloud request auditable and lets
    the repository's existing secret scanner reject credentials before upload.
    """

    name = "openai-batch"
    _QUEUED_STATUSES = {"validating"}
    _RUNNING_STATUSES = {"in_progress", "finalizing", "cancelling"}
    _SUCCEEDED_STATUSES = {"completed"}
    _FAILED_STATUSES = {"failed", "expired"}
    _CANCELLED_STATUSES = {"cancelled"}

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        endpoint: str = "/v1/chat/completions",
        completion_window: str = "24h",
    ):
        if not endpoint.startswith("/v1/"):
            raise BatchServiceError("OpenAI Batch endpoint must be an /v1/ endpoint")
        if not completion_window.strip():
            raise BatchServiceError("OpenAI Batch completion_window cannot be empty")
        if client is None:
            if not api_key or not api_key.strip():
                raise BatchServiceError("OpenAI Batch provider requires a server-side API key")
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - project dependency guards production
                raise BatchServiceError("OpenAI SDK is not installed") from exc
            client = OpenAI(api_key=api_key)
        self._client = client
        self._endpoint = endpoint
        self._completion_window = completion_window

    @staticmethod
    def _value(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            return value.get(key, default)
        return getattr(value, key, default)

    @classmethod
    def _error_text(cls, value: Any, default: str) -> str:
        if value is None:
            return default
        if isinstance(value, str):
            return value or default
        if isinstance(value, (list, tuple)):
            messages = [cls._error_text(item, "") for item in value]
            return "; ".join(message for message in messages if message) or default
        nested_error = cls._value(value, "error")
        if nested_error is not None:
            return cls._error_text(nested_error, default)
        nested_data = cls._value(value, "data")
        if nested_data is not None:
            return cls._error_text(nested_data, default)
        message = cls._value(value, "message")
        if isinstance(message, str) and message:
            code = cls._value(value, "code")
            return f"{code}: {message}" if isinstance(code, str) and code else message
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return default

    def _validate_jsonl(self, jsonl: str) -> None:
        if not jsonl.strip():
            raise BatchServiceError("OpenAI Batch JSONL cannot be empty")
        for line_number, line in enumerate(jsonl.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BatchServiceError(
                    f"OpenAI Batch JSONL line {line_number} is not valid JSON"
                ) from exc
            if not isinstance(request, Mapping):
                raise BatchServiceError(f"OpenAI Batch JSONL line {line_number} must be an object")
            _assert_no_secrets(request)
            custom_id = request.get("custom_id")
            if not isinstance(custom_id, str) or not custom_id.strip():
                raise BatchServiceError(
                    f"OpenAI Batch JSONL line {line_number} needs a non-empty custom_id"
                )
            if request.get("method") != "POST" or request.get("url") != self._endpoint:
                raise BatchServiceError(
                    f"OpenAI Batch JSONL line {line_number} must POST to {self._endpoint}"
                )
            if not isinstance(request.get("body"), Mapping):
                raise BatchServiceError(f"OpenAI Batch JSONL line {line_number} needs an object body")

    @classmethod
    def _jsonl_text(cls, response: Any) -> str:
        text = cls._value(response, "text")
        if isinstance(text, str):
            return text
        content = cls._value(response, "content")
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            return content.decode("utf-8")
        read = getattr(response, "read", None)
        if callable(read):
            content = read()
            if isinstance(content, bytes):
                return content.decode("utf-8")
            if isinstance(content, str):
                return content
        if isinstance(response, bytes):
            return response.decode("utf-8")
        if isinstance(response, str):
            return response
        raise BatchServiceError("OpenAI Batch file response did not contain UTF-8 JSONL")

    @staticmethod
    def _parse_jsonl(text: str, file_id: str) -> Iterable[Mapping[str, Any]]:
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BatchServiceError(
                    f"OpenAI Batch result {file_id} line {line_number} is not valid JSON"
                ) from exc
            if not isinstance(record, Mapping):
                raise BatchServiceError(
                    f"OpenAI Batch result {file_id} line {line_number} must be an object"
                )
            yield record

    @classmethod
    def _success_payload(cls, response: Any) -> Mapping[str, Any] | None:
        body = cls._value(response, "body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                return None
        if not isinstance(body, Mapping):
            return None
        # This also supports a provider-side response adapter that directly
        # writes the expected concept payload into the OpenAI response body.
        if isinstance(body.get("concepts"), list):
            return body
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = cls._value(choices[0], "message")
        content = cls._value(message, "content")
        if not isinstance(content, str):
            return None
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, Mapping) else None

    @classmethod
    def _item_result(cls, record: Mapping[str, Any]) -> ProviderItemResult:
        custom_id = record.get("custom_id")
        if not isinstance(custom_id, str) or not custom_id.strip():
            raise BatchServiceError("OpenAI Batch result is missing custom_id")
        error = record.get("error")
        if error is not None:
            return ProviderItemResult(custom_id=custom_id, error=cls._error_text(error, "OpenAI Batch item failed"))
        response = record.get("response")
        if response is None:
            return ProviderItemResult(
                custom_id=custom_id,
                error="OpenAI Batch item did not include a response or error",
            )
        status_code = cls._value(response, "status_code")
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            return ProviderItemResult(
                custom_id=custom_id,
                error=cls._error_text(
                    cls._value(response, "body"),
                    f"OpenAI Batch item returned HTTP {status_code!r}",
                ),
            )
        payload = cls._success_payload(response)
        if payload is None:
            return ProviderItemResult(
                custom_id=custom_id,
                error="OpenAI Batch item did not return a JSON concept payload",
            )
        return ProviderItemResult(custom_id=custom_id, payload=payload)

    def submit(self, *, jsonl: str, idempotency_key: str) -> str:
        if not idempotency_key.strip():
            raise BatchServiceError("OpenAI Batch idempotency key cannot be empty")
        self._validate_jsonl(jsonl)
        encoded = jsonl.encode("utf-8")
        file_headers = {"Idempotency-Key": f"epub-input:{idempotency_key}"}
        uploaded = self._client.files.create(
            file=(f"epub-concepts-{idempotency_key}.jsonl", encoded, "application/jsonl"),
            purpose="batch",
            extra_headers=file_headers,
        )
        input_file_id = self._value(uploaded, "id")
        if not isinstance(input_file_id, str) or not input_file_id:
            raise BatchServiceError("OpenAI Batch upload returned no input file ID")
        created = self._client.batches.create(
            input_file_id=input_file_id,
            endpoint=self._endpoint,
            completion_window=self._completion_window,
            metadata={"epub_batch_job_id": idempotency_key},
            extra_headers={"Idempotency-Key": f"epub-batch:{idempotency_key}"},
        )
        provider_job_id = self._value(created, "id")
        if not isinstance(provider_job_id, str) or not provider_job_id:
            raise BatchServiceError("OpenAI Batch create returned no Batch job ID")
        return provider_job_id

    def poll(self, provider_job_id: str) -> ProviderSnapshot:
        batch = self._client.batches.retrieve(provider_job_id)
        status = self._value(batch, "status")
        if not isinstance(status, str):
            raise BatchServiceError("OpenAI Batch retrieve returned no status")
        if status in self._QUEUED_STATUSES:
            state = "queued"
        elif status in self._RUNNING_STATUSES:
            state = "running"
        elif status in self._SUCCEEDED_STATUSES:
            state = "succeeded"
        elif status in self._FAILED_STATUSES:
            state = "failed"
        elif status in self._CANCELLED_STATUSES:
            state = "cancelled"
        else:
            raise BatchServiceError(f"OpenAI Batch returned unsupported status: {status}")
        return ProviderSnapshot(state, self._error_text(self._value(batch, "errors"), "") or None)

    def fetch_results(self, provider_job_id: str) -> Iterable[ProviderItemResult]:
        batch = self._client.batches.retrieve(provider_job_id)
        file_ids = [
            value
            for value in (
                self._value(batch, "output_file_id"),
                self._value(batch, "error_file_id"),
            )
            if isinstance(value, str) and value
        ]
        results: list[ProviderItemResult] = []
        seen_custom_ids: set[str] = set()
        for file_id in file_ids:
            response = self._client.files.content(file_id)
            for record in self._parse_jsonl(self._jsonl_text(response), file_id):
                item = self._item_result(record)
                if item.custom_id in seen_custom_ids:
                    raise BatchServiceError(
                        f"OpenAI Batch returned duplicate output for custom_id: {item.custom_id}"
                    )
                seen_custom_ids.add(item.custom_id)
                results.append(item)
        return results


class BatchRepository(Protocol):
    """Persistence operations required by :class:`BatchJobService`."""

    def create_draft(
        self,
        *,
        version_id: str,
        provider: str,
        profile_name: str,
        job_kind: str,
        is_sample: bool,
        items: Sequence[BatchItemInput],
        batch_job_id: str | None = None,
    ) -> str: ...

    def get_job(self, batch_job_id: str) -> dict[str, Any]: ...

    def get_job_summary(self, batch_job_id: str) -> dict[str, Any]: ...

    def list_job_summaries(
        self, *, version_id: str | None, offset: int, limit: int
    ) -> tuple[int, list[dict[str, Any]]]: ...

    def list_items(self, batch_job_id: str) -> list[dict[str, Any]]: ...

    def jsonl_for_job(self, batch_job_id: str) -> str: ...

    def mark_submitted(self, batch_job_id: str, provider_job_id: str) -> None: ...

    def set_provider_state(self, batch_job_id: str, state: str, error: str | None) -> None: ...

    def ingest_success(self, batch_job_id: str, custom_id: str, payload: Mapping[str, Any]) -> bool: ...

    def record_item_failure(
        self,
        batch_job_id: str,
        custom_id: str,
        error: str,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> bool: ...

    def mark_results_pending_retrieval(self, batch_job_id: str) -> None: ...

    def reconcile_terminal_missing_results(self, batch_job_id: str) -> int: ...

    def create_retry_child(self, batch_job_id: str) -> str: ...

    def list_recoverable_jobs(self, provider: str | None = None) -> list[str]: ...

    def review_sample_job(
        self, batch_job_id: str, *, status: str, reviewed_by: str
    ) -> dict[str, Any]: ...

    def list_sample_reviews(
        self, *, version_id: str | None = None, job_kind: str | None = None
    ) -> list[dict[str, Any]]: ...


_ACTIVE_JOB_STATES = {"DRAFT", "SUBMITTED", "RUNNING"}
_TERMINAL_JOB_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}
_PROVIDER_TO_JOB_STATE = {
    "queued": "SUBMITTED",
    "running": "RUNNING",
    "succeeded": "SUCCEEDED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
}
_SENSITIVE_REQUEST_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "x-api-key",
}
_SAMPLE_REVIEW_STATUSES = {"APPROVED", "REJECTED"}
_RESULTS_PENDING_RETRIEVAL = "RESULTS_PENDING_RETRIEVAL"
_TERMINAL_MISSING_RESULT = "TERMINAL_WITHOUT_ITEM_RESULT"
_LEGACY_CONCEPT_MENTION_FIELDS = {"start_codepoint", "end_codepoint", "evidence"}
_GROUNDED_CONCEPT_MENTION_FIELDS = {
    "start_codepoint",
    "end_codepoint",
    "evidence",
    "context_before",
    "context_after",
}
_MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS = 48

# Stable, machine-readable failure classes for prompt tuning.  The human
# ``error_text`` strings stay exactly as they are: they are durable and
# compared for idempotency.  These slugs are the tunable dimension, and are
# deliberately coarser than the messages so an aggregate stays legible.
_GROUNDING_FAILURE_REASONS = frozenset(
    {
        # The response, a concept, or a mention did not have the constrained shape.
        "INVALID_SCHEMA",
        # A concept carried no mention at all.
        "MENTIONS_MISSING",
        # Offsets were not integers, or evidence was not a non-empty string.
        "INVALID_OFFSETS",
        # A v4 context anchor was not a string, or exceeded the anchor budget.
        "ANCHOR_INVALID",
        # The literal evidence does not occur in the immutable source at all:
        # the model paraphrased, normalized, or translated instead of copying.
        "EVIDENCE_ABSENT",
        # Repeated evidence arrived with both anchors empty, so nothing can
        # select an occurrence.
        "ANCHOR_MISSING",
        # The model's own offsets sliced the evidence exactly, but the adjacent
        # source text is not what the model claimed surrounds it.
        "ANCHOR_MISMATCH",
        # After anchor filtering the evidence still does not identify exactly
        # one occurrence.  ``occurrence_count`` and ``anchored_candidate_count``
        # separate "anchors selected nothing" from "anchors were not selective";
        # both need the same class of prompt fix, so they share one slug.
        "EVIDENCE_AMBIGUOUS",
        # The item's immutable passage could not be read back.
        "PASSAGE_UNAVAILABLE",
        # The provider itself reported this item as failed.  No grounding ran,
        # so nothing was measured; the raw provider error is never persisted.
        "PROVIDER_ITEM_ERROR",
        # A terminal provider job returned no result at all for this item.
        "TERMINAL_WITHOUT_RESULT",
    }
)
# Every diagnostic field is a count, a code point length, an index, or a flag.
# Nothing here can be inverted into a passage, an evidence string, an anchor,
# a prompt, or model output.
_GROUNDING_DIAGNOSTIC_FIELDS = frozenset(
    {
        "concept_index",
        "concept_count",
        "mention_index",
        "mention_count",
        "passage_codepoints",
        "evidence_codepoints",
        "occurrence_count",
        "has_anchors",
        "anchor_before_codepoints",
        "anchor_after_codepoints",
        "anchored_candidate_count",
        "direct_offsets_in_range",
        "direct_is_exact",
    }
)
_UNDIAGNOSED_FAILURE_REASON = "UNDIAGNOSED"
_PROVIDER_ITEM_ERROR_DIAGNOSTICS: Mapping[str, Any] = {"reason": "PROVIDER_ITEM_ERROR"}
_TERMINAL_WITHOUT_RESULT_DIAGNOSTICS: Mapping[str, Any] = {"reason": "TERMINAL_WITHOUT_RESULT"}


def _grounding_diagnostics(reason: str, **fields: Any) -> dict[str, Any]:
    """Build the content-free failure record persisted beside a failed item.

    This function is the single write-side gate for the invariant that makes
    the diagnostics safe to store and to show an administrator: a value is
    accepted only if it is a known flag or a number.  A string that came from
    the model or from the source can therefore never reach the database through
    this path, even by accident, because it raises instead of being persisted.
    ``None`` fields are dropped rather than stored, so a reader can distinguish
    "not measurable at this raise site" from a genuine zero.
    """
    if reason not in _GROUNDING_FAILURE_REASONS:
        raise BatchServiceError(f"unknown Batch failure reason: {reason}")
    diagnostics: dict[str, Any] = {"reason": reason}
    for name, value in fields.items():
        if value is None:
            continue
        if name not in _GROUNDING_DIAGNOSTIC_FIELDS:
            raise BatchServiceError(f"unknown Batch failure diagnostic field: {name}")
        if not isinstance(value, (bool, int)):
            raise BatchServiceError("Batch failure diagnostics accept only counts and flags")
        diagnostics[name] = value if isinstance(value, bool) else int(value)
    return diagnostics


def _safe_failure_diagnostics(serialized: Any) -> dict[str, Any] | None:
    """Re-validate persisted diagnostics on the way out to an administrator.

    The write path already refuses anything but counts and flags.  Validating
    again on read means the operator-facing summary cannot leak source text
    even from a row written by an older, hand-edited, or restored database:
    unknown keys and non-numeric values are dropped rather than displayed.
    """
    if not serialized:
        return None
    try:
        decoded = json.loads(serialized)
    except ValueError:
        return None
    if not isinstance(decoded, Mapping):
        return None
    reason = decoded.get("reason")
    safe: dict[str, Any] = {
        "reason": reason if reason in _GROUNDING_FAILURE_REASONS else _UNDIAGNOSED_FAILURE_REASON
    }
    for name in sorted(_GROUNDING_DIAGNOSTIC_FIELDS):
        value = decoded.get(name)
        if isinstance(value, (bool, int)):
            safe[name] = value if isinstance(value, bool) else int(value)
    return safe


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise BatchServiceError("Batch request/result must be JSON serializable") from exc


def _normalize_name(value: str) -> str:
    normalized = " ".join(value.split()).casefold()
    if not normalized:
        raise BatchPayloadError("concept names and aliases must not be empty")
    return normalized


def _assert_no_secrets(value: Any) -> None:
    """Reject credentials before a request is persisted or exported as JSONL."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.casefold() in _SENSITIVE_REQUEST_KEYS:
                raise BatchServiceError("Batch request must not contain provider credentials")
            _assert_no_secrets(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_no_secrets(nested)


class SQLiteBatchRepository:
    """SQLite adapter layered on the canonical ``SQLiteEpubStore`` transaction API.

    The canonical store already migrates the source and Batch base tables.  This
    adapter adds a tiny, namespaced lineage table so failed items can be retried
    in a successor remote batch without overwriting the original provider job.
    """

    def __init__(self, store: Any):
        if not hasattr(store, "_connection") or not hasattr(store, "_write"):
            raise TypeError("SQLiteBatchRepository requires the canonical SQLite EPUB store")
        self._store = store
        self._migrate_service_tables()

    def _migrate_service_tables(self) -> None:
        # This is independent from source-schema versioning.  It is idempotent
        # and is deliberately namespaced so the eventual PostgreSQL adapter can
        # own an equivalent migration without affecting the source schema.
        with self._store._write() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS epub_batch_job_lineage (
                    child_batch_job_id TEXT PRIMARY KEY
                        REFERENCES batch_jobs(batch_job_id) ON DELETE RESTRICT,
                    parent_batch_job_id TEXT NOT NULL
                        REFERENCES batch_jobs(batch_job_id) ON DELETE RESTRICT,
                    reason TEXT NOT NULL CHECK (reason IN ('FAILED_ITEMS')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(parent_batch_job_id, reason)
                )"""
            )
            # This audit table deliberately holds only durable identifiers and
            # the review decision.  It never copies source passages, model
            # output, request JSON, or provider credentials.
            connection.execute(
                """CREATE TABLE IF NOT EXISTS epub_batch_sample_reviews (
                    sample_batch_job_id TEXT PRIMARY KEY
                        REFERENCES batch_jobs(batch_job_id) ON DELETE RESTRICT,
                    version_id TEXT NOT NULL REFERENCES book_versions(version_id) ON DELETE RESTRICT,
                    job_kind TEXT NOT NULL CHECK (job_kind IN ('CONCEPT_MENTIONS', 'SECTION_GRAPH')),
                    status TEXT NOT NULL CHECK (status IN ('APPROVED', 'REJECTED')),
                    reviewed_by TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_epub_batch_sample_reviews_gate
                   ON epub_batch_sample_reviews(version_id, job_kind, status, reviewed_at)"""
            )

    @staticmethod
    def _require_job(connection: Any, batch_job_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM batch_jobs WHERE batch_job_id = ?", (batch_job_id,)
        ).fetchone()
        if row is None:
            raise BatchServiceError(f"unknown batch_job_id: {batch_job_id}")
        return row

    @staticmethod
    def _validate_items(items: Sequence[BatchItemInput]) -> None:
        if not items:
            raise BatchServiceError("a Batch job must contain at least one item")
        custom_ids: set[str] = set()
        passage_ids: set[str] = set()
        for item in items:
            if not item.custom_id.strip() or not item.passage_id.strip():
                raise BatchServiceError("Batch item custom_id and passage_id cannot be empty")
            if item.custom_id in custom_ids or item.passage_id in passage_ids:
                raise BatchServiceError("a Batch job cannot repeat a custom_id or passage")
            _assert_no_secrets(item.request)
            custom_ids.add(item.custom_id)
            passage_ids.add(item.passage_id)

    def create_draft(
        self,
        *,
        version_id: str,
        provider: str,
        profile_name: str,
        job_kind: str = "CONCEPT_MENTIONS",
        is_sample: bool = False,
        items: Sequence[BatchItemInput],
        batch_job_id: str | None = None,
    ) -> str:
        if not provider.strip() or not profile_name.strip():
            raise BatchServiceError("provider and profile_name cannot be empty")
        if job_kind not in _JOB_KINDS:
            raise BatchServiceError(f"unsupported EPUB Batch job kind: {job_kind}")
        self._validate_items(items)
        job_id = batch_job_id or str(uuid4())
        expected_items = {
            item.custom_id: (item.passage_id, _canonical_json(item.request)) for item in items
        }
        with self._store._write() as connection:
            existing = connection.execute(
                "SELECT * FROM batch_jobs WHERE batch_job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                same_job = (
                    existing["version_id"] == version_id
                    and existing["provider"] == provider
                    and existing["profile_name"] == profile_name
                    and existing["job_kind"] == job_kind
                    and bool(existing["is_sample"]) == is_sample
                )
                actual_items = {
                    row["custom_id"]: (row["passage_id"], row["request_json"])
                    for row in connection.execute(
                        "SELECT custom_id, passage_id, request_json FROM batch_items WHERE batch_job_id = ?",
                        (job_id,),
                    )
                }
                if not same_job or actual_items != expected_items:
                    raise BatchServiceError("idempotency key already belongs to different Batch input")
                return job_id

            version = connection.execute(
                "SELECT status FROM book_versions WHERE version_id = ?", (version_id,)
            ).fetchone()
            if version is None:
                raise BatchServiceError(f"unknown version_id: {version_id}")
            if version["status"] != "READY":
                raise BatchServiceError("only a READY EPUB version may enter offline Batch processing")
            passages = {
                row["passage_id"]
                for row in connection.execute(
                    "SELECT passage_id FROM passages WHERE version_id = ?", (version_id,)
                )
            }
            unknown = [item.passage_id for item in items if item.passage_id not in passages]
            if unknown:
                raise BatchServiceError("every Batch item must belong to the job's ready book version")
            if provider == "openai-batch" and not is_sample:
                self._require_approved_sample(
                    connection,
                    version_id=version_id,
                    job_kind=job_kind,
                    profile_name=profile_name,
                )
            connection.execute(
                """INSERT INTO batch_jobs(batch_job_id, version_id, provider, profile_name, job_kind, is_sample)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, version_id, provider, profile_name, job_kind, int(is_sample)),
            )
            for item in items:
                connection.execute(
                    """INSERT INTO batch_items(
                           batch_item_id, batch_job_id, passage_id, custom_id, request_json
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (str(uuid4()), job_id, item.passage_id, item.custom_id, _canonical_json(item.request)),
                )
        return job_id

    @staticmethod
    def _require_approved_sample(
        connection: Any, *, version_id: str, job_kind: str, profile_name: str
    ) -> None:
        """Keep the cloud quality gate inside the durable creation transaction.

        An OpenAI Batch can reach the provider's successful terminal state
        while an individual item fails schema validation or ingestion.  A full
        job is therefore permitted only after an administrator approves a
        sample with the same pinned model profile whose every item was durably
        ingested.
        """
        approved = connection.execute(
            """SELECT review.sample_batch_job_id
                 FROM epub_batch_sample_reviews AS review
                 JOIN batch_jobs AS job ON job.batch_job_id = review.sample_batch_job_id
                WHERE review.version_id = ?
                  AND review.job_kind = ?
                  AND review.status = 'APPROVED'
                  AND job.provider = 'openai-batch'
                  AND job.profile_name = ?
                  AND job.is_sample = 1
                  AND job.status = 'SUCCEEDED'
                  AND EXISTS (
                      SELECT 1 FROM batch_items AS item
                       WHERE item.batch_job_id = job.batch_job_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM batch_items AS item
                       WHERE item.batch_job_id = job.batch_job_id
                         AND item.status <> 'SUCCEEDED'
                  )
                ORDER BY review.reviewed_at DESC, review.sample_batch_job_id DESC
                LIMIT 1""",
            (version_id, job_kind, profile_name),
        ).fetchone()
        if approved is None:
            raise BatchServiceError(
                "creating a full OpenAI EPUB Batch requires an administrator-approved sample "
                "for the same version and job kind, with the same model profile; "
                "the sample must be SUCCEEDED with every item ingested"
            )

    def get_job(self, batch_job_id: str) -> dict[str, Any]:
        row = self._require_job(self._store._connection(), batch_job_id)
        return dict(row)

    @staticmethod
    def _summary(connection: Any, row: Any, *, include_items: bool) -> dict[str, Any]:
        """Return operator-safe state only, never durable request/result text.

        Cloud prompts and model responses can contain source material.  The
        history UI therefore exposes identifiers, lifecycle timestamps and
        aggregate counts, but intentionally omits ``request_json``,
        ``response_json``, raw item errors, and raw provider errors.

        Failure diagnostics are exposed because they are, by construction,
        counts and flags: they are validated on write and again on read, so
        they cannot carry a passage, an evidence string, or model output.  The
        per-job aggregate lets an administrator read "7 ambiguous, 3 absent"
        off the history list without opening a single item.
        """
        statuses = {
            str(count["status"]): int(count["count"])
            for count in connection.execute(
                """SELECT status, COUNT(*) AS count FROM batch_items
                   WHERE batch_job_id = ? GROUP BY status""",
                (row["batch_job_id"],),
            )
        }
        # Grouping happens here rather than in SQL because the reason lives
        # inside a JSON document, and the JSON1 extension is not guaranteed on
        # every SQLite build this service is deployed against.  Failed items
        # without a measurement are counted rather than dropped, so the
        # aggregate always sums to the FAILED item count.
        reasons: dict[str, int] = {}
        for failure in connection.execute(
            """SELECT failure_diagnostics_json FROM batch_items
               WHERE batch_job_id = ? AND status = 'FAILED'""",
            (row["batch_job_id"],),
        ):
            diagnostics = _safe_failure_diagnostics(failure["failure_diagnostics_json"])
            reason = str(diagnostics["reason"]) if diagnostics else _UNDIAGNOSED_FAILURE_REASON
            reasons[reason] = reasons.get(reason, 0) + 1
        summary: dict[str, Any] = {
            "batch_job_id": row["batch_job_id"],
            "version_id": row["version_id"],
            "provider": row["provider"],
            "provider_job_id": row["provider_job_id"],
            "profile_name": row["profile_name"],
            "job_kind": row["job_kind"],
            "status": row["status"],
            "is_sample": bool(row["is_sample"]),
            "submitted_at": row["submitted_at"],
            "completed_at": row["completed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "has_error": bool(row["last_error"]),
            # This is deliberately a boolean rather than durable error text.
            # Provider errors and results can contain source material.
            "results_pending_retrieval": row["last_error"] == _RESULTS_PENDING_RETRIEVAL,
            "item_count": sum(statuses.values()),
            "item_status_counts": statuses,
            "item_failure_reason_counts": reasons,
        }
        if include_items:
            summary["items"] = [
                {
                    "batch_item_id": item["batch_item_id"],
                    "passage_id": item["passage_id"],
                    "custom_id": item["custom_id"],
                    "status": item["status"],
                    "attempt_count": item["attempt_count"],
                    "has_response": bool(item["response_json"]),
                    "has_error": bool(item["error_text"]),
                    "failure_diagnostics": _safe_failure_diagnostics(
                        item["failure_diagnostics_json"]
                    ),
                    "updated_at": item["updated_at"],
                }
                for item in connection.execute(
                    """SELECT batch_item_id, passage_id, custom_id, status, attempt_count,
                              response_json, error_text, failure_diagnostics_json, updated_at
                       FROM batch_items WHERE batch_job_id = ? ORDER BY custom_id""",
                    (row["batch_job_id"],),
                )
            ]
        return summary

    def get_job_summary(self, batch_job_id: str) -> dict[str, Any]:
        connection = self._store._connection()
        return self._summary(connection, self._require_job(connection, batch_job_id), include_items=True)

    def list_job_summaries(
        self, *, version_id: str | None, offset: int, limit: int
    ) -> tuple[int, list[dict[str, Any]]]:
        if offset < 0 or not 1 <= limit <= 200:
            raise BatchServiceError("Batch history pagination values are invalid")
        connection = self._store._connection()
        where = ""
        parameters: tuple[Any, ...] = ()
        if version_id is not None:
            where = " WHERE version_id = ?"
            parameters = (version_id,)
        total = int(connection.execute(f"SELECT COUNT(*) FROM batch_jobs{where}", parameters).fetchone()[0])
        rows = connection.execute(
            f"""SELECT * FROM batch_jobs{where}
                ORDER BY created_at DESC, batch_job_id DESC LIMIT ? OFFSET ?""",
            parameters + (limit, offset),
        ).fetchall()
        return total, [self._summary(connection, row, include_items=False) for row in rows]

    def review_sample_job(
        self, batch_job_id: str, *, status: str, reviewed_by: str
    ) -> dict[str, Any]:
        """Persist an administrator review of a fully ingested cloud sample.

        The audit record intentionally contains only identifiers, decision,
        and time.  It cannot become an alternate copy of the EPUB source,
        model output, request envelope, or server credential.
        """
        if status not in _SAMPLE_REVIEW_STATUSES:
            raise BatchServiceError("sample review status must be APPROVED or REJECTED")
        reviewer = reviewed_by.strip()
        if not reviewer or len(reviewer) > 200:
            raise BatchServiceError(
                "sample reviewer identity must be a non-empty value of at most 200 characters"
            )
        with self._store._write() as connection:
            job = self._require_job(connection, batch_job_id)
            if job["provider"] != "openai-batch" or not bool(job["is_sample"]):
                raise BatchServiceError("only an OpenAI EPUB sample Batch can be reviewed")
            item_counts = connection.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN status = 'SUCCEEDED' THEN 1 ELSE 0 END) AS succeeded
                     FROM batch_items WHERE batch_job_id = ?""",
                (batch_job_id,),
            ).fetchone()
            if (
                job["status"] != "SUCCEEDED"
                or item_counts is None
                or item_counts["total"] == 0
                or item_counts["total"] != item_counts["succeeded"]
            ):
                raise BatchServiceError(
                    "a sample can be reviewed only after it is SUCCEEDED and every item was ingested"
                )
            connection.execute(
                """INSERT INTO epub_batch_sample_reviews(
                       sample_batch_job_id, version_id, job_kind, status, reviewed_by, reviewed_at
                   ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(sample_batch_job_id) DO UPDATE SET
                       status = excluded.status,
                       reviewed_by = excluded.reviewed_by,
                       reviewed_at = CURRENT_TIMESTAMP""",
                (batch_job_id, job["version_id"], job["job_kind"], status, reviewer),
            )
            row = connection.execute(
                """SELECT sample_batch_job_id, version_id, job_kind, status, reviewed_by, reviewed_at
                     FROM epub_batch_sample_reviews WHERE sample_batch_job_id = ?""",
                (batch_job_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_sample_reviews(
        self, *, version_id: str | None = None, job_kind: str | None = None
    ) -> list[dict[str, Any]]:
        if job_kind is not None and job_kind not in _JOB_KINDS:
            raise BatchServiceError(f"unsupported EPUB Batch job kind: {job_kind}")
        clauses: list[str] = []
        parameters: list[str] = []
        if version_id is not None:
            clauses.append("review.version_id = ?")
            parameters.append(version_id)
        if job_kind is not None:
            clauses.append("review.job_kind = ?")
            parameters.append(job_kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._store._connection().execute(
            f"""SELECT review.sample_batch_job_id, review.version_id, review.job_kind,
                       review.status, review.reviewed_by, review.reviewed_at,
                       job.status AS batch_status
                  FROM epub_batch_sample_reviews AS review
                  JOIN batch_jobs AS job ON job.batch_job_id = review.sample_batch_job_id
                  {where}
                 ORDER BY review.reviewed_at DESC, review.sample_batch_job_id DESC""",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_items(self, batch_job_id: str) -> list[dict[str, Any]]:
        self.get_job(batch_job_id)
        return [
            dict(row)
            for row in self._store._connection()
            .execute(
                "SELECT * FROM batch_items WHERE batch_job_id = ? ORDER BY custom_id", (batch_job_id,)
            )
            .fetchall()
        ]

    def jsonl_for_job(self, batch_job_id: str) -> str:
        payloads: list[str] = []
        for item in self.list_items(batch_job_id):
            request = json.loads(item["request_json"])
            _assert_no_secrets(request)
            if "custom_id" in request and request["custom_id"] != item["custom_id"]:
                raise BatchServiceError("request custom_id conflicts with its durable Batch item")
            request["custom_id"] = item["custom_id"]
            payloads.append(_canonical_json(request))
        return "\n".join(payloads) + "\n"

    def mark_submitted(self, batch_job_id: str, provider_job_id: str) -> None:
        if not provider_job_id.strip():
            raise BatchServiceError("provider returned an empty Batch job ID")
        with self._store._write() as connection:
            job = self._require_job(connection, batch_job_id)
            if job["status"] in _TERMINAL_JOB_STATES:
                raise BatchServiceError("a terminal Batch job cannot be submitted again")
            stored_provider_id = job["provider_job_id"]
            if stored_provider_id is not None and stored_provider_id != provider_job_id:
                raise BatchServiceError("provider returned a different ID for an existing idempotency key")
            connection.execute(
                """UPDATE batch_jobs
                   SET provider_job_id = ?, status = 'SUBMITTED',
                       submitted_at = COALESCE(submitted_at, CURRENT_TIMESTAMP),
                       updated_at = CURRENT_TIMESTAMP, last_error = NULL
                   WHERE batch_job_id = ?""",
                (provider_job_id, batch_job_id),
            )
            connection.execute(
                """UPDATE batch_items SET status = 'SUBMITTED', updated_at = CURRENT_TIMESTAMP
                   WHERE batch_job_id = ? AND status = 'PENDING'""",
                (batch_job_id,),
            )

    def set_provider_state(self, batch_job_id: str, state: str, error: str | None) -> None:
        if state not in _ACTIVE_JOB_STATES | _TERMINAL_JOB_STATES:
            raise BatchServiceError(f"invalid normalized Batch state: {state}")
        with self._store._write() as connection:
            job = self._require_job(connection, batch_job_id)
            current = job["status"]
            if current in _TERMINAL_JOB_STATES:
                if current == state:
                    return
                raise BatchServiceError(f"terminal Batch state cannot change from {current} to {state}")
            if current == "DRAFT" and state not in {"DRAFT", "CANCELLED", "FAILED"}:
                raise BatchServiceError("a provider state cannot advance an unsubmitted Batch job")
            if current == "RUNNING" and state == "SUBMITTED":
                return  # providers sometimes report a stale queued snapshot
            connection.execute(
                """UPDATE batch_jobs
                   SET status = ?, last_error = ?,
                       completed_at = CASE WHEN ? IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
                                           THEN COALESCE(completed_at, CURRENT_TIMESTAMP)
                                           ELSE completed_at END,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE batch_job_id = ?""",
                (state, error, state, batch_job_id),
            )

    @staticmethod
    def _item_for_update(connection: Any, batch_job_id: str, custom_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM batch_items WHERE batch_job_id = ? AND custom_id = ?",
            (batch_job_id, custom_id),
        ).fetchone()
        if row is None:
            raise BatchPayloadError(f"provider result has unknown custom_id: {custom_id}")
        return row

    @staticmethod
    def _resolve_or_create_concept(connection: Any, suggestion: Mapping[str, Any]) -> str:
        name = suggestion.get("name")
        aliases = suggestion.get("aliases", [])
        if not isinstance(name, str) or not isinstance(aliases, list) or not all(
            isinstance(alias, str) for alias in aliases
        ):
            raise BatchPayloadError("each concept needs string name and aliases")
        candidates = {_normalize_name(name), *(_normalize_name(alias) for alias in aliases)}
        placeholders = ",".join("?" for _ in candidates)
        matches = connection.execute(
            f"""SELECT concept_id, canonical_name FROM concepts WHERE normalized_name IN ({placeholders})
                UNION
                SELECT c.concept_id, c.canonical_name
                FROM concept_aliases a JOIN concepts c ON c.concept_id = a.concept_id
                WHERE a.normalized_alias IN ({placeholders})""",
            tuple(candidates) + tuple(candidates),
        ).fetchall()
        matched_ids = {row["concept_id"] for row in matches}
        if len(matched_ids) > 1:
            raise BatchPayloadError(
                "model output exactly matches aliases belonging to multiple concepts; admin review is required"
            )
        if matched_ids:
            # Seed/admin concepts win.  A model may attach mentions through an
            # exact normalized name/alias match, but it cannot rewrite metadata
            # or create a speculative semantic merge.
            return next(iter(matched_ids))

        definition = suggestion.get("definition", "")
        if not isinstance(definition, str):
            raise BatchPayloadError("concept definition must be a string")
        concept_id = str(uuid4())
        connection.execute(
            """INSERT INTO concepts(concept_id, canonical_name, normalized_name, definition, status)
               VALUES (?, ?, ?, ?, 'PROVISIONAL')""",
            (concept_id, name, _normalize_name(name), definition),
        )
        # Add the canonical spelling too.  This mirrors the canonical store's
        # alias convention and makes deterministic lookup uniform.  Deduplicate
        # by normalized spelling: model output frequently repeats an alias with
        # inconsequential whitespace or case differences.
        aliases_by_normalized: dict[str, str] = {}
        for alias in [name, *aliases]:
            aliases_by_normalized.setdefault(_normalize_name(alias), alias)
        for normalized_alias, alias in aliases_by_normalized.items():
            connection.execute(
                """INSERT INTO concept_aliases(alias_id, concept_id, alias, normalized_alias, source)
                   VALUES (?, ?, ?, ?, 'MODEL')""",
                (str(uuid4()), concept_id, alias, normalized_alias),
            )
        return concept_id

    @staticmethod
    def _ground_openai_concept_payload(
        connection: Any, *, item: Any, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Canonically ground a cloud concept payload against immutable text.

        OpenAI Structured Outputs constrains the response shape, but a model
        can still count Unicode code points incorrectly.  A bad numeric offset
        is never trusted: it can be repaired only from an exact evidence string
        whose literal occurrence is unique, or (for v4) whose short adjacent
        context anchor selects exactly one occurrence.  Legacy v1-v3 output is
        retained for replay compatibility, but it has no ambiguity escape hatch.

        Every rejection below also carries content-free diagnostics.  A failed
        item persists no result payload at all, so without them the durable
        record would retain only a failure class, and a prompt author could not
        tell "the model paraphrased" from "the evidence was two code points long
        and occurred forty-seven times".  Only counts and flags are attached;
        see :func:`_grounding_diagnostics`.
        """
        if set(payload) != {"concepts"} or not isinstance(payload.get("concepts"), list):
            raise BatchPayloadError(
                "OpenAI concept output must contain only a concepts list",
                diagnostics=_grounding_diagnostics("INVALID_SCHEMA"),
            )
        passage = connection.execute(
            "SELECT content FROM passages WHERE passage_id = ?", (item["passage_id"],)
        ).fetchone()
        if passage is None:
            raise BatchPayloadError(
                "Batch item passage is unavailable",
                diagnostics=_grounding_diagnostics("PASSAGE_UNAVAILABLE"),
            )
        content = passage["content"]
        passage_codepoints = len(content)
        concept_count = len(payload["concepts"])
        grounded_concepts: list[dict[str, Any]] = []
        for concept_index, concept in enumerate(payload["concepts"]):
            if not isinstance(concept, Mapping) or set(concept) != {
                "name", "aliases", "definition", "mentions"
            }:
                raise BatchPayloadError(
                    "OpenAI concept has an invalid schema",
                    diagnostics=_grounding_diagnostics(
                        "INVALID_SCHEMA",
                        concept_index=concept_index,
                        concept_count=concept_count,
                        passage_codepoints=passage_codepoints,
                    ),
                )
            mentions = concept.get("mentions")
            if not isinstance(mentions, list) or not mentions:
                raise BatchPayloadError(
                    "OpenAI concept needs a visible mention",
                    diagnostics=_grounding_diagnostics(
                        "MENTIONS_MISSING",
                        concept_index=concept_index,
                        concept_count=concept_count,
                        mention_count=len(mentions) if isinstance(mentions, list) else None,
                        passage_codepoints=passage_codepoints,
                    ),
                )
            grounded_mentions: list[dict[str, Any]] = []
            for mention_index, mention in enumerate(mentions):
                # Position within the response is itself a signal: a model that
                # degrades after the Nth concept needs a very different prompt
                # fix from one that fails uniformly.
                position: dict[str, Any] = {
                    "concept_index": concept_index,
                    "concept_count": concept_count,
                    "mention_index": mention_index,
                    "mention_count": len(mentions),
                    "passage_codepoints": passage_codepoints,
                }
                if not isinstance(mention, Mapping):
                    raise BatchPayloadError(
                        "OpenAI concept mention has an invalid schema",
                        diagnostics=_grounding_diagnostics("INVALID_SCHEMA", **position),
                    )
                fields = set(mention)
                if fields != _LEGACY_CONCEPT_MENTION_FIELDS and fields != _GROUNDED_CONCEPT_MENTION_FIELDS:
                    raise BatchPayloadError(
                        "OpenAI concept mention has an invalid schema",
                        diagnostics=_grounding_diagnostics("INVALID_SCHEMA", **position),
                    )
                has_anchors = fields == _GROUNDED_CONCEPT_MENTION_FIELDS
                start = mention.get("start_codepoint")
                end = mention.get("end_codepoint")
                evidence = mention.get("evidence")
                if (
                    isinstance(start, bool)
                    or isinstance(end, bool)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or not isinstance(evidence, str)
                    or not evidence
                ):
                    raise BatchPayloadError(
                        "OpenAI concept mention has invalid offsets or evidence",
                        diagnostics=_grounding_diagnostics(
                            "INVALID_OFFSETS",
                            has_anchors=has_anchors,
                            evidence_codepoints=len(evidence) if isinstance(evidence, str) else None,
                            **position,
                        ),
                    )
                evidence_codepoints = len(evidence)
                before = after = ""
                if has_anchors:
                    before = mention["context_before"]
                    after = mention["context_after"]
                    if (
                        not isinstance(before, str)
                        or not isinstance(after, str)
                        or len(before) > _MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS
                        or len(after) > _MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS
                    ):
                        raise BatchPayloadError(
                            "OpenAI evidence context anchor is invalid",
                            diagnostics=_grounding_diagnostics(
                                "ANCHOR_INVALID",
                                has_anchors=True,
                                evidence_codepoints=evidence_codepoints,
                                anchor_before_codepoints=(
                                    len(before) if isinstance(before, str) else None
                                ),
                                anchor_after_codepoints=(
                                    len(after) if isinstance(after, str) else None
                                ),
                                **position,
                            ),
                        )
                shape: dict[str, Any] = {
                    "has_anchors": has_anchors,
                    "evidence_codepoints": evidence_codepoints,
                    "anchor_before_codepoints": len(before),
                    "anchor_after_codepoints": len(after),
                }

                # ``direct_is_exact`` is a pure expression over values that are
                # already final here, so it is evaluated before the occurrence
                # scan purely so every rejection below can report whether the
                # model's own offsets were even in range.  Grounding itself is
                # unchanged: it is still consulted only after the scan.
                direct_offsets_in_range = 0 <= start < end <= passage_codepoints
                direct_is_exact = direct_offsets_in_range and content[start:end] == evidence
                direct: dict[str, Any] = {
                    "direct_offsets_in_range": direct_offsets_in_range,
                    "direct_is_exact": direct_is_exact,
                }

                occurrences: list[int] = []
                cursor = content.find(evidence)
                while cursor >= 0:
                    occurrences.append(cursor)
                    # Advance one code point so overlapping literals cannot be
                    # misclassified as a unique source occurrence.
                    cursor = content.find(evidence, cursor + 1)
                if not occurrences:
                    raise BatchPayloadError(
                        "OpenAI evidence is absent from the immutable source",
                        diagnostics=_grounding_diagnostics(
                            "EVIDENCE_ABSENT",
                            occurrence_count=0,
                            **shape,
                            **direct,
                            **position,
                        ),
                    )
                if len(occurrences) > 1 and has_anchors and not (before or after):
                    raise BatchPayloadError(
                        "repeated OpenAI evidence needs a non-empty context anchor",
                        diagnostics=_grounding_diagnostics(
                            "ANCHOR_MISSING",
                            occurrence_count=len(occurrences),
                            **shape,
                            **direct,
                            **position,
                        ),
                    )

                if direct_is_exact and has_anchors:
                    if (
                        content[max(0, start - len(before)):start] != before
                        or content[end:end + len(after)] != after
                    ):
                        raise BatchPayloadError(
                            "OpenAI evidence context anchor does not match the immutable source",
                            diagnostics=_grounding_diagnostics(
                                "ANCHOR_MISMATCH",
                                occurrence_count=len(occurrences),
                                **shape,
                                **direct,
                                **position,
                            ),
                        )
                if not direct_is_exact:
                    candidates = occurrences
                    if has_anchors:
                        candidates = [
                            occurrence
                            for occurrence in occurrences
                            if content[max(0, occurrence - len(before)):occurrence] == before
                            and content[
                                occurrence + len(evidence):occurrence + len(evidence) + len(after)
                            ] == after
                        ]
                    if len(candidates) != 1:
                        raise BatchPayloadError(
                            "OpenAI evidence cannot be uniquely located in the immutable source",
                            diagnostics=_grounding_diagnostics(
                                "EVIDENCE_AMBIGUOUS",
                                occurrence_count=len(occurrences),
                                anchored_candidate_count=len(candidates),
                                **shape,
                                **direct,
                                **position,
                            ),
                        )
                    start = candidates[0]
                    end = start + len(evidence)
                normalized = dict(mention)
                normalized["start_codepoint"] = start
                normalized["end_codepoint"] = end
                grounded_mentions.append(normalized)
            grounded = dict(concept)
            grounded["mentions"] = grounded_mentions
            grounded_concepts.append(grounded)
        return {"concepts": grounded_concepts}

    @staticmethod
    def _add_mentions(
        connection: Any,
        concept_id: str,
        fallback_passage_id: str,
        mentions: Any,
        *,
        version_id: str | None = None,
        require_passage_ids: bool = False,
    ) -> None:
        if mentions is None:
            return
        if not isinstance(mentions, list):
            raise BatchPayloadError("concept mentions must be a list")
        for mention in mentions:
            if not isinstance(mention, Mapping):
                raise BatchPayloadError("each mention must be an object")
            if require_passage_ids and set(mention) != {
                "passage_id",
                "start_codepoint",
                "end_codepoint",
                "evidence",
            }:
                raise BatchPayloadError("section graph mention has an invalid schema")
            passage_id = mention.get("passage_id", fallback_passage_id)
            if require_passage_ids and "passage_id" not in mention:
                raise BatchPayloadError("section graph mentions need a passage_id")
            if not isinstance(passage_id, str) or not passage_id:
                raise BatchPayloadError("concept mention passage_id must be a non-empty string")
            query = "SELECT content FROM passages WHERE passage_id = ?"
            parameters: tuple[Any, ...] = (passage_id,)
            if version_id is not None:
                query += " AND version_id = ?"
                parameters = (passage_id, version_id)
            passage = connection.execute(query, parameters).fetchone()
            if passage is None:
                raise BatchPayloadError("concept mention does not belong to this EPUB version")
            content = passage["content"]
            start = mention.get("start_codepoint")
            end = mention.get("end_codepoint")
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start or end > len(content):
                raise BatchPayloadError("concept mention offsets must identify a non-empty source substring")
            evidence = mention.get("evidence")
            expected = content[start:end]
            if evidence is not None and evidence != expected:
                raise BatchPayloadError("concept mention evidence must equal the immutable source substring")
            exists = connection.execute(
                """SELECT 1 FROM concept_mentions
                   WHERE concept_id = ? AND passage_id = ? AND start_codepoint = ? AND end_codepoint = ?""",
                (concept_id, passage_id, start, end),
            ).fetchone()
            if exists is None:
                connection.execute(
                    """INSERT INTO concept_mentions(
                           mention_id, concept_id, passage_id, start_codepoint, end_codepoint, evidence, source
                       ) VALUES (?, ?, ?, ?, ?, ?, 'MODEL')""",
                    (str(uuid4()), concept_id, passage_id, start, end, expected),
                )

    def _ingest_section_graph(
        self, connection: Any, *, version_id: str, item: Any, payload: Mapping[str, Any]
    ) -> None:
        """Validate and commit one multi-passage packet as a single transaction."""
        if set(payload) != {"concepts", "relations"}:
            raise BatchPayloadError("section graph output must contain only concepts and relations")
        concepts = payload.get("concepts")
        relations = payload.get("relations")
        if not isinstance(concepts, list) or not isinstance(relations, list):
            raise BatchPayloadError("section graph output needs concepts and relations lists")
        local_concepts: dict[str, str] = {}
        for suggestion in concepts:
            if not isinstance(suggestion, Mapping):
                raise BatchPayloadError("section graph concepts must contain objects")
            if set(suggestion) != {"local_id", "name", "aliases", "definition", "mentions"}:
                raise BatchPayloadError("section graph concept has an invalid schema")
            local_id = suggestion.get("local_id")
            if not isinstance(local_id, str) or not local_id.strip() or local_id in local_concepts:
                raise BatchPayloadError("section graph local_id must be unique and non-empty")
            concept_id = self._resolve_or_create_concept(connection, suggestion)
            self._add_mentions(
                connection,
                concept_id,
                item["passage_id"],
                suggestion["mentions"],
                version_id=version_id,
                require_passage_ids=True,
            )
            local_concepts[local_id] = concept_id
        for relation in relations:
            if not isinstance(relation, Mapping):
                raise BatchPayloadError("section graph relations must contain objects")
            if set(relation) != {"subject_local_id", "predicate", "object_local_id", "evidence"}:
                raise BatchPayloadError("section graph relation has an invalid schema")
            subject = relation.get("subject_local_id")
            predicate = relation.get("predicate")
            object_ = relation.get("object_local_id")
            evidence = relation.get("evidence")
            if not isinstance(subject, str) or not isinstance(object_, str) or subject == object_:
                raise BatchPayloadError("section graph relation needs two distinct local concept IDs")
            if subject not in local_concepts or object_ not in local_concepts:
                raise BatchPayloadError("section graph relation endpoint is not a packet concept")
            if predicate not in _RELATION_PREDICATES or not isinstance(evidence, list) or not evidence:
                raise BatchPayloadError("section graph relation predicate or evidence is invalid")
            if any(
                not isinstance(item, Mapping)
                or set(item) != {"passage_id", "start_codepoint", "end_codepoint", "evidence"}
                for item in evidence
            ):
                raise BatchPayloadError("section graph relation evidence has an invalid schema")
            try:
                self._store._add_concept_relation(
                    connection,
                    version_id=version_id,
                    subject_concept_id=local_concepts[subject],
                    predicate=predicate,
                    object_concept_id=local_concepts[object_],
                    evidence=evidence,
                )
            except ValueError as exc:
                raise BatchPayloadError(str(exc)) from exc

    def ingest_success(self, batch_job_id: str, custom_id: str, payload: Mapping[str, Any]) -> bool:
        """Atomically ingest one model result and mark the durable item complete.

        Repeating byte-equivalent output is a no-op.  Different output for an
        already succeeded item is rejected rather than silently rewriting the
        graph, preserving reproducibility of an offline Batch run.
        """
        with self._store._write() as connection:
            job = self._require_job(connection, batch_job_id)
            item = self._item_for_update(connection, batch_job_id, custom_id)
            if job["provider"] == "openai-batch" and job["job_kind"] == "CONCEPT_MENTIONS":
                payload = self._ground_openai_concept_payload(connection, item=item, payload=payload)
            serialized = _canonical_json(payload)
            if item["status"] == "SUCCEEDED":
                if item["response_json"] == serialized:
                    return False
                raise BatchPayloadError("different output received for an already ingested Batch item")
            if item["status"] not in {"PENDING", "SUBMITTED", "RETRY", "FAILED"}:
                raise BatchPayloadError(f"cannot ingest output for item state {item['status']}")
            if job["job_kind"] == "SECTION_GRAPH":
                self._ingest_section_graph(
                    connection, version_id=job["version_id"], item=item, payload=payload
                )
            else:
                concepts = payload.get("concepts")
                if not isinstance(concepts, list):
                    raise BatchPayloadError("provider success payload must contain a concepts list")
                for suggestion in concepts:
                    if not isinstance(suggestion, Mapping):
                        raise BatchPayloadError("concepts must contain objects")
                    concept_id = self._resolve_or_create_concept(connection, suggestion)
                    self._add_mentions(connection, concept_id, item["passage_id"], suggestion.get("mentions"))
            connection.execute(
                """UPDATE batch_items
                   SET status = 'SUCCEEDED', response_json = ?, error_text = NULL,
                       failure_diagnostics_json = NULL, updated_at = CURRENT_TIMESTAMP
                   WHERE batch_item_id = ?""",
                (serialized, item["batch_item_id"]),
            )
        return True

    def record_item_failure(
        self,
        batch_job_id: str,
        custom_id: str,
        error: str,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> bool:
        """Mark one item failed, keeping the failure class and its measurement.

        ``error`` stays the durable human failure class.  ``diagnostics`` is the
        content-free numeric record of the same rejection; it is optional
        because a provider transport failure measures nothing.
        """
        if not error.strip():
            raise BatchPayloadError("a failed Batch item needs an error message")
        serialized = _canonical_json(diagnostics) if diagnostics is not None else None
        with self._store._write() as connection:
            item = self._item_for_update(connection, batch_job_id, custom_id)
            if item["status"] == "SUCCEEDED":
                raise BatchPayloadError("a succeeded Batch item cannot be overwritten as failed")
            if item["status"] == "FAILED" and item["error_text"] == error:
                # Repeating a failure class is still not a new failure, so the
                # caller's count is unchanged.  The measurement is refreshed
                # anyway: it is re-derived from the same durable inputs, and
                # refreshing is what lets an item that failed before this
                # instrumentation existed gain diagnostics from a re-poll of
                # its original remote job, without paying for a new run.  A
                # stored measurement is never replaced with nothing.
                if serialized is not None and serialized != item["failure_diagnostics_json"]:
                    connection.execute(
                        """UPDATE batch_items
                           SET failure_diagnostics_json = ?, updated_at = CURRENT_TIMESTAMP
                           WHERE batch_item_id = ?""",
                        (serialized, item["batch_item_id"]),
                    )
                return False
            connection.execute(
                """UPDATE batch_items
                   SET status = 'FAILED', error_text = ?, failure_diagnostics_json = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE batch_item_id = ?""",
                (error, serialized, item["batch_item_id"]),
            )
        return True

    def mark_results_pending_retrieval(self, batch_job_id: str) -> None:
        """Record that a terminal provider job must be polled again safely.

        A terminal provider state does not prove this process read every
        output/error line.  A transient download or parse failure must never
        become local item failures: doing that could make a successor Batch
        replay work whose outcome is unknown.  Persist only a controlled
        marker, never a raw provider exception.
        """
        with self._store._write() as connection:
            self._require_job(connection, batch_job_id)
            connection.execute(
                """UPDATE batch_jobs
                   SET last_error = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE batch_job_id = ?""",
                (_RESULTS_PENDING_RETRIEVAL, batch_job_id),
            )

    def reconcile_terminal_missing_results(self, batch_job_id: str) -> int:
        """Fail only items absent from a complete, validated terminal result set.

        This method is intentionally separate from result retrieval.  Its
        caller must have fetched and validated the whole provider stream first;
        otherwise an interrupted stream could cause duplicate paid work.
        """
        with self._store._write() as connection:
            job = self._require_job(connection, batch_job_id)
            if job["status"] not in _TERMINAL_JOB_STATES:
                raise BatchServiceError("only a terminal Batch job can reconcile missing results")
            cursor = connection.execute(
                """UPDATE batch_items
                   SET status = 'FAILED', error_text = ?, failure_diagnostics_json = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE batch_job_id = ?
                     AND status <> 'SUCCEEDED'
                     AND response_json IS NULL
                     AND error_text IS NULL""",
                (
                    _TERMINAL_MISSING_RESULT,
                    # Nothing was measured because nothing arrived; the reason
                    # slug alone keeps these rows inside the per-job aggregate
                    # instead of silently widening its undiagnosed bucket.
                    _canonical_json(_TERMINAL_WITHOUT_RESULT_DIAGNOSTICS),
                    batch_job_id,
                ),
            )
            if job["last_error"] == _RESULTS_PENDING_RETRIEVAL:
                connection.execute(
                    """UPDATE batch_jobs SET last_error = NULL, updated_at = CURRENT_TIMESTAMP
                       WHERE batch_job_id = ?""",
                    (batch_job_id,),
                )
        return int(cursor.rowcount)

    def create_retry_child(self, batch_job_id: str) -> str:
        """Create one durable successor job containing exactly this job's failures."""
        with self._store._write() as connection:
            parent = self._require_job(connection, batch_job_id)
            existing = connection.execute(
                """SELECT child_batch_job_id FROM epub_batch_job_lineage
                   WHERE parent_batch_job_id = ? AND reason = 'FAILED_ITEMS'""",
                (batch_job_id,),
            ).fetchone()
            if existing is not None:
                return existing["child_batch_job_id"]
            failed_items = connection.execute(
                """SELECT passage_id, custom_id, request_json FROM batch_items
                   WHERE batch_job_id = ? AND status = 'FAILED' ORDER BY custom_id""",
                (batch_job_id,),
            ).fetchall()
            if not failed_items:
                raise BatchServiceError("Batch job has no failed items to retry")
            child_id = str(uuid4())
            connection.execute(
                """INSERT INTO batch_jobs(batch_job_id, version_id, provider, profile_name, job_kind, is_sample)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    child_id,
                    parent["version_id"],
                    parent["provider"],
                    parent["profile_name"],
                    parent["job_kind"],
                    parent["is_sample"],
                ),
            )
            for item in failed_items:
                retry_custom_id = f"retry:{batch_job_id}:{item['custom_id']}"
                retry_request = json.loads(item["request_json"])
                # ``custom_id`` is an envelope field.  If a provider requires
                # it in the request body, keep it in sync with the successor's
                # durable ID instead of making JSONL construction ambiguous.
                if "custom_id" in retry_request:
                    retry_request["custom_id"] = retry_custom_id
                connection.execute(
                    """INSERT INTO batch_items(
                           batch_item_id, batch_job_id, passage_id, custom_id, request_json, attempt_count
                       ) VALUES (?, ?, ?, ?, ?, 1)""",
                    (
                        str(uuid4()),
                        child_id,
                        item["passage_id"],
                        retry_custom_id,
                        _canonical_json(retry_request),
                    ),
                )
            connection.execute(
                """INSERT INTO epub_batch_job_lineage(child_batch_job_id, parent_batch_job_id, reason)
                   VALUES (?, ?, 'FAILED_ITEMS')""",
                (child_id, batch_job_id),
            )
        return child_id

    def list_recoverable_jobs(self, provider: str | None = None) -> list[str]:
        where = "status IN ('SUBMITTED', 'RUNNING')"
        parameters: tuple[Any, ...] = ()
        if provider is not None:
            where = "provider = ? AND " + where
            parameters = (provider,)
        return [
            row[0]
            for row in self._store._connection()
            .execute(
                f"""SELECT batch_job_id FROM batch_jobs
                   WHERE {where}
                   ORDER BY created_at, batch_job_id""",
                parameters,
            )
            .fetchall()
        ]


class BatchJobService:
    """State-machine service for administrator-triggered offline Batch work."""

    def __init__(self, repository: BatchRepository):
        self._repository = repository

    def create_draft(
        self,
        *,
        version_id: str,
        provider: str,
        profile_name: str,
        items: Sequence[BatchItemInput],
        job_kind: str = "CONCEPT_MENTIONS",
        is_sample: bool = False,
        batch_job_id: str | None = None,
    ) -> str:
        return self._repository.create_draft(
            version_id=version_id,
            provider=provider,
            profile_name=profile_name,
            job_kind=job_kind,
            is_sample=is_sample,
            items=items,
            batch_job_id=batch_job_id,
        )

    def get_job(self, batch_job_id: str) -> dict[str, Any]:
        """Internal lifecycle lookup; API callers must use safe summaries."""
        return self._repository.get_job(batch_job_id)

    def get_job_summary(self, batch_job_id: str) -> dict[str, Any]:
        return self._repository.get_job_summary(batch_job_id)

    def list_job_summaries(
        self, *, version_id: str | None = None, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        total, items = self._repository.list_job_summaries(
            version_id=version_id, offset=offset, limit=limit
        )
        return {"total": total, "offset": offset, "items": items}

    def review_sample_job(
        self, batch_job_id: str, *, status: str, reviewed_by: str
    ) -> dict[str, Any]:
        return self._repository.review_sample_job(
            batch_job_id, status=status, reviewed_by=reviewed_by
        )

    def list_sample_reviews(
        self, *, version_id: str | None = None, job_kind: str | None = None
    ) -> list[dict[str, Any]]:
        return self._repository.list_sample_reviews(version_id=version_id, job_kind=job_kind)

    def submit(self, batch_job_id: str, provider: BatchProvider) -> str:
        job = self._repository.get_job(batch_job_id)
        if job["provider"] != provider.name:
            raise BatchServiceError("provider implementation does not match durable Batch job provider")
        if job["status"] in _TERMINAL_JOB_STATES:
            raise BatchServiceError("a terminal Batch job cannot be submitted")
        if job["provider_job_id"]:
            return str(job["provider_job_id"])
        # The job/items were committed before this call.  If the process dies
        # now, retrying this method uses the same idempotency key and therefore
        # reclaims the same remote job instead of paying for a duplicate batch.
        provider_job_id = provider.submit(
            jsonl=self._repository.jsonl_for_job(batch_job_id), idempotency_key=batch_job_id
        )
        self._repository.mark_submitted(batch_job_id, provider_job_id)
        return provider_job_id

    def poll_and_ingest(self, batch_job_id: str, provider: BatchProvider) -> dict[str, int | str | bool]:
        job = self._repository.get_job(batch_job_id)
        if job["provider"] != provider.name:
            raise BatchServiceError("provider implementation does not match durable Batch job provider")
        provider_job_id = job["provider_job_id"]
        if not provider_job_id:
            raise BatchServiceError("cannot poll an unsubmitted Batch job")
        snapshot = provider.poll(str(provider_job_id))
        state = _PROVIDER_TO_JOB_STATE.get(snapshot.state)
        if state is None:
            raise BatchServiceError(f"provider returned unknown Batch state: {snapshot.state}")
        self._repository.set_provider_state(batch_job_id, state, snapshot.error)
        added = failed = 0
        if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            # Read and validate the complete result set before reconciling
            # missing items.  A provider output iterator can fail halfway
            # through a download, in which case no absence is conclusive.
            try:
                results = list(provider.fetch_results(str(provider_job_id)))
                known_custom_ids = {
                    item["custom_id"] for item in self._repository.list_items(batch_job_id)
                }
                seen_custom_ids: set[str] = set()
                for result in results:
                    if not result.custom_id:
                        raise BatchPayloadError("provider result custom_id cannot be empty")
                    if result.custom_id in seen_custom_ids:
                        raise BatchPayloadError("provider returned duplicate item output")
                    if result.custom_id not in known_custom_ids:
                        raise BatchPayloadError("provider result has unknown custom_id")
                    if (result.payload is None) == (result.error is None):
                        raise BatchPayloadError("provider result needs exactly one of payload or error")
                    if result.payload is not None and not isinstance(result.payload, Mapping):
                        raise BatchPayloadError("provider success payload must be an object")
                    if result.error is not None and (
                        not isinstance(result.error, str) or not result.error.strip()
                    ):
                        raise BatchPayloadError("provider item failure must be a non-empty string")
                    seen_custom_ids.add(result.custom_id)
            except Exception:
                # The API summary exposes only this controlled boolean.  An
                # administrator may poll again; a retry child is impossible
                # until an item is known to have no terminal result.
                self._repository.mark_results_pending_retrieval(batch_job_id)
                return {
                    "job_id": batch_job_id,
                    "state": state,
                    "ingested": 0,
                    "failed": 0,
                    "results_pending_retrieval": True,
                }

            for result in results:
                if result.error is not None:
                    # The provider rejected the item itself, so nothing local
                    # was measured.  Only the class is durable; the provider's
                    # own error string is never persisted as a diagnostic.
                    failed += int(
                        self._repository.record_item_failure(
                            batch_job_id,
                            result.custom_id,
                            result.error,
                            _PROVIDER_ITEM_ERROR_DIAGNOSTICS,
                        )
                    )
                    continue
                assert result.payload is not None
                try:
                    added += int(self._repository.ingest_success(batch_job_id, result.custom_id, result.payload))
                except BatchPayloadError as exc:
                    # ``exc.diagnostics`` is None for rejections that are not
                    # instrumented (a section graph packet, for instance); the
                    # item then simply fails without a measurement.
                    failed += int(
                        self._repository.record_item_failure(
                            batch_job_id, result.custom_id, str(exc), exc.diagnostics
                        )
                    )
            failed += self._repository.reconcile_terminal_missing_results(batch_job_id)
        return {
            "job_id": batch_job_id,
            "state": state,
            "ingested": added,
            "failed": failed,
        }

    def recover(self, provider: BatchProvider) -> list[dict[str, int | str | bool]]:
        """Poll all non-terminal work after a process restart; safe to repeat."""
        return [self.poll_and_ingest(job_id, provider) for job_id in self._repository.list_recoverable_jobs(provider.name)]

    def recover_all(self, providers: Mapping[str, BatchProvider]) -> dict[str, list[dict[str, Any]]]:
        """Resume submitted/running jobs without submitting any new cloud work.

        A restart may happen while a provider job is already executing.  This
        method only polls its durable remote ID and ingests resulting output;
        DRAFT jobs remain untouched.  Jobs whose provider is not configured
        are reported, not silently advanced or submitted.
        """
        recovered: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for batch_job_id in self._repository.list_recoverable_jobs():
            job = self._repository.get_job(batch_job_id)
            provider_name = str(job["provider"])
            provider = providers.get(provider_name)
            if provider is None:
                skipped.append(
                    {
                        "job_id": batch_job_id,
                        "provider": provider_name,
                        "reason": "provider is not configured",
                    }
                )
                continue
            recovered.append(self.poll_and_ingest(batch_job_id, provider))
        return {"recovered": recovered, "skipped": skipped}

    def retry_failed_items(self, batch_job_id: str) -> str:
        """Return an idempotently-created successor job; submit it normally."""
        return self._repository.create_retry_child(batch_job_id)
