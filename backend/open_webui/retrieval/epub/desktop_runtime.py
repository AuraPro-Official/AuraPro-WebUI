"""Desktop-owned llama.cpp runtime handoff for EPUB concept resolution.

AuraPro Desktop owns the process that launches ``llama-server``. WebUI must
not hard-code that process's loopback port: Desktop can select a new port each
time it starts or restarts. The only configuration WebUI accepts is the path
to Desktop's local runtime descriptor, which it re-reads for each operation.

Desktop atomically replaces the descriptor named by
``AURAPRO_DESKTOP_LLM_RUNTIME_FILE``. Version 1 is credential-free::

    {"version": 1, "llama_cpp": {
        "endpoint": "http://127.0.0.1:18881", "active_model": true
    }}

An absent, partial, or invalid descriptor is a degraded local runtime, never a
reason to use a static endpoint or a cloud fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

from .inference import (
    LlamaCppConceptResolver,
    LlamaCppTransport,
    LocalInferenceUnavailable,
    ModelAvailability,
    PrivateModelEndpoint,
    UrllibLlamaCppTransport,
)


class DesktopRuntimeDescriptorError(LocalInferenceUnavailable):
    """Desktop's local runtime handoff cannot safely be consumed."""


class DesktopManagedLlamaCppConceptResolver:
    """Resolve concepts through Desktop's current private llama.cpp runtime."""

    component = LlamaCppConceptResolver.component
    # The actual model profile is sourced from every descriptor snapshot.
    profile = 'aurapro-desktop-managed'

    def __init__(
        self,
        *,
        descriptor_path: str | Path,
        trusted_hostnames: frozenset[str] = frozenset(),
        timeout_seconds: float = 30,
        max_tokens: int = 96,
        transport: LlamaCppTransport | None = None,
    ) -> None:
        candidate = Path(descriptor_path).expanduser()
        if not candidate.is_absolute():
            raise DesktopRuntimeDescriptorError('Desktop runtime descriptor path must be absolute')
        self._descriptor_path = candidate
        self._trusted_hostnames = trusted_hostnames
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._transport = transport

    def availability(self) -> ModelAvailability:
        try:
            return self._resolver().availability()
        except Exception as error:
            return ModelAvailability.degraded(self.component, _safe_reason(error))

    def resolve(self, query: str, candidates: Sequence[str]) -> str | None:
        return self._resolver().resolve(query, candidates)

    def _resolver(self) -> LlamaCppConceptResolver:
        endpoint, profile = resolve_desktop_runtime(
            self._descriptor_path,
            transport=self._transport or UrllibLlamaCppTransport(timeout_seconds=self._timeout_seconds),
            trusted_hostnames=self._trusted_hostnames,
        )
        return LlamaCppConceptResolver(
            endpoint=endpoint,
            transport=self._transport or UrllibLlamaCppTransport(timeout_seconds=self._timeout_seconds),
            profile=profile,
            max_tokens=self._max_tokens,
        )


def read_desktop_runtime_descriptor(descriptor_path: str | Path) -> tuple[str, str | None]:
    """Read one atomic Desktop runtime descriptor snapshot without caching it."""
    path = Path(descriptor_path).expanduser()
    if not path.is_absolute():
        raise DesktopRuntimeDescriptorError('Desktop runtime descriptor path must be absolute')
    try:
        with path.open('r', encoding='utf-8') as descriptor_file:
            decoded = json.load(descriptor_file)
    except FileNotFoundError as error:
        raise DesktopRuntimeDescriptorError('Desktop local runtime is not running') from error
    except (OSError, json.JSONDecodeError) as error:
        raise DesktopRuntimeDescriptorError('Desktop runtime descriptor is unreadable') from error

    if not isinstance(decoded, Mapping) or decoded.get('version') != 1:
        raise DesktopRuntimeDescriptorError('Desktop runtime descriptor has an unsupported schema')
    llama_cpp = decoded.get('llama_cpp')
    if not isinstance(llama_cpp, Mapping):
        raise DesktopRuntimeDescriptorError('Desktop runtime descriptor has no llama.cpp runtime')
    endpoint = llama_cpp.get('endpoint')
    model = llama_cpp.get('model')
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise DesktopRuntimeDescriptorError('Desktop runtime descriptor has no local endpoint')
    if llama_cpp.get('active_model') is True:
        return endpoint.strip(), None
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise DesktopRuntimeDescriptorError('Desktop runtime descriptor has no valid model identifier')
    if any(character in model for character in '\r\n\x00'):
        raise DesktopRuntimeDescriptorError('Desktop runtime descriptor has no valid model identifier')
    return endpoint.strip(), model.strip()


def resolve_desktop_runtime(
    descriptor_path: str | Path,
    *,
    transport: LlamaCppTransport,
    trusted_hostnames: frozenset[str] = frozenset(),
) -> tuple[PrivateModelEndpoint, str]:
    """Use the model currently loaded by chat without loading another model.

    Router mode reports loaded/sleeping states; a single-model server omits
    status. Re-read on every operation so chat model switches take effect.
    """
    url, model = read_desktop_runtime_descriptor(descriptor_path)
    endpoint = PrivateModelEndpoint(url, trusted_hostnames=trusted_hostnames)
    if model is not None:
        return endpoint, model
    response = transport.get_json(f'{endpoint.url.rstrip("/")}/v1/models')
    data = response.get('data')
    if not isinstance(data, list):
        raise DesktopRuntimeDescriptorError('Desktop local runtime returned no model list')
    active = []
    for entry in data:
        if not isinstance(entry, Mapping):
            continue
        status = entry.get('status')
        if 'status' in entry and (not isinstance(status, Mapping) or status.get('value') not in {'loaded', 'sleeping'}):
            continue
        identifier = entry.get('id')
        if (
            isinstance(identifier, str)
            and identifier.strip()
            and len(identifier) <= 256
            and not any(character in identifier for character in '\r\n\x00')
        ):
            active.append(identifier.strip())
    if len(active) != 1:
        raise DesktopRuntimeDescriptorError('Activate one local model in WebUI chat before using EPUB model features')
    return endpoint, active[0]


def _safe_reason(error: Exception) -> str:
    return (str(error).strip() or type(error).__name__)[:240]
