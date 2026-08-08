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
from typing import Any, Iterable, Mapping, NamedTuple, Protocol, Sequence
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


class DurableSuccessRetained(BatchPayloadError):
    """A re-poll re-rejected an item that had already succeeded durably.

    Refusing to demote a stored success is deliberate: a later output can never
    replace one, and an ingest contract can tighten after an item was accepted
    -- an evidence floor introduced afterwards, for instance -- so a re-poll can
    legitimately re-reject work that was valid under the contract it was
    ingested under.  The stored success stands.

    This is a distinct type purely so ``poll_and_ingest`` can tell it apart from
    a real payload rejection and carry on with the remaining items.  Aborting
    the whole poll on it would make an already-succeeded item block every item
    after it, which is the opposite of what protecting the success is for.
    """


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
        prompt_profile: str | None = None,
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

    def list_jobs_without_prompt_profile(self) -> list[dict[str, Any]]: ...

    def first_item_request(self, batch_job_id: str) -> dict[str, Any] | None: ...

    def backfill_prompt_profile(self, batch_job_id: str, prompt_profile: str) -> bool: ...


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
# One message for every way the gate can refuse, so a refusal never tells a
# caller which of the four coordinates missed and therefore never becomes a
# probe for what has already been approved on this server.
_UNAPPROVED_SAMPLE_MESSAGE = (
    "creating a full OpenAI EPUB Batch requires an administrator-approved sample "
    "for the same version and job kind, with the same model profile and the same "
    "prompt profile; the sample must be SUCCEEDED with every item ingested, and a "
    "job whose prompt profile is unknown can neither be approved for nor created "
    "as a full run"
)
_RESULTS_PENDING_RETRIEVAL = "RESULTS_PENDING_RETRIEVAL"
_TERMINAL_MISSING_RESULT = "TERMINAL_WITHOUT_ITEM_RESULT"
# The three concept mention shapes, recognised per mention from the field set
# the model actually returned rather than from the job's profile.  All three
# stay live: v1-v6 requests replay from their persisted ``request_json``, and
# the approved v6 sample has to remain re-ingestable.  ``zh-glossary-v1`` to
# ``-v3`` ask for offsets and no anchors; ``-v4`` to ``-v6`` ask for both;
# ``-v7`` asks for anchors and no offsets, for the reason recorded on that
# profile - a model gets the offset pair right about one time in thirty-seven,
# and grounding re-derives it from the literal regardless.
_LEGACY_CONCEPT_MENTION_FIELDS = {"start_codepoint", "end_codepoint", "evidence"}
_GROUNDED_CONCEPT_MENTION_FIELDS = {
    "start_codepoint",
    "end_codepoint",
    "evidence",
    "context_before",
    "context_after",
}
_OFFSETLESS_CONCEPT_MENTION_FIELDS = {"evidence", "context_before", "context_after"}
_MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS = 48
# A section graph span names its own passage.  ``zh-section-graph-v1`` asks for
# offsets and no anchors; ``zh-section-graph-v2`` and ``-v3`` ask for anchors
# and no offsets.  Ingest accepts both, per span rather than per payload, so a stored
# v1 request still replays and a mixed response is not a special case.  Neither
# shape's offsets are trusted: v1's are re-derived exactly like v2's.
_SECTION_GRAPH_SPAN_FIELDS_V1 = {"passage_id", "start_codepoint", "end_codepoint", "evidence"}
_SECTION_GRAPH_SPAN_FIELDS_V2 = {"passage_id", "evidence", "context_before", "context_after"}

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
        # Historical only, and deliberately still here.  A sub-floor span used
        # to fail its whole item; it is now dropped from the payload during
        # grounding and counted in ``batch_items.skipped_short_evidence``, so no
        # code path raises this any more (SDD 4.2.2 point 6b).  The slug stays in
        # the vocabulary because it is *durable*: the completed full-book runs
        # persisted it in ``failure_diagnostics_json``, and removing it would
        # make ``_safe_failure_diagnostics`` re-read those rows as UNDIAGNOSED -
        # silently destroying the very measurement that justified this change.
        "EVIDENCE_TOO_SHORT",
        # Section graph only, and a strict subset of the case above: the
        # evidence is absent from the passage it names but is exactly a TOC
        # title this packet showed the model.  Conflating the two cost a whole
        # diagnosis cycle on the zh-section-graph-v2 sample - every failing
        # item read as "the model invented text", when most of them were the
        # model quoting a ``toc_path`` field the packet handed it and never
        # scoped.  The two need opposite fixes: this one is repaired by not
        # sending the field (and saying so), hallucination is not.
        "EVIDENCE_FROM_TOC_PATH",
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
        # The item's immutable passage could not be read back.  For a section
        # graph packet this also covers a span naming a passage_id that is not
        # in this EPUB version at all.
        "PASSAGE_UNAVAILABLE",
        # Section graph only: a concept's packet-local ID is missing, blank, or
        # reused.  Distinct from INVALID_SCHEMA because the local_id mechanism
        # is what makes relations expressible within one packet, and a model
        # that cannot keep those IDs unique needs a different prompt fix from
        # one that returns the wrong field set.
        "LOCAL_ID_INVALID",
        # Section graph only: a relation names a local_id that no concept in
        # this packet defined.  This is the interesting one - the model
        # described a real edge but hallucinated an endpoint, or dropped the
        # concept it was pointing at - and it costs the whole packet.
        "RELATION_ENDPOINT_UNRESOLVED",
        # Section graph only: the canonical store refused a fully grounded
        # relation (an unsupported predicate, or an endpoint with no mention in
        # this EPUB version).
        "RELATION_REJECTED",
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
        # Section graph packets carry many spans across many passages, so the
        # position of a rejection needs two more axes than a single-passage
        # concept result does.
        "relation_index",
        "relation_count",
        "evidence_index",
        "evidence_count",
        "local_concept_count",
    }
)
_UNDIAGNOSED_FAILURE_REASON = "UNDIAGNOSED"
_PROVIDER_ITEM_ERROR_DIAGNOSTICS: Mapping[str, Any] = {"reason": "PROVIDER_ITEM_ERROR"}
_TERMINAL_WITHOUT_RESULT_DIAGNOSTICS: Mapping[str, Any] = {"reason": "TERMINAL_WITHOUT_RESULT"}


class _GroundedPayload(NamedTuple):
    """One grounded result plus what the grounding pass removed from it.

    ``payload`` is what gets written and stored; ``skipped_short_evidence`` is
    how many spans the enforced evidence floor dropped on the way there.  The
    count cannot be recovered from ``payload`` afterwards - the dropped spans
    are exactly what is no longer in it - so it travels out with it and is
    persisted beside the result, the way ``skipped_self_relations`` already is.

    One counter covers both span kinds.  A dropped concept mention and a dropped
    relation-evidence span are the same defect with the same remedy - the
    instruction has to ask for a more distinctive citation - and both are
    removed by the same resolver, so splitting them would add a column that
    answers no question an operator actually asks and that is structurally
    always zero for a CONCEPT_MENTIONS job.
    """

    payload: dict[str, Any]
    skipped_short_evidence: int


class _SectionGraphWrite(NamedTuple):
    """What one section-graph packet's write skipped, per cause.

    Both counts are SDD 4.2.2 point 6 skips - 6a and 6c - and both are facts
    about the write rather than about the payload: concept resolution is a
    write, so neither is knowable during the read-only grounding pass and
    neither is recoverable from the stored response afterwards.

    They are separate columns rather than one total because they answer
    different questions.  ``skipped_self_relations`` says an administrator merged
    two endpoints after the model answered, and is expected to fall to zero as
    the graph settles.  ``skipped_ambiguous_concepts`` says the graph holds two
    concepts a passage names together, which is a standing property of an
    adjudicated graph and does not settle - the same collisions recur on every
    future book.  A single counter would let one mask the other.
    """

    skipped_self_relations: int
    skipped_ambiguous_concepts: int


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


def _packet_toc_titles(request_json: Any) -> frozenset[str]:
    """Recover the TOC strings one section-graph packet actually showed a model.

    The durable request row is the only honest source for this: it is what the
    provider was sent, so a title that is in it is a title the model could copy
    without inventing anything.  Reading it back here classifies a rejection
    without widening what is persisted - no new column, and the titles never
    leave this function's caller, which turns them into a slug and nothing else.

    Deliberately tolerant.  A request predating this code carries per-passage
    ``toc_path`` as well as the packet-level one and both are collected; a
    request that is not a section-graph packet at all yields an empty set and
    every span then classifies exactly as it did before.
    """
    if not isinstance(request_json, str) or not request_json:
        return frozenset()
    try:
        request = json.loads(request_json)
    except ValueError:
        return frozenset()
    body = request.get("body") if isinstance(request, Mapping) else None
    messages = body.get("messages") if isinstance(body, Mapping) else None
    if not isinstance(messages, list):
        return frozenset()
    titles: set[str] = set()
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            _collect_toc_titles(json.loads(content), titles)
        except ValueError:
            continue
    return frozenset(title for title in titles if title)


def _collect_toc_titles(value: Any, titles: set[str]) -> None:
    """Gather every ``toc_path`` element in a decoded packet user message."""
    if isinstance(value, Mapping):
        path = value.get("toc_path")
        if isinstance(path, (list, tuple)):
            titles.update(part.strip() for part in path if isinstance(part, str))
        for nested in value.values():
            _collect_toc_titles(nested, titles)
    elif isinstance(value, list):
        for nested in value:
            _collect_toc_titles(nested, titles)


def _resolve_evidence_span(
    content: str,
    *,
    evidence: str,
    before: str,
    after: str,
    has_anchors: bool,
    start: int | None,
    end: int | None,
    position: Mapping[str, Any],
    toc_titles: frozenset[str] = frozenset(),
    evidence_floor: int = 0,
) -> tuple[int, int] | None:
    """Locate ``evidence`` in one immutable passage and return its exact span.

    This is the single span-resolution implementation for every cloud ingest
    path.  Concept mentions and section-graph mentions and relation evidence
    all reach the source through here, because two copies of fidelity-critical
    code is exactly how the section-graph path came to have no repair at all.

    Measured against four cloud samples, a model supplies a correct code-point
    offset roughly one time in thirty-seven while naming the right text almost
    every time.  The literal is therefore the evidence and the offset is
    derived, never trusted.  ``start``/``end`` are ``None`` for a payload shape
    that does not ask the model for offsets at all; when they are supplied they
    are still verified, per SDD 4.2.1.

    ``toc_titles`` changes no outcome: an unlocatable span is rejected either
    way.  It only splits the rejection's *slug*, because "quoted a navigational
    field we sent it" and "invented fluent prose" are the same symptom with
    opposite fixes.  The concept path passes none, because a single-passage
    concept request contains no TOC string to quote.

    ``evidence_floor`` is the minimum evidence length, in code points, that
    ingest enforces for this job's prompt profile; ``0`` for a profile that has
    none, which is every profile predating the clause.  It is deliberately
    *lower* than the length that profile's instruction asks the model for - see
    the profile dataclasses, which name the two numbers apart - because the
    request encourages a substantive citation while this floor rejects only what
    is genuinely unusable.  It is a parameter rather than a constant on purpose,
    twice over.  It is a property of the profile: ``zh-glossary-v1`` to ``-v5``
    and ``zh-section-graph-v1`` never asked for a minimum and their samples are
    still replayable, so enforcing one on them would retroactively invalidate
    output that honoured its own contract.  And it is injected rather than
    looked up: this module deliberately does not import an extraction-policy
    module, so the value arrives from the caller that legitimately knows
    profiles.  See ``SQLiteBatchRepository``.

    Returns ``None`` - and only for a sub-floor span - to tell the caller to
    drop this span from the payload rather than to fail the item.  Every other
    rejection raises ``BatchPayloadError`` carrying content-free diagnostics,
    because every other rejection means the model's output is not grounded in
    the source at all.  A sub-floor span is grounded; it is merely too small to
    be a useful citation, and one of those must not discard the valid concepts,
    mentions and relations that arrived beside it.  See SDD 4.2.2 point 6b.
    """
    passage_codepoints = len(content)
    evidence_codepoints = len(evidence)
    # ``passage_codepoints`` deliberately belongs to the caller's ``position``
    # rather than here, so a rejection raised before this helper runs still
    # reports it and no key is contributed twice.
    shape: dict[str, Any] = {
        "has_anchors": has_anchors,
        "evidence_codepoints": evidence_codepoints,
        "anchor_before_codepoints": len(before),
        "anchor_after_codepoints": len(after),
    }

    # Pure expressions over already-final values, evaluated before the scan so
    # every rejection can report whether the model's own offsets were even in
    # range.  Grounding still consults them only after the scan.
    offsets_supplied = isinstance(start, int) and isinstance(end, int)
    direct_offsets_in_range = bool(offsets_supplied and 0 <= start < end <= passage_codepoints)
    direct_is_exact = direct_offsets_in_range and content[start:end] == evidence
    # A shape that never asks for offsets reports neither flag rather than
    # reporting both as false: "not measurable here" and "measured false" are
    # different readings, and an aggregate that conflates them would show a
    # fleet-wide collapse in offset accuracy the moment such a profile is
    # promoted.  ``_grounding_diagnostics`` drops ``None`` for exactly this.
    direct: dict[str, Any] = {
        "direct_offsets_in_range": direct_offsets_in_range if offsets_supplied else None,
        "direct_is_exact": direct_is_exact if offsets_supplied else None,
    }

    # The evidence floor, applied before the source is scanned at all.
    #
    # Ordering is deliberate.  A sub-floor citation is overwhelmingly likely to
    # be ambiguous as well - a single character repeats everywhere - so a floor
    # checked after the scan would classify most of the very population it
    # exists to remove as an ambiguity failure, and would fail the whole item
    # for it.  Checking first also costs nothing: both operands are already
    # known.
    #
    # Dropping rather than raising is the point.  A span this short is genuine
    # source text; it is simply too small to locate anything for a reader, so it
    # is not worth storing and is worth nothing else either.  Failing the item
    # over it discards everything that arrived with it, which is what the
    # section-graph run measured: 13 of 43 packets lost on this alone, taking
    # 140 concepts, 140 mentions and 105 relations with them, against 184
    # relations actually ingested.  The caller drops the span and keeps going;
    # what cascades from the drop is the caller's rule, not this function's.
    #
    # The escape hatch.  The instruction that names a minimum also says that a
    # passage shorter than it is quoted in full, so evidence equal to the entire
    # passage is compliant however short it is.  This is not a rare allowance:
    # of the twenty sampled passages, eight are at or below the requested 10
    # code points and two are 9, so a floor without the hatch would drop
    # legitimate output.  ``evidence == content`` is the whole test, and is
    # exactly equivalent to the instruction's wording - a passage at or above
    # the floor cannot equal a sub-floor evidence string.
    if evidence_floor > 0 and evidence_codepoints < evidence_floor and evidence != content:
        return None

    occurrences: list[int] = []
    cursor = content.find(evidence)
    while cursor >= 0:
        occurrences.append(cursor)
        # Advance one code point so overlapping literals cannot be
        # misclassified as a unique source occurrence.
        cursor = content.find(evidence, cursor + 1)
    if not occurrences:
        # Exact equality against the strings the packet actually carried, not a
        # substring or fuzzy test: a false attribution here would send a prompt
        # author to fix a field that was never quoted.
        from_toc_path = evidence.strip() in toc_titles
        raise BatchPayloadError(
            "OpenAI evidence quotes a TOC title instead of the passage it names"
            if from_toc_path
            else "OpenAI evidence is absent from the immutable source",
            diagnostics=_grounding_diagnostics(
                "EVIDENCE_FROM_TOC_PATH" if from_toc_path else "EVIDENCE_ABSENT",
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

    # Reachable only for a shape that asks for offsets - ``zh-glossary-v1`` to
    # ``-v6`` and ``zh-section-graph-v1``.  Where no offsets are supplied
    # ``direct_is_exact`` is false by construction, so ANCHOR_MISMATCH cannot
    # occur; the branch stays because those older shapes still replay from
    # stored requests, and retiring the failure class is one of the reasons for
    # dropping the offsets in the first place.
    #
    # Deliberate asymmetry with the repair path below, and not an inconsistency
    # to "fix": when the model's own offsets already slice the evidence exactly,
    # SDD 4.2.1 requires that direct offset to be verified against *both* the
    # source and the anchors the model supplied.  A claim that is internally
    # contradictory - correct offsets, but adjacent text the model says is
    # something else - is not evidence of a located mention.  Below, by
    # contrast, the offsets are already known to be wrong or absent and the
    # anchors are only a disambiguation device.
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
    if direct_is_exact:
        assert isinstance(start, int) and isinstance(end, int)
        return start, end

    # The anchors exist for exactly one purpose: to choose among repeated
    # occurrences of the same literal.  A literal that occurs once is
    # self-verifying - the derived span is the only slice of the immutable
    # source that can equal this evidence - so a wrong anchor is a defect in
    # the model's description of a mention it nonetheless located, not a reason
    # to discard the one valid occurrence.  SDD 4.2.1 scopes the anchor filter
    # to repeated evidence and separately sanctions unique-literal repair;
    # filtering here would also contradict the sibling ANCHOR_MISSING check
    # above, which is already scoped to len(occurrences) > 1.
    candidates = occurrences
    if has_anchors and len(occurrences) > 1:
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
    resolved = candidates[0]
    return resolved, resolved + evidence_codepoints


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

    ``evidence_floors`` maps a prompt profile identifier to the minimum evidence
    length, in Unicode code points, that ingest *enforces* for that profile.
    That is deliberately not the length the profile's instruction asks the model
    for, which is higher; the profile dataclasses name and justify the two
    numbers separately, and only the enforced one is any of this module's
    business.  It is injected rather than imported: this module recognises a
    payload shape from the fields a model returned and must keep doing so
    without depending on an extraction-policy module, so the mapping is supplied
    by the service layer, which legitimately knows profiles.  A flat mapping is enough
    for both job kinds because the two profile namespaces are disjoint
    (``zh-glossary-*`` and ``zh-section-graph-*``), and a job records exactly
    one of them in ``batch_jobs.prompt_profile``.

    Omitting it, or naming a profile it does not list, yields a floor of ``0``
    and therefore the pre-existing behaviour.  That default is the replay
    guarantee, not an oversight: a job predating ``batch_jobs.prompt_profile``
    has NULL there and must stay ingestable on the contract it was actually
    given, and so must a profile that never asked for a minimum.
    """

    def __init__(self, store: Any, *, evidence_floors: Mapping[str, int] | None = None):
        if not hasattr(store, "_connection") or not hasattr(store, "_write"):
            raise TypeError("SQLiteBatchRepository requires the canonical SQLite EPUB store")
        self._store = store
        self._evidence_floors: dict[str, int] = {
            str(profile_id): max(0, int(floor))
            for profile_id, floor in dict(evidence_floors or {}).items()
        }
        self._migrate_service_tables()

    def _evidence_floor(self, prompt_profile: Any) -> int:
        """Resolve one job's evidence floor from its recorded prompt profile.

        Deliberately total: an unrecorded profile (NULL, from a job created
        before migration 6) and an unlisted one both resolve to ``0``, so a
        stored request can never become un-ingestable because this adapter was
        constructed without a mapping.
        """
        if not isinstance(prompt_profile, str):
            return 0
        return self._evidence_floors.get(prompt_profile, 0)

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
            # An approval must state on its face which extraction instruction
            # it approved, not merely which model snapshot.  This is a plain
            # identifier copied from the reviewed job, never instruction text.
            # ``CREATE TABLE IF NOT EXISTS`` cannot add a column to a table an
            # earlier release already created, so the column is added
            # separately and idempotently.  It is nullable for the same reason
            # ``batch_jobs.prompt_profile`` is: an approval recorded before
            # this change has no value, and the gate reads the job rather than
            # this audit copy, so a NULL here can never widen it.
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(epub_batch_sample_reviews)")
            }
            if "prompt_profile" not in columns:
                connection.execute(
                    "ALTER TABLE epub_batch_sample_reviews ADD COLUMN prompt_profile TEXT"
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
        prompt_profile: str | None = None,
    ) -> str:
        if not provider.strip() or not profile_name.strip():
            raise BatchServiceError("provider and profile_name cannot be empty")
        if job_kind not in _JOB_KINDS:
            raise BatchServiceError(f"unsupported EPUB Batch job kind: {job_kind}")
        # A blank identifier is not "no profile": it would be a second value
        # that compares equal to itself and could quietly satisfy the gate.
        # Reject it, and keep exactly one representation of "unknown": NULL.
        if prompt_profile is not None:
            prompt_profile = prompt_profile.strip()
            if not prompt_profile:
                raise BatchServiceError("Batch prompt_profile cannot be blank")
            if len(prompt_profile) > 100:
                raise BatchServiceError("Batch prompt_profile identifier is too long")
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
                    and existing["prompt_profile"] == prompt_profile
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
                    prompt_profile=prompt_profile,
                )
            connection.execute(
                """INSERT INTO batch_jobs(
                       batch_job_id, version_id, provider, profile_name, job_kind,
                       prompt_profile, is_sample
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    version_id,
                    provider,
                    profile_name,
                    job_kind,
                    prompt_profile,
                    int(is_sample),
                ),
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
        connection: Any,
        *,
        version_id: str,
        job_kind: str,
        profile_name: str,
        prompt_profile: str | None,
    ) -> None:
        """Keep the cloud quality gate inside the durable creation transaction.

        An OpenAI Batch can reach the provider's successful terminal state
        while an individual item fails schema validation or ingestion.  A full
        job is therefore permitted only after an administrator approves a
        sample with the same pinned model profile *and the same prompt
        profile* whose every item was durably ingested.

        ``profile_name`` pins the model snapshot; ``prompt_profile`` pins the
        extraction instruction.  Binding only the former means an approval of
        one instruction unlocks a full book on any other instruction sent to
        the same model -- the reviewed quality says nothing about the
        unreviewed prompt.

        An unknown prompt profile on either side is never a match.  The
        requested side is refused outright below; the stored side falls out of
        SQL three-valued logic, since ``job.prompt_profile = ?`` is NULL rather
        than true for a row that has no recorded profile.  The explicit check
        exists so the rule does not rest on that subtlety alone.
        """
        if prompt_profile is None:
            raise BatchServiceError(_UNAPPROVED_SAMPLE_MESSAGE)
        approved = connection.execute(
            """SELECT review.sample_batch_job_id
                 FROM epub_batch_sample_reviews AS review
                 JOIN batch_jobs AS job ON job.batch_job_id = review.sample_batch_job_id
                WHERE review.version_id = ?
                  AND review.job_kind = ?
                  AND review.status = 'APPROVED'
                  AND job.provider = 'openai-batch'
                  AND job.profile_name = ?
                  AND job.prompt_profile IS NOT NULL
                  AND job.prompt_profile = ?
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
            (version_id, job_kind, profile_name, prompt_profile),
        ).fetchone()
        if approved is None:
            raise BatchServiceError(_UNAPPROVED_SAMPLE_MESSAGE)

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

        ``skipped_self_relations``, ``skipped_short_evidence`` and
        ``skipped_ambiguous_concepts`` are exposed for the same reason and are
        safe for a stronger one: each column can only hold an integer or NULL,
        so none of them needs a read-side validator to be sure it is
        content-free.  All three are aggregated per job as well, because a
        succeeded item is exactly the one an administrator never opens - a skip
        that only a stored result knew about would be silent in practice, and a
        dropped span is not even in the stored result.
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
        # SUM over three integer-or-NULL columns: three scalars, no JSON to
        # parse, and no response ever loaded to produce them.
        skips = connection.execute(
            """SELECT COALESCE(SUM(skipped_self_relations), 0),
                      COALESCE(SUM(skipped_short_evidence), 0),
                      COALESCE(SUM(skipped_ambiguous_concepts), 0)
               FROM batch_items WHERE batch_job_id = ?""",
            (row["batch_job_id"],),
        ).fetchone()
        skipped_self_relations = int(skips[0])
        skipped_short_evidence = int(skips[1])
        skipped_ambiguous_concepts = int(skips[2])
        summary: dict[str, Any] = {
            "batch_job_id": row["batch_job_id"],
            "version_id": row["version_id"],
            "provider": row["provider"],
            "provider_job_id": row["provider_job_id"],
            "profile_name": row["profile_name"],
            # The extraction instruction identifier only, never its text.  An
            # administrator has to be able to see which prompt a job actually
            # used, because that is now what the approval gate binds to.
            "prompt_profile": row["prompt_profile"],
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
            # Relations this job's succeeded items ingested past because both
            # endpoints had been merged into one concept (SDD 4.2.2 point 6a).
            "item_skipped_self_relations": skipped_self_relations,
            # Spans this job's succeeded items dropped for being below the
            # enforced evidence floor (SDD 4.2.2 point 6b).  Concept mentions and
            # relation evidence share one counter: same defect, same fix.
            "item_skipped_short_evidence": skipped_short_evidence,
            # Concepts this job's succeeded items skipped because their
            # spellings matched several existing concepts (SDD 4.2.2 point 6c).
            # Counts the concepts, not the relations that cascaded off them, for
            # the same reason as the counter above: the cause is what is
            # unrecoverable, and a cascade is derivable from the stored payload.
            "item_skipped_ambiguous_concepts": skipped_ambiguous_concepts,
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
                    # None where nothing was measured: a CONCEPT_MENTIONS item,
                    # an item that has not succeeded, or a row that predates the
                    # column.
                    "skipped_self_relations": (
                        None
                        if item["skipped_self_relations"] is None
                        else int(item["skipped_self_relations"])
                    ),
                    # None where no grounding pass ran: an item that has not
                    # succeeded, one whose provider output is not grounded here,
                    # or a row that predates the column.
                    "skipped_short_evidence": (
                        None
                        if item["skipped_short_evidence"] is None
                        else int(item["skipped_short_evidence"])
                    ),
                    # None only where the column predates the row: unlike the
                    # two above, every succeeded item resolves concepts, so a
                    # measured zero here is always a real zero.
                    "skipped_ambiguous_concepts": (
                        None
                        if item["skipped_ambiguous_concepts"] is None
                        else int(item["skipped_ambiguous_concepts"])
                    ),
                    "updated_at": item["updated_at"],
                }
                for item in connection.execute(
                    """SELECT batch_item_id, passage_id, custom_id, status, attempt_count,
                              response_json, error_text, failure_diagnostics_json,
                              skipped_self_relations, skipped_short_evidence,
                              skipped_ambiguous_concepts, updated_at
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
                       sample_batch_job_id, version_id, job_kind, prompt_profile,
                       status, reviewed_by, reviewed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(sample_batch_job_id) DO UPDATE SET
                       prompt_profile = excluded.prompt_profile,
                       status = excluded.status,
                       reviewed_by = excluded.reviewed_by,
                       reviewed_at = CURRENT_TIMESTAMP""",
                (
                    batch_job_id,
                    job["version_id"],
                    job["job_kind"],
                    # Identifier copied from the job under review, so the audit
                    # row states which extraction instruction was approved
                    # rather than only which model snapshot.
                    job["prompt_profile"],
                    status,
                    reviewer,
                ),
            )
            row = connection.execute(
                """SELECT sample_batch_job_id, version_id, job_kind, prompt_profile,
                          status, reviewed_by, reviewed_at
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
                       review.prompt_profile, review.status, review.reviewed_by,
                       review.reviewed_at, job.status AS batch_status
                  FROM epub_batch_sample_reviews AS review
                  JOIN batch_jobs AS job ON job.batch_job_id = review.sample_batch_job_id
                  {where}
                 ORDER BY review.reviewed_at DESC, review.sample_batch_job_id DESC""",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_jobs_without_prompt_profile(self) -> list[dict[str, Any]]:
        """List identifier-only descriptors of jobs predating the column.

        Deriving the missing value needs the registered extraction policy,
        which this layer must not import, so the persistence half of the
        backfill is split from the matching half: this returns what to look
        at, the service decides what it is.
        """
        return [
            {
                "batch_job_id": row["batch_job_id"],
                "version_id": row["version_id"],
                "job_kind": row["job_kind"],
                "is_sample": bool(row["is_sample"]),
            }
            for row in self._store._connection().execute(
                """SELECT batch_job_id, version_id, job_kind, is_sample FROM batch_jobs
                   WHERE prompt_profile IS NULL
                   ORDER BY created_at, batch_job_id"""
            )
        ]

    def first_item_request(self, batch_job_id: str) -> dict[str, Any] | None:
        """Return one stored request envelope for server-side inspection only.

        Unlike every operator-facing accessor on this class, the value can
        contain the passage that was sent, so it must never be returned to a
        caller outside the server.  Its only in-tree use is the prompt-profile
        backfill, which reads the system instruction, matches it against the
        registered profiles, and keeps nothing but the resulting identifier.
        """
        row = self._store._connection().execute(
            """SELECT request_json FROM batch_items
               WHERE batch_job_id = ? ORDER BY custom_id LIMIT 1""",
            (batch_job_id,),
        ).fetchone()
        if row is None:
            return None
        request = json.loads(row["request_json"])
        return request if isinstance(request, dict) else None

    def backfill_prompt_profile(self, batch_job_id: str, prompt_profile: str) -> bool:
        """Record a derived prompt profile on a job that has none.

        This only ever fills a NULL.  A stored profile is what the gate binds
        to, so overwriting one would silently re-point an existing approval;
        the ``IS NULL`` predicate makes that impossible even if a caller asks
        for it, and makes a repeated backfill a no-op.

        The matching approval row, if any, is completed the same way and under
        the same restriction.  That is the same derivation applied to the audit
        copy of the same fact -- no decision, reviewer or timestamp changes.
        """
        if not prompt_profile.strip():
            raise BatchServiceError("Batch prompt_profile cannot be blank")
        with self._store._write() as connection:
            self._require_job(connection, batch_job_id)
            updated = connection.execute(
                """UPDATE batch_jobs SET prompt_profile = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE batch_job_id = ? AND prompt_profile IS NULL""",
                (prompt_profile, batch_job_id),
            ).rowcount
            connection.execute(
                """UPDATE epub_batch_sample_reviews SET prompt_profile = ?
                   WHERE sample_batch_job_id = ? AND prompt_profile IS NULL""",
                (prompt_profile, batch_job_id),
            )
        return bool(updated)

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
    def _resolve_or_create_concept(connection: Any, suggestion: Mapping[str, Any]) -> str | None:
        """Resolve one suggested concept, or report that it is unresolvable.

        Returns ``None`` — rather than raising — when the suggestion's spellings
        match more than one existing concept.  Linking it would assert that
        those concepts are the same, which SDD 4.2 forbids a model from doing,
        so the caller skips the concept and counts it (SDD 4.2.2 point 6c).

        This was a hard failure until the full-book runs measured what that
        cost.  It held 33 items, and 32 of them collided on pairs an
        administrator had already adjudicated as *distinct*, which no merge can
        resolve without reversing the adjudication — and a model will keep
        proposing them, because the source text genuinely uses both spellings in
        one passage.  So the item was not being held for review; it was being
        discarded, along with every valid concept and mention that arrived
        beside the collision.

        Returning ``None`` rather than raising keeps the decision at the call
        site.  A resolver that skipped silently would look identical to one that
        resolved, and the count is the only durable record that the concept was
        ever proposed.
        """
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
            return None
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
        connection: Any, *, item: Any, payload: Mapping[str, Any], evidence_floor: int = 0
    ) -> _GroundedPayload:
        """Canonically ground a cloud concept payload against immutable text.

        OpenAI Structured Outputs constrains the response shape, but a model
        can still count Unicode code points incorrectly.  A bad numeric offset
        is never trusted: it can be repaired only from an exact evidence string
        whose literal occurrence is unique, or - when that literal repeats -
        whose short adjacent context anchor selects exactly one of those
        occurrences.  Legacy v1-v3 output carries no anchors, so for it a
        repeated literal is always a hard failure.  ``zh-glossary-v7`` output
        carries no offsets, which changes nothing about the resolution: the
        offset was already derived from the literal on every shape, so the only
        difference is that there is no wrong number to ignore.

        The shape is read per mention from the returned field set, never from
        the job's profile.  That is what lets one poll ingest a mixed response,
        keeps every stored v1-v6 request replayable, and keeps the approved v6
        sample re-ingestable after v7 becomes the default.

        Every rejection below also carries content-free diagnostics.  A failed
        item persists no result payload at all, so without them the durable
        record would retain only a failure class, and a prompt author could not
        tell "the model paraphrased" from "the evidence was two code points long
        and occurred forty-seven times".  Only counts and flags are attached;
        see :func:`_grounding_diagnostics`.

        ``evidence_floor`` is this job's enforced profile minimum, resolved by
        the caller from ``batch_jobs.prompt_profile``; it is passed straight
        through to the shared resolver, which owns the rule and its
        short-passage escape hatch.  A span below it is *dropped* rather than
        rejected, and the cascade is deliberate: a concept left with no mentions
        is itself dropped, because the contract requires a concept to carry at
        least one, and a payload reduced to no concepts at all is still a
        success - ``{"concepts": []}`` is a valid response and always has been.
        Returns the surviving payload and how many spans were dropped.
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
        skipped_short_evidence = 0
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
                if fields not in (
                    _LEGACY_CONCEPT_MENTION_FIELDS,
                    _GROUNDED_CONCEPT_MENTION_FIELDS,
                    _OFFSETLESS_CONCEPT_MENTION_FIELDS,
                ):
                    raise BatchPayloadError(
                        "OpenAI concept mention has an invalid schema",
                        diagnostics=_grounding_diagnostics("INVALID_SCHEMA", **position),
                    )
                has_anchors = fields != _LEGACY_CONCEPT_MENTION_FIELDS
                supplies_offsets = fields != _OFFSETLESS_CONCEPT_MENTION_FIELDS
                # ``None`` for a v7 mention, and passed through as ``None``:
                # ``_resolve_evidence_span`` treats absent offsets as "derive
                # the span from the literal", which is what it does with a
                # supplied offset 97% of the time anyway.
                start = mention.get("start_codepoint")
                end = mention.get("end_codepoint")
                evidence = mention.get("evidence")
                if not isinstance(evidence, str) or not evidence or (
                    supplies_offsets
                    and (
                        isinstance(start, bool)
                        or isinstance(end, bool)
                        or not isinstance(start, int)
                        or not isinstance(end, int)
                    )
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
                resolved = _resolve_evidence_span(
                    content,
                    evidence=evidence,
                    before=before,
                    after=after,
                    has_anchors=has_anchors,
                    start=start,
                    end=end,
                    position=position,
                    evidence_floor=evidence_floor,
                )
                if resolved is None:
                    # Sub-floor: too small to cite, but everything else this
                    # concept and this response carried is untouched.
                    skipped_short_evidence += 1
                    continue
                start, end = resolved
                normalized = dict(mention)
                normalized["start_codepoint"] = start
                normalized["end_codepoint"] = end
                grounded_mentions.append(normalized)
            if not grounded_mentions:
                # Every mention this concept had was sub-floor.  A concept with
                # no mention is not something the contract can express - it is
                # an unevidenced term, which is precisely what MENTIONS_MISSING
                # rejects when a model returns one - so the concept goes with
                # its citations rather than being stored unanchored.
                continue
            grounded = dict(concept)
            grounded["mentions"] = grounded_mentions
            grounded_concepts.append(grounded)
        return _GroundedPayload({"concepts": grounded_concepts}, skipped_short_evidence)

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

    @staticmethod
    def _ground_section_graph_span(
        connection: Any,
        *,
        version_id: str,
        span: Any,
        position: Mapping[str, Any],
        toc_titles: frozenset[str] = frozenset(),
        evidence_floor: int = 0,
    ) -> dict[str, Any] | None:
        """Return one packet span as an exact slice of the passage it names.

        Returns ``None`` where the shared resolver dropped the span for being
        below the enforced evidence floor; the caller decides what that costs.

        A concept mention and a relation evidence span are the same shape and
        are grounded identically; only ``position`` differs, so an operator can
        still tell which of the two failed.  That is also why the evidence floor
        needs no separate implementation for relations: both reach the source
        through this one function and then through the one shared resolver.  The returned span is canonicalized
        to the four fields the store writes: the anchors are a device for
        choosing among repeated literals, and once the occurrence is chosen the
        derived offset is the fact.  Storing the resolved span rather than the
        model's description is also what makes re-ingesting the same packet
        byte-identical, and therefore idempotent.
        """
        if not isinstance(span, Mapping) or (
            set(span) != _SECTION_GRAPH_SPAN_FIELDS_V1
            and set(span) != _SECTION_GRAPH_SPAN_FIELDS_V2
        ):
            raise BatchPayloadError(
                "section graph evidence span has an invalid schema",
                diagnostics=_grounding_diagnostics("INVALID_SCHEMA", **position),
            )
        has_anchors = set(span) == _SECTION_GRAPH_SPAN_FIELDS_V2
        passage_id = span["passage_id"]
        evidence = span["evidence"]
        if (
            not isinstance(passage_id, str)
            or not passage_id
            or not isinstance(evidence, str)
            or not evidence
        ):
            raise BatchPayloadError(
                "section graph evidence span has invalid offsets or evidence",
                diagnostics=_grounding_diagnostics(
                    "INVALID_OFFSETS",
                    has_anchors=has_anchors,
                    evidence_codepoints=len(evidence) if isinstance(evidence, str) else None,
                    **position,
                ),
            )
        # Version-scoped: a packet may name any passage it was shown, but never
        # a passage from another book or another version of this one.
        passage = connection.execute(
            "SELECT content FROM passages WHERE passage_id = ? AND version_id = ?",
            (passage_id, version_id),
        ).fetchone()
        if passage is None:
            raise BatchPayloadError(
                "section graph evidence does not belong to this EPUB version",
                diagnostics=_grounding_diagnostics(
                    "PASSAGE_UNAVAILABLE",
                    has_anchors=has_anchors,
                    evidence_codepoints=len(evidence),
                    **position,
                ),
            )
        content = passage["content"]
        span_position = {**position, "passage_codepoints": len(content)}
        start: int | None = None
        end: int | None = None
        before = after = ""
        if has_anchors:
            before = span["context_before"]
            after = span["context_after"]
            if (
                not isinstance(before, str)
                or not isinstance(after, str)
                or len(before) > _MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS
                or len(after) > _MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS
            ):
                raise BatchPayloadError(
                    "section graph evidence context anchor is invalid",
                    diagnostics=_grounding_diagnostics(
                        "ANCHOR_INVALID",
                        has_anchors=True,
                        evidence_codepoints=len(evidence),
                        anchor_before_codepoints=len(before) if isinstance(before, str) else None,
                        anchor_after_codepoints=len(after) if isinstance(after, str) else None,
                        **span_position,
                    ),
                )
        else:
            start = span["start_codepoint"]
            end = span["end_codepoint"]
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
            ):
                raise BatchPayloadError(
                    "section graph evidence span has invalid offsets or evidence",
                    diagnostics=_grounding_diagnostics(
                        "INVALID_OFFSETS",
                        has_anchors=False,
                        evidence_codepoints=len(evidence),
                        **span_position,
                    ),
                )
        resolved = _resolve_evidence_span(
            content,
            evidence=evidence,
            before=before,
            after=after,
            has_anchors=has_anchors,
            start=start,
            end=end,
            position=span_position,
            toc_titles=toc_titles,
            evidence_floor=evidence_floor,
        )
        if resolved is None:
            return None
        start, end = resolved
        return {
            "passage_id": passage_id,
            "start_codepoint": start,
            "end_codepoint": end,
            "evidence": evidence,
        }

    def _ground_section_graph_payload(
        self,
        connection: Any,
        *,
        version_id: str,
        payload: Mapping[str, Any],
        toc_titles: frozenset[str] = frozenset(),
        evidence_floor: int = 0,
    ) -> _GroundedPayload:
        """Validate and ground a whole packet without writing anything.

        Every span in the packet - concept mentions and relation evidence
        alike - goes through the shared resolver, so a section graph result is
        repaired exactly the way a concept result is instead of being rejected
        wholesale.  Both packet shapes are accepted: ``zh-section-graph-v1``
        supplies offsets, which are verified and re-derived rather than
        trusted, and ``zh-section-graph-v2`` and ``-v3`` supply none at all.

        ``toc_titles`` are the TOC strings this packet's stored request carried.
        They classify an unlocatable span - a section title quoted as evidence
        is a different defect from invented prose - and are used for nothing
        else; no title is stored, compared for idempotency, or written.

        ``evidence_floor`` applies to every span in the packet, mention and
        relation evidence alike, because both go through the same resolver.  A
        sub-floor span is dropped here rather than failing the packet, and the
        cascade runs to whatever the contract can no longer express:

        * a concept whose every mention was dropped is dropped, because a
          concept must carry at least one mention;
        * a relation whose every evidence span was dropped is dropped, because
          an assertion must name at least one exact span;
        * a relation whose subject or object was one of those dropped concepts
          is dropped, because the endpoint no longer exists to point at.  That
          last one is emphatically *not* RELATION_ENDPOINT_UNRESOLVED: the model
          defined the concept and named it correctly, and ingest is what removed
          it.  An endpoint naming a ``local_id`` the response never defined
          stays a hard failure, exactly as before, because that is genuinely
          ungrounded output.

        A packet reduced to nothing is still a success contributing nothing;
        empty concepts and relations arrays are a valid response.

        Evidence spans are grounded even for a relation already doomed by a
        dropped endpoint, so a genuinely ungrounded span inside it still fails
        the item.  Only the floor is lenient here; nothing else becomes so.

        This pass is deliberately read-only and complete.  Ingest is atomic per
        item and the enclosing transaction would roll back anyway, but doing
        every rejection before the first insert makes atomicity structural: an
        unresolvable relation endpoint discovered after nine concepts were
        written can never depend on the rollback to undo them.
        """
        if set(payload) != {"concepts", "relations"}:
            raise BatchPayloadError(
                "section graph output must contain only concepts and relations",
                diagnostics=_grounding_diagnostics("INVALID_SCHEMA"),
            )
        concepts = payload["concepts"]
        relations = payload["relations"]
        if not isinstance(concepts, list) or not isinstance(relations, list):
            raise BatchPayloadError(
                "section graph output needs concepts and relations lists",
                diagnostics=_grounding_diagnostics("INVALID_SCHEMA"),
            )
        concept_count = len(concepts)
        relation_count = len(relations)
        local_ids: set[str] = set()
        # Defined by the response but removed by the floor cascade.  Kept apart
        # from ``local_ids`` so uniqueness is still checked against every ID the
        # model declared, and so a relation pointing at one of these can be told
        # from a relation pointing at an ID that was never declared at all.
        dropped_local_ids: set[str] = set()
        skipped_short_evidence = 0
        grounded_concepts: list[dict[str, Any]] = []
        for concept_index, suggestion in enumerate(concepts):
            position: dict[str, Any] = {
                "concept_index": concept_index,
                "concept_count": concept_count,
            }
            if not isinstance(suggestion, Mapping):
                raise BatchPayloadError(
                    "section graph concepts must contain objects",
                    diagnostics=_grounding_diagnostics("INVALID_SCHEMA", **position),
                )
            if set(suggestion) != {"local_id", "name", "aliases", "definition", "mentions"}:
                raise BatchPayloadError(
                    "section graph concept has an invalid schema",
                    diagnostics=_grounding_diagnostics("INVALID_SCHEMA", **position),
                )
            local_id = suggestion["local_id"]
            if not isinstance(local_id, str) or not local_id.strip() or local_id in local_ids:
                raise BatchPayloadError(
                    "section graph local_id must be unique and non-empty",
                    diagnostics=_grounding_diagnostics(
                        "LOCAL_ID_INVALID", local_concept_count=len(local_ids), **position
                    ),
                )
            local_ids.add(local_id)
            mentions = suggestion["mentions"]
            if not isinstance(mentions, list) or not mentions:
                raise BatchPayloadError(
                    "section graph concept needs a visible mention",
                    diagnostics=_grounding_diagnostics(
                        "MENTIONS_MISSING",
                        mention_count=len(mentions) if isinstance(mentions, list) else None,
                        **position,
                    ),
                )
            grounded_mentions: list[dict[str, Any]] = []
            for mention_index, mention in enumerate(mentions):
                grounded_mention = self._ground_section_graph_span(
                    connection,
                    version_id=version_id,
                    span=mention,
                    position={
                        **position,
                        "mention_index": mention_index,
                        "mention_count": len(mentions),
                    },
                    toc_titles=toc_titles,
                    evidence_floor=evidence_floor,
                )
                if grounded_mention is None:
                    skipped_short_evidence += 1
                    continue
                grounded_mentions.append(grounded_mention)
            if not grounded_mentions:
                dropped_local_ids.add(local_id)
                continue
            grounded_concepts.append({**suggestion, "mentions": grounded_mentions})

        grounded_relations: list[dict[str, Any]] = []
        for relation_index, relation in enumerate(relations):
            position = {
                "relation_index": relation_index,
                "relation_count": relation_count,
                "local_concept_count": len(local_ids),
            }
            if not isinstance(relation, Mapping):
                raise BatchPayloadError(
                    "section graph relations must contain objects",
                    diagnostics=_grounding_diagnostics("INVALID_SCHEMA", **position),
                )
            if set(relation) != {"subject_local_id", "predicate", "object_local_id", "evidence"}:
                raise BatchPayloadError(
                    "section graph relation has an invalid schema",
                    diagnostics=_grounding_diagnostics("INVALID_SCHEMA", **position),
                )
            subject = relation["subject_local_id"]
            predicate = relation["predicate"]
            object_ = relation["object_local_id"]
            evidence = relation["evidence"]
            if not isinstance(subject, str) or not isinstance(object_, str) or subject == object_:
                raise BatchPayloadError(
                    "section graph relation needs two distinct local concept IDs",
                    diagnostics=_grounding_diagnostics("INVALID_SCHEMA", **position),
                )
            if subject not in local_ids or object_ not in local_ids:
                raise BatchPayloadError(
                    "section graph relation endpoint is not a packet concept",
                    diagnostics=_grounding_diagnostics(
                        "RELATION_ENDPOINT_UNRESOLVED", **position
                    ),
                )
            if predicate not in _RELATION_PREDICATES or not isinstance(evidence, list) or not evidence:
                raise BatchPayloadError(
                    "section graph relation predicate or evidence is invalid",
                    diagnostics=_grounding_diagnostics(
                        "INVALID_SCHEMA",
                        evidence_count=len(evidence) if isinstance(evidence, list) else None,
                        **position,
                    ),
                )
            grounded_evidence: list[dict[str, Any]] = []
            for evidence_index, span in enumerate(evidence):
                grounded_span = self._ground_section_graph_span(
                    connection,
                    version_id=version_id,
                    span=span,
                    position={
                        **position,
                        "evidence_index": evidence_index,
                        "evidence_count": len(evidence),
                    },
                    toc_titles=toc_titles,
                    evidence_floor=evidence_floor,
                )
                if grounded_span is None:
                    skipped_short_evidence += 1
                    continue
                grounded_evidence.append(grounded_span)
            # Grounded first, dropped second: a relation whose endpoint the
            # floor removed is still checked for ungrounded evidence, so
            # leniency stays confined to the floor.
            if subject in dropped_local_ids or object_ in dropped_local_ids:
                continue
            if not grounded_evidence:
                continue
            grounded_relations.append({**relation, "evidence": grounded_evidence})
        return _GroundedPayload(
            {"concepts": grounded_concepts, "relations": grounded_relations},
            skipped_short_evidence,
        )

    def _write_section_graph(
        self, connection: Any, *, version_id: str, item: Any, payload: Mapping[str, Any]
    ) -> _SectionGraphWrite:
        """Commit one already-grounded packet as a single transaction.

        Every span here is already an exact slice of its own immutable passage.
        The store's own equality checks still run: they are the last gate that
        keeps a stored citation byte-exact, and they cost one comparison.

        Returns the two counts this write produces, both of them cases of SDD
        4.2.2 point 6 — grounded output that a decision on our side made
        unusable — and both of them measurable only here.  Neither test can live
        in the read-only grounding pass, however much the rest of the packet's
        validation does: a ``local_id`` becomes a ``concept_id`` only through
        ``_resolve_or_create_concept``, which is a write.  They belong one step
        after resolution and before anything is offered to the store.  Nothing
        about the read-only pass weakens as a result - it still decides every
        *rejection* before the first insert, and a skip is not a rejection.

        * Relations whose two endpoints resolved to the same concept (point 6a).
          That is what an administrator merge looks like from the far side of an
          offline Batch, and failing the packet would discard valid concepts and
          mentions over an edge the administrator themselves collapsed.
          ``merge_concepts`` drops exactly this relation rather than refusing the
          merge; this keeps ingest consistent with it.
        * Concepts whose spellings matched several existing concepts (point 6c),
          which ingest cannot link without asserting a merge nobody decided.

        A relation that named a skipped concept is dropped with it, and is not
        counted separately: the cascade is derivable from the stored payload,
        the cause is not.  This mirrors the evidence floor, which counts the
        spans it removed and not the relations they left empty.  An endpoint
        naming a ``local_id`` the response never defined remains a different
        thing entirely, and is still rejected by the read-only pass before
        anything is written.
        """
        relations = payload["relations"]
        local_concepts: dict[str, str] = {}
        skipped_local_ids: set[str] = set()
        for suggestion in payload["concepts"]:
            local_id = str(suggestion["local_id"])
            concept_id = self._resolve_or_create_concept(connection, suggestion)
            if concept_id is None:
                # No concept to hang them on, so the mentions go too.  They stay
                # in the stored response, which is what the model returned.
                skipped_local_ids.add(local_id)
                continue
            self._add_mentions(
                connection,
                concept_id,
                item["passage_id"],
                suggestion["mentions"],
                version_id=version_id,
                require_passage_ids=True,
            )
            local_concepts[local_id] = concept_id
        skipped_self_relations = 0
        for relation_index, relation in enumerate(relations):
            subject_local_id = str(relation["subject_local_id"])
            object_local_id = str(relation["object_local_id"])
            if subject_local_id in skipped_local_ids or object_local_id in skipped_local_ids:
                continue
            # Both lookups are total once the skips are excluded: the grounding
            # pass already refused any endpoint that is not a packet concept, so
            # a KeyError here would mean that guard had been removed.
            subject_concept_id = local_concepts[subject_local_id]
            object_concept_id = local_concepts[object_local_id]
            if subject_concept_id == object_concept_id:
                skipped_self_relations += 1
                continue
            try:
                self._store._add_concept_relation(
                    connection,
                    version_id=version_id,
                    subject_concept_id=subject_concept_id,
                    predicate=str(relation["predicate"]),
                    object_concept_id=object_concept_id,
                    evidence=relation["evidence"],
                )
            except ValueError as exc:
                raise BatchPayloadError(
                    str(exc),
                    diagnostics=_grounding_diagnostics(
                        "RELATION_REJECTED",
                        relation_index=relation_index,
                        relation_count=len(relations),
                        local_concept_count=len(local_concepts),
                    ),
                ) from exc
        return _SectionGraphWrite(skipped_self_relations, len(skipped_local_ids))

    def ingest_success(self, batch_job_id: str, custom_id: str, payload: Mapping[str, Any]) -> bool:
        """Atomically ingest one model result and mark the durable item complete.

        Repeating byte-equivalent output is a no-op.  Different output for an
        already succeeded item is rejected rather than silently rewriting the
        graph, preserving reproducibility of an offline Batch run.  That is
        also why ``response_json`` stays exactly the grounded model output: a
        relation skipped as a merged-away self-relation is still part of what
        the model returned, so it stays in the stored result and a replay
        serializes byte-identically.  How many such relations the *write* then
        skipped is a fact about the write, and is recorded beside the result in
        ``batch_items.skipped_self_relations``, where ``_summary`` can show it
        to an administrator without ever exposing a response.

        A span dropped by the evidence floor is the mirror image: the grounding
        pass removes it, so it is *not* in the stored result, and re-ingesting
        the same provider output re-derives the identical grounded payload and
        therefore the identical serialization.  Its count is recorded the same
        way, in ``batch_items.skipped_short_evidence``, because it is likewise
        unrecoverable from what was stored.

        An ambiguous concept behaves like the self-relation rather than like the
        floor, and for the same reason: resolution is a write, so the grounding
        pass never sees it and the concept is still in the stored response
        verbatim.  Its count goes to ``batch_items.skipped_ambiguous_concepts``.
        """
        with self._store._write() as connection:
            job = self._require_job(connection, batch_job_id)
            item = self._item_for_update(connection, batch_job_id, custom_id)
            # Grounding runs before serialization so the durable response is the
            # graph that was actually written, not the model's description of
            # it.  A section graph packet is grounded whatever the provider,
            # because the resolver - not the provider - is what keeps a stored
            # span byte-exact.
            #
            # The floor comes from the job's own recorded prompt profile rather
            # than from whichever profile is currently the default, for the same
            # reason the mention shape does: a stored request replays under the
            # contract it was submitted with.
            evidence_floor = self._evidence_floor(job["prompt_profile"])
            # NULL rather than 0 where no grounding pass ran at all, so "not
            # measured" stays distinguishable from a measured zero.
            skipped_short_evidence: int | None = None
            if job["job_kind"] == "SECTION_GRAPH":
                payload, skipped_short_evidence = self._ground_section_graph_payload(
                    connection,
                    version_id=job["version_id"],
                    payload=payload,
                    # Read back from the request that was actually sent, so a
                    # rejection can name the field the model copied from.
                    toc_titles=_packet_toc_titles(item["request_json"]),
                    evidence_floor=evidence_floor,
                )
            elif job["provider"] == "openai-batch":
                payload, skipped_short_evidence = self._ground_openai_concept_payload(
                    connection, item=item, payload=payload, evidence_floor=evidence_floor
                )
            serialized = _canonical_json(payload)
            if item["status"] == "SUCCEEDED":
                if item["response_json"] == serialized:
                    return False
                raise BatchPayloadError("different output received for an already ingested Batch item")
            if item["status"] not in {"PENDING", "SUBMITTED", "RETRY", "FAILED"}:
                raise BatchPayloadError(f"cannot ingest output for item state {item['status']}")
            # NULL rather than 0 for a CONCEPT_MENTIONS item: it carries no
            # relations, so there is nothing to have skipped, and "not
            # measured" must stay distinguishable from a measured zero.
            skipped_self_relations: int | None = None
            # Measured on both job kinds, unlike the counter above: every
            # success resolves concepts, so a zero here is always a real zero
            # and NULL means only "written before this column existed".
            skipped_ambiguous_concepts = 0
            if job["job_kind"] == "SECTION_GRAPH":
                skipped_self_relations, skipped_ambiguous_concepts = self._write_section_graph(
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
                    if concept_id is None:
                        # SDD 4.2.2 point 6c.  The mentions are skipped with it:
                        # there is no concept to anchor them to, and inventing
                        # one would perform the merge the guard exists to refuse.
                        skipped_ambiguous_concepts += 1
                        continue
                    self._add_mentions(connection, concept_id, item["passage_id"], suggestion.get("mentions"))
            connection.execute(
                """UPDATE batch_items
                   SET status = 'SUCCEEDED', response_json = ?, error_text = NULL,
                       failure_diagnostics_json = NULL, skipped_self_relations = ?,
                       skipped_short_evidence = ?, skipped_ambiguous_concepts = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE batch_item_id = ?""",
                (
                    serialized,
                    skipped_self_relations,
                    skipped_short_evidence,
                    skipped_ambiguous_concepts,
                    item["batch_item_id"],
                ),
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
                raise DurableSuccessRetained(
                    "a succeeded Batch item cannot be overwritten as failed"
                )
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
                """INSERT INTO batch_jobs(
                       batch_job_id, version_id, provider, profile_name, job_kind,
                       prompt_profile, is_sample
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    child_id,
                    parent["version_id"],
                    parent["provider"],
                    parent["profile_name"],
                    parent["job_kind"],
                    # A retry replays the parent's own stored requests, so it
                    # is the same prompt profile by construction.  It does not
                    # re-enter the approval gate for the same reason: nothing
                    # new is being sent.
                    parent["prompt_profile"],
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
        prompt_profile: str | None = None,
    ) -> str:
        return self._repository.create_draft(
            version_id=version_id,
            provider=provider,
            profile_name=profile_name,
            job_kind=job_kind,
            is_sample=is_sample,
            items=items,
            batch_job_id=batch_job_id,
            prompt_profile=prompt_profile,
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

    def list_jobs_without_prompt_profile(self) -> list[dict[str, Any]]:
        return self._repository.list_jobs_without_prompt_profile()

    def first_item_request(self, batch_job_id: str) -> dict[str, Any] | None:
        """Server-internal accessor; the envelope must not reach a caller."""
        return self._repository.first_item_request(batch_job_id)

    def backfill_prompt_profile(self, batch_job_id: str, prompt_profile: str) -> bool:
        return self._repository.backfill_prompt_profile(batch_job_id, prompt_profile)

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
        added = failed = retained = 0
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
                    try:
                        failed += int(
                            self._repository.record_item_failure(
                                batch_job_id,
                                result.custom_id,
                                result.error,
                                _PROVIDER_ITEM_ERROR_DIAGNOSTICS,
                            )
                        )
                    except DurableSuccessRetained:
                        retained += 1
                    continue
                assert result.payload is not None
                try:
                    added += int(self._repository.ingest_success(batch_job_id, result.custom_id, result.payload))
                except DurableSuccessRetained:
                    # ``ingest_success`` itself refuses to replace a stored
                    # success with a different output; nothing to record.
                    retained += 1
                except BatchPayloadError as exc:
                    # ``exc.diagnostics`` is None for rejections raised outside
                    # grounding (a lifecycle violation, for instance); the item
                    # then simply fails without a measurement.
                    try:
                        failed += int(
                            self._repository.record_item_failure(
                                batch_job_id, result.custom_id, str(exc), exc.diagnostics
                            )
                        )
                    except DurableSuccessRetained:
                        # The item was accepted under an earlier contract and a
                        # later one re-rejects it.  The stored success stands and
                        # the poll continues; aborting here would let one such
                        # item block every item after it.
                        retained += 1
            failed += self._repository.reconcile_terminal_missing_results(batch_job_id)
        return {
            "job_id": batch_job_id,
            "state": state,
            "ingested": added,
            "failed": failed,
            # Items a tightened contract re-rejects but whose stored success is
            # retained.  Reported so a re-poll that changes nothing is visibly
            # different from one that had nothing to do.
            "retained": retained,
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
