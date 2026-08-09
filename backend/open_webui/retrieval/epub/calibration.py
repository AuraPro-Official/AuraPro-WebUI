"""Local-only concept prompt calibration against an approved llama.cpp runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .desktop_runtime import read_desktop_runtime_descriptor
from .inference import (
    LlamaCppTransport,
    LocalInferenceUnavailable,
    ModelAvailability,
    PrivateModelEndpoint,
    UrllibLlamaCppTransport,
)
from .prompt_profiles import (
    ConceptPayloadValidation,
    PromptProfileError,
    build_concept_completion_request,
    normalize_local_payload_offsets,
    select_stratified_passages,
    validate_concept_payload,
)


@dataclass(frozen=True, slots=True)
class CalibrationItemReport:
    """Content-free outcome of a single local calibration request."""

    passage_id: str
    ordinal: int
    toc_path: tuple[str, ...]
    valid: bool
    concept_count: int
    mention_count: int
    reason: str | None = None


class LocalConceptCalibrationRunner:
    """Run a deterministic sample without persisting or exposing passage text."""

    component = 'local-concept-calibration'

    def __init__(
        self,
        *,
        descriptor_path: str | Path,
        trusted_hostnames: frozenset[str] = frozenset(),
        timeout_seconds: float = 120,
        transport: LlamaCppTransport | None = None,
    ) -> None:
        path = Path(descriptor_path).expanduser()
        if not path.is_absolute():
            raise LocalInferenceUnavailable('Desktop calibration runtime descriptor path must be absolute')
        self._descriptor_path = path
        self._trusted_hostnames = trusted_hostnames
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def availability(self) -> ModelAvailability:
        try:
            endpoint, _ = self._runtime()
            response = self._transport_for_request().get_json(f'{endpoint.url.rstrip("/")}/health')
            if response.get('status') in {'ok', 'no slot available'}:
                return ModelAvailability.ready(self.component)
            return ModelAvailability.degraded(self.component, 'local llama.cpp health check did not report ready')
        except Exception as error:
            return ModelAvailability.degraded(self.component, _safe_reason(error))

    def run(self, *, passages: Sequence[Mapping[str, Any]], prompt_profile: str, sample_limit: int) -> dict[str, Any]:
        selected = select_stratified_passages(passages, limit=sample_limit)
        endpoint, model = self._runtime()
        transport = self._transport_for_request()
        reports = [
            self._evaluate_one(
                endpoint=endpoint,
                model=model,
                transport=transport,
                prompt_profile=prompt_profile,
                passage=passage,
            )
            for passage in selected
        ]
        valid = sum(1 for report in reports if report.valid)
        return {
            'mode': 'LOCAL_QWEN',
            'prompt_profile': prompt_profile,
            'model': model,
            'sample_count': len(reports),
            'chapter_count': len({report.toc_path[:1] for report in reports}),
            'valid_items': valid,
            'invalid_items': len(reports) - valid,
            'schema_valid_rate': valid / len(reports) if reports else 0.0,
            'concept_count': sum(report.concept_count for report in reports),
            'mention_count': sum(report.mention_count for report in reports),
            'items': [asdict(report) for report in reports],
        }

    def _runtime(self) -> tuple[PrivateModelEndpoint, str]:
        endpoint, model = read_desktop_runtime_descriptor(self._descriptor_path)
        return PrivateModelEndpoint(endpoint, trusted_hostnames=self._trusted_hostnames), model

    def _transport_for_request(self) -> LlamaCppTransport:
        return self._transport or UrllibLlamaCppTransport(timeout_seconds=self._timeout_seconds)

    @staticmethod
    def _evaluate_one(
        *,
        endpoint: PrivateModelEndpoint,
        model: str,
        transport: LlamaCppTransport,
        prompt_profile: str,
        passage: Mapping[str, Any],
    ) -> CalibrationItemReport:
        passage_id = str(passage.get('passage_id', ''))
        ordinal = int(passage.get('ordinal', 0))
        raw_path = passage.get('toc_path')
        toc_path = tuple(str(part) for part in raw_path) if isinstance(raw_path, (list, tuple)) else ()
        content = passage.get('content')
        if not passage_id or not isinstance(content, str) or not content:
            return CalibrationItemReport(passage_id, ordinal, toc_path, False, 0, 0, 'stored passage is invalid')
        try:
            request = build_concept_completion_request(
                model=model,
                profile_id=prompt_profile,
                passage=content,
                remote_structured_output=False,
            )
            response = transport.post_json(f'{endpoint.url.rstrip("/")}/v1/chat/completions', request)
            payload = normalize_local_payload_offsets(_completion_payload(response), passage=content)
            validation = validate_concept_payload(payload, passage=content)
        except (PromptProfileError, LocalInferenceUnavailable, ValueError) as error:
            validation = ConceptPayloadValidation(False, 0, 0, _safe_reason(error))
        except Exception as error:
            validation = ConceptPayloadValidation(False, 0, 0, _safe_reason(error))
        return CalibrationItemReport(
            passage_id,
            ordinal,
            toc_path,
            validation.valid,
            validation.concept_count,
            validation.mention_count,
            validation.reason,
        )


def _completion_payload(response: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = response.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise LocalInferenceUnavailable('local llama.cpp returned no completion choices')
    message = choices[0].get('message')
    if not isinstance(message, Mapping) or not isinstance(message.get('content'), str):
        raise LocalInferenceUnavailable('local llama.cpp returned no JSON completion')
    try:
        payload = json.loads(message['content'])
    except json.JSONDecodeError as error:
        raise LocalInferenceUnavailable('local llama.cpp returned invalid JSON') from error
    if not isinstance(payload, Mapping):
        raise LocalInferenceUnavailable('local llama.cpp returned a non-object JSON result')
    return payload


def _safe_reason(error: Exception) -> str:
    return (str(error).strip() or type(error).__name__)[:240]
