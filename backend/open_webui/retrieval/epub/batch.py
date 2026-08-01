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
    """A provider result cannot safely be turned into concept graph records."""


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
        is_sample: bool,
        items: Sequence[BatchItemInput],
        batch_job_id: str | None = None,
    ) -> str: ...

    def get_job(self, batch_job_id: str) -> dict[str, Any]: ...

    def list_items(self, batch_job_id: str) -> list[dict[str, Any]]: ...

    def jsonl_for_job(self, batch_job_id: str) -> str: ...

    def mark_submitted(self, batch_job_id: str, provider_job_id: str) -> None: ...

    def set_provider_state(self, batch_job_id: str, state: str, error: str | None) -> None: ...

    def ingest_success(self, batch_job_id: str, custom_id: str, payload: Mapping[str, Any]) -> bool: ...

    def record_item_failure(self, batch_job_id: str, custom_id: str, error: str) -> bool: ...

    def create_retry_child(self, batch_job_id: str) -> str: ...

    def list_recoverable_jobs(self, provider: str) -> list[str]: ...


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
        is_sample: bool,
        items: Sequence[BatchItemInput],
        batch_job_id: str | None = None,
    ) -> str:
        if not provider.strip() or not profile_name.strip():
            raise BatchServiceError("provider and profile_name cannot be empty")
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
            connection.execute(
                """INSERT INTO batch_jobs(batch_job_id, version_id, provider, profile_name, is_sample)
                   VALUES (?, ?, ?, ?, ?)""",
                (job_id, version_id, provider, profile_name, int(is_sample)),
            )
            for item in items:
                connection.execute(
                    """INSERT INTO batch_items(
                           batch_item_id, batch_job_id, passage_id, custom_id, request_json
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (str(uuid4()), job_id, item.passage_id, item.custom_id, _canonical_json(item.request)),
                )
        return job_id

    def get_job(self, batch_job_id: str) -> dict[str, Any]:
        row = self._require_job(self._store._connection(), batch_job_id)
        return dict(row)

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
    def _add_mentions(
        connection: Any, concept_id: str, passage_id: str, mentions: Any
    ) -> None:
        if mentions is None:
            return
        if not isinstance(mentions, list):
            raise BatchPayloadError("concept mentions must be a list")
        passage = connection.execute(
            "SELECT content FROM passages WHERE passage_id = ?", (passage_id,)
        ).fetchone()
        if passage is None:
            raise BatchPayloadError("Batch item has no source passage")
        content = passage["content"]
        for mention in mentions:
            if not isinstance(mention, Mapping):
                raise BatchPayloadError("each mention must be an object")
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

    def ingest_success(self, batch_job_id: str, custom_id: str, payload: Mapping[str, Any]) -> bool:
        """Atomically ingest one model result and mark the durable item complete.

        Repeating byte-equivalent output is a no-op.  Different output for an
        already succeeded item is rejected rather than silently rewriting the
        graph, preserving reproducibility of an offline Batch run.
        """
        serialized = _canonical_json(payload)
        concepts = payload.get("concepts")
        if not isinstance(concepts, list):
            raise BatchPayloadError("provider success payload must contain a concepts list")
        with self._store._write() as connection:
            item = self._item_for_update(connection, batch_job_id, custom_id)
            if item["status"] == "SUCCEEDED":
                if item["response_json"] == serialized:
                    return False
                raise BatchPayloadError("different output received for an already ingested Batch item")
            if item["status"] not in {"PENDING", "SUBMITTED", "RETRY", "FAILED"}:
                raise BatchPayloadError(f"cannot ingest output for item state {item['status']}")
            for suggestion in concepts:
                if not isinstance(suggestion, Mapping):
                    raise BatchPayloadError("concepts must contain objects")
                concept_id = self._resolve_or_create_concept(connection, suggestion)
                self._add_mentions(connection, concept_id, item["passage_id"], suggestion.get("mentions"))
            connection.execute(
                """UPDATE batch_items
                   SET status = 'SUCCEEDED', response_json = ?, error_text = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE batch_item_id = ?""",
                (serialized, item["batch_item_id"]),
            )
        return True

    def record_item_failure(self, batch_job_id: str, custom_id: str, error: str) -> bool:
        if not error.strip():
            raise BatchPayloadError("a failed Batch item needs an error message")
        with self._store._write() as connection:
            item = self._item_for_update(connection, batch_job_id, custom_id)
            if item["status"] == "SUCCEEDED":
                raise BatchPayloadError("a succeeded Batch item cannot be overwritten as failed")
            if item["status"] == "FAILED" and item["error_text"] == error:
                return False
            connection.execute(
                """UPDATE batch_items SET status = 'FAILED', error_text = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE batch_item_id = ?""",
                (error, item["batch_item_id"]),
            )
        return True

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
                """INSERT INTO batch_jobs(batch_job_id, version_id, provider, profile_name, is_sample)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    child_id,
                    parent["version_id"],
                    parent["provider"],
                    parent["profile_name"],
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

    def list_recoverable_jobs(self, provider: str) -> list[str]:
        return [
            row[0]
            for row in self._store._connection()
            .execute(
                """SELECT batch_job_id FROM batch_jobs
                   WHERE provider = ? AND status IN ('SUBMITTED', 'RUNNING')
                   ORDER BY created_at, batch_job_id""",
                (provider,),
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
        is_sample: bool = False,
        batch_job_id: str | None = None,
    ) -> str:
        return self._repository.create_draft(
            version_id=version_id,
            provider=provider,
            profile_name=profile_name,
            is_sample=is_sample,
            items=items,
            batch_job_id=batch_job_id,
        )

    def get_job(self, batch_job_id: str) -> dict[str, Any]:
        """Read durable job metadata without exposing request bodies or credentials."""
        return self._repository.get_job(batch_job_id)

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

    def poll_and_ingest(self, batch_job_id: str, provider: BatchProvider) -> dict[str, int | str]:
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
            for result in provider.fetch_results(str(provider_job_id)):
                if not result.custom_id:
                    raise BatchPayloadError("provider result custom_id cannot be empty")
                if (result.payload is None) == (result.error is None):
                    raise BatchPayloadError("provider result needs exactly one of payload or error")
                if result.error is not None:
                    failed += int(self._repository.record_item_failure(batch_job_id, result.custom_id, result.error))
                    continue
                assert result.payload is not None
                try:
                    added += int(self._repository.ingest_success(batch_job_id, result.custom_id, result.payload))
                except BatchPayloadError as exc:
                    failed += int(self._repository.record_item_failure(batch_job_id, result.custom_id, str(exc)))
        return {"job_id": batch_job_id, "state": state, "ingested": added, "failed": failed}

    def recover(self, provider: BatchProvider) -> list[dict[str, int | str]]:
        """Poll all non-terminal work after a process restart; safe to repeat."""
        return [self.poll_and_ingest(job_id, provider) for job_id in self._repository.list_recoverable_jobs(provider.name)]

    def retry_failed_items(self, batch_job_id: str) -> str:
        """Return an idempotently-created successor job; submit it normally."""
        return self._repository.create_retry_child(batch_job_id)
