"""Authenticated REST API for the independent EPUB concept domain.

This router intentionally does not share the legacy ``epub_concept``
prototype's global objects or unauthenticated routes.  Startup integration
sets ``app.state.EPUB_CONCEPT_SERVICE`` to an ``EpubConceptService`` configured
with the server's independent store and private model/provider adapters.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field

from open_webui.retrieval.epub.batch import BatchPayloadError, BatchServiceError
from open_webui.retrieval.epub.search import SearchError
from open_webui.retrieval.epub.store import IntegrityError
from open_webui.services.epub_concept import (
    EpubConceptService,
    EpubServiceError,
    EpubServiceUnavailable,
)
from open_webui.utils.auth import get_admin_user, get_verified_user


router = APIRouter(prefix="/api/v1/epub", tags=["epub"])
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class SearchForm(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)
    graph_offset: int = Field(default=0, ge=0)
    graph_limit: int = Field(default=20, ge=1, le=200)
    vector_limit: int = Field(default=10, ge=1, le=100)


class BatchDraftForm(BaseModel):
    version_id: str = Field(min_length=1, max_length=128)
    profile_name: str = Field(min_length=1, max_length=200)
    prompt_profile: str = Field(default="zh-glossary-v3", min_length=1, max_length=100)
    is_sample: bool = False
    sample_limit: int = Field(default=20, ge=1, le=500)


class SectionGraphBatchDraftForm(BaseModel):
    version_id: str = Field(min_length=1, max_length=128)
    profile_name: str = Field(min_length=1, max_length=200)
    is_sample: bool = False
    sample_limit: int = Field(default=20, ge=1, le=500)


class LocalCalibrationForm(BaseModel):
    version_id: str = Field(min_length=1, max_length=128)
    prompt_profile: str = Field(default="zh-glossary-v3", min_length=1, max_length=100)
    sample_limit: int = Field(default=20, ge=1, le=100)


class ConceptUpsertForm(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    definition: str = Field(default="", max_length=10_000)
    status: str = Field(default="APPROVED", pattern="^(PROVISIONAL|APPROVED|REJECTED)$")


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
async def poll_batch(batch_job_id: str, service: ServiceDep, user=Depends(get_admin_user)) -> dict[str, int | str]:
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
