"""Route existing bilingual glossaries into an effective language pair."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import langcodes

GlossaryOrigin = Literal['user', 'direct', 'combined']

_LANGUAGE_ALIASES = {
    '中文': 'zh',
    '汉语': 'zh',
    '漢語': 'zh',
    '普通话': 'zh',
    '普通話': 'zh',
    '简体中文': 'zh',
    '簡體中文': 'zh',
    '繁体中文': 'zh',
    '繁體中文': 'zh',
    'chinese': 'zh',
    'mandarin': 'zh',
    '西语': 'es',
    '西語': 'es',
    '西班牙语': 'es',
    '西班牙語': 'es',
    'spanish': 'es',
    'español': 'es',
    '葡语': 'pt',
    '葡語': 'pt',
    '葡萄牙语': 'pt',
    '葡萄牙語': 'pt',
    'portuguese': 'pt',
    'português': 'pt',
    '英语': 'en',
    '英語': 'en',
    '英文': 'en',
    'english': 'en',
}
_ALIAS_SEPARATOR = re.compile(r'\s*(?:/|／|\||｜)\s*')
_BRACKET_ALIASES = re.compile(r'([（(\[])([^）)\]]*(?:/|／|\||｜)[^）)\]]*)([）)\]])')
_BRACKET_PAIRS = {'（': '）', '(': ')', '[': ']'}


@dataclass(frozen=True, slots=True)
class GlossaryDataset:
    id: str
    name: str
    source_lang: str
    target_lang: str
    entries: dict[str, str]
    official: bool = False
    user_override: bool = False
    version: str = '1.0.0'


@dataclass(frozen=True, slots=True)
class GlossaryRoute:
    kind: Literal['direct', 'combined']
    source_lang: str
    target_lang: str
    glossary_ids: tuple[str, ...]
    glossary_names: tuple[str, ...]
    coverage: int
    pivot_lang: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            'kind': self.kind,
            'source_lang': self.source_lang,
            'target_lang': self.target_lang,
            'pivot_lang': self.pivot_lang,
            'glossary_ids': list(self.glossary_ids),
            'glossary_names': list(self.glossary_names),
            'coverage': self.coverage,
        }


@dataclass(frozen=True, slots=True)
class SmartGlossaryResult:
    entries: dict[str, str]
    entry_origins: dict[str, GlossaryOrigin]
    routes: tuple[GlossaryRoute, ...]
    available_languages: tuple[str, ...]


@dataclass(slots=True)
class _TermBucket:
    source: str
    targets: list[str]


def clean_language_name(value: str) -> str:
    return ' '.join(str(value or '').strip().split())


@lru_cache(maxsize=256)
def language_key(value: str) -> str:
    cleaned = clean_language_name(value)
    normalized = unicodedata.normalize('NFKC', cleaned).casefold()
    compact = re.sub(r'\s+', '', normalized)
    alias = _LANGUAGE_ALIASES.get(compact)
    if alias:
        return alias

    code = compact.replace('_', '-')
    if re.fullmatch(r'[a-z]{2,3}(?:-[a-z0-9]{2,8})*', code):
        return code

    try:
        language = langcodes.find(cleaned).language
        if language:
            return language.casefold()
    except LookupError:
        pass

    return compact


def same_language(left: str, right: str) -> bool:
    left_key = language_key(left)
    right_key = language_key(right)
    if left_key == right_key:
        return True
    return left_key.split('-', 1)[0] == right_key.split('-', 1)[0]


def _term_key(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.sub(r'\s+', '', normalized)


def _strip_outer_brackets(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and _BRACKET_PAIRS.get(value[0]) == value[-1]:
        return value[1:-1].strip()
    return value


def _split_terms(value: str) -> tuple[str, ...]:
    value = unicodedata.normalize('NFKC', str(value or '')).strip()
    if not value:
        return ()

    match = _BRACKET_ALIASES.search(value)
    if match and _BRACKET_PAIRS.get(match.group(1)) == match.group(3):
        prefix = value[: match.start()]
        suffix = value[match.end() :]
        expanded: list[str] = []
        for alias in _ALIAS_SEPARATOR.split(match.group(2)):
            alias = alias.strip()
            if alias:
                expanded.extend(_split_terms(f'{prefix}{alias}{suffix}'))
        return tuple(dict.fromkeys(expanded))

    terms = [_strip_outer_brackets(part) for part in _ALIAS_SEPARATOR.split(value) if _strip_outer_brackets(part)]
    return tuple(dict.fromkeys(terms))


def _entry_pairs(entries: dict[str, str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for source, target in entries.items():
        sources = _split_terms(source)
        targets = _split_terms(target)
        if not sources or not targets:
            continue

        if len(sources) == len(targets):
            pairs.extend(zip(sources, targets, strict=True))
        elif len(targets) == 1:
            pairs.extend((item, targets[0]) for item in sources)
        else:
            pairs.extend((item, mapped) for item in sources for mapped in targets)
    return pairs


def _add_term(mapping: dict[str, _TermBucket], source: str, target: str) -> None:
    source = source.strip()
    target = target.strip()
    key = _term_key(source)
    if not key or not target:
        return
    bucket = mapping.setdefault(key, _TermBucket(source=source, targets=[]))
    if target not in bucket.targets:
        bucket.targets.append(target)


def _merge_same_priority(
    destination: dict[str, _TermBucket],
    source: dict[str, _TermBucket],
) -> None:
    for bucket in source.values():
        for target in bucket.targets:
            _add_term(destination, bucket.source, target)


def _oriented_entries(
    dataset: GlossaryDataset,
    source_lang: str,
    target_lang: str,
) -> dict[str, _TermBucket]:
    forward = same_language(dataset.source_lang, source_lang) and same_language(dataset.target_lang, target_lang)
    reverse = same_language(dataset.target_lang, source_lang) and same_language(dataset.source_lang, target_lang)
    if not (forward or reverse):
        return {}

    result: dict[str, _TermBucket] = {}
    for source, target in _entry_pairs(dataset.entries):
        if reverse:
            source, target = target, source
        _add_term(result, source, target)
    return result


def _compose(
    left: dict[str, _TermBucket],
    right: dict[str, _TermBucket],
) -> dict[str, _TermBucket]:
    result: dict[str, _TermBucket] = {}
    for left_bucket in left.values():
        for pivot_term in left_bucket.targets:
            right_bucket = right.get(_term_key(pivot_term))
            if right_bucket is None:
                continue
            for target in right_bucket.targets:
                _add_term(result, left_bucket.source, target)
    return result


def _datasets_for_pair(
    datasets: list[GlossaryDataset],
    source_lang: str,
    target_lang: str,
) -> list[GlossaryDataset]:
    return [
        dataset
        for dataset in datasets
        if (same_language(dataset.source_lang, source_lang) and same_language(dataset.target_lang, target_lang))
        or (same_language(dataset.target_lang, source_lang) and same_language(dataset.source_lang, target_lang))
    ]


def _merge_dataset_entries(
    datasets: list[GlossaryDataset],
    source_lang: str,
    target_lang: str,
) -> dict[str, _TermBucket]:
    result: dict[str, _TermBucket] = {}
    for dataset in datasets:
        _merge_same_priority(result, _oriented_entries(dataset, source_lang, target_lang))
    return result


def _apply_layer(
    destination: dict[str, tuple[_TermBucket, GlossaryOrigin]],
    layer: dict[str, _TermBucket],
    origin: GlossaryOrigin,
) -> None:
    for key, bucket in layer.items():
        destination[key] = (
            _TermBucket(source=bucket.source, targets=list(bucket.targets)),
            origin,
        )


def _unique_datasets(datasets: list[GlossaryDataset]) -> list[GlossaryDataset]:
    unique: list[GlossaryDataset] = []
    seen_ids: set[str] = set()
    for dataset in datasets:
        if dataset.id not in seen_ids:
            unique.append(dataset)
            seen_ids.add(dataset.id)
    return unique


def _resolve_combined_layer(
    datasets: list[GlossaryDataset],
    source_lang: str,
    target_lang: str,
) -> tuple[dict[str, _TermBucket], list[GlossaryRoute]]:
    candidates: list[tuple[int, str, dict[str, _TermBucket], list[GlossaryDataset]]] = []
    source_key = language_key(source_lang)
    target_key = language_key(target_lang)

    for pivot_key, pivot_label in _language_labels(datasets).items():
        if pivot_key in {source_key, target_key}:
            continue

        left_datasets = _datasets_for_pair(datasets, source_lang, pivot_label)
        right_datasets = _datasets_for_pair(datasets, pivot_label, target_lang)
        if not left_datasets or not right_datasets:
            continue

        combined = _compose(
            _merge_dataset_entries(left_datasets, source_lang, pivot_label),
            _merge_dataset_entries(right_datasets, pivot_label, target_lang),
        )
        if combined:
            candidates.append(
                (
                    len(combined),
                    pivot_label,
                    combined,
                    _unique_datasets([*left_datasets, *right_datasets]),
                )
            )

    layer: dict[str, _TermBucket] = {}
    routes: list[GlossaryRoute] = []
    if not candidates:
        return layer, routes

    best_coverage = max(candidate[0] for candidate in candidates)
    for coverage, pivot_label, combined, used in candidates:
        if coverage != best_coverage:
            continue
        _merge_same_priority(layer, combined)
        routes.append(
            GlossaryRoute(
                kind='combined',
                source_lang=source_lang,
                target_lang=target_lang,
                pivot_lang=pivot_label,
                glossary_ids=tuple(item.id for item in used),
                glossary_names=tuple(item.name for item in used),
                coverage=coverage,
            )
        )
    return layer, routes


def resolve_smart_glossary(
    datasets: list[GlossaryDataset],
    source_lang: str,
    target_lang: str,
) -> SmartGlossaryResult:
    source_lang = clean_language_name(source_lang)
    target_lang = clean_language_name(target_lang)
    if not source_lang or not target_lang or same_language(source_lang, target_lang):
        languages = _available_languages(datasets, source_lang, target_lang)
        return SmartGlossaryResult({}, {}, (), languages)

    direct_datasets = _datasets_for_pair(datasets, source_lang, target_lang)
    user_direct = [item for item in direct_datasets if not item.official]
    official_direct = [item for item in direct_datasets if item.official]

    combined_layer, routes = _resolve_combined_layer(datasets, source_lang, target_lang)

    official_layer = _merge_dataset_entries(official_direct, source_lang, target_lang)
    user_layer = _merge_dataset_entries(user_direct, source_lang, target_lang)

    if direct_datasets and (official_layer or user_layer):
        direct_entries: dict[str, _TermBucket] = {}
        _merge_same_priority(direct_entries, official_layer)
        _merge_same_priority(direct_entries, user_layer)
        routes.insert(
            0,
            GlossaryRoute(
                kind='direct',
                source_lang=source_lang,
                target_lang=target_lang,
                glossary_ids=tuple(item.id for item in direct_datasets),
                glossary_names=tuple(item.name for item in direct_datasets),
                coverage=len(direct_entries),
            ),
        )

    effective: dict[str, tuple[_TermBucket, GlossaryOrigin]] = {}
    _apply_layer(effective, combined_layer, 'combined')
    _apply_layer(effective, official_layer, 'direct')
    _apply_layer(effective, user_layer, 'user')

    entries: dict[str, str] = {}
    origins: dict[str, GlossaryOrigin] = {}
    for bucket, origin in effective.values():
        entries[bucket.source] = ' / '.join(bucket.targets)
        origins[bucket.source] = origin

    return SmartGlossaryResult(
        entries=entries,
        entry_origins=origins,
        routes=tuple(routes),
        available_languages=_available_languages(datasets, source_lang, target_lang),
    )


def _language_labels(datasets: list[GlossaryDataset]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for dataset in datasets:
        for language in (dataset.source_lang, dataset.target_lang):
            language = clean_language_name(language)
            if language:
                labels.setdefault(language_key(language), language)
    return labels


def _available_languages(
    datasets: list[GlossaryDataset],
    source_lang: str,
    target_lang: str,
) -> tuple[str, ...]:
    labels = _language_labels(datasets)
    for language in (source_lang, target_lang):
        language = clean_language_name(language)
        if language:
            labels.setdefault(language_key(language), language)
    return tuple(sorted(labels.values(), key=str.casefold))
