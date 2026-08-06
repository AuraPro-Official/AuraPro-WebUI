"""Authenticated API application service for the independent EPUB domain.

The service owns HTTP-adjacent orchestration only.  The canonical store remains
the authority for source text and the search/Batch modules retain their own
local-only and durable-workflow policies.  In particular, no browser request
can configure a provider credential or replace an inference endpoint.
"""

from __future__ import annotations

from dataclasses import asdict
import asyncio
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Protocol, Sequence

from open_webui.retrieval.epub.batch import (
    BatchItemInput,
    BatchJobService,
    BatchPayloadError,
    BatchProvider,
    BatchServiceError,
    SQLiteBatchRepository,
)
from open_webui.retrieval.epub.calibration import LocalConceptCalibrationRunner
from open_webui.retrieval.epub.prompt_profiles import (
    DEFAULT_CONCEPT_PROMPT_PROFILE,
    PromptProfileError,
    available_prompt_profiles,
    build_concept_completion_request,
    select_stratified_passages,
)
from open_webui.retrieval.epub.overlay import (
    ConceptOverlay,
    OverlayError,
    overlay_sha256,
    parse_overlay_json,
)
from open_webui.retrieval.epub.retrieval_units import plan_retrieval_windows
from open_webui.retrieval.epub.search import EpubSearchService, SearchResponse
from open_webui.retrieval.epub.section_graph import (
    DEFAULT_SECTION_GRAPH_PROFILE,
    SECTION_GRAPH_MAX_CHARACTERS,
    SectionGraphError,
    build_section_graph_completion_request,
    build_section_graph_packets,
    get_section_graph_profile,
)
from open_webui.retrieval.epub.store import (
    IntegrityError,
    SQLiteEpubStore,
    UnknownConceptError,
)
from open_webui.retrieval.parsers.epub.parser import PARSER_FORMAT_VERSION, EPUBParser


class EpubServiceError(ValueError):
    """A safe, client-actionable failure in the EPUB API application service."""


class EpubServiceUnavailable(EpubServiceError):
    """A server-only integration has not been configured or is unavailable."""


class EpubResourceNotFound(EpubServiceError):
    """A referenced EPUB record does not exist, so the route answers 404."""


class EpubApiRepository(Protocol):
    """Canonical-store surface needed by the HTTP application service.

    The protocol keeps route orchestration independent of a SQLite connection;
    a PostgreSQL implementation can provide the same source records later.
    """

    def list_books(self) -> list[dict[str, Any]]: ...
    def get_book(self, book_id: str) -> dict[str, Any] | None: ...
    def list_versions(self, book_id: str) -> list[dict[str, Any]]: ...
    def get_version(self, version_id: str) -> dict[str, Any] | None: ...
    def list_passages(self, version_id: str) -> list[dict[str, Any]]: ...
    def get_passage(self, passage_id: str) -> dict[str, Any] | None: ...
    def find_version_by_sha256(self, epub_sha256: str) -> dict[str, Any] | None: ...
    def create_book(self, title: str, *, book_id: str | None = None) -> str: ...
    def create_book_version(self, book_id: str, *, epub_bytes: bytes, source_locator: str | None = None) -> Any: ...
    def add_toc_nodes(self, version_id: str, nodes: Sequence[Mapping[str, Any]]) -> list[str]: ...
    def add_passages(self, version_id: str, passages: Sequence[Mapping[str, Any]]) -> list[str]: ...
    def add_retrieval_unit(
        self,
        passage_id: str,
        start_codepoint: int,
        end_codepoint: int,
        **metadata: Any,
    ) -> str: ...
    def list_retrieval_units_for_version(self, version_id: str) -> list[dict[str, Any]]: ...
    def set_retrieval_unit_vector_state(self, retrieval_unit_id: str, vector_state: str) -> None: ...
    def set_version_status(self, version_id: str, status: str, *, failure_reason: str | None = None) -> None: ...
    def upsert_concept(self, canonical_name: str, **kwargs: Any) -> str: ...
    def list_concepts(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def count_concepts(self, **kwargs: Any) -> int: ...
    def merge_concepts(self, **kwargs: Any) -> dict[str, Any]: ...
    def list_concept_relation_assertions(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def count_concept_relation_assertions(self, **kwargs: Any) -> int: ...
    def set_concept_relation_assertion_status(self, assertion_id: str, status: str) -> None: ...
    def export_concept_overlay(self, version_id: str) -> ConceptOverlay: ...
    def apply_overlay(self, overlay: ConceptOverlay, **kwargs: Any) -> dict[str, Any]: ...


class EpubConceptService:
    """Compose parsing, canonical storage, offline Batch, and search.

    ``providers`` and ``vector_indexer`` are injected by server startup code.
    They are deliberately not inferred from HTTP payloads: this prevents an
    ordinary API caller from selecting a cloud endpoint or supplying a secret.
    """

    def __init__(
        self,
        *,
        store: EpubApiRepository,
        search: EpubSearchService | None = None,
        batch: BatchJobService | None = None,
        providers: Mapping[str, BatchProvider] | None = None,
        vector_indexer: Any | None = None,
        calibration_runner: LocalConceptCalibrationRunner | None = None,
        retrieval_embedding_profile: str | None = None,
    ) -> None:
        self._store = store
        self._search = search or EpubSearchService(source=store)
        if batch is None:
            if not isinstance(store, SQLiteEpubStore):
                raise EpubServiceUnavailable(
                    "the configured EPUB store needs a BatchRepository adapter before Batch APIs can start"
                )
            batch = BatchJobService(SQLiteBatchRepository(store))
        self._batch = batch
        self._providers = dict(providers or {})
        self._vector_indexer = vector_indexer
        self._calibration_runner = calibration_runner
        self._retrieval_embedding_profile = retrieval_embedding_profile or None

    def list_books(self) -> list[dict[str, Any]]:
        return self._store.list_books()

    def get_book(self, book_id: str) -> dict[str, Any] | None:
        book = self._store.get_book(book_id)
        if book is None:
            return None
        return {**book, "versions": self._store.list_versions(book_id)}

    def list_passages(self, version_id: str, *, offset: int, limit: int) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 200:
            raise EpubServiceError("passage pagination values are invalid")
        if self._store.get_version(version_id) is None:
            raise EpubServiceError("unknown EPUB version")
        passages = self._store.list_passages(version_id)
        return {
            "version_id": version_id,
            "total": len(passages),
            "offset": offset,
            "items": passages[offset : offset + limit],
        }

    def get_passage(self, passage_id: str) -> dict[str, Any] | None:
        return self._store.get_passage(passage_id)

    def search(
        self,
        query: str,
        *,
        graph_offset: int = 0,
        graph_limit: int = 20,
        vector_limit: int = 10,
    ) -> dict[str, Any]:
        response = self._search.search(
            query,
            graph_offset=graph_offset,
            graph_limit=graph_limit,
            vector_limit=vector_limit,
        )
        return self._search_response(response)

    async def search_async(
        self, query: str, *, graph_offset: int = 0, graph_limit: int = 20, vector_limit: int = 10
    ) -> dict[str, Any]:
        """Keep canonical SQLite reads and current sync adapters off the ASGI loop.

        Native AuraPro async model adapters will replace this compatibility
        bridge in the next integration step; the HTTP contract is async now.
        """
        return await asyncio.to_thread(
            self.search, query, graph_offset=graph_offset, graph_limit=graph_limit, vector_limit=vector_limit
        )

    def import_epub(
        self,
        *,
        filename: str,
        epub_bytes: bytes,
        source_locator: str | None = None,
    ) -> dict[str, Any]:
        if not filename.lower().endswith(".epub"):
            raise EpubServiceError("uploaded file must have an .epub extension")
        if not epub_bytes:
            raise EpubServiceError("uploaded EPUB cannot be empty")
        digest = sha256(epub_bytes).hexdigest()
        duplicate = self._store.find_version_by_sha256(digest)
        if duplicate is not None:
            return {"created": False, "duplicate": True, **duplicate}

        # The parser accepts a path so it can perform ZIP safety checks before
        # extraction.  NamedTemporaryFile is closed before Windows-compatible
        # reopening and always removed afterwards.
        suffix = Path(filename).suffix or ".epub"
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="epub-import-", suffix=suffix, delete=False) as handle:
                handle.write(epub_bytes)
                temp_path = handle.name
            parsed = EPUBParser(temp_path).parse_book()
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

        if not parsed.passages:
            raise EpubServiceError("EPUB contains no supported visible textual passages")

        book_id = self._store.create_book(parsed.book_title)
        version = self._store.create_book_version(
            book_id,
            epub_bytes=epub_bytes,
            source_locator=source_locator or filename,
        )
        # Another worker may have stored the exact archive after the first
        # check.  Its canonical version wins and this request is a no-op.
        if not version.created:
            return {
                "created": False,
                "duplicate": True,
                "version_id": version.version_id,
                "book_id": version.book_id,
                "epub_sha256": version.epub_sha256,
            }
        try:
            toc_ids = self._persist_toc(version.version_id, parsed.passages)
            passage_ids = self._store.add_passages(
                version.version_id,
                [
                    {
                        "toc_node_id": toc_ids.get(passage.toc_path),
                        "source_href": passage.source_path,
                        "source_fragment": passage.source_fragment,
                        "spine_index": passage.spine_index,
                        "ordinal": passage.ordinal,
                        "content_kind": passage.content_kind,
                        "content": passage.content,
                    }
                    for passage in parsed.passages
                ],
            )
            retrieval_unit_count = self._create_retrieval_units(passage_ids)
            self._store.set_version_status(version.version_id, "READY")
        except Exception as error:
            self._store.set_version_status(version.version_id, "FAILED", failure_reason=_safe_reason(error))
            raise
        return {
            "created": True,
            "duplicate": False,
            "book_id": version.book_id,
            "version_id": version.version_id,
            "book_title": parsed.book_title,
            "epub_sha256": version.epub_sha256,
            "total_passages": len(parsed.passages),
            "total_retrieval_units": retrieval_unit_count,
            "warnings": [asdict(warning) for warning in parsed.warnings],
        }

    def _create_retrieval_units(self, passage_ids: Sequence[str]) -> int:
        """Persist the standard vector windows for newly imported passages.

        The store's exact-window lookup makes this safe to call again after a
        retry: it returns existing units instead of duplicating them.  Source
        passage content remains the only input and is never rewritten.
        """
        created_or_reused = 0
        for passage_id in passage_ids:
            passage = self._store.get_passage(passage_id)
            if passage is None:
                raise EpubServiceError("newly stored EPUB passage is unavailable")
            content = passage.get("content")
            if not isinstance(content, str):
                raise EpubServiceError("newly stored EPUB passage has invalid source content")
            for window in plan_retrieval_windows(content):
                self._store.add_retrieval_unit(
                    passage_id,
                    window.start_codepoint,
                    window.end_codepoint,
                    embedding_profile=self._retrieval_embedding_profile,
                )
                created_or_reused += 1
        return created_or_reused

    def export_concept_overlay(self, version_id: str) -> dict[str, Any]:
        """Publish one version's analysis as portable, text-free artifact bytes.

        The canonical JSON *text* is returned rather than a decoded object:
        the published SHA-256 has to cover the exact bytes an administrator
        redistributes, and re-serializing the object downstream would not
        reproduce them.
        """
        if self._store.get_version(version_id) is None:
            raise EpubResourceNotFound("unknown EPUB version")
        try:
            overlay = self._store.export_concept_overlay(version_id)
        except (IntegrityError, OverlayError) as error:
            raise EpubServiceError(str(error)) from error
        overlay_json = overlay.to_json()
        return {
            "version_id": version_id,
            "epub_sha256": overlay.epub_sha256,
            "parser_version": overlay.parser_version,
            "overlay_format_version": overlay.overlay_format_version,
            "overlay_json": overlay_json,
            "overlay_sha256": overlay_sha256(overlay_json),
            "passage_count": overlay.fingerprint.count,
            "concept_count": len(overlay.concepts),
            "mention_count": len(overlay.mentions),
            "relation_count": len(overlay.relations),
        }

    def apply_concept_overlay(self, *, overlay_bytes: bytes) -> dict[str, Any]:
        """Attach a published analysis to this server's own copy of the book.

        The uploaded artifact never supplies passage text, so this cannot add
        source material: the store re-derives every mention and evidence
        string from its own passages, and refuses the whole upload if a single
        location fails to verify.  An applied overlay has no vectors, so the
        caller is told to rebuild the version's derived index afterwards.
        """
        if not overlay_bytes:
            raise EpubServiceError("the uploaded overlay artifact cannot be empty")
        try:
            overlay = parse_overlay_json(overlay_bytes)
        except OverlayError as error:
            raise EpubServiceError(str(error)) from error
        if overlay.parser_version != str(PARSER_FORMAT_VERSION):
            # The store checks this again against the target version's own
            # recorded format.  Both matter: this one refuses an artifact no
            # build of this server could ever have produced, before any
            # version is even resolved.
            raise EpubServiceError(
                "the overlay was produced by an EPUB parser format this server does not implement"
            )
        version = self._store.find_version_by_sha256(overlay.epub_sha256)
        if version is None:
            raise EpubResourceNotFound(
                "no EPUB version in this library matches the overlay's archive hash"
            )
        try:
            summary = self._store.apply_overlay(overlay, version_id=str(version["version_id"]))
        except IntegrityError as error:
            raise EpubServiceError(
                f"{getattr(error, 'reason', 'overlay_rejected')}: {error}"
            ) from error
        return {
            **summary,
            "book_id": version.get("book_id"),
            "book_title": version.get("book_title"),
            "uploaded_overlay_sha256": sha256(overlay_bytes).hexdigest(),
            "canonical_overlay_sha256": overlay.digest(),
            # An imported overlay carries no vectors by design; the derived
            # index has to be rebuilt before the new concepts are searchable.
            "vectors_require_reindex": True,
        }

    def list_prompt_profiles(self) -> dict[str, Any]:
        """List the selectable concept prompt profile identifiers only.

        An administrator has to be able to choose any profile the server
        actually implements, including the current default; hardcoding a
        client-side list silently strips newer profiles from the UI.  Only the
        identifiers travel: instruction text and output schemas stay server-
        owned so no browser can read or replace the extraction policy.
        """
        return {
            "prompt_profiles": list(available_prompt_profiles()),
            "default_prompt_profile": DEFAULT_CONCEPT_PROMPT_PROFILE,
        }

    def create_batch_draft(
        self,
        *,
        version_id: str,
        profile_name: str,
        prompt_profile: str = DEFAULT_CONCEPT_PROMPT_PROFILE,
        is_sample: bool,
        sample_limit: int,
    ) -> dict[str, Any]:
        if not profile_name.strip():
            raise EpubServiceError("Batch profile_name cannot be empty")
        if not 1 <= sample_limit <= 500:
            raise EpubServiceError("sample_limit must be between 1 and 500")
        passages = self._store.list_passages(version_id)
        if not passages:
            raise EpubServiceError("EPUB version contains no passages")
        try:
            selected = (
                select_stratified_passages(passages, limit=sample_limit) if is_sample else passages
            )
            items = [
                BatchItemInput(
                    passage_id=str(passage["passage_id"]),
                    custom_id=f"{version_id}:{passage['passage_id']}",
                    request=self._batch_request(
                        model=profile_name,
                        prompt_profile=prompt_profile,
                        content=str(passage["content"]),
                    ),
                )
                for passage in selected
            ]
        except PromptProfileError as error:
            raise EpubServiceError(str(error)) from error
        job_id = self._batch.create_draft(
            version_id=version_id,
            provider="openai-batch",
            profile_name=profile_name,
            items=items,
            is_sample=is_sample,
        )
        return {
            "batch_job_id": job_id,
            "item_count": len(items),
            "status": "DRAFT",
            "job_kind": "CONCEPT_MENTIONS",
            "is_sample": is_sample,
            "prompt_profile": prompt_profile,
        }

    def create_section_graph_batch_draft(
        self,
        *,
        version_id: str,
        profile_name: str,
        is_sample: bool,
        sample_limit: int,
        section_graph_profile: str = DEFAULT_SECTION_GRAPH_PROFILE,
    ) -> dict[str, Any]:
        """Create one durable cloud item per bounded TOC section packet.

        Each item is anchored to an actual passage only for Batch lifecycle
        lineage.  Its request contains all immutable passages in the packet;
        response mentions and relation evidence must still name their exact
        source passage and character span.
        """
        if not profile_name.strip():
            raise EpubServiceError("Batch profile_name cannot be empty")
        if not 1 <= sample_limit <= 500:
            raise EpubServiceError("sample_limit must be between 1 and 500")
        try:
            get_section_graph_profile(section_graph_profile)
        except SectionGraphError as error:
            raise EpubServiceError(str(error)) from error
        passages = self._store.list_passages(version_id)
        if not passages:
            raise EpubServiceError("EPUB version contains no passages")
        if is_sample:
            selected = select_stratified_passages(passages, limit=sample_limit)
        else:
            selected = passages
        try:
            packets = build_section_graph_packets(selected, max_characters=SECTION_GRAPH_MAX_CHARACTERS)
        except SectionGraphError as error:
            raise EpubServiceError(str(error)) from error
        items = [
            BatchItemInput(
                passage_id=packet.anchor_passage_id,
                custom_id=f"{version_id}:section-graph:{index}",
                request=self._section_graph_batch_request(
                    model=profile_name, packet=packet, profile_id=section_graph_profile
                ),
            )
            for index, packet in enumerate(packets)
        ]
        job_id = self._batch.create_draft(
            version_id=version_id,
            provider="openai-batch",
            profile_name=profile_name,
            job_kind="SECTION_GRAPH",
            items=items,
            is_sample=is_sample,
        )
        return {
            "batch_job_id": job_id,
            "item_count": len(items),
            "status": "DRAFT",
            "job_kind": "SECTION_GRAPH",
            "is_sample": is_sample,
            "prompt_profile": section_graph_profile,
        }

    def submit_batch(self, batch_job_id: str) -> dict[str, Any]:
        provider = self._provider_for_job(batch_job_id)
        provider_job_id = self._batch.submit(batch_job_id, provider)
        return {"batch_job_id": batch_job_id, "provider_job_id": provider_job_id}

    def list_batch_jobs(
        self, *, version_id: str | None = None, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """Return safe operator history, excluding prompts, results and raw errors."""
        try:
            return self._batch.list_job_summaries(version_id=version_id, offset=offset, limit=limit)
        except BatchServiceError as error:
            raise EpubServiceError(str(error)) from error

    def get_batch_job(self, batch_job_id: str) -> dict[str, Any]:
        try:
            return self._batch.get_job_summary(batch_job_id)
        except BatchServiceError as error:
            raise EpubServiceError(str(error)) from error

    def review_sample_batch(
        self, *, batch_job_id: str, status: str, reviewed_by: str
    ) -> dict[str, Any]:
        """Persist an administrator quality decision for a completed cloud sample."""
        try:
            return self._batch.review_sample_job(
                batch_job_id, status=status, reviewed_by=reviewed_by
            )
        except BatchServiceError as error:
            raise EpubServiceError(str(error)) from error

    def list_sample_batch_reviews(
        self, *, version_id: str | None, job_kind: str | None
    ) -> dict[str, Any]:
        try:
            return {
                "items": self._batch.list_sample_reviews(
                    version_id=version_id, job_kind=job_kind
                )
            }
        except BatchServiceError as error:
            raise EpubServiceError(str(error)) from error

    def recover_batches(self) -> dict[str, list[dict[str, Any]]]:
        """Poll all durable active jobs using only server-configured providers.

        This deliberately does not call ``submit`` and therefore cannot create
        cloud work or costs after a restart.  An absent provider is reported
        as skipped so administrators can repair configuration before retrying.
        """
        try:
            return self._batch.recover_all(self._providers)
        except (BatchServiceError, BatchPayloadError) as error:
            raise EpubServiceError(str(error)) from error

    def list_relation_assertions(
        self, *, status: str | None, version_id: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        try:
            return {
                "total": self._store.count_concept_relation_assertions(
                    status=status, version_id=version_id
                ),
                "offset": offset,
                "items": self._store.list_concept_relation_assertions(
                    status=status, version_id=version_id, offset=offset, limit=limit
                ),
            }
        except IntegrityError as error:
            raise EpubServiceError(str(error)) from error

    def review_relation_assertion(self, *, assertion_id: str, status: str) -> dict[str, str]:
        try:
            self._store.set_concept_relation_assertion_status(assertion_id, status)
        except IntegrityError as error:
            raise EpubServiceError(str(error)) from error
        return {"assertion_id": assertion_id, "status": status}

    def run_local_calibration(
        self, *, version_id: str, prompt_profile: str, sample_limit: int
    ) -> dict[str, Any]:
        if self._calibration_runner is None:
            raise EpubServiceUnavailable("the Desktop-managed local calibration runtime is not configured")
        if self._store.get_version(version_id) is None:
            raise EpubServiceError("unknown EPUB version")
        try:
            return self._calibration_runner.run(
                passages=self._store.list_passages(version_id),
                prompt_profile=prompt_profile,
                sample_limit=sample_limit,
            )
        except (PromptProfileError, ValueError) as error:
            raise EpubServiceError(str(error)) from error

    async def run_local_calibration_async(
        self, *, version_id: str, prompt_profile: str, sample_limit: int
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.run_local_calibration,
            version_id=version_id,
            prompt_profile=prompt_profile,
            sample_limit=sample_limit,
        )

    def poll_batch(self, batch_job_id: str) -> dict[str, int | str]:
        return self._batch.poll_and_ingest(batch_job_id, self._provider_for_job(batch_job_id))

    def retry_batch(self, batch_job_id: str) -> dict[str, str]:
        return {"batch_job_id": self._batch.retry_failed_items(batch_job_id), "parent_batch_job_id": batch_job_id}

    def upsert_concept(
        self, *, canonical_name: str, aliases: Sequence[str], definition: str, status: str
    ) -> dict[str, str]:
        concept_id = self._store.upsert_concept(
            canonical_name,
            aliases=aliases,
            definition=definition,
            status=status,
            alias_source="ADMIN",
        )
        return {"concept_id": concept_id}

    def list_concepts(self, *, status: str | None, offset: int, limit: int) -> dict[str, Any]:
        """Page the concept graph so an administrator can find merge candidates.

        Aliases and canonical names are concept labels, and mention counts are
        integers; no passage text, evidence span, prompt or model output is
        part of this response.
        """
        try:
            return {
                "total": self._store.count_concepts(status=status),
                "offset": offset,
                "items": self._store.list_concepts(status=status, offset=offset, limit=limit),
            }
        except IntegrityError as error:
            raise EpubServiceError(str(error)) from error

    def merge_concepts(
        self,
        *,
        target_concept_id: str,
        source_concept_id: str,
        canonical_name: str | None,
        merged_by: str,
    ) -> dict[str, Any]:
        """Resolve a model-suggested duplicate by folding one concept into another.

        Ingest deliberately refuses an item whose suggestion exactly matches
        two concepts, and nothing else in the API could resolve that.  This is
        the administrator remedy; the acting user is recorded in the audit row.
        """
        try:
            return self._store.merge_concepts(
                target_concept_id=target_concept_id,
                source_concept_id=source_concept_id,
                canonical_name=canonical_name,
                merged_by=merged_by,
            )
        except UnknownConceptError as error:
            raise EpubResourceNotFound(str(error)) from error
        except IntegrityError as error:
            raise EpubServiceError(str(error)) from error

    def index_retrieval_unit(self, retrieval_unit_id: str) -> dict[str, Any]:
        if self._vector_indexer is None:
            raise EpubServiceUnavailable("the server has no private EPUB vector indexer configured")
        result = self._vector_indexer.index(retrieval_unit_id)
        return {
            "retrieval_unit_id": result.retrieval_unit_id,
            "state": result.state,
            "availability": asdict(result.availability),
            "reason": result.reason,
        }

    async def index_retrieval_unit_async(self, retrieval_unit_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.index_retrieval_unit, retrieval_unit_id)

    def index_version_retrieval_units(self, version_id: str, *, rebuild: bool = False) -> dict[str, Any]:
        """Index one EPUB version's derived windows with isolated outcomes.

        A normal run retries every non-ready unit, while a rebuild deliberately
        re-embeds all units for the version.  Individual failures are recorded
        and returned without abandoning the remaining source windows.  A
        degraded local embedding runtime leaves the existing state untouched:
        a previously ready vector remains usable and a pending unit can be
        retried after the private runtime recovers.
        """
        if self._vector_indexer is None:
            raise EpubServiceUnavailable("the server has no private EPUB vector indexer configured")
        if self._store.get_version(version_id) is None:
            raise EpubServiceError("unknown EPUB version")

        all_units = self._store.list_retrieval_units_for_version(version_id)
        selected = all_units if rebuild else [
            unit for unit in all_units if unit.get("vector_state") != "READY"
        ]
        errors: list[dict[str, str]] = []
        ready = degraded = failed = 0
        for unit in selected:
            retrieval_unit_id = str(unit["retrieval_unit_id"])
            try:
                result = self._vector_indexer.index(retrieval_unit_id)
                if result.state == "READY":
                    self._store.set_retrieval_unit_vector_state(retrieval_unit_id, "READY")
                    ready += 1
                elif result.state == "DEGRADED":
                    degraded += 1
                    errors.append(
                        {
                            "retrieval_unit_id": retrieval_unit_id,
                            "reason": result.reason or "private embedding runtime is unavailable",
                        }
                    )
                else:
                    self._store.set_retrieval_unit_vector_state(retrieval_unit_id, "FAILED")
                    failed += 1
                    errors.append(
                        {
                            "retrieval_unit_id": retrieval_unit_id,
                            "reason": result.reason or f"unexpected index state: {result.state}",
                        }
                    )
            except Exception as error:
                self._store.set_retrieval_unit_vector_state(retrieval_unit_id, "FAILED")
                failed += 1
                errors.append({"retrieval_unit_id": retrieval_unit_id, "reason": _safe_reason(error)})

        return {
            "version_id": version_id,
            "mode": "REBUILD" if rebuild else "PENDING",
            "total_retrieval_units": len(all_units),
            "selected_retrieval_units": len(selected),
            "skipped_ready": len(all_units) - len(selected),
            "ready": ready,
            "degraded": degraded,
            "failed": failed,
            # The count is present even when individual details are capped so
            # a large malformed EPUB cannot turn an operational response into
            # an unbounded payload.
            "error_count": len(errors),
            "errors": errors[:20],
        }

    async def index_version_retrieval_units_async(
        self, version_id: str, *, rebuild: bool = False
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.index_version_retrieval_units, version_id, rebuild=rebuild)

    def _provider_for_job(self, batch_job_id: str) -> BatchProvider:
        job = self._batch.get_job(batch_job_id)
        provider_name = str(job["provider"])
        provider = self._providers.get(provider_name)
        if provider is None:
            raise EpubServiceUnavailable(
                f"the server administrator has not configured Batch provider {provider_name!r}"
            )
        return provider

    def _persist_toc(self, version_id: str, passages: Sequence[Any]) -> dict[tuple[str, ...], str]:
        paths = sorted({tuple(passage.toc_path) for passage in passages if passage.toc_path})
        if not paths:
            return {}
        ids: dict[tuple[str, ...], str] = {}
        nodes: list[dict[str, Any]] = []
        for ordinal, path in enumerate(paths):
            parent = path[:-1]
            node_id = f"{version_id}:toc-{ordinal + 1}"
            ids[path] = node_id
            first = next(passage for passage in passages if tuple(passage.toc_path) == path)
            nodes.append(
                {
                    "toc_node_id": node_id,
                    "parent_toc_node_id": ids.get(parent),
                    "title": path[-1],
                    "href": first.source_path,
                    "fragment": first.source_fragment,
                    "spine_index": first.spine_index,
                    "ordinal": ordinal,
                }
            )
        self._store.add_toc_nodes(version_id, nodes)
        return ids

    @staticmethod
    def _batch_request(*, model: str, prompt_profile: str, content: str) -> dict[str, Any]:
        """Build a server-owned OpenAI Batch request with no credential fields."""
        body = build_concept_completion_request(
            model=model,
            profile_id=prompt_profile,
            passage=content,
            remote_structured_output=True,
        )
        return {
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        }

    @staticmethod
    def _section_graph_batch_request(
        *, model: str, packet: Any, profile_id: str = DEFAULT_SECTION_GRAPH_PROFILE
    ) -> dict[str, Any]:
        return {
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": build_section_graph_completion_request(
                model=model, packet=packet, profile_id=profile_id
            ),
        }

    @staticmethod
    def _search_response(response: SearchResponse) -> dict[str, Any]:
        def hit(value: Any) -> dict[str, Any]:
            return {
                "passage_id": value.passage_id,
                "book_title": value.book_title,
                "toc_path": list(value.toc_path),
                "content": value.content,
                "content_sha256": value.content_sha256,
                "matched_concepts": list(value.matched_concepts),
                "provenance": list(value.provenance),
                "excerpt": asdict(value.excerpt),
                "score": value.score,
            }

        return {
            "query": response.query,
            "resolved_concepts": list(response.resolved_concepts),
            "graph_total": response.graph_total,
            "graph_offset": response.graph_offset,
            "graph_results": [hit(value) for value in response.graph_results],
            "vector_results": [hit(value) for value in response.vector_results],
            "fused_results": [hit(value) for value in response.fused_results],
            "degraded": [asdict(value) for value in response.degraded],
        }


def _safe_reason(error: Exception) -> str:
    return (str(error).strip() or type(error).__name__)[:240]
