"""Versioned EPUB data-domain interfaces and implementations."""

from .store import (
    DuplicateEpubError,
    EpubStore,
    IntegrityError,
    SQLiteEpubStore,
    VersionCreation,
)
from .sqlite_vec import SQLiteVecHealth, SQLiteVecUnavailable, load_sqlite_vec
from .inference import (
    ConceptResolver,
    EmbeddingService,
    JsonTransport,
    LocalConceptResolverAdapter,
    LocalEmbeddingAdapter,
    LocalEndpointRejected,
    LocalInferenceError,
    LocalInferenceUnavailable,
    LocalRerankerAdapter,
    ModelAvailability,
    PrivateModelEndpoint,
    RerankerService,
)
from .vector_index import (
    DerivedVectorBackend,
    DerivedVectorIndexer,
    DerivedVectorRecord,
    InMemoryDerivedVectorBackend,
    IndexingResult,
    VectorIndexError,
)

__all__ = [
    "DuplicateEpubError",
    "ConceptResolver",
    "DerivedVectorBackend",
    "DerivedVectorIndexer",
    "DerivedVectorRecord",
    "EmbeddingService",
    "EpubStore",
    "InMemoryDerivedVectorBackend",
    "IntegrityError",
    "IndexingResult",
    "JsonTransport",
    "LocalConceptResolverAdapter",
    "LocalEmbeddingAdapter",
    "LocalEndpointRejected",
    "LocalInferenceError",
    "LocalInferenceUnavailable",
    "LocalRerankerAdapter",
    "ModelAvailability",
    "PrivateModelEndpoint",
    "RerankerService",
    "SQLiteEpubStore",
    "SQLiteVecHealth",
    "SQLiteVecUnavailable",
    "VersionCreation",
    "VectorIndexError",
    "load_sqlite_vec",
]
