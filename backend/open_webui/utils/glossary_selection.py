from copy import deepcopy
from typing import Any


VALID_GLOSSARY_MODES = {'smart', 'fixed'}
MAX_LANGUAGE_LENGTH = 80


def _clean_language(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback

    language = ' '.join(value.split()).strip()
    if not language or len(language) > MAX_LANGUAGE_LENGTH:
        return fallback
    return language


def apply_conversation_glossary(settings: dict[str, Any], selection: Any) -> dict[str, Any]:
    """Return settings with a validated per-conversation glossary override."""
    resolved = deepcopy(settings)
    if not isinstance(selection, dict):
        return resolved

    mode = str(selection.get('mode') or '').strip().lower()
    if mode not in VALID_GLOSSARY_MODES:
        return resolved

    if mode == 'fixed':
        glossary_id = str(selection.get('glossary_id') or '').strip()
        glossary = next(
            (
                item
                for item in resolved.get('glossaries', [])
                if isinstance(item, dict) and item.get('id') == glossary_id
            ),
            None,
        )
        if glossary is None:
            return resolved

        resolved['glossary_mode'] = 'fixed'
        resolved['active_glossary_id'] = glossary_id
        resolved['glossary_path'] = glossary.get('path')
        resolved['glossary_version'] = str(glossary.get('version') or '1.0.0')
        resolved['source_lang'] = glossary.get('source_lang')
        resolved['glossary_lang'] = glossary.get('glossary_lang')
        resolved['target_lang'] = glossary.get('target_lang') or glossary.get('glossary_lang')
        return resolved

    source_fallback = str(resolved.get('smart_source_lang') or resolved.get('source_lang') or '')
    target_fallback = str(
        resolved.get('smart_target_lang') or resolved.get('target_lang') or resolved.get('glossary_lang') or ''
    )
    resolved['glossary_mode'] = 'smart'
    resolved['smart_source_lang'] = _clean_language(selection.get('source_lang'), source_fallback)
    resolved['smart_target_lang'] = _clean_language(selection.get('target_lang'), target_fallback)
    return resolved
