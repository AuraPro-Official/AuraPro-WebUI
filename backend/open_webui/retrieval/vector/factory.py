import threading
from typing import Any, Optional

from open_webui.config import (
    ENABLE_MILVUS_MULTITENANCY_MODE,
    ENABLE_QDRANT_MULTITENANCY_MODE,
    VECTOR_DB,
)
from open_webui.retrieval.vector.main import GetResult, SearchResult, VectorDBBase, VectorItem
from open_webui.retrieval.vector.type import VectorType


class Vector:
    @staticmethod
    def get_vector(vector_type: str) -> VectorDBBase:
        """
        get vector db instance by vector type
        """
        match vector_type:
            case VectorType.MILVUS:
                if ENABLE_MILVUS_MULTITENANCY_MODE:
                    from open_webui.retrieval.vector.dbs.milvus_multitenancy import (
                        MilvusClient,
                    )

                    return MilvusClient()
                else:
                    from open_webui.retrieval.vector.dbs.milvus import MilvusClient

                    return MilvusClient()
            case VectorType.QDRANT:
                if ENABLE_QDRANT_MULTITENANCY_MODE:
                    from open_webui.retrieval.vector.dbs.qdrant_multitenancy import (
                        QdrantClient,
                    )

                    return QdrantClient()
                else:
                    from open_webui.retrieval.vector.dbs.qdrant import QdrantClient

                    return QdrantClient()
            case VectorType.PINECONE:
                from open_webui.retrieval.vector.dbs.pinecone import PineconeClient

                return PineconeClient()
            case VectorType.S3VECTOR:
                from open_webui.retrieval.vector.dbs.s3vector import S3VectorClient

                return S3VectorClient()
            case VectorType.OPENSEARCH:
                from open_webui.retrieval.vector.dbs.opensearch import OpenSearchClient

                return OpenSearchClient()
            case VectorType.PGVECTOR:
                from open_webui.retrieval.vector.dbs.pgvector import PgvectorClient

                return PgvectorClient()
            case VectorType.OPENGAUSS:
                from open_webui.retrieval.vector.dbs.opengauss import OpenGaussClient

                return OpenGaussClient()
            case VectorType.MARIADB_VECTOR:
                from open_webui.retrieval.vector.dbs.mariadb_vector import (
                    MariaDBVectorClient,
                )

                return MariaDBVectorClient()
            case VectorType.ELASTICSEARCH:
                from open_webui.retrieval.vector.dbs.elasticsearch import (
                    ElasticsearchClient,
                )

                return ElasticsearchClient()
            case VectorType.CHROMA:
                from open_webui.retrieval.vector.dbs.chroma import ChromaClient

                return ChromaClient()
            case VectorType.ORACLE23AI:
                from open_webui.retrieval.vector.dbs.oracle23ai import Oracle23aiClient

                return Oracle23aiClient()
            case VectorType.WEAVIATE:
                from open_webui.retrieval.vector.dbs.weaviate import WeaviateClient

                return WeaviateClient()
            case VectorType.VALKEY:
                from open_webui.retrieval.vector.dbs.valkey import ValkeyClient

                return ValkeyClient()
            case _:
                raise ValueError(f'Unsupported vector type: {vector_type}')


class LazyVectorDBClient(VectorDBBase):
    """Initialize the configured vector backend on first use.

    Most desktop sessions never touch RAG or memory search. Importing and
    creating Chroma (or another remote driver) during application startup
    adds latency and memory pressure without serving those sessions.
    """

    def __init__(self, vector_type: str):
        self._vector_type = vector_type
        self._client: VectorDBBase | None = None
        self._lock = threading.Lock()

    def _get_client(self) -> VectorDBBase:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = Vector.get_vector(self._vector_type)
        return self._client

    @property
    def client(self) -> Any:
        return self._get_client().client

    @property
    def supports_hybrid_search(self) -> bool:
        client = self._get_client()
        return type(client).hybrid_search is not VectorDBBase.hybrid_search

    def has_collection(self, collection_name: str) -> bool:
        return self._get_client().has_collection(collection_name)

    def delete_collection(self, collection_name: str) -> None:
        return self._get_client().delete_collection(collection_name)

    def insert(self, collection_name: str, items: list[VectorItem]) -> None:
        return self._get_client().insert(collection_name, items)

    def upsert(self, collection_name: str, items: list[VectorItem]) -> None:
        return self._get_client().upsert(collection_name, items)

    def search(
        self,
        collection_name: str,
        vectors: list[list[float | int]],
        filter: Optional[dict] = None,
        limit: int = 10,
    ) -> Optional[SearchResult]:
        return self._get_client().search(collection_name, vectors, filter, limit)

    def hybrid_search(
        self,
        collection_name: str,
        query: str,
        vectors: list[list[float | int]],
        filter: Optional[dict] = None,
        limit: int = 10,
        hybrid_bm25_weight: float = 0.5,
    ) -> Optional[SearchResult]:
        return self._get_client().hybrid_search(
            collection_name,
            query,
            vectors,
            filter,
            limit,
            hybrid_bm25_weight,
        )

    def query(
        self,
        collection_name: str,
        filter: dict,
        limit: Optional[int] = None,
    ) -> Optional[GetResult]:
        return self._get_client().query(collection_name, filter, limit)

    def get(self, collection_name: str) -> Optional[GetResult]:
        return self._get_client().get(collection_name)

    def delete(
        self,
        collection_name: str,
        ids: Optional[list[str]] = None,
        filter: Optional[dict] = None,
    ) -> None:
        return self._get_client().delete(collection_name, ids, filter)

    def reset(self) -> None:
        return self._get_client().reset()


VECTOR_DB_CLIENT = LazyVectorDBClient(VECTOR_DB)
