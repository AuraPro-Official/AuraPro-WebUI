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

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import ipaddress
import inspect
import json
import math
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.request import Request, urlopen
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
    def ready(cls, component: str) -> 'ModelAvailability':
        return cls(available=True, component=component)

    @classmethod
    def degraded(cls, component: str, reason: str) -> 'ModelAvailability':
        return cls(available=False, component=component, reason=reason)


class JsonTransport(Protocol):
    """Private transport injection seam. It must never select a fallback URL."""

    def post_json(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class UrllibJsonTransport:
    """Small server-side JSON transport for an already-approved private URL."""

    def __init__(self, *, timeout_seconds: float = 15.0):
        self._timeout_seconds = timeout_seconds

    def post_json(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:  # nosec B310: endpoint policy is enforced above
            value = json.loads(response.read().decode('utf-8'))
        if not isinstance(value, Mapping):
            raise LocalInferenceUnavailable('local model endpoint returned a non-object JSON response')
        return value


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
        if parsed.scheme not in {'http', 'https'}:
            raise LocalEndpointRejected('local inference endpoint must use http or https')
        if not parsed.hostname:
            raise LocalEndpointRejected('local inference endpoint must include a host')
        if parsed.username or parsed.password:
            raise LocalEndpointRejected('local inference endpoint must not embed credentials')
        if parsed.query or parsed.fragment:
            raise LocalEndpointRejected('local inference endpoint must not include query or fragment data')
        try:
            _ = parsed.port
        except ValueError as error:
            raise LocalEndpointRejected('local inference endpoint has an invalid port') from error
        if not self._is_private_host(parsed.hostname):
            raise LocalEndpointRejected('local inference endpoint host is neither local/private nor explicitly trusted')

    def _is_private_host(self, host: str) -> bool:
        normalized = host.rstrip('.').casefold()
        if normalized == 'localhost' or normalized.endswith('.localhost'):
            return True
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            return normalized in {name.rstrip('.').casefold() for name in self.trusted_hostnames}
        return bool(
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or getattr(address, 'is_site_local', False)
        )


class _LocalJsonModel:
    """Shared availability and response validation for local model adapters."""

    component = 'local-model'

    def __init__(
        self,
        *,
        endpoint: PrivateModelEndpoint,
        transport: JsonTransport,
        profile: str,
    ):
        if not profile or not profile.strip():
            raise LocalInferenceError('model profile cannot be empty')
        self._endpoint = endpoint
        self._transport = transport
        self.profile = profile

    def availability(self) -> ModelAvailability:
        try:
            # A small protocol-neutral health request is deliberate.  It does
            # not contain user text and still proves the configured private URL
            # is reachable through the injected transport.
            response = self._transport.post_json(self._endpoint.url, {'op': 'health'})
            if response.get('available') is False:
                detail = response.get('reason')
                return ModelAvailability.degraded(self.component, str(detail or 'model service reported unavailable'))
            return ModelAvailability.ready(self.component)
        except Exception as error:  # The UI needs a degraded state, not a cloud retry.
            return ModelAvailability.degraded(self.component, _safe_reason(error))

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            response = self._transport.post_json(self._endpoint.url, payload)
        except Exception as error:
            raise LocalInferenceUnavailable(f'{self.component} is unavailable: {_safe_reason(error)}') from error
        if not isinstance(response, Mapping):
            raise LocalInferenceUnavailable(f'{self.component} returned a non-object response')
        if response.get('available') is False:
            raise LocalInferenceUnavailable(
                f'{self.component} is unavailable: {response.get("reason") or "unknown reason"}'
            )
        return response


class LocalEmbeddingAdapter(_LocalJsonModel):
    """Embedding adapter for an explicitly private endpoint; never cloud-falls back."""

    component = 'local-embedding'

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        _require_nonempty_texts(texts, 'embedding input')
        response = self._post({'op': 'embed', 'model': self.profile, 'input': list(texts)})
        raw = response.get('embeddings')
        if raw is None and isinstance(response.get('data'), list):
            raw = [item.get('embedding') if isinstance(item, Mapping) else None for item in response['data']]
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise LocalInferenceUnavailable('local-embedding returned an invalid embedding count')
        vectors = [_validated_vector(vector) for vector in raw]
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1:
            raise LocalInferenceUnavailable('local-embedding returned inconsistent dimensions')
        return vectors


class LocalRerankerAdapter(_LocalJsonModel):
    """Cross-encoder reranker adapter for an explicitly private endpoint."""

    component = 'local-reranker'

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not isinstance(query, str) or not query:
            raise LocalInferenceError('reranker query cannot be empty')
        _require_nonempty_texts(documents, 'reranker documents')
        response = self._post({'op': 'rerank', 'model': self.profile, 'query': query, 'documents': list(documents)})
        raw = response.get('scores')
        if not isinstance(raw, list) or len(raw) != len(documents):
            raise LocalInferenceUnavailable('local-reranker returned an invalid score count')
        scores: list[float] = []
        for value in raw:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise LocalInferenceUnavailable('local-reranker returned a non-finite score')
            scores.append(float(value))
        return scores


class LocalConceptResolverAdapter(_LocalJsonModel):
    """Tier-2 local LLM resolver. ``None`` means no confident resolution."""

    component = 'local-concept-resolver'

    def resolve(self, query: str, candidates: Sequence[str]) -> str | None:
        if not isinstance(query, str) or not query:
            raise LocalInferenceError('concept query cannot be empty')
        _require_nonempty_texts(candidates, 'concept candidates')
        response = self._post(
            {'op': 'resolve_concept', 'model': self.profile, 'query': query, 'candidates': list(candidates)}
        )
        resolved = response.get('concept')
        if resolved is None:
            return None
        if not isinstance(resolved, str):
            raise LocalInferenceUnavailable('local-concept-resolver returned a non-text concept')
        return resolved or None


class LlamaCppTransport(Protocol):
    """The narrow HTTP surface exposed by ``llama-server``.

    It is intentionally separate from the generic EPUB JSON transport: llama.cpp
    exposes ``GET /v1/models`` and OpenAI-compatible ``POST
    /v1/chat/completions``.  Keeping these requests explicit prevents a resolver
    from accidentally inheriting an OpenAI cloud endpoint or a generic
    chat-client fallback.
    """

    def get_json(self, url: str) -> Mapping[str, Any]: ...

    def post_json(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class UrllibLlamaCppTransport:
    """Blocking transport used only behind the EPUB worker-thread boundary."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise LocalInferenceError('llama.cpp timeout_seconds must be positive')
        self._timeout_seconds = float(timeout_seconds)

    def get_json(self, url: str) -> Mapping[str, Any]:
        return self._request(Request(url, method='GET'))

    def post_json(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request(
            Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
        )

    def _request(self, request: Request) -> Mapping[str, Any]:
        with urlopen(request, timeout=self._timeout_seconds) as response:  # nosec B310: endpoint is policy-validated by resolver
            decoded = json.loads(response.read().decode('utf-8'))
        if not isinstance(decoded, Mapping):
            raise LocalInferenceUnavailable('llama.cpp returned a non-object JSON response')
        return decoded


LLAMA_CPP_LOADED_STATUS = 'loaded'


def llama_cpp_base_url(endpoint: PrivateModelEndpoint) -> str:
    """Normalize an approved endpoint to ``llama-server``'s root URL.

    Desktop registers ``${result.url}/v1`` with Open WebUI.  Administrators can
    configure either that familiar form or the llama-server root URL, so both
    are reduced to the root here and every llama.cpp path is appended to it.
    """
    base = endpoint.url.rstrip('/')
    return base[:-3] if base.endswith('/v1') else base


# Three degrade classes an operator has to be able to tell apart.  Holding
# nothing is the ordinary cold-start state and resolves itself at the user's
# first chat message; an inventory that cannot be read is a misconfiguration
# and will not resolve itself; an ambiguous one is a runtime in a shape this
# policy will not act on.
NOTHING_RESIDENT_REASON = (
    'no model is loaded in the local llama.cpp runtime; this tier borrows the model already in '
    'memory and never triggers a load, so it is unavailable until something else loads one'
)
INVENTORY_UNREADABLE_REASON = 'the local llama.cpp model inventory could not be read'
INVENTORY_AMBIGUOUS_REASON = 'the local llama.cpp model inventory is ambiguous'
INVENTORY_UNREACHABLE_REASON = 'the local llama.cpp model inventory is unreachable'


def _llama_cpp_model_id(entry: Mapping[str, Any]) -> str:
    identifier = entry.get('id')
    # llama-server reports whatever it was given, and a plain single-model
    # launch reports the model's full filesystem path rather than a short name.
    # That is why a configured model name is a tie-breaker and never a selector:
    # on such a runtime it could not match, and matching is not how this chooses.
    return identifier.strip() if isinstance(identifier, str) and identifier.strip() else ''


def _llama_cpp_entry_is_loaded(entry: Mapping[str, Any]) -> bool:
    status = entry.get('status')
    value = status.get('value') if isinstance(status, Mapping) else None
    # Only an explicit `loaded` counts.  `unloaded`, an unrecognised value and a
    # malformed status member are all "we cannot prove this is in memory".
    return value == LLAMA_CPP_LOADED_STATUS


def select_resident_llama_cpp_model(inventory: Mapping[str, Any], *, hint: str = '') -> str:
    """Return the model llama.cpp already holds in memory, or fail closed.

    llama.cpp's model router answers ``GET /v1/models`` with every *servable*
    entry, each carrying a ``status.value`` of ``loaded`` or ``unloaded``.  Only
    a loaded entry may be named in a completion request.  Desktop runs the
    router with ``--models-max 1``: naming any other servable entry evicts
    whatever the user is currently chatting with, and their next message evicts
    ours and reloads theirs.  A deployer's chat model can be several gigabytes,
    so a Tier-2 concept lookup that costs two model loads is far worse than a
    Tier-2 lookup that does not happen.  This tier therefore *borrows* the
    resident model and can never cause a load.

    A plain, non-router ``llama-server`` -- one model given on the command line,
    which is what the documented static-configuration route points at -- answers
    the same path with entries that carry **no** ``status`` member at all, only
    ``id``/``object``/``owned_by``/``created``/``meta``/``tags``/``aliases``.
    Requiring a status there would permanently degrade a runtime whose single
    model is loaded and serving, and report it as "no model is loaded".  So load
    state is read when the runtime reports it and inferred only when the runtime
    reports none *anywhere*: a lone entry on a server that has no concept of
    loading is the resident model, and there is nothing it could evict.  The
    exception cannot fire on a router, which stamps a status on every entry, so
    the eviction guarantee is untouched.  More than one status-less entry is
    left alone: residency is unprovable, and a single-model server cannot
    produce that shape.

    ``hint`` is the model named by Desktop's runtime descriptor or by static
    configuration.  It is a tie-breaker only: it can never promote an unloaded
    entry, and it is not consulted at all when exactly one model is resident --
    which is the only shape ``--models-max 1`` can normally produce.  When
    several are somehow resident and none matches, this refuses to guess rather
    than risk borrowing the wrong one.
    """
    # `data` and never `models`: llama-server answers with an OpenAI-style
    # `data` array *and* an ollama-style `models` array of a different shape,
    # and only `data` carries the router's load state.
    data = inventory.get('data')
    if not isinstance(data, list):
        raise LocalInferenceUnavailable(f'{INVENTORY_UNREADABLE_REASON}: it has no OpenAI-style `data` list')
    if not data:
        raise LocalInferenceUnavailable(NOTHING_RESIDENT_REASON)
    entries = tuple(entry for entry in data if isinstance(entry, Mapping))
    if not entries:
        raise LocalInferenceUnavailable(f'{INVENTORY_UNREADABLE_REASON}: its entries are not objects')

    if any('status' in entry for entry in entries):
        loaded: list[str] = []
        for entry in entries:
            identifier = _llama_cpp_model_id(entry)
            if identifier and _llama_cpp_entry_is_loaded(entry):
                loaded.append(identifier)
        resident = tuple(dict.fromkeys(loaded))
        if not resident:
            raise LocalInferenceUnavailable(NOTHING_RESIDENT_REASON)
        if len(resident) == 1:
            return resident[0]
        normalized = hint.strip() if isinstance(hint, str) else ''
        if normalized and normalized in resident:
            return normalized
        raise LocalInferenceUnavailable(
            f'{INVENTORY_AMBIGUOUS_REASON}: {len(resident)} models are loaded and none of them is the '
            'configured model; refusing to guess which one to borrow'
        )

    if len(data) != 1:
        raise LocalInferenceUnavailable(
            f'{INVENTORY_AMBIGUOUS_REASON}: it lists {len(data)} models and reports load state for '
            'none of them; refusing to guess which one to borrow'
        )
    identifier = _llama_cpp_model_id(entries[0])
    if not identifier:
        raise LocalInferenceUnavailable(f'{INVENTORY_UNREADABLE_REASON}: its only entry has no model id')
    return identifier


def resident_llama_cpp_model(*, transport: LlamaCppTransport, base_url: str, hint: str = '') -> str:
    """Ask a private llama.cpp runtime which model it currently holds."""
    try:
        inventory = transport.get_json(f'{base_url}/v1/models')
    except Exception as error:
        raise LocalInferenceUnavailable(f'{INVENTORY_UNREACHABLE_REASON}: {_safe_reason(error)}') from error
    if not isinstance(inventory, Mapping):
        raise LocalInferenceUnavailable(f'{INVENTORY_UNREADABLE_REASON}: it is not a JSON object')
    return select_resident_llama_cpp_model(inventory, hint=hint)


class LlamaCppConceptResolver:
    """Tier-2 resolver for AuraPro Desktop's local ``llama-server``.

    The synchronous search service is always called through
    :meth:`EpubConceptService.search_async`, which runs it in a worker thread.
    ``resolve_async`` is supplied for future native async orchestration and
    similarly moves the blocking stdlib transport off the event loop.  Neither
    method can select any endpoint other than the validated private one.

    ``profile`` is the model *hint* a deployment configured, not an instruction.
    Which model is actually requested is decided per operation by
    :func:`select_resident_llama_cpp_model` from the runtime's own inventory.
    """

    component = 'llama.cpp-concept-resolver'

    def __init__(
        self,
        *,
        endpoint: PrivateModelEndpoint,
        transport: LlamaCppTransport | None = None,
        profile: str = '',
        max_tokens: int = 96,
    ) -> None:
        if not isinstance(profile, str):
            raise LocalInferenceError('model profile hint must be a string')
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 512:
            raise LocalInferenceError('llama.cpp max_tokens must be an integer between 1 and 512')
        self._endpoint = endpoint
        self._transport = transport or UrllibLlamaCppTransport()
        self.profile = profile.strip()
        self._max_tokens = max_tokens
        self._base_url = llama_cpp_base_url(endpoint)

    def availability(self) -> ModelAvailability:
        """Report whether a model is actually resident, not merely reachable.

        ``GET /health`` deliberately is not used: a llama.cpp router with zero
        models in memory still answers it with ``{"status": "ok"}``, so health
        alone reported this tier ready and let it degrade later with a raw HTTP
        error from the completion request.  Reading the inventory instead is one
        loopback GET, the same cost the health request had, and it answers the
        question the caller is really asking.
        """
        try:
            self.resident_model()
        except Exception as error:
            return ModelAvailability.degraded(self.component, _safe_reason(error))
        return ModelAvailability.ready(self.component)

    def resident_model(self) -> str:
        """The model this runtime currently holds; raises when none is loaded."""
        return resident_llama_cpp_model(transport=self._transport, base_url=self._base_url, hint=self.profile)

    def resolve(self, query: str, candidates: Sequence[str]) -> str | None:
        if not isinstance(query, str) or not query.strip():
            raise LocalInferenceError('concept query cannot be empty')
        _require_nonempty_texts(candidates, 'concept candidates')
        unique_candidates = tuple(dict.fromkeys(candidates))
        # Deliberately re-read immediately before the completion request rather
        # than reusing what `availability()` just saw: a stale identifier is the
        # one way this tier could still name a model the runtime has since
        # evicted, which would load it back and evict the user's chat model.
        model = self.resident_model()
        payload = {
            'model': model,
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        'You resolve a user query to one existing EPUB concept. '
                        'Return only a JSON object with exactly one key, `concept`. '
                        'Its value must be one exact candidate string or null. Do not explain.'
                    ),
                },
                {
                    'role': 'user',
                    'content': json.dumps({'query': query, 'candidates': unique_candidates}, ensure_ascii=False),
                },
            ],
            'temperature': 0,
            'max_tokens': self._max_tokens,
            'stream': False,
            'response_format': {'type': 'json_object'},
        }
        try:
            response = self._transport.post_json(f'{self._base_url}/v1/chat/completions', payload)
        except Exception as error:
            raise LocalInferenceUnavailable(f'{self.component} is unavailable: {_safe_reason(error)}') from error
        if not isinstance(response, Mapping):
            raise LocalInferenceUnavailable('llama.cpp returned a non-object completion response')
        return self._parse_completion(response, unique_candidates)

    async def resolve_async(self, query: str, candidates: Sequence[str]) -> str | None:
        """Async-safe facade that never blocks the calling ASGI event loop."""
        return await asyncio.to_thread(self.resolve, query, candidates)

    def _parse_completion(self, response: Mapping[str, Any], candidates: Sequence[str]) -> str | None:
        choices = response.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise LocalInferenceUnavailable('llama.cpp returned no completion choices')
        message = choices[0].get('message')
        if not isinstance(message, Mapping) or not isinstance(message.get('content'), str):
            raise LocalInferenceUnavailable('llama.cpp returned a completion without text content')
        try:
            decoded = json.loads(message['content'])
        except (TypeError, json.JSONDecodeError) as error:
            raise LocalInferenceUnavailable('llama.cpp returned invalid concept JSON') from error
        # A bare JSON ``null`` is the abstention, spelled the shortest way the
        # model can spell it.  Measured against a local Qwen2.5-3B on the
        # acceptance store, it is what the model returns for *every* query it
        # declines — including all three out-of-domain controls — so reading it
        # as a malformed response reported the tier's single most valuable
        # behaviour as an unavailable resolver, and made a clean abstention
        # indistinguishable from a broken runtime in the degraded list.  The
        # schema stays strict everywhere it can carry a concept: only the
        # no-answer case has a second spelling, and it cannot smuggle one in.
        if decoded is None:
            return None
        if not isinstance(decoded, Mapping) or set(decoded) != {'concept'}:
            raise LocalInferenceUnavailable('llama.cpp concept JSON has an invalid schema')
        resolved = decoded.get('concept')
        if resolved is None:
            return None
        if not isinstance(resolved, str) or resolved not in candidates:
            raise LocalInferenceUnavailable('llama.cpp returned a concept outside the supplied candidates')
        return resolved


class AuraProEmbeddingAdapter:
    """Synchronously consume AuraPro's already-configured local embedding function.

    EPUB indexing and search deliberately execute their synchronous domain
    services in a worker thread.  AuraPro's ``EMBEDDING_FUNCTION`` is instead
    an async callable owned by the application event loop.  This adapter is
    the one narrow bridge between those two execution models: it submits the
    coroutine to the *existing* application loop with
    :func:`asyncio.run_coroutine_threadsafe` and waits from the worker thread.

    ``local_permitted`` is intentionally explicit.  The generic AuraPro RAG
    function can be backed by OpenAI, Azure, Ollama, or a local sentence
    transformer.  EPUB must not infer that a callable is local merely because
    it exists; runtime wiring has to prove the selected RAG configuration is
    local/private before this adapter can invoke it.  If that proof is absent,
    this class fails closed and does not call the supplied function.
    """

    component = 'aurapro-local-embedding'

    def __init__(
        self,
        *,
        embedding_function: Callable[..., Awaitable[Any]] | None,
        event_loop: asyncio.AbstractEventLoop | None,
        profile: str,
        local_permitted: bool,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not profile or not profile.strip():
            raise LocalInferenceError('model profile cannot be empty')
        if not isinstance(local_permitted, bool):
            raise LocalInferenceError('local_permitted must be a boolean')
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise LocalInferenceError('embedding timeout_seconds must be positive')
        self._embedding_function = embedding_function
        self._event_loop = event_loop
        self._local_permitted = local_permitted
        self._timeout_seconds = float(timeout_seconds)
        self.profile = profile

    @classmethod
    def from_app_state(
        cls,
        *,
        app_state: Any,
        event_loop: asyncio.AbstractEventLoop | None,
        profile: str,
        local_permitted: bool,
        timeout_seconds: float = 30.0,
    ) -> 'AuraProEmbeddingAdapter':
        """Construct from ``app.state`` without importing FastAPI at this layer."""
        return cls(
            embedding_function=getattr(app_state, 'EMBEDDING_FUNCTION', None),
            event_loop=event_loop,
            profile=profile,
            local_permitted=local_permitted,
            timeout_seconds=timeout_seconds,
        )

    def availability(self) -> ModelAvailability:
        reason = self._unavailable_reason()
        if reason is not None:
            return ModelAvailability.degraded(self.component, reason)
        return ModelAvailability.ready(self.component)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        _require_nonempty_texts(texts, 'embedding input')
        reason = self._unavailable_reason()
        if reason is not None:
            raise LocalInferenceUnavailable(f'{self.component} is unavailable: {reason}')
        if self._runs_on_configured_loop():
            # Blocking the application loop would deadlock the submitted
            # coroutine.  Callers must use the EPUB worker-thread boundary.
            raise LocalInferenceUnavailable(
                f'{self.component} cannot synchronously bridge from the application event loop'
            )
        assert self._event_loop is not None
        coroutine = self._invoke(list(texts))
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self._event_loop)
        except Exception as error:
            coroutine.close()
            raise LocalInferenceUnavailable(f'{self.component} is unavailable: {_safe_reason(error)}') from error
        try:
            raw = future.result(timeout=self._timeout_seconds)
        except FutureTimeoutError as error:
            future.cancel()
            raise LocalInferenceUnavailable(f'{self.component} is unavailable: embedding request timed out') from error
        except Exception as error:
            raise LocalInferenceUnavailable(f'{self.component} is unavailable: {_safe_reason(error)}') from error
        if not isinstance(raw, (list, tuple)) or len(raw) != len(texts):
            raise LocalInferenceUnavailable(f'{self.component} returned an invalid embedding count')
        vectors = [_validated_vector(vector) for vector in raw]
        if len({len(vector) for vector in vectors}) != 1:
            raise LocalInferenceUnavailable(f'{self.component} returned inconsistent dimensions')
        return vectors

    async def _invoke(self, texts: list[str]) -> Any:
        assert self._embedding_function is not None
        result = self._embedding_function(texts)
        if not inspect.isawaitable(result):
            raise LocalInferenceUnavailable('AuraPro EMBEDDING_FUNCTION must return an awaitable')
        return await result

    def _unavailable_reason(self) -> str | None:
        if not self._local_permitted:
            return 'AuraPro RAG embedding is not explicitly configured as local/private'
        if not callable(self._embedding_function):
            return 'AuraPro EMBEDDING_FUNCTION is not configured'
        if self._event_loop is None:
            return 'AuraPro application event loop is not configured'
        if self._event_loop.is_closed() or not self._event_loop.is_running():
            return 'AuraPro application event loop is not running'
        return None

    def _runs_on_configured_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._event_loop
        except RuntimeError:
            return False


@dataclass(frozen=True, slots=True)
class AuraProRerankDocument:
    """The minimal LangChain-compatible document surface AuraPro expects."""

    page_content: str


class AuraProRerankerAdapter:
    """Use AuraPro's configured local Cross-Encoder without an HTTP shim.

    AuraPro's reranking function expects document-like values with a
    ``page_content`` attribute.  EPUB owns immutable strings, so this adapter
    wraps each string in :class:`AuraProRerankDocument` at the boundary.
    ``local_permitted`` follows the same fail-closed policy as embedding.
    """

    component = 'aurapro-local-reranker'

    def __init__(
        self,
        *,
        reranking_function: Callable[[str, Sequence[AuraProRerankDocument]], Any] | None,
        profile: str,
        local_permitted: bool,
    ) -> None:
        if not profile or not profile.strip():
            raise LocalInferenceError('model profile cannot be empty')
        if not isinstance(local_permitted, bool):
            raise LocalInferenceError('local_permitted must be a boolean')
        self._reranking_function = reranking_function
        self._local_permitted = local_permitted
        self.profile = profile

    @classmethod
    def from_app_state(
        cls,
        *,
        app_state: Any,
        profile: str,
        local_permitted: bool,
    ) -> 'AuraProRerankerAdapter':
        """Construct from ``app.state`` without a FastAPI dependency."""
        return cls(
            reranking_function=getattr(app_state, 'RERANKING_FUNCTION', None),
            profile=profile,
            local_permitted=local_permitted,
        )

    def availability(self) -> ModelAvailability:
        reason = self._unavailable_reason()
        if reason is not None:
            return ModelAvailability.degraded(self.component, reason)
        return ModelAvailability.ready(self.component)

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not isinstance(query, str) or not query:
            raise LocalInferenceError('reranker query cannot be empty')
        _require_nonempty_texts(documents, 'reranker documents')
        reason = self._unavailable_reason()
        if reason is not None:
            raise LocalInferenceUnavailable(f'{self.component} is unavailable: {reason}')
        assert self._reranking_function is not None
        wrapped = [AuraProRerankDocument(page_content=document) for document in documents]
        try:
            raw = self._reranking_function(query, wrapped)
        except Exception as error:
            raise LocalInferenceUnavailable(f'{self.component} is unavailable: {_safe_reason(error)}') from error
        if isinstance(raw, (str, bytes, Mapping)):
            raise LocalInferenceUnavailable(f'{self.component} returned an invalid score list')
        try:
            values = list(raw)
        except TypeError as error:
            raise LocalInferenceUnavailable(f'{self.component} returned an invalid score list') from error
        if len(values) != len(documents):
            raise LocalInferenceUnavailable(f'{self.component} returned an invalid score count')
        scores: list[float] = []
        for value in values:
            if isinstance(value, bool):
                raise LocalInferenceUnavailable(f'{self.component} returned a non-finite score')
            try:
                score = float(value)
            except (TypeError, ValueError) as error:
                raise LocalInferenceUnavailable(f'{self.component} returned a non-finite score') from error
            if not math.isfinite(score):
                raise LocalInferenceUnavailable(f'{self.component} returned a non-finite score')
            scores.append(score)
        return scores

    def _unavailable_reason(self) -> str | None:
        if not self._local_permitted:
            return 'AuraPro RAG reranker is not explicitly configured as local/private'
        if not callable(self._reranking_function):
            return 'AuraPro RERANKING_FUNCTION is not configured'
        return None


def _require_nonempty_texts(values: Sequence[str], label: str) -> None:
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise LocalInferenceError(f'{label} must contain non-empty strings')


def _validated_vector(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or not value:
        raise LocalInferenceUnavailable('local-embedding returned an empty or invalid vector')
    result: list[float] = []
    for dimension in value:
        if isinstance(dimension, bool) or not isinstance(dimension, (int, float)) or not math.isfinite(dimension):
            raise LocalInferenceUnavailable('local-embedding returned a non-finite vector value')
        result.append(float(dimension))
    return result


def _safe_reason(error: Exception) -> str:
    """Keep a short availability reason; never include request payloads/secrets."""
    reason = str(error).strip() or type(error).__name__
    return reason[:240]
