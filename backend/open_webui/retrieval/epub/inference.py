"""Local/private-only inference boundaries for EPUB search.

The EPUB domain must not accidentally inherit a cloud fallback from a generic
Open WebUI model adapter.  This module therefore validates the configured
endpoint before any request is made and exposes availability as data.  Callers
can render a degraded search state when a local model is unavailable instead of
substituting a public embedding, reranking, or LLM service.

The JSON protocol intentionally mirrors common self-hosted OpenAI-compatible
model runtimes, but transport is injected.  Application wiring can use an HTTP
transport; tests and deployments with a native runtime can use another private
transport without changing the policy boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import math
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse


class LocalInferenceError(RuntimeError):
    """A local inference contract or endpoint-policy requirement failed."""


class LocalEndpointRejected(LocalInferenceError):
    """The configured model endpoint is not explicitly local/private."""


class LocalInferenceUnavailable(LocalInferenceError):
    """A permitted local model service is not currently usable."""


@dataclass(frozen=True, slots=True)
class ModelAvailability:
    """A non-exceptional health result surfaced by search services."""

    available: bool
    component: str
    reason: str | None = None

    @classmethod
    def ready(cls, component: str) -> "ModelAvailability":
        return cls(available=True, component=component)

    @classmethod
    def degraded(cls, component: str, reason: str) -> "ModelAvailability":
        return cls(available=False, component=component, reason=reason)


class JsonTransport(Protocol):
    """Private transport injection seam. It must never select a fallback URL."""

    def post_json(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class EmbeddingService(Protocol):
    """Embedding model interface consumed by the derived vector index."""

    profile: str

    def availability(self) -> ModelAvailability: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class RerankerService(Protocol):
    """Cross-encoder interface consumed by the search pipeline."""

    profile: str

    def availability(self) -> ModelAvailability: ...

    def score(self, query: str, documents: Sequence[str]) -> list[float]: ...


class ConceptResolver(Protocol):
    """Small local LLM interface for Tier-2 concept resolution only."""

    profile: str

    def availability(self) -> ModelAvailability: ...

    def resolve(self, query: str, candidates: Sequence[str]) -> str | None: ...


@dataclass(frozen=True, slots=True)
class PrivateModelEndpoint:
    """An explicitly allowlisted local/private service URL.

    Private DNS names cannot be proven private without a deployment-specific
    resolver.  They are therefore allowed only when the administrator lists
    the exact host in ``trusted_hostnames``.  Literal IPs are restricted to
    loopback, RFC1918 private, link-local, or IPv6 unique-local addresses.
    """

    url: str
    trusted_hostnames: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"}:
            raise LocalEndpointRejected("local inference endpoint must use http or https")
        if not parsed.hostname:
            raise LocalEndpointRejected("local inference endpoint must include a host")
        if parsed.username or parsed.password:
            raise LocalEndpointRejected("local inference endpoint must not embed credentials")
        if parsed.query or parsed.fragment:
            raise LocalEndpointRejected("local inference endpoint must not include query or fragment data")
        try:
            _ = parsed.port
        except ValueError as error:
            raise LocalEndpointRejected("local inference endpoint has an invalid port") from error
        if not self._is_private_host(parsed.hostname):
            raise LocalEndpointRejected(
                "local inference endpoint host is neither local/private nor explicitly trusted"
            )

    def _is_private_host(self, host: str) -> bool:
        normalized = host.rstrip(".").casefold()
        if normalized == "localhost" or normalized.endswith(".localhost"):
            return True
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            return normalized in {name.rstrip(".").casefold() for name in self.trusted_hostnames}
        return bool(
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or getattr(address, "is_site_local", False)
        )


class _LocalJsonModel:
    """Shared availability and response validation for local model adapters."""

    component = "local-model"

    def __init__(
        self,
        *,
        endpoint: PrivateModelEndpoint,
        transport: JsonTransport,
        profile: str,
    ):
        if not profile or not profile.strip():
            raise LocalInferenceError("model profile cannot be empty")
        self._endpoint = endpoint
        self._transport = transport
        self.profile = profile

    def availability(self) -> ModelAvailability:
        try:
            # A small protocol-neutral health request is deliberate.  It does
            # not contain user text and still proves the configured private URL
            # is reachable through the injected transport.
            response = self._transport.post_json(self._endpoint.url, {"op": "health"})
            if response.get("available") is False:
                detail = response.get("reason")
                return ModelAvailability.degraded(
                    self.component, str(detail or "model service reported unavailable")
                )
            return ModelAvailability.ready(self.component)
        except Exception as error:  # The UI needs a degraded state, not a cloud retry.
            return ModelAvailability.degraded(self.component, _safe_reason(error))

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            response = self._transport.post_json(self._endpoint.url, payload)
        except Exception as error:
            raise LocalInferenceUnavailable(
                f"{self.component} is unavailable: {_safe_reason(error)}"
            ) from error
        if not isinstance(response, Mapping):
            raise LocalInferenceUnavailable(f"{self.component} returned a non-object response")
        if response.get("available") is False:
            raise LocalInferenceUnavailable(
                f"{self.component} is unavailable: {response.get('reason') or 'unknown reason'}"
            )
        return response


class LocalEmbeddingAdapter(_LocalJsonModel):
    """Embedding adapter for an explicitly private endpoint; never cloud-falls back."""

    component = "local-embedding"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        _require_nonempty_texts(texts, "embedding input")
        response = self._post({"op": "embed", "model": self.profile, "input": list(texts)})
        raw = response.get("embeddings")
        if raw is None and isinstance(response.get("data"), list):
            raw = [item.get("embedding") if isinstance(item, Mapping) else None for item in response["data"]]
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise LocalInferenceUnavailable("local-embedding returned an invalid embedding count")
        vectors = [_validated_vector(vector) for vector in raw]
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1:
            raise LocalInferenceUnavailable("local-embedding returned inconsistent dimensions")
        return vectors


class LocalRerankerAdapter(_LocalJsonModel):
    """Cross-encoder reranker adapter for an explicitly private endpoint."""

    component = "local-reranker"

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not isinstance(query, str) or not query:
            raise LocalInferenceError("reranker query cannot be empty")
        _require_nonempty_texts(documents, "reranker documents")
        response = self._post(
            {"op": "rerank", "model": self.profile, "query": query, "documents": list(documents)}
        )
        raw = response.get("scores")
        if not isinstance(raw, list) or len(raw) != len(documents):
            raise LocalInferenceUnavailable("local-reranker returned an invalid score count")
        scores: list[float] = []
        for value in raw:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise LocalInferenceUnavailable("local-reranker returned a non-finite score")
            scores.append(float(value))
        return scores


class LocalConceptResolverAdapter(_LocalJsonModel):
    """Tier-2 local LLM resolver. ``None`` means no confident resolution."""

    component = "local-concept-resolver"

    def resolve(self, query: str, candidates: Sequence[str]) -> str | None:
        if not isinstance(query, str) or not query:
            raise LocalInferenceError("concept query cannot be empty")
        _require_nonempty_texts(candidates, "concept candidates")
        response = self._post(
            {"op": "resolve_concept", "model": self.profile, "query": query, "candidates": list(candidates)}
        )
        resolved = response.get("concept")
        if resolved is None:
            return None
        if not isinstance(resolved, str):
            raise LocalInferenceUnavailable("local-concept-resolver returned a non-text concept")
        return resolved or None


def _require_nonempty_texts(values: Sequence[str], label: str) -> None:
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise LocalInferenceError(f"{label} must contain non-empty strings")


def _validated_vector(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or not value:
        raise LocalInferenceUnavailable("local-embedding returned an empty or invalid vector")
    result: list[float] = []
    for dimension in value:
        if isinstance(dimension, bool) or not isinstance(dimension, (int, float)) or not math.isfinite(dimension):
            raise LocalInferenceUnavailable("local-embedding returned a non-finite vector value")
        result.append(float(dimension))
    return result


def _safe_reason(error: Exception) -> str:
    """Keep a short availability reason; never include request payloads/secrets."""
    reason = str(error).strip() or type(error).__name__
    return reason[:240]
