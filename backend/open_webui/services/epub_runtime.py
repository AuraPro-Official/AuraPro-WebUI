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
from open_webui.retrieval.epub.calibration import LocalConceptCalibrationRunner
from open_webui.retrieval.epub.inference import (
    AuraProEmbeddingAdapter,
    AuraProRerankerAdapter,
    LlamaCppConceptResolver,
    LocalEndpointRejected,
    LocalInferenceUnavailable,
    ModelAvailability,
    PrivateModelEndpoint,
    UrllibLlamaCppTransport,
)
from open_webui.retrieval.epub.desktop_runtime import (
    DesktopManagedLlamaCppConceptResolver,
    DesktopRuntimeDescriptorError,
)
from open_webui.retrieval.epub.search import EpubSearchService
from open_webui.retrieval.epub.store import SQLiteEpubStore
from open_webui.retrieval.epub.sqlite_vec import SQLiteVecUnavailable
from open_webui.retrieval.epub.sqlite_vec_backend import SQLiteVecDerivedVectorBackend
from open_webui.retrieval.epub.vector_index import DerivedVectorIndexer
from open_webui.services.epub_concept import EpubConceptService


log = logging.getLogger(__name__)

_TRUSTED_MODEL_HOSTS_ENV = 'EPUB_CONCEPT_TRUSTED_MODEL_HOSTNAMES'
_LLAMA_CPP_TRUSTED_MODEL_HOSTS_ENV = 'EPUB_CONCEPT_LOCAL_LLM_TRUSTED_HOSTNAMES'
_DESKTOP_LLM_RUNTIME_FILE_ENV = 'AURAPRO_DESKTOP_LLM_RUNTIME_FILE'


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
    concept_resolver, resolver_availability = _llama_cpp_concept_resolver(values)
    calibration_runner = _local_calibration_runner(values)
    vector_backend = None
    vector_availability: ModelAvailability
    if embeddings is not None:
        try:
            vector_backend = SQLiteVecDerivedVectorBackend(store)
            vector_availability = ModelAvailability.ready('sqlite-vec')
        except SQLiteVecUnavailable as error:
            log.warning('EPUB vector search is degraded: %s', error)
            vector_availability = ModelAvailability.degraded('sqlite-vec', _safe_reason(error))
    else:
        vector_availability = ModelAvailability.degraded(
            'sqlite-vec', 'a private EPUB embedding model is not configured'
        )
    search = EpubSearchService(
        source=store,
        vector_backend=vector_backend,
        embeddings=embeddings,
        reranker=reranker,
        concept_resolver=concept_resolver,
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
        calibration_runner=calibration_runner,
        # Derived source windows bind to the configured local embedding profile
        # at import time.  This keeps later vector records profile-isolated.
        retrieval_embedding_profile=embeddings.profile if embeddings is not None else None,
    )
    app.state.EPUB_CONCEPT_STORE = store
    app.state.EPUB_CONCEPT_SERVICE = service
    # This callable intentionally returns health data only: no model URL,
    # database path, or credential can reach a browser.  The admin API can
    # consume it later without coupling the independent domain service to
    # FastAPI's application state.
    app.state.EPUB_CONCEPT_RUNTIME_STATUS = lambda: _runtime_status(
        vector_backend=vector_backend,
        vector_availability=vector_availability,
        embeddings=embeddings,
        reranker=reranker,
        concept_resolver=concept_resolver,
        resolver_availability=resolver_availability,
    )
    log.info('Initialized independent EPUB concept store at %s', database_path)
    return service


def close_epub_concept_service(app: Any) -> None:
    """Release the process-local SQLite connection during application shutdown."""
    store = getattr(app.state, 'EPUB_CONCEPT_STORE', None)
    if isinstance(store, SQLiteEpubStore):
        store.close()


def configure_epub_rag_inference_policy(app_state: Any, rag_config: Mapping[str, Any]) -> None:
    """Record whether AuraPro's selected RAG functions are safe for EPUB.

    AuraPro's generic RAG configuration is allowed to use public OpenAI/Azure
    services.  EPUB cannot inherit that choice.  The built-in sentence
    transformer and built-in Cross-Encoder paths execute in-process.  Ollama
    is usable only after its concrete base URL passes the private endpoint
    check.  An administrator may explicitly trust a private DNS name through
    ``EPUB_CONCEPT_TRUSTED_MODEL_HOSTNAMES``; arbitrary DNS is never inferred
    to be private.
    """
    embedding_engine = str(rag_config.get('rag.embedding_engine') or '')
    reranking_engine = str(rag_config.get('rag.reranking_engine') or '')
    embedding_profile = rag_config.get('rag.embedding_model')
    reranker_profile = rag_config.get('rag.reranking_model')

    app_state.EPUB_RAG_EMBEDDING_PROFILE = (
        embedding_profile if isinstance(embedding_profile, str) and embedding_profile else None
    )
    app_state.EPUB_RAG_RERANKER_PROFILE = (
        reranker_profile if isinstance(reranker_profile, str) and reranker_profile else None
    )

    embedding_policy = _embedding_policy(
        engine=embedding_engine,
        endpoint=rag_config.get('rag.ollama.base_url'),
        trusted_hostnames=_trusted_model_hostnames(),
    )
    reranker_policy = _reranker_policy(engine=reranking_engine)
    app_state.EPUB_RAG_EMBEDDING_LOCAL = embedding_policy.available
    app_state.EPUB_RAG_RERANKER_LOCAL = reranker_policy.available
    app_state.EPUB_RAG_EMBEDDING_POLICY = embedding_policy
    app_state.EPUB_RAG_RERANKER_POLICY = reranker_policy


def _embedding_policy(*, engine: str, endpoint: Any, trusted_hostnames: frozenset[str]) -> ModelAvailability:
    if engine == '':
        return ModelAvailability.ready('aurapro-local-embedding-policy')
    if engine != 'ollama':
        return ModelAvailability.degraded(
            'aurapro-local-embedding-policy',
            'EPUB permits only the built-in local embedding engine or a verified private Ollama endpoint',
        )
    if not isinstance(endpoint, str) or not endpoint.strip():
        return ModelAvailability.degraded(
            'aurapro-local-embedding-policy', 'Ollama embedding endpoint is not configured'
        )
    try:
        PrivateModelEndpoint(endpoint.strip(), trusted_hostnames=trusted_hostnames)
    except LocalEndpointRejected:
        return ModelAvailability.degraded(
            'aurapro-local-embedding-policy',
            'configured Ollama embedding endpoint is not local/private or explicitly trusted',
        )
    return ModelAvailability.ready('aurapro-local-embedding-policy')


def _reranker_policy(*, engine: str) -> ModelAvailability:
    if engine == '':
        return ModelAvailability.ready('aurapro-local-reranker-policy')
    return ModelAvailability.degraded(
        'aurapro-local-reranker-policy',
        "EPUB permits only AuraPro's built-in local Cross-Encoder reranker",
    )


def _trusted_model_hostnames() -> frozenset[str]:
    return _trusted_hostnames(os.environ, _TRUSTED_MODEL_HOSTS_ENV)


def _trusted_hostnames(values: Mapping[str, str], key: str) -> frozenset[str]:
    return frozenset(value.strip().casefold() for value in values.get(key, '').split(',') if value.strip())


def _runtime_status(
    *,
    vector_backend: SQLiteVecDerivedVectorBackend | None,
    vector_availability: ModelAvailability,
    embeddings: AuraProEmbeddingAdapter | None,
    reranker: AuraProRerankerAdapter | None,
    concept_resolver: LlamaCppConceptResolver | DesktopManagedLlamaCppConceptResolver | None,
    resolver_availability: ModelAvailability,
) -> dict[str, Any]:
    """Return a fresh, credential-free runtime health snapshot."""
    vector = vector_availability
    if vector_backend is not None:
        try:
            health = vector_backend.healthcheck()
            vector = ModelAvailability.ready('sqlite-vec')
            vector_payload: dict[str, Any] = {**_availability_payload(vector), 'version': health.version}
        except SQLiteVecUnavailable as error:
            vector_payload = _availability_payload(ModelAvailability.degraded('sqlite-vec', _safe_reason(error)))
    else:
        vector_payload = _availability_payload(vector)
    return {
        'vector_index': vector_payload,
        'embedding': _model_status(embeddings, 'aurapro-local-embedding'),
        'reranker': _model_status(reranker, 'aurapro-local-reranker'),
        'concept_resolver': (
            _model_status(concept_resolver, LlamaCppConceptResolver.component)
            if concept_resolver is not None
            else _availability_payload(resolver_availability)
        ),
    }


def _model_status(model: Any | None, component: str) -> dict[str, Any]:
    if model is None:
        return _availability_payload(ModelAvailability.degraded(component, 'not configured'))
    return _availability_payload(model.availability())


def _availability_payload(availability: ModelAvailability) -> dict[str, Any]:
    return {
        'available': availability.available,
        'component': availability.component,
        'reason': availability.reason,
    }


def _sqlite_path(values: Mapping[str, str], data_dir: Path) -> Path:
    database_url = values.get('EPUB_CONCEPT_DATABASE_URL', '').strip()
    if database_url:
        parsed = urlparse(database_url)
        if parsed.scheme not in {'sqlite', ''}:
            raise EpubRuntimeConfigurationError(
                'EPUB_CONCEPT_DATABASE_URL requires a PostgreSQL adapter that is not installed; '
                'do not fall back to the main WebUI database'
            )
        if parsed.scheme == 'sqlite':
            # Accept the ordinary absolute sqlite:///path form only.  Ambiguous
            # host-qualified forms are not a safe configuration for a shared
            # source library.
            if parsed.netloc not in {'', 'localhost'}:
                raise EpubRuntimeConfigurationError('EPUB_CONCEPT_DATABASE_URL must use an absolute sqlite:/// path')
            candidate = Path(parsed.path)
        else:
            candidate = Path(database_url)
    else:
        candidate = Path(values.get('EPUB_CONCEPT_DB_PATH', data_dir / 'epub_concept_v1.db'))
    if str(candidate) == ':memory:':
        raise EpubRuntimeConfigurationError('the shared EPUB concept store cannot use SQLite :memory:')
    return candidate.expanduser().resolve()


def _batch_providers(values: Mapping[str, str]) -> dict[str, BatchProvider]:
    """Build optional offline providers from server-only administrator config."""
    api_key = values.get('EPUB_CONCEPT_BATCH_OPENAI_API_KEY', '').strip()
    if not api_key:
        return {}
    completion_window = values.get('EPUB_CONCEPT_BATCH_OPENAI_COMPLETION_WINDOW', '24h').strip() or '24h'
    endpoint = values.get('EPUB_CONCEPT_BATCH_OPENAI_ENDPOINT', '/v1/chat/completions').strip()
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
        log.error('EPUB OpenAI Batch provider is unavailable: %s', type(error).__name__)
        return {}
    return {provider.name: provider}


def _aurapro_rag_models(app: Any, values: Mapping[str, str]):
    state = app.state
    embedding_profile = getattr(state, 'EPUB_RAG_EMBEDDING_PROFILE', None)
    reranker_profile = getattr(state, 'EPUB_RAG_RERANKER_PROFILE', None)
    timeout = float(values.get('EPUB_CONCEPT_LOCAL_MODEL_TIMEOUT_SECONDS', '30'))
    embeddings = (
        AuraProEmbeddingAdapter.from_app_state(
            app_state=state,
            event_loop=getattr(state, 'main_loop', None),
            profile=embedding_profile,
            local_permitted=bool(getattr(state, 'EPUB_RAG_EMBEDDING_LOCAL', False)),
            timeout_seconds=timeout,
        )
        if isinstance(embedding_profile, str) and embedding_profile
        else None
    )
    reranker = (
        AuraProRerankerAdapter.from_app_state(
            app_state=state,
            profile=reranker_profile,
            local_permitted=bool(getattr(state, 'EPUB_RAG_RERANKER_LOCAL', False)),
        )
        if isinstance(reranker_profile, str) and reranker_profile
        else None
    )
    return embeddings, reranker


def _llama_cpp_concept_resolver(
    values: Mapping[str, str],
) -> tuple[LlamaCppConceptResolver | DesktopManagedLlamaCppConceptResolver | None, ModelAvailability]:
    """Build the Tier-2 resolver only from server-owned private settings."""
    desktop_runtime_file = values.get(_DESKTOP_LLM_RUNTIME_FILE_ENV, '').strip()
    if desktop_runtime_file:
        # A configured Desktop handoff is authoritative. If Desktop is still
        # downloading or has stopped the runtime, do not fall back to stale
        # development-only static settings.
        try:
            timeout = _positive_timeout(values.get('EPUB_CONCEPT_LOCAL_LLM_TIMEOUT_SECONDS', '30'))
            max_tokens = _positive_int(values.get('EPUB_CONCEPT_LOCAL_LLM_MAX_TOKENS', '96'), minimum=1, maximum=512)
            return (
                DesktopManagedLlamaCppConceptResolver(
                    descriptor_path=desktop_runtime_file,
                    trusted_hostnames=_trusted_hostnames(values, _LLAMA_CPP_TRUSTED_MODEL_HOSTS_ENV),
                    timeout_seconds=timeout,
                    max_tokens=max_tokens,
                ),
                ModelAvailability.ready(LlamaCppConceptResolver.component),
            )
        except (DesktopRuntimeDescriptorError, ValueError) as error:
            log.warning('EPUB Desktop Tier-2 resolver is disabled: %s', _safe_reason(error))
            return None, ModelAvailability.degraded(LlamaCppConceptResolver.component, _safe_reason(error))
    endpoint = values.get('EPUB_CONCEPT_LOCAL_LLM_ENDPOINT', '').strip()
    profile = values.get('EPUB_CONCEPT_LOCAL_LLM_MODEL', '').strip()
    component = LlamaCppConceptResolver.component
    if not endpoint and not profile:
        return None, ModelAvailability.degraded(component, 'not configured')
    if not endpoint or not profile:
        return None, ModelAvailability.degraded(
            component, 'both EPUB_CONCEPT_LOCAL_LLM_ENDPOINT and EPUB_CONCEPT_LOCAL_LLM_MODEL are required'
        )
    try:
        private_endpoint = PrivateModelEndpoint(
            endpoint,
            trusted_hostnames=_trusted_hostnames(values, _LLAMA_CPP_TRUSTED_MODEL_HOSTS_ENV),
        )
        timeout = _positive_timeout(values.get('EPUB_CONCEPT_LOCAL_LLM_TIMEOUT_SECONDS', '30'))
        max_tokens = _positive_int(values.get('EPUB_CONCEPT_LOCAL_LLM_MAX_TOKENS', '96'), minimum=1, maximum=512)
        return (
            LlamaCppConceptResolver(
                endpoint=private_endpoint,
                transport=UrllibLlamaCppTransport(timeout_seconds=timeout),
                profile=profile,
                max_tokens=max_tokens,
            ),
            ModelAvailability.ready(component),
        )
    except (LocalEndpointRejected, ValueError) as error:
        log.warning('EPUB Tier-2 resolver is disabled: %s', _safe_reason(error))
        return None, ModelAvailability.degraded(component, _safe_reason(error))


def _local_calibration_runner(values: Mapping[str, str]) -> LocalConceptCalibrationRunner | None:
    """Calibration is deliberately available only through Desktop's live descriptor."""
    descriptor_path = values.get(_DESKTOP_LLM_RUNTIME_FILE_ENV, '').strip()
    if not descriptor_path:
        return None
    try:
        timeout = _positive_timeout(values.get('EPUB_CONCEPT_LOCAL_LLM_TIMEOUT_SECONDS', '30'))
        return LocalConceptCalibrationRunner(
            descriptor_path=descriptor_path,
            trusted_hostnames=_trusted_hostnames(values, _LLAMA_CPP_TRUSTED_MODEL_HOSTS_ENV),
            timeout_seconds=max(timeout, 120),
        )
    except (DesktopRuntimeDescriptorError, LocalInferenceUnavailable, ValueError) as error:
        log.warning('EPUB local prompt calibration is disabled: %s', _safe_reason(error))
        return None


def _positive_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError('local llama.cpp timeout must be a positive number') from error
    if parsed <= 0:
        raise ValueError('local llama.cpp timeout must be a positive number')
    return parsed


def _positive_int(value: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError('local llama.cpp max tokens must be an integer') from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f'local llama.cpp max tokens must be between {minimum} and {maximum}')
    return parsed


def _safe_reason(error: Exception) -> str:
    """Return a short operational reason without configuration values/secrets."""
    return (str(error).strip() or type(error).__name__)[:240]
