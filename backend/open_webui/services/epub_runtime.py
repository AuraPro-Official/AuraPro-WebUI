"""Production construction for the independent EPUB concept domain.

The HTTP router intentionally fails closed until this module constructs a
service.  Keeping this wiring separate from the router makes its storage and
credential decisions reviewable: the EPUB source store never aliases the main
WebUI database and browser requests cannot supply provider configuration.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from open_webui.retrieval.epub.batch import BatchProvider, OpenAIBatchProvider
from open_webui.retrieval.epub.inference import (
    AuraProEmbeddingAdapter,
    AuraProRerankerAdapter,
)
from open_webui.retrieval.epub.search import EpubSearchService
from open_webui.retrieval.epub.store import SQLiteEpubStore
from open_webui.retrieval.epub.sqlite_vec import SQLiteVecUnavailable
from open_webui.retrieval.epub.sqlite_vec_backend import SQLiteVecDerivedVectorBackend
from open_webui.retrieval.epub.vector_index import DerivedVectorIndexer
from open_webui.services.epub_concept import EpubConceptService


log = logging.getLogger(__name__)


class EpubRuntimeConfigurationError(ValueError):
    """The independent EPUB deployment configuration is unsafe or unsupported."""


def initialize_epub_concept_service(
    app: Any,
    *,
    data_dir: str | Path,
    environment: Mapping[str, str] | None = None,
) -> EpubConceptService:
    """Create and attach the local independent EPUB service.

    ``EPUB_CONCEPT_DATABASE_URL`` is deliberately separate from the main
    application database.  The current production implementation supports an
    independent SQLite file; a PostgreSQL URL is rejected instead of silently
    writing a remote-server library into the wrong database engine.
    """
    values = os.environ if environment is None else environment
    database_path = _sqlite_path(values, Path(data_dir))
    store = SQLiteEpubStore(database_path)
    providers = _batch_providers(values)
    embeddings, reranker = _aurapro_rag_models(app, values)
    vector_backend = None
    if embeddings is not None:
        try:
            vector_backend = SQLiteVecDerivedVectorBackend(store)
        except SQLiteVecUnavailable as error:
            log.warning("EPUB vector search is degraded: %s", error)
    search = EpubSearchService(
        source=store,
        vector_backend=vector_backend,
        embeddings=embeddings,
        reranker=reranker,
        concept_resolver=None,
    )
    indexer = (
        DerivedVectorIndexer(source=store, embeddings=embeddings, backend=vector_backend)
        if embeddings is not None and vector_backend is not None
        else None
    )
    service = EpubConceptService(
        store=store,
        providers=providers,
        search=search,
        vector_indexer=indexer,
        # Derived source windows bind to the configured local embedding profile
        # at import time.  This keeps later vector records profile-isolated.
        retrieval_embedding_profile=embeddings.profile if embeddings is not None else None,
    )
    app.state.EPUB_CONCEPT_STORE = store
    app.state.EPUB_CONCEPT_SERVICE = service
    log.info("Initialized independent EPUB concept store at %s", database_path)
    return service


def close_epub_concept_service(app: Any) -> None:
    """Release the process-local SQLite connection during application shutdown."""
    store = getattr(app.state, "EPUB_CONCEPT_STORE", None)
    if isinstance(store, SQLiteEpubStore):
        store.close()


def _sqlite_path(values: Mapping[str, str], data_dir: Path) -> Path:
    database_url = values.get("EPUB_CONCEPT_DATABASE_URL", "").strip()
    if database_url:
        parsed = urlparse(database_url)
        if parsed.scheme not in {"sqlite", ""}:
            raise EpubRuntimeConfigurationError(
                "EPUB_CONCEPT_DATABASE_URL requires a PostgreSQL adapter that is not installed; "
                "do not fall back to the main WebUI database"
            )
        if parsed.scheme == "sqlite":
            # Accept the ordinary absolute sqlite:///path form only.  Ambiguous
            # host-qualified forms are not a safe configuration for a shared
            # source library.
            if parsed.netloc not in {"", "localhost"}:
                raise EpubRuntimeConfigurationError(
                    "EPUB_CONCEPT_DATABASE_URL must use an absolute sqlite:/// path"
                )
            candidate = Path(parsed.path)
        else:
            candidate = Path(database_url)
    else:
        candidate = Path(values.get("EPUB_CONCEPT_DB_PATH", data_dir / "epub_concept_v1.db"))
    if str(candidate) == ":memory:":
        raise EpubRuntimeConfigurationError("the shared EPUB concept store cannot use SQLite :memory:")
    return candidate.expanduser().resolve()


def _batch_providers(values: Mapping[str, str]) -> dict[str, BatchProvider]:
    """Build optional offline providers from server-only administrator config."""
    api_key = values.get("EPUB_CONCEPT_BATCH_OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}
    completion_window = values.get("EPUB_CONCEPT_BATCH_OPENAI_COMPLETION_WINDOW", "24h").strip() or "24h"
    endpoint = values.get("EPUB_CONCEPT_BATCH_OPENAI_ENDPOINT", "/v1/chat/completions").strip()
    try:
        provider = OpenAIBatchProvider(
            api_key=api_key,
            endpoint=endpoint,
            completion_window=completion_window,
        )
    except Exception as error:
        # Browse/search/import remain usable.  Batch mutation routes explain
        # that no provider is configured instead of exposing a credential or
        # making a startup request to a cloud API.
        log.error("EPUB OpenAI Batch provider is unavailable: %s", type(error).__name__)
        return {}
    return {provider.name: provider}


def _aurapro_rag_models(app: Any, values: Mapping[str, str]):
    state = app.state
    embedding_profile = getattr(state, "EPUB_RAG_EMBEDDING_PROFILE", None)
    reranker_profile = getattr(state, "EPUB_RAG_RERANKER_PROFILE", None)
    timeout = float(values.get("EPUB_CONCEPT_LOCAL_MODEL_TIMEOUT_SECONDS", "30"))
    embeddings = (
        AuraProEmbeddingAdapter.from_app_state(
            app_state=state,
            event_loop=getattr(state, "main_loop", None),
            profile=embedding_profile,
            local_permitted=bool(getattr(state, "EPUB_RAG_EMBEDDING_LOCAL", False)),
            timeout_seconds=timeout,
        )
        if isinstance(embedding_profile, str) and embedding_profile
        else None
    )
    reranker = (
        AuraProRerankerAdapter.from_app_state(
            app_state=state,
            profile=reranker_profile,
            local_permitted=bool(getattr(state, "EPUB_RAG_RERANKER_LOCAL", False)),
        )
        if isinstance(reranker_profile, str) and reranker_profile
        else None
    )
    return embeddings, reranker
