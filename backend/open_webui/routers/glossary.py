import asyncio
import csv
import io
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from open_webui.constants import ERROR_MESSAGES
from open_webui.utils.auth import get_verified_user
from open_webui.utils.glossary_translation import (
    get_editable_glossary,
    get_glossary_view,
    invalidate_cache,
    is_official_glossary_path,
    make_relative_glossary_path,
    normalize_settings,
    normalize_term,
    read_glossary_entries,
    read_settings,
    resolve_glossary_path,
    write_glossary_entries,
    write_settings,
)
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
router = APIRouter()
glossary_lock = asyncio.Lock()


class GlossaryEntry(BaseModel):
    source: str = Field(..., min_length=1)
    target: str = Field(..., min_length=1)
    note: Optional[str] = None
    chat_id: Optional[str] = None
    message_id: Optional[str] = None


class GlossaryResponse(BaseModel):
    entries: dict[str, str]
    updated_at: Optional[float] = None
    entry_origins: dict[str, str] = Field(default_factory=dict)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    mode: str = 'fixed'


class GlossarySettings(BaseModel):
    active_glossary_id: Optional[str] = None
    glossaries: Optional[list[dict[str, Any]]] = None
    glossary_mode: Optional[str] = None
    smart_source_lang: Optional[str] = None
    smart_target_lang: Optional[str] = None
    glossary_path: Optional[str] = None
    glossary_version: Optional[str] = None
    source_lang: Optional[str] = None
    glossary_lang: Optional[str] = None
    target_lang: Optional[str] = None
    max_terms_injected: Optional[int] = None
    max_turns: Optional[int] = None
    token_limit: Optional[int] = None
    mtp_enabled: Optional[bool] = None
    multimodal_enabled: Optional[bool] = None
    debug: Optional[bool] = None


class GlossarySettingsResponse(BaseModel):
    settings: dict[str, Any]


class GlossaryImport(BaseModel):
    content: str = Field(..., min_length=1)
    replace: bool = False
    as_new_glossary: bool = False


class GlossaryCreate(BaseModel):
    name: Optional[str] = None
    version: Optional[str] = None
    source_lang: Optional[str] = None
    glossary_lang: Optional[str] = None
    target_lang: Optional[str] = None


class GlossaryBulkDelete(BaseModel):
    sources: list[str] = Field(default_factory=list)


def safe_glossary_id(value: str, fallback: str = 'glossary') -> str:
    return re.sub(r'[^A-Za-z0-9_-]+', '-', value.strip()).strip('-').lower() or fallback


def safe_filename_stem(value: str, fallback: str = 'glossary') -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '-', normalize_term(value)).strip(' .-')
    return cleaned or fallback


def default_glossary_name(source_lang: Optional[str], target_lang: Optional[str]) -> str:
    source = normalize_term(source_lang or '中文')
    target = normalize_term(target_lang or '外文')
    return f'{source}-{target}'


def unique_glossary_name(base_name: str, glossaries: list[dict[str, Any]]) -> str:
    existing = {normalize_term(str(item.get('name') or '')) for item in glossaries if isinstance(item, dict)}
    if base_name not in existing:
        return base_name

    suffix = 2
    while f'{base_name}{suffix}' in existing:
        suffix += 1
    return f'{base_name}{suffix}'


def parse_glossary_package(content: str) -> tuple[Optional[dict[str, Any]], dict[str, str]]:
    content = content.lstrip('\ufeff')
    try:
        raw = json.loads(content)
        for _ in range(2):
            if isinstance(raw, str):
                raw = json.loads(raw.lstrip('\ufeff').strip())
            else:
                break
        if isinstance(raw, dict) and (raw.get('format') == 'aurapro-glossary' or 'glossary' in raw or 'entries' in raw):
            metadata = raw.get('glossary') if isinstance(raw.get('glossary'), dict) else {}
            entries = _parse_entries_value(raw.get('entries') or {})
            return metadata, entries
    except Exception:
        pass
    return None, parse_import_content(content)


def _clean_import_cell(value: Any) -> str:
    text = normalize_term(str(value))
    for _ in range(4):
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
        elif text.startswith(('"', "'")):
            text = text[1:].strip()
        elif text.endswith(('"', "'")):
            text = text[:-1].strip()
        else:
            break
    return text.replace('""', '"').strip()


def _parse_entries_value(raw: Any) -> dict[str, str]:
    entries: dict[str, str] = {}
    if isinstance(raw, str):
        return parse_import_content(raw)
    if isinstance(raw, dict):
        for key, value in raw.items():
            source = _clean_import_cell(key)
            target = _clean_import_cell(value)
            if source and target:
                entries[source] = target
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            source = _clean_import_cell(
                item.get('source') or item.get('src') or item.get('term') or item.get('??') or item.get('??') or ''
            )
            target = _clean_import_cell(
                item.get('target')
                or item.get('dst')
                or item.get('translation')
                or item.get('??')
                or item.get('??')
                or ''
            )
            if source and target:
                entries[source] = target
    return entries


def parse_import_content(content: str) -> dict[str, str]:
    text = content.lstrip('\ufeff').strip()
    entries: dict[str, str] = {}

    try:
        raw = json.loads(text)
        parsed = _parse_entries_value(raw)
        if parsed:
            return parsed
    except Exception:
        pass

    sample = text[:2048]
    delimiter = '\t' if '\t' in sample else ',' if ',' in sample else None
    if delimiter:
        try:
            reader = csv.reader(io.StringIO(text), delimiter=delimiter)
            rows = list(reader)
            if rows:
                start = 0
                header = [cell.strip().lower() for cell in rows[0]]
                source_idx, target_idx = 0, 1
                source_names = {'source', 'src', 'term', '??', '??', '?', '??'}
                target_names = {'target', 'dst', 'translation', '??', '??', '??'}
                for idx, name in enumerate(header):
                    if name in source_names:
                        source_idx = idx
                        start = 1
                    if name in target_names:
                        target_idx = idx
                        start = 1
                for row in rows[start:]:
                    if len(row) <= max(source_idx, target_idx):
                        continue
                    source = _clean_import_cell(row[source_idx])
                    target = _clean_import_cell(row[target_idx])
                    if source and target:
                        entries[source] = target
                if entries:
                    return entries
        except Exception:
            pass

    separators = ['=>', '->', '?', '=', ':', '?', '|']
    for line in text.splitlines():
        line = line.strip().strip('-*')
        if not line:
            continue
        for separator in separators:
            if separator in line:
                source, target = line.split(separator, 1)
                source = _clean_import_cell(source)
                target = _clean_import_cell(target)
                if source and target:
                    entries[source] = target
                break
        else:
            parts = line.split()
            if len(parts) >= 2:
                entries[_clean_import_cell(parts[0])] = _clean_import_cell(' '.join(parts[1:]))

    return entries


@router.get('/', response_model=GlossaryResponse)
async def get_glossary(user=Depends(get_verified_user)):
    try:
        view = await get_glossary_view()
    except Exception as e:
        log.warning('Glossary JSON is invalid: %s', e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Glossary JSON is invalid. Fix the file or replace it from the app.',
        )
    return GlossaryResponse(**view)


@router.get('/settings', response_model=GlossarySettingsResponse)
async def get_glossary_settings(user=Depends(get_verified_user)):
    return GlossarySettingsResponse(settings=await read_settings())


@router.put('/settings', response_model=GlossarySettingsResponse)
async def update_glossary_settings(form_data: GlossarySettings, user=Depends(get_verified_user)):
    values = form_data.model_dump(exclude_unset=True)
    settings = await write_settings(values)
    return GlossarySettingsResponse(settings=settings)


@router.post('/glossaries', response_model=GlossarySettingsResponse)
async def create_glossary(form_data: GlossaryCreate, user=Depends(get_verified_user)):
    settings = await read_settings()
    glossaries = list(settings.get('glossaries') or [])
    source_lang = form_data.source_lang or '中文'
    target_lang = form_data.target_lang or form_data.glossary_lang or settings.get('target_lang') or '外文'
    requested_name = normalize_term(form_data.name or '')
    glossary_name = unique_glossary_name(
        requested_name or default_glossary_name(source_lang, target_lang),
        glossaries,
    )
    base = safe_glossary_id(glossary_name)
    glossary_id = base
    suffix = 2
    existing = {item.get('id') for item in glossaries if isinstance(item, dict)}
    while glossary_id in existing:
        glossary_id = f'{base}-{suffix}'
        suffix += 1

    relative_path = make_relative_glossary_path(f'glossaries/{glossary_id}.json')
    path = resolve_glossary_path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        await asyncio.to_thread(path.write_text, '{}\n', encoding='utf-8')

    glossaries.append(
        {
            'id': glossary_id,
            'name': glossary_name,
            'path': relative_path,
            'version': form_data.version or '1.0.0',
            'source_lang': source_lang,
            'glossary_lang': form_data.glossary_lang or form_data.target_lang or settings.get('glossary_lang'),
            'target_lang': target_lang,
        }
    )
    values: dict[str, Any] = {
        'active_glossary_id': glossary_id,
        'glossaries': glossaries,
    }
    if settings.get('glossary_mode') == 'smart':
        values.update(
            {
                'smart_source_lang': source_lang,
                'smart_target_lang': target_lang,
            }
        )
    settings = await write_settings(values)
    return GlossarySettingsResponse(settings=settings)


@router.post('/glossaries/{glossary_id}/activate', response_model=GlossarySettingsResponse)
async def activate_glossary(glossary_id: str, user=Depends(get_verified_user)):
    settings = normalize_settings(await read_settings())
    selected = next(
        (item for item in settings.get('glossaries', []) if item.get('id') == glossary_id),
        None,
    )
    if selected is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Glossary not found')

    values: dict[str, Any] = {
        'active_glossary_id': glossary_id,
        'glossaries': settings['glossaries'],
    }
    if settings.get('glossary_mode') == 'smart':
        values.update(
            {
                'smart_source_lang': selected.get('source_lang'),
                'smart_target_lang': selected.get('target_lang') or selected.get('glossary_lang'),
            }
        )
    settings = await write_settings(values)
    return GlossarySettingsResponse(settings=settings)


@router.delete('/glossaries/{glossary_id}', response_model=GlossarySettingsResponse)
async def delete_glossary(glossary_id: str, user=Depends(get_verified_user)):
    async with glossary_lock:
        settings = normalize_settings(await read_settings())
        glossaries = [item for item in settings.get('glossaries', []) if isinstance(item, dict)]
        target = next((item for item in glossaries if item.get('id') == glossary_id), None)
        if not target:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Glossary not found')
        if target.get('official') or is_official_glossary_path(target.get('path') or ''):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Official glossaries cannot be deleted.',
            )
        if len(glossaries) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Create another glossary before deleting the last one.',
            )

        remaining = [item for item in glossaries if item.get('id') != glossary_id]
        active_id = settings.get('active_glossary_id')
        next_active_id = remaining[0].get('id') if active_id == glossary_id else active_id

        path = resolve_glossary_path(target.get('path') or '')
        data_root = resolve_glossary_path('.').resolve()
        try:
            resolved_path = path.resolve()
            if (
                not is_official_glossary_path(target.get('path') or '')
                and resolved_path.is_file()
                and resolved_path.is_relative_to(data_root)
            ):
                await asyncio.to_thread(resolved_path.unlink)
        except Exception as e:
            log.warning('Failed to delete glossary file %s: %s', path, e)

        settings = await write_settings({'active_glossary_id': next_active_id, 'glossaries': remaining})
        await invalidate_cache()
        return GlossarySettingsResponse(settings=settings)


@router.get('/export')
async def export_active_glossary(user=Depends(get_verified_user)):
    settings = normalize_settings(await read_settings())
    view = await get_glossary_view(settings)
    entries = dict(view['entries'])
    updated_at = view.get('updated_at')

    if settings.get('glossary_mode') == 'smart':
        source_lang = str(settings.get('smart_source_lang') or '')
        target_lang = str(settings.get('smart_target_lang') or '')
        glossary_meta = {
            'id': f'smart-{safe_glossary_id(source_lang)}-{safe_glossary_id(target_lang)}',
            'name': f'{source_lang}-{target_lang}',
            'version': '1.0.0',
            'source_lang': source_lang,
            'glossary_lang': target_lang,
            'target_lang': target_lang,
            'updated_at': updated_at,
            'routes': view.get('routes') or [],
        }
    else:
        active = next(item for item in settings['glossaries'] if item.get('id') == settings['active_glossary_id'])
        glossary_meta = {
            'id': active.get('id'),
            'name': active.get('name'),
            'version': active.get('version') or '1.0.0',
            'source_lang': active.get('source_lang'),
            'glossary_lang': active.get('glossary_lang'),
            'target_lang': active.get('target_lang'),
            'updated_at': updated_at,
        }

    payload = {
        'format': 'aurapro-glossary',
        'format_version': 1,
        'exported_at': time.time(),
        'glossary': glossary_meta,
        'entries': entries,
    }
    export_name = safe_filename_stem(str(glossary_meta.get('name') or glossary_meta.get('id') or 'glossary'))
    version = str(glossary_meta.get('version') or '1.0.0')
    filename = f'{export_name}-{version}.aurapro-glossary.json'
    fallback_filename = f'{safe_glossary_id(export_name)}-{version}.aurapro-glossary.json'
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        media_type='application/json',
        headers={
            'Content-Disposition': (f'attachment; filename="{fallback_filename}"; filename*=UTF-8\'\'{quote(filename)}')
        },
    )


@router.post('/reload', response_model=GlossarySettingsResponse)
async def reload_glossary(user=Depends(get_verified_user)):
    await invalidate_cache()
    return GlossarySettingsResponse(settings=await read_settings())


@router.put('/entry', response_model=GlossaryResponse)
async def upsert_glossary_entry(form_data: GlossaryEntry, user=Depends(get_verified_user)):
    source = normalize_term(form_data.source)
    target = form_data.target.strip()
    if not source or not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.EMPTY_CONTENT,
        )

    async with glossary_lock:
        _settings, editable = await get_editable_glossary()
        entries, _updated_at, _signature = await read_glossary_entries(editable)
        entries[source] = target
        await write_glossary_entries(entries, editable)

    return GlossaryResponse(
        entries={source: target},
        entry_origins={source: 'user'},
        mode=str(_settings.get('glossary_mode') or 'fixed'),
        updated_at=time.time(),
    )


@router.post('/import', response_model=GlossaryResponse)
async def import_glossary_entries(form_data: GlossaryImport, user=Depends(get_verified_user)):
    package_meta, incoming = parse_glossary_package(form_data.content)
    if not incoming:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No glossary entries were found. Use JSON, CSV/TSV, or one pair per line.',
        )

    async with glossary_lock:
        if form_data.as_new_glossary or package_meta:
            name = str((package_meta or {}).get('name') or 'Imported glossary')
            source_lang = (package_meta or {}).get('source_lang') or (package_meta or {}).get('sourceLanguage')
            glossary_lang = (
                (package_meta or {}).get('glossary_lang')
                or (package_meta or {}).get('target_lang')
                or (package_meta or {}).get('targetLanguage')
            )
            target_lang = (
                (package_meta or {}).get('target_lang')
                or (package_meta or {}).get('glossary_lang')
                or (package_meta or {}).get('targetLanguage')
            )
            if form_data.as_new_glossary and not package_meta:
                source_lang = source_lang or '中文'
                glossary_lang = glossary_lang or '外文'
                target_lang = target_lang or glossary_lang
            created = await create_glossary(
                GlossaryCreate(
                    name=name,
                    version=str((package_meta or {}).get('version') or '1.0.0'),
                    source_lang=source_lang,
                    glossary_lang=glossary_lang,
                    target_lang=target_lang,
                ),
                user,
            )
            settings = created.settings
            editable = next(item for item in settings['glossaries'] if item.get('id') == settings['active_glossary_id'])
            current: dict[str, str] = {}
        else:
            settings, editable = await get_editable_glossary()
            if form_data.replace:
                current = {}
            else:
                current, _updated_at, _signature = await read_glossary_entries(editable)

        current.update(incoming)
        await write_glossary_entries(current, editable)

    return GlossaryResponse(**(await get_glossary_view(settings)))


@router.delete('/entry/{source}', response_model=GlossaryResponse)
async def delete_glossary_entry(source: str, user=Depends(get_verified_user)):
    source = normalize_term(source)
    async with glossary_lock:
        settings, editable = await get_editable_glossary()
        entries, _updated_at, _signature = await read_glossary_entries(editable)
        entries.pop(source, None)
        await write_glossary_entries(entries, editable)

    return GlossaryResponse(**(await get_glossary_view(settings)))


@router.post('/entries/delete', response_model=GlossaryResponse)
async def delete_glossary_entries(form_data: GlossaryBulkDelete, user=Depends(get_verified_user)):
    sources = {normalize_term(source) for source in form_data.sources if normalize_term(source)}
    if not sources:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.EMPTY_CONTENT,
        )

    async with glossary_lock:
        settings, editable = await get_editable_glossary()
        entries, _updated_at, _signature = await read_glossary_entries(editable)
        for source in sources:
            entries.pop(source, None)
        await write_glossary_entries(entries, editable)

    return GlossaryResponse(**(await get_glossary_view(settings)))
