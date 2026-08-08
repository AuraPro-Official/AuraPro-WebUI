"""Authenticated REST API for the independent EPUB concept domain.

Every route is authorized: reads require ``get_verified_user`` and every
import, destructive, glossary, indexing, and Batch command requires
``get_admin_user``.  Startup integration sets
``app.state.EPUB_CONCEPT_SERVICE`` to an ``EpubConceptService`` configured
with the server's independent store and private model/provider adapters.
"""

from __future__ import annotations

from typing import Annotated, Any

import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from open_webui.retrieval.epub.batch import BatchPayloadError, BatchServiceError
from open_webui.retrieval.epub.prompt_profiles import DEFAULT_CONCEPT_PROMPT_PROFILE
from open_webui.retrieval.epub.search import SearchError
from open_webui.retrieval.epub.store import IntegrityError
from open_webui.services.epub_concept import (
    EpubConceptService,
    EpubResourceNotFound,
    EpubServiceError,
    EpubServiceUnavailable,
)
from open_webui.utils.auth import get_admin_user, get_verified_user


router = APIRouter(prefix="/api/v1/epub", tags=["epub"])
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
# An overlay is compact JSON that carries no book text, so it is bounded far
# below an EPUB archive; a larger upload is a mistake or an attack, not a graph.
MAX_OVERLAY_BYTES = 32 * 1024 * 1024
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]")


class SearchForm(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)
    graph_offset: int = Field(default=0, ge=0)
    graph_limit: int = Field(default=20, ge=1, le=200)
    vector_limit: int = Field(default=10, ge=1, le=100)


class BatchDraftForm(BaseModel):
    version_id: str = Field(min_length=1, max_length=128)
    profile_name: str = Field(min_length=1, max_length=200)
    # Track the registered default rather than a second literal, so a new
    # profile version cannot leave the API pinned to a superseded instruction.
    prompt_profile: str = Field(
        default=DEFAULT_CONCEPT_PROMPT_PROFILE, min_length=1, max_length=100
    )
    is_sample: bool = False
    sample_limit: int = Field(default=20, ge=1, le=500)


class SectionGraphBatchDraftForm(BaseModel):
    version_id: str = Field(min_length=1, max_length=128)
    profile_name: str = Field(min_length=1, max_length=200)
    is_sample: bool = False
    sample_limit: int = Field(default=20, ge=1, le=500)


class LocalCalibrationForm(BaseModel):
    version_id: str = Field(min_length=1, max_length=128)
    prompt_profile: str = Field(
        default=DEFAULT_CONCEPT_PROMPT_PROFILE, min_length=1, max_length=100
    )
    sample_limit: int = Field(default=20, ge=1, le=100)


class ConceptUpsertForm(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    definition: str = Field(default="", max_length=10_000)
    status: str = Field(default="APPROVED", pattern="^(PROVISIONAL|APPROVED|REJECTED)$")


class ConceptMergeForm(BaseModel):
    """Fold ``source_concept_id`` into ``target_concept_id``.

    ``canonical_name`` is optional and lets the surviving concept adopt a
    different preferred spelling — typically the source's, when the source was
    the better label of the two.  Every other spelling becomes an alias.
    """

    target_concept_id: str = Field(min_length=1, max_length=128)
    source_concept_id: str = Field(min_length=1, max_length=128)
    canonical_name: str | None = Field(default=None, min_length=1, max_length=500)


class ConceptMentionRef(BaseModel):
    """One mention, named the way the rest of this API names a mention.

    ``concept_mentions`` is unique per (concept, passage, span) and
    ``add_concept_mention`` addresses a mention by passage and offsets, so a
    split does too.  Omit both offsets to name an unanchored mention.
    """

    passage_id: str = Field(min_length=1, max_length=128)
    start_codepoint: int | None = Field(default=None, ge=0)
    end_codepoint: int | None = Field(default=None, ge=1)


class ConceptSplitForm(BaseModel):
    """Carve ``aliases`` and ``mentions`` out of ``source_concept_id``.

    A merge is one-way and an administrator merge is a fallible judgement; this
    is the correction path, and it is an explicit new decision rather than a
    rewind of a recorded merge.  ``canonical_name`` must be one of the moving
    aliases or a spelling no concept owns.
    """

    source_concept_id: str = Field(min_length=1, max_length=128)
    canonical_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    mentions: list[ConceptMentionRef] = Field(default_factory=list, max_length=1_000)


class RelationAssertionReviewForm(BaseModel):
    status: str = Field(pattern="^(APPROVED|REJECTED|PROVISIONAL)$")


class SampleBatchReviewForm(BaseModel):
    status: str = Field(pattern="^(APPROVED|REJECTED)$")


class VersionIndexForm(BaseModel):
    rebuild: bool = False


def get_epub_concept_service(request: Request) -> EpubConceptService:
    """Fetch the server-configured service without silently creating storage.

    A missing service is an operations/configuration error, not an invitation to
    create an accidental data file or to use a generic cloud RAG integration.
    """
    service = getattr(request.app.state, "EPUB_CONCEPT_SERVICE", None)
    if not isinstance(service, EpubConceptService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="EPUB concept service is not configured on this server",
        )
    return service


ServiceDep = Annotated[EpubConceptService, Depends(get_epub_concept_service)]


@router.get("/books")
async def list_books(service: ServiceDep, user=Depends(get_verified_user)) -> list[dict[str, Any]]:
    """Browse the shared EPUB catalogue as any verified user."""
    return service.list_books()


@router.get("/books/{book_id}")
async def get_book(book_id: str, service: ServiceDep, user=Depends(get_verified_user)) -> dict[str, Any]:
    book = service.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EPUB book not found")
    return book


@router.get("/versions/{version_id}/passages")
async def list_passages(
    version_id: str,
    service: ServiceDep,
    user=Depends(get_verified_user),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    try:
        return service.list_passages(version_id, offset=offset, limit=limit)
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get("/passages/{passage_id}")
async def get_passage(
    passage_id: str, service: ServiceDep, user=Depends(get_verified_user)
) -> dict[str, Any]:
    passage = service.get_passage(passage_id)
    if passage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EPUB passage not found")
    return passage


@router.post("/search")
async def search_epub(form_data: SearchForm, service: ServiceDep, user=Depends(get_verified_user)) -> dict[str, Any]:
    try:
        return await service.search_async(
            form_data.query,
            graph_offset=form_data.graph_offset,
            graph_limit=form_data.graph_limit,
            vector_limit=form_data.vector_limit,
        )
    except (EpubServiceError, SearchError, IntegrityError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/import", status_code=status.HTTP_201_CREATED)
async def import_epub(
    service: ServiceDep,
    user=Depends(get_admin_user),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    filename = file.filename or ""
    if not filename.lower().endswith(".epub"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="uploaded file must be an .epub")
    epub_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(epub_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="EPUB upload exceeds 200 MiB")
    try:
        return service.import_epub(filename=filename, epub_bytes=epub_bytes, source_locator=filename)
    except (EpubServiceError, IntegrityError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get("/admin/versions/{version_id}/overlay")
async def export_epub_overlay(
    version_id: str, service: ServiceDep, user=Depends(get_admin_user)
) -> Response:
    """Download one version's analysis as a portable, text-free artifact.

    The body is the artifact's exact canonical bytes and ``X-Overlay-SHA256``
    is their digest, so an administrator can publish the file and the hash
    together and any recipient can verify what they downloaded.  Concept
    labels, aliases and definitions are the analysis product and do travel;
    passage text, evidence strings, EPUB blobs and vectors never do.
    """
    try:
        result = service.export_concept_overlay(version_id)
    except EpubResourceNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (EpubServiceError, IntegrityError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    filename = f"{_SAFE_FILENAME.sub('-', version_id)[:64]}-overlay.json"
    return Response(
        content=result["overlay_json"].encode("utf-8"),
        media_type="application/json",
        headers={
            "X-Overlay-SHA256": result["overlay_sha256"],
            "X-Overlay-Epub-SHA256": result["epub_sha256"],
            "X-Overlay-Concept-Count": str(result["concept_count"]),
            "X-Overlay-Mention-Count": str(result["mention_count"]),
            "X-Overlay-Relation-Count": str(result["relation_count"]),
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/admin/overlays")
async def apply_epub_overlay(
    service: ServiceDep,
    user=Depends(get_admin_user),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Apply a published analysis overlay to this server's own EPUB copy.

    The response is deliberately content-free: counts and stable reason
    classes only.  A failed source-fidelity gate rejects the whole artifact
    and answers 400 with its reason class, never with the passage, span or
    label that failed.
    """
    filename = file.filename or ""
    if not filename.lower().endswith(".json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="uploaded file must be a .json overlay"
        )
    overlay_bytes = await file.read(MAX_OVERLAY_BYTES + 1)
    if len(overlay_bytes) > MAX_OVERLAY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="overlay upload exceeds 32 MiB",
        )
    try:
        return service.apply_concept_overlay(overlay_bytes=overlay_bytes)
    except EpubResourceNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (EpubServiceError, IntegrityError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get("/admin/prompt-profiles")
async def list_concept_prompt_profiles(
    service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, Any]:
    """List selectable prompt profile IDs and the server's current default.

    Batch and calibration configuration is administrator surface, so this is
    gated like every other ``/admin/`` route.  The response carries identifiers
    only; prompt text, system instructions and output schemas are never
    exposed through an administrator surface.
    """
    return service.list_prompt_profiles()


@router.post("/admin/batches", status_code=status.HTTP_201_CREATED)
async def create_batch_draft(
    form_data: BatchDraftForm, service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, Any]:
    try:
        return service.create_batch_draft(
            version_id=form_data.version_id,
            profile_name=form_data.profile_name,
            prompt_profile=form_data.prompt_profile,
            is_sample=form_data.is_sample,
            sample_limit=form_data.sample_limit,
        )
    except (EpubServiceError, BatchServiceError, IntegrityError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get("/admin/batches")
async def list_batch_jobs(
    service: ServiceDep,
    user=Depends(get_admin_user),
    version_id: str | None = Query(default=None, min_length=1, max_length=128),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """List lifecycle-only Batch history; prompts/results never leave the server."""
    try:
        return service.list_batch_jobs(version_id=version_id, offset=offset, limit=limit)
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get("/admin/batches/{batch_job_id}")
async def get_batch_job(
    batch_job_id: str, service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, Any]:
    """Show safe per-item operational status without source-bearing JSON."""
    try:
        return service.get_batch_job(batch_job_id)
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get("/admin/sample-batch-reviews")
async def list_sample_batch_reviews(
    service: ServiceDep,
    user=Depends(get_admin_user),
    version_id: str | None = Query(default=None, min_length=1, max_length=128),
    job_kind: str | None = Query(default=None, pattern="^(CONCEPT_MENTIONS|SECTION_GRAPH)$"),
) -> dict[str, Any]:
    """List identifier-only administrator decisions for completed cloud samples."""
    try:
        return service.list_sample_batch_reviews(version_id=version_id, job_kind=job_kind)
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.put("/admin/sample-batches/{batch_job_id}/review")
async def review_sample_batch(
    batch_job_id: str,
    form_data: SampleBatchReviewForm,
    service: ServiceDep,
    user=Depends(get_admin_user),
) -> dict[str, Any]:
    """Approve/reject a fully ingested OpenAI sample before full cloud work."""
    try:
        reviewed_by = str(getattr(user, "id", "")).strip()
        return service.review_sample_batch(
            batch_job_id=batch_job_id, status=form_data.status, reviewed_by=reviewed_by
        )
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/batches/backfill-prompt-profiles")
async def backfill_batch_prompt_profiles(
    service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, Any]:
    """Recover the prompt profile of jobs created before it was recorded.

    The full-run approval gate binds to the prompt profile, so a job that
    never stored one can neither unlock nor be unlocked.  This derives it from
    the instruction each job actually sent, by exact match against the
    registered profiles, and leaves anything uncertain unresolved rather than
    guessing.

    The response is identifier-only, like every other administrator surface
    here: job IDs, kinds, a resolved profile identifier or a reason class.
    """
    try:
        return service.backfill_batch_prompt_profiles()
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/batches/recover")
async def recover_batch_jobs(
    service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, list[dict[str, Any]]]:
    """Resume persisted submitted/running jobs; never submits a draft."""
    try:
        return service.recover_batches()
    except EpubServiceUnavailable as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/section-graph-batches", status_code=status.HTTP_201_CREATED)
async def create_section_graph_batch_draft(
    form_data: SectionGraphBatchDraftForm, service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, Any]:
    """Create a server-owned offline Batch job for grounded TOC section graphs."""
    try:
        return service.create_section_graph_batch_draft(
            version_id=form_data.version_id,
            profile_name=form_data.profile_name,
            is_sample=form_data.is_sample,
            sample_limit=form_data.sample_limit,
        )
    except (EpubServiceError, BatchServiceError, IntegrityError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/calibrations/local")
async def run_local_calibration(
    form_data: LocalCalibrationForm, service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, Any]:
    """Run a content-free prompt/schema calibration only through Desktop llama.cpp."""
    try:
        return await service.run_local_calibration_async(
            version_id=form_data.version_id,
            prompt_profile=form_data.prompt_profile,
            sample_limit=form_data.sample_limit,
        )
    except EpubServiceUnavailable as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except (EpubServiceError, BatchPayloadError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/batches/{batch_job_id}/submit")
async def submit_batch(batch_job_id: str, service: ServiceDep, user=Depends(get_admin_user)) -> dict[str, Any]:
    try:
        return service.submit_batch(batch_job_id)
    except EpubServiceUnavailable as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except (EpubServiceError, BatchServiceError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/batches/{batch_job_id}/poll")
async def poll_batch(
    batch_job_id: str, service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, int | str | bool]:
    try:
        return service.poll_batch(batch_job_id)
    except EpubServiceUnavailable as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except (EpubServiceError, BatchServiceError, BatchPayloadError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/batches/{batch_job_id}/retry", status_code=status.HTTP_201_CREATED)
async def retry_batch(batch_job_id: str, service: ServiceDep, user=Depends(get_admin_user)) -> dict[str, str]:
    try:
        return service.retry_batch(batch_job_id)
    except (EpubServiceError, BatchServiceError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.put("/admin/concepts")
async def upsert_concept(
    form_data: ConceptUpsertForm, service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, str]:
    try:
        return service.upsert_concept(
            canonical_name=form_data.canonical_name,
            aliases=form_data.aliases,
            definition=form_data.definition,
            status=form_data.status,
        )
    except (EpubServiceError, IntegrityError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get("/admin/concepts")
async def list_concepts(
    service: ServiceDep,
    user=Depends(get_admin_user),
    concept_status: str | None = Query(
        default=None, alias="status", pattern="^(PROVISIONAL|APPROVED|REJECTED)$"
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """List concepts with aliases, status and mention counts for review.

    Without this an administrator cannot see the graph at all and therefore
    cannot find the duplicate pair a refused Batch item is waiting on.
    """
    try:
        return service.list_concepts(status=concept_status, offset=offset, limit=limit)
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/concepts/merge")
async def merge_concepts(
    form_data: ConceptMergeForm, service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, Any]:
    """Fold one concept into another so a refused Batch item can be retried."""
    try:
        return service.merge_concepts(
            target_concept_id=form_data.target_concept_id,
            source_concept_id=form_data.source_concept_id,
            canonical_name=form_data.canonical_name,
            merged_by=str(getattr(user, "id", "")).strip(),
        )
    except EpubResourceNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (EpubServiceError, IntegrityError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/concepts/split")
async def split_concept(
    form_data: ConceptSplitForm, service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, Any]:
    """Undo an over-eager merge by naming, explicitly, what becomes its own concept."""
    try:
        return service.split_concept(
            source_concept_id=form_data.source_concept_id,
            canonical_name=form_data.canonical_name,
            aliases=form_data.aliases,
            mentions=[mention.model_dump() for mention in form_data.mentions],
            split_by=str(getattr(user, "id", "")).strip(),
        )
    except EpubResourceNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (EpubServiceError, IntegrityError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get("/admin/relation-assertions")
async def list_relation_assertions(
    service: ServiceDep,
    user=Depends(get_admin_user),
    relation_status: str | None = Query(default="PROVISIONAL", alias="status", pattern="^(PROVISIONAL|APPROVED|REJECTED)$"),
    version_id: str | None = Query(default=None, min_length=1, max_length=128),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    try:
        return service.list_relation_assertions(
            status=relation_status, version_id=version_id, offset=offset, limit=limit
        )
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.put("/admin/relation-assertions/{assertion_id}")
async def review_relation_assertion(
    assertion_id: str,
    form_data: RelationAssertionReviewForm,
    service: ServiceDep,
    user=Depends(get_admin_user),
) -> dict[str, str]:
    try:
        return service.review_relation_assertion(assertion_id=assertion_id, status=form_data.status)
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/retrieval-units/{retrieval_unit_id}/index")
async def index_retrieval_unit(
    retrieval_unit_id: str, service: ServiceDep, user=Depends(get_admin_user)
) -> dict[str, Any]:
    try:
        return await service.index_retrieval_unit_async(retrieval_unit_id)
    except EpubServiceUnavailable as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post("/admin/versions/{version_id}/index")
async def index_epub_version(
    version_id: str,
    form_data: VersionIndexForm,
    service: ServiceDep,
    user=Depends(get_admin_user),
) -> dict[str, Any]:
    """Build or rebuild all derived vectors for a single immutable version."""
    try:
        return await service.index_version_retrieval_units_async(version_id, rebuild=form_data.rebuild)
    except EpubServiceUnavailable as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except EpubServiceError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get("/admin/runtime-status")
async def epub_runtime_status(request: Request, user=Depends(get_admin_user)) -> dict[str, Any]:
    """Expose server-owned EPUB runtime readiness to administrators only."""
    status_reader = getattr(request.app.state, "EPUB_CONCEPT_RUNTIME_STATUS", None)
    if not callable(status_reader):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="EPUB runtime health status is not configured on this server",
        )
    try:
        result = status_reader()
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"EPUB runtime health status is unavailable: {type(error).__name__}",
        ) from error
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="EPUB runtime health status returned an invalid response",
        )
    return result
