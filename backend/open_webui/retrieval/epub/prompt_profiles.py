"""Versioned, model-neutral concept-extraction prompt profiles.

Profiles are deliberately source-code assets rather than administrator-supplied
free text.  That makes a local calibration replayable by a later cloud Batch
run without allowing a browser to change the extraction policy or smuggle
provider configuration into a durable request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class PromptProfileError(ValueError):
    """A requested concept-extraction profile or result is unsafe to use."""


@dataclass(frozen=True, slots=True)
class ConceptPromptProfile:
    """A stable extraction instruction and its output constraints."""

    profile_id: str
    system_instruction: str
    max_tokens: int = 1_200
    temperature: float = 0.0


@dataclass(frozen=True, slots=True)
class ConceptPayloadValidation:
    """Content-free validation outcome for one immutable source passage."""

    valid: bool
    concept_count: int
    mention_count: int
    reason: str | None = None


DEFAULT_CONCEPT_PROMPT_PROFILE = 'zh-glossary-v3'


# The strict schema is suitable for a remote Chat Completions model that
# supports Structured Outputs. Local llama.cpp still uses json_object plus the
# same textual contract because smaller GGUF models and server builds do not
# reliably implement the complete json_schema surface.
CONCEPT_OUTPUT_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['concepts'],
    'properties': {
        'concepts': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['name', 'aliases', 'definition', 'mentions'],
                'properties': {
                    'name': {'type': 'string', 'minLength': 1},
                    'aliases': {'type': 'array', 'items': {'type': 'string'}},
                    'definition': {'type': 'string'},
                    'mentions': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'additionalProperties': False,
                            'required': ['start_codepoint', 'end_codepoint', 'evidence'],
                            'properties': {
                                'start_codepoint': {'type': 'integer', 'minimum': 0},
                                'end_codepoint': {'type': 'integer', 'minimum': 1},
                                'evidence': {'type': 'string', 'minLength': 1},
                            },
                        },
                    },
                },
            },
        }
    },
}


_PROFILES: dict[str, ConceptPromptProfile] = {
    'zh-glossary-v1': ConceptPromptProfile(
        profile_id='zh-glossary-v1',
        system_instruction=(
            '你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、'
            '在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。'
            '不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。'
            '每个概念必须有至少一个出现位置；name 是最适合索引的规范写法，aliases 只包含本段可见的等价写法。'
            'definition 用一句简短中文说明，且只能依据本段。mentions 的 start_codepoint 从 0 开始，'
            'end_codepoint 为排他位置，evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，'
            '包括标点和空格。没有合格概念时返回 concepts 的空数组。'
            '仅返回符合指定 JSON 结构的对象，不要 Markdown 或解释。'
        ),
    ),
    'zh-glossary-v2': ConceptPromptProfile(
        profile_id='zh-glossary-v2',
        max_tokens=512,
        system_instruction=(
            '你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、'
            '在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。'
            '不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。'
            '每段最多抽取 6 个最值得检索的概念。name 是最适合索引的规范写法；aliases 最多 2 个，'
            '且只包含本段可见的等价写法；definition 是不超过 30 个汉字的一句说明，且只能依据本段。'
            '每个概念只保留一个最有代表性的出现位置。mentions 的 start_codepoint 从 0 开始，'
            'end_codepoint 为排他位置，evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，'
            '包括标点和空格。没有合格概念时返回 {"concepts":[]}。'
            '输出必须是一个 JSON 对象：第一个字符必须是 {，唯一顶层键必须是 concepts；'
            '不得返回 JSON 数组、JSON 字符串、Markdown 代码块或任何解释。'
        ),
    ),
    DEFAULT_CONCEPT_PROMPT_PROFILE: ConceptPromptProfile(
        profile_id=DEFAULT_CONCEPT_PROMPT_PROFILE,
        max_tokens=512,
        system_instruction=(
            '你是中文 EPUB 的术语与专名抽取器。只抽取读者可能需要检索或解释的、'
            '在本段中有明确依据的专有名词、人物、组织、地点、事件、制度、作品名或专业术语。'
            '不要抽取普通功能词、泛化主题、纯修辞、没有可验证文本依据的推测，也不要根据外部知识补充事实。'
            '每段最多抽取 6 个最值得检索的概念。name 是最适合索引的规范写法；aliases 最多 2 个，'
            '且只包含本段可见的等价写法；definition 是不超过 30 个汉字的一句说明，且只能依据本段。'
            '每个概念必须有且只能有 name、aliases、definition、mentions 四个字段；mentions 不能为空，'
            '且只保留一个最有代表性的出现位置。每个 mention 必须有且只能有 start_codepoint、'
            'end_codepoint、evidence 三个字段；start_codepoint 从 0 开始，end_codepoint 为排他位置，'
            'evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，包括标点和空格。'
            '没有合格概念时返回 {"concepts":[]}。输出形状必须为'
            '{"concepts":[{"name":"…","aliases":[],"definition":"…",'
            '"mentions":[{"start_codepoint":0,"end_codepoint":1,"evidence":"…"}]}]}。'
            '输出必须是一个 JSON 对象：第一个字符必须是 {，唯一顶层键必须是 concepts；'
            '不得返回 JSON 数组、JSON 字符串、Markdown 代码块或任何解释。'
        ),
    ),
}


def available_prompt_profiles() -> tuple[str, ...]:
    return tuple(_PROFILES)


def get_prompt_profile(profile_id: str) -> ConceptPromptProfile:
    profile = _PROFILES.get(profile_id)
    if profile is None:
        raise PromptProfileError(f'unknown EPUB concept prompt profile: {profile_id}')
    return profile


def build_concept_completion_request(
    *,
    model: str,
    profile_id: str,
    passage: str,
    remote_structured_output: bool,
) -> dict[str, Any]:
    """Build the exact model request used by local and remote calibration."""
    if not model.strip():
        raise PromptProfileError('concept extraction model cannot be empty')
    if not passage:
        raise PromptProfileError('concept extraction passage cannot be empty')
    profile = get_prompt_profile(profile_id)
    response_format: dict[str, Any]
    if remote_structured_output:
        response_format = {
            'type': 'json_schema',
            'json_schema': {
                'name': 'epub_concepts',
                'strict': True,
                'schema': CONCEPT_OUTPUT_SCHEMA,
            },
        }
    else:
        response_format = {'type': 'json_object'}
    return {
        'model': model.strip(),
        'temperature': profile.temperature,
        'seed': 0,
        'max_tokens': profile.max_tokens,
        'response_format': response_format,
        'messages': [
            {'role': 'system', 'content': profile.system_instruction},
            {'role': 'user', 'content': passage},
        ],
    }


def select_stratified_passages(passages: Sequence[Mapping[str, Any]], *, limit: int) -> list[Mapping[str, Any]]:
    """Choose a deterministic, evenly distributed cross-chapter calibration set."""
    if not 1 <= limit <= 500:
        raise PromptProfileError('calibration sample limit must be between 1 and 500')
    ordered = sorted(passages, key=lambda value: (int(value.get('ordinal', 0)), str(value.get('passage_id', ''))))
    if not ordered:
        raise PromptProfileError('EPUB version contains no passages')
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for passage in ordered:
        raw_path = passage.get('toc_path')
        path = tuple(str(part) for part in raw_path) if isinstance(raw_path, (list, tuple)) and raw_path else ('',)
        groups.setdefault(path[:1], []).append(passage)
    chapters = sorted(
        groups.values(), key=lambda values: (int(values[0].get('ordinal', 0)), str(values[0].get('passage_id', '')))
    )
    selected_groups = _evenly_spaced(chapters, min(limit, len(chapters)))
    remaining = limit
    selected: list[Mapping[str, Any]] = []
    for index, group in enumerate(selected_groups):
        groups_left = len(selected_groups) - index
        count = min(len(group), max(1, remaining // groups_left))
        selected.extend(_evenly_spaced(group, count))
        remaining -= count
    # When some short chapters cannot absorb their fair share, fill the rest
    # deterministically without duplicating a passage.
    if remaining > 0:
        selected_ids = {str(value.get('passage_id', '')) for value in selected}
        for passage in ordered:
            if str(passage.get('passage_id', '')) not in selected_ids:
                selected.append(passage)
                selected_ids.add(str(passage.get('passage_id', '')))
                if len(selected) >= limit:
                    break
    return sorted(selected[:limit], key=lambda value: (int(value.get('ordinal', 0)), str(value.get('passage_id', ''))))


def validate_concept_payload(payload: Any, *, passage: str) -> ConceptPayloadValidation:
    """Validate the model payload before it can affect a concept graph."""
    if not isinstance(payload, Mapping) or set(payload) != {'concepts'}:
        return ConceptPayloadValidation(False, 0, 0, 'response must be an object with only concepts')
    concepts = payload.get('concepts')
    if not isinstance(concepts, list):
        return ConceptPayloadValidation(False, 0, 0, 'concepts must be a list')
    mentions = 0
    for concept in concepts:
        if not isinstance(concept, Mapping) or set(concept) != {'name', 'aliases', 'definition', 'mentions'}:
            return ConceptPayloadValidation(False, 0, mentions, 'each concept has an invalid schema')
        if not isinstance(concept['name'], str) or not concept['name'].strip():
            return ConceptPayloadValidation(False, 0, mentions, 'concept name must be non-empty text')
        if not isinstance(concept['definition'], str):
            return ConceptPayloadValidation(False, 0, mentions, 'concept definition must be text')
        aliases = concept['aliases']
        if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
            return ConceptPayloadValidation(False, 0, mentions, 'concept aliases must be text')
        concept_mentions = concept['mentions']
        if not isinstance(concept_mentions, list) or not concept_mentions:
            return ConceptPayloadValidation(False, 0, mentions, 'each concept needs a visible mention')
        for mention in concept_mentions:
            if not isinstance(mention, Mapping) or set(mention) != {'start_codepoint', 'end_codepoint', 'evidence'}:
                return ConceptPayloadValidation(False, 0, mentions, 'each mention has an invalid schema')
            start = mention['start_codepoint']
            end = mention['end_codepoint']
            evidence = mention['evidence']
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or start < 0
                or end <= start
                or end > len(passage)
                or not isinstance(evidence, str)
                or passage[start:end] != evidence
            ):
                return ConceptPayloadValidation(False, 0, mentions, 'mention evidence or codepoint offsets are invalid')
            mentions += 1
    return ConceptPayloadValidation(True, len(concepts), mentions)


def normalize_local_payload_offsets(payload: Any, *, passage: str) -> Any:
    """Derive local-model offsets only when its visible evidence is unique.

    Small local models frequently identify the right evidence but count Unicode
    code points unreliably.  The stored citation remains exact because this
    normalization only replaces offsets from one unique literal occurrence;
    absent or ambiguous evidence is left for strict validation to reject.
    """
    if not isinstance(payload, Mapping):
        return payload
    concepts = payload.get('concepts')
    if not isinstance(concepts, list):
        return payload
    normalized_concepts: list[Any] = []
    for concept in concepts:
        if not isinstance(concept, Mapping):
            normalized_concepts.append(concept)
            continue
        normalized_concept = dict(concept)
        mentions = concept.get('mentions')
        if not isinstance(mentions, list):
            normalized_concepts.append(normalized_concept)
            continue
        normalized_mentions: list[Any] = []
        for mention in mentions:
            if not isinstance(mention, Mapping) or not isinstance(mention.get('evidence'), str):
                normalized_mentions.append(mention)
                continue
            evidence = mention['evidence']
            start = passage.find(evidence)
            if not evidence or start < 0 or passage.find(evidence, start + len(evidence)) >= 0:
                normalized_mentions.append(dict(mention))
                continue
            normalized_mention = dict(mention)
            normalized_mention['start_codepoint'] = start
            normalized_mention['end_codepoint'] = start + len(evidence)
            normalized_mentions.append(normalized_mention)
        normalized_concept['mentions'] = normalized_mentions
        normalized_concepts.append(normalized_concept)
    return {**payload, 'concepts': normalized_concepts}


def _evenly_spaced(values: Sequence[Any], count: int) -> list[Any]:
    if count <= 0:
        return []
    if count >= len(values):
        return list(values)
    if count == 1:
        return [values[len(values) // 2]]
    return [values[round(index * (len(values) - 1) / (count - 1))] for index in range(count)]
