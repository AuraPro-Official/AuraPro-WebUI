import ast
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

import langcodes
import unicodedata
from rapidfuzz import fuzz
from langchain_core.documents import Document

from open_webui.config import DATA_DIR
from open_webui.internal.db import get_async_db
from open_webui.models.config import Config
from open_webui.models.knowledge import Knowledges
from open_webui.retrieval.utils import VectorSearchRetriever
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
from open_webui.utils.lazy_model import LazyModel
log = logging.getLogger(__name__)


def _load_opencc_t2s():
    from opencc import OpenCC

    return OpenCC('t2s')


def _load_jieba():
    import jieba

    jieba.initialize()
    return jieba

OFFICIAL_GLOSSARIES: list[dict[str, str]] = [
    {
        'id': 'zh-es',
        'name': '\u4e2d\u6587-\u897f\u8bed\u8bcd\u5178',
        'path': 'glossary_es.json',
        'version': '1.0.1',
        'source_lang': '\u4e2d\u6587',
        'glossary_lang': '\u897f\u73ed\u7259\u8bed',
        'target_lang': '\u897f\u73ed\u7259\u8bed',
    },
    {
        'id': 'zh-sr',
        'name': '\u4e2d\u6587-\u585e\u5c14\u7ef4\u4e9a\u8bed\u8bcd\u5178',
        'path': 'glossary_sr.json',
        'version': '1.0.0',
        'source_lang': '\u4e2d\u6587',
        'glossary_lang': '\u585e\u5c14\u7ef4\u4e9a\u8bed',
        'target_lang': '\u585e\u5c14\u7ef4\u4e9a\u8bed',
    },
    {
        'id': 'zh-hr',
        'name': '\u4e2d\u6587-\u514b\u7f57\u5730\u4e9a\u8bed\u8bcd\u5178',
        'path': 'glossary_hr.json',
        'version': '1.0.0',
        'source_lang': '\u4e2d\u6587',
        'glossary_lang': '\u514b\u7f57\u5730\u4e9a\u8bed',
        'target_lang': '\u514b\u7f57\u5730\u4e9a\u8bed',
    },
    {
        'id': 'zh-en',
        'name': '\u4e2d\u6587-\u82f1\u8bed\u8bcd\u5178',
        'path': 'glossary_en.json',
        'version': '1.0.0',
        'source_lang': '\u4e2d\u6587',
        'glossary_lang': '\u82f1\u8bed',
        'target_lang': '\u82f1\u8bed',
    },
    {
        'id': 'zh-nl',
        'name': '\u4e2d\u6587-\u8377\u5170\u8bed\u8bcd\u5178',
        'path': 'glossary_nl.json',
        'version': '1.0.0',
        'source_lang': '\u4e2d\u6587',
        'glossary_lang': '\u8377\u5170\u8bed',
        'target_lang': '\u8377\u5170\u8bed',
    },
    {
        'id': 'zh-ka',
        'name': '\u4e2d\u6587-\u683c\u9c81\u5409\u4e9a\u8bed\u8bcd\u5178',
        'path': 'glossary_ka.json',
        'version': '1.0.0',
        'source_lang': '\u4e2d\u6587',
        'glossary_lang': '\u683c\u9c81\u5409\u4e9a\u8bed',
        'target_lang': '\u683c\u9c81\u5409\u4e9a\u8bed',
    },
    {
        'id': 'ru-ka',
        'name': '\u4fc4\u8bed-\u683c\u9c81\u5409\u4e9a\u8bed\u8bcd\u5178',
        'path': 'glossary_ru_ka.json',
        'version': '1.0.0',
        'source_lang': '\u4fc4\u8bed',
        'glossary_lang': '\u683c\u9c81\u5409\u4e9a\u8bed',
        'target_lang': '\u683c\u9c81\u5409\u4e9a\u8bed',
    },
    {
        'id': 'zh-fr',
        'name': '中文-法语词典',
        'path': 'glossary_fr.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '法语',
        'target_lang': '法语',
    },
    {
        'id': 'zh-ne',
        'name': '中文-尼泊尔语词典',
        'path': 'glossary_ne.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '尼泊尔语',
        'target_lang': '尼泊尔语',
    },
    {
        'id': 'en-ne',
        'name': '英语-尼泊尔语词典',
        'path': 'glossary_ne_en.json',
        'version': '1.0.0',
        'source_lang': '英语',
        'glossary_lang': '尼泊尔语',
        'target_lang': '尼泊尔语',
    },
    {
        'id': 'zh-tl',
        'name': '中文-他加禄语词典',
        'path': 'glossary_tl.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '他加禄语',
        'target_lang': '他加禄语',
    },
    {
        'id': 'en-tl',
        'name': '英语-他加禄语词典',
        'path': 'glossary_tl_en.json',
        'version': '1.0.0',
        'source_lang': '英语',
        'glossary_lang': '他加禄语',
        'target_lang': '他加禄语',
    },
    {
        'id': 'zh-hi',
        'name': '中文-印地语词典',
        'path': 'glossary_hi.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '印地语',
        'target_lang': '印地语',
    },
    {
        'id': 'en-hi',
        'name': '英语-印地语词典',
        'path': 'glossary_hi_en.json',
        'version': '1.0.0',
        'source_lang': '英语',
        'glossary_lang': '印地语',
        'target_lang': '印地语',
    },
    {
        'id': 'zh-ar',
        'name': '中文-阿拉伯语词典',
        'path': 'glossary_ar.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '阿拉伯语',
        'target_lang': '阿拉伯语',
    },
    {
        'id': 'zh-bg',
        'name': '中文-保加利亚语词典',
        'path': 'glossary_bg.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '保加利亚语',
        'target_lang': '保加利亚语',
    },
    {
        'id': 'zh-pl',
        'name': '中文-波兰语词典',
        'path': 'glossary_pl.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '波兰语',
        'target_lang': '波兰语',
    },
    {
        'id': 'zh-de',
        'name': '中文-德语词典',
        'path': 'glossary_de.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '德语',
        'target_lang': '德语',
    },
    {
        'id': 'zh-es',
        'name': '中文-西班牙语词典',
        'path': 'glossary_es.json',
        'version': '1.0.1',
        'source_lang': '中文',
        'glossary_lang': '西班牙语',
        'target_lang': '西班牙语',
    },
    {
        'id': 'zh-ru',
        'name': '中文-俄语词典',
        'path': 'glossary_ru.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '俄语',
        'target_lang': '俄语',
    },
    {
        'id': 'zh-fr',
        'name': '中文-法语词典',
        'path': 'glossary_fr.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '法语',
        'target_lang': '法语',
    },
    {
        'id': 'zh-tl',
        'name': '中文-菲律宾语词典',
        'path': 'glossary_tl.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '菲律宾语',
        'target_lang': '菲律宾语',
    },
    {
        'id': 'en-tl',
        'name': '英语-他加禄语词典',
        'path': 'glossary_tl_en.json',
        'version': '1.0.0',
        'source_lang': '英语',
        'glossary_lang': '他加禄语',
        'target_lang': '他加禄语',
    },
    {
        'id': 'zh-km',
        'name': '中文-高棉语词典',
        'path': 'glossary_km.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '高棉语',
        'target_lang': '高棉语',
    },
    {
        'id': 'zh-ka',
        'name': '中文-格鲁吉亚语词典',
        'path': 'glossary_ka.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '格鲁吉亚语',
        'target_lang': '格鲁吉亚语',
    },
    {
        'id': 'ru-ka',
        'name': '俄语-格鲁吉亚语词典',
        'path': 'glossary_ru_ka.json',
        'version': '1.0.0',
        'source_lang': '俄语',
        'glossary_lang': '格鲁吉亚语',
        'target_lang': '格鲁吉亚语',
    },
    {
        'id': 'zh-ko',
        'name': '中文-韩语词典',
        'path': 'glossary_ko.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '韩语',
        'target_lang': '韩语',
    },
    {
        'id': 'zh-nl',
        'name': '中文-荷兰语词典',
        'path': 'glossary_nl.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '荷兰语',
        'target_lang': '荷兰语',
    },
    {
        'id': 'zh-hr',
        'name': '中文-克罗地亚语词典',
        'path': 'glossary_hr.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '克罗地亚语',
        'target_lang': '克罗地亚语',
    },
    {
        'id': 'zh-lo',
        'name': '中文-老挝语词典',
        'path': 'glossary_lo.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '老挝语',
        'target_lang': '老挝语',
    },
    {
        'id': 'zh-ro',
        'name': '中文-罗马尼亚语词典',
        'path': 'glossary_ro.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '罗马尼亚语',
        'target_lang': '罗马尼亚语',
    },
    {
        'id': 'zh-mn',
        'name': '中文-蒙语词典',
        'path': 'glossary_mn.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '蒙语',
        'target_lang': '蒙语',
    },
    {
        'id': 'zh-my',
        'name': '中文-缅甸语词典',
        'path': 'glossary_my.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '缅甸语',
        'target_lang': '缅甸语',
    },
    {
        'id': 'zh-ne',
        'name': '中文-尼泊尔语词典',
        'path': 'glossary_ne.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '尼泊尔语',
        'target_lang': '尼泊尔语',
    },
    {
        'id': 'en-ne',
        'name': '英语-尼泊尔语词典',
        'path': 'glossary_ne_en.json',
        'version': '1.0.0',
        'source_lang': '英语',
        'glossary_lang': '尼泊尔语',
        'target_lang': '尼泊尔语',
    },
    {
        'id': 'zh-pt',
        'name': '中文-葡萄牙语词典',
        'path': 'glossary_pt.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '葡萄牙语',
        'target_lang': '葡萄牙语',
    },
    {
        'id': 'zh-sr',
        'name': '中文-塞尔维亚语词典',
        'path': 'glossary_sr.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '塞尔维亚语',
        'target_lang': '塞尔维亚语',
    },
    {
        'id': 'zh-sk',
        'name': '中文-斯洛伐克语词典',
        'path': 'glossary_sk.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '斯洛伐克语',
        'target_lang': '斯洛伐克语',
    },
    {
        'id': 'zh-sw',
        'name': '中文-斯瓦希里语词典',
        'path': 'glossary_sw.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '斯瓦希里语',
        'target_lang': '斯瓦希里语',
    },
    {
        'id': 'zh-th',
        'name': '中文-泰语词典',
        'path': 'glossary_th.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '泰语',
        'target_lang': '泰语',
    },
    {
        'id': 'zh-uk',
        'name': '中文-乌克兰语词典',
        'path': 'glossary_uk.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '乌克兰语',
        'target_lang': '乌克兰语',
    },
    {
        'id': 'zh-el',
        'name': '中文-希腊语词典',
        'path': 'glossary_el.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '希腊语',
        'target_lang': '希腊语',
    },
    {
        'id': 'zh-hu',
        'name': '中文-匈牙利语词典',
        'path': 'glossary_hu.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '匈牙利语',
        'target_lang': '匈牙利语',
    },
    {
        'id': 'zh-hy',
        'name': '中文-亚美尼亚语词典',
        'path': 'glossary_hy.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '亚美尼亚语',
        'target_lang': '亚美尼亚语',
    },
    {
        'id': 'zh-it',
        'name': '中文-意大利语词典',
        'path': 'glossary_it.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '意大利语',
        'target_lang': '意大利语',
    },
    {
        'id': 'zh-hi',
        'name': '中文-印地语词典',
        'path': 'glossary_hi.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '印地语',
        'target_lang': '印地语',
    },
    {
        'id': 'en-hi',
        'name': '英语-印地语词典',
        'path': 'glossary_hi_en.json',
        'version': '1.0.0',
        'source_lang': '英语',
        'glossary_lang': '印地语',
        'target_lang': '印地语',
    },
    {
        'id': 'zh-id',
        'name': '中文-印尼语词典',
        'path': 'glossary_id.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '印尼语',
        'target_lang': '印尼语',
    },
    {
        'id': 'zh-vi',
        'name': '中文-越南语词典',
        'path': 'glossary_vi.json',
        'version': '1.0.0',
        'source_lang': '中文',
        'glossary_lang': '越南语',
        'target_lang': '越南语',
    },
    {
        'id': 'zh-hmn',
        'name': '\u4e2d\u6587-\u82d7\u8bed\u8bcd\u5178',
        'path': 'glossary_hmn.json',
        'version': '1.0.0',
        'source_lang': '\u4e2d\u6587',
        'glossary_lang': '\u82d7\u8bed',
        'target_lang': '\u82d7\u8bed',
    },
]
DEFAULT_OFFICIAL_GLOSSARY = OFFICIAL_GLOSSARIES[0]
DEFAULT_GLOSSARY_ID = 'personal'
DEFAULT_GLOSSARY_NAME = '\u6211\u7684\u8bcd\u5178'
DEFAULT_GLOSSARY_RELATIVE_PATH = 'glossaries/personal.json'
DEFAULT_GLOSSARY_PATH = Path(DATA_DIR) / DEFAULT_GLOSSARY_RELATIVE_PATH
LEGACY_OFFICIAL_GLOSSARY_PATH = Path(DATA_DIR) / 'glossary.json'
OFFICIAL_GLOSSARY_MANIFEST_PATH = Path(DATA_DIR) / 'official-glossaries.manifest.json'
USER_GLOSSARY_DIR = Path(DATA_DIR) / 'glossaries'
SETTINGS_PATH = Path(DATA_DIR) / 'glossary.settings.json'

DEFAULT_SETTINGS: dict[str, Any] = {
    'active_glossary_id': DEFAULT_GLOSSARY_ID,
    'glossaries': [],
    'glossary_path': DEFAULT_GLOSSARY_RELATIVE_PATH,
    'glossary_version': '1.0.0',
    'source_lang': '\u4e2d\u6587',
    'glossary_lang': '\u5916\u6587',
    'target_lang': '\u5916\u6587',
    'max_terms_injected': 10,
    'max_turns': 3,
    'token_limit': 16384,
    'debug': False,
}

_cache_lock = asyncio.Lock()
_cache_path: Optional[str] = None
_cache_signature: Optional[tuple[float, int]] = None
_cache_entries: dict[str, str] = {}
_official_configs_cache_marker = object()
_official_configs_cache_signature: object | tuple[int, int] | None = _official_configs_cache_marker
_official_configs_cache: list[dict[str, str]] = []


def normalize_term(value: str) -> str:
    return ' '.join(value.strip().split())


def _data_dir() -> Path:
    return Path(DATA_DIR).expanduser()


def make_relative_glossary_path(path: str | Path) -> str:
    value = Path(str(path)).expanduser()
    try:
        if value.is_absolute():
            return str(value.resolve().relative_to(_data_dir().resolve()))
    except Exception:
        pass
    return str(value)


def resolve_glossary_path(path: str | Path) -> Path:
    value = Path(str(path)).expanduser()
    if value.is_absolute():
        return value
    return _data_dir() / value


def _safe_glossary_id(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9_-]+', '-', value.strip()).strip('-').lower() or 'glossary'


def _available_official_glossary_configs() -> list[dict[str, str]]:
    global _official_configs_cache_signature, _official_configs_cache

    try:
        manifest_stat = OFFICIAL_GLOSSARY_MANIFEST_PATH.stat()
        manifest_signature: tuple[int, int] | None = (
            manifest_stat.st_mtime_ns,
            manifest_stat.st_size,
        )
    except OSError:
        manifest_signature = None

    if _official_configs_cache_signature == manifest_signature:
        return [dict(item) for item in _official_configs_cache]

    configs: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    if OFFICIAL_GLOSSARY_MANIFEST_PATH.exists():
        try:
            raw = json.loads(OFFICIAL_GLOSSARY_MANIFEST_PATH.read_text(encoding='utf-8'))
            for item in raw.get('files', []) if isinstance(raw, dict) else []:
                if not isinstance(item, dict):
                    continue
                filename = str(item.get('filename') or '').strip()
                if (
                    not filename
                    or filename != Path(filename).name
                    or not re.fullmatch(r'glossary_[A-Za-z0-9_]+\.json', filename)
                    or not resolve_glossary_path(filename).exists()
                ):
                    continue
                configs.append(
                    {
                        'id': str(item.get('id') or _safe_glossary_id(filename)),
                        'name': str(item.get('name') or item.get('id') or filename),
                        'path': filename,
                        'version': str(item.get('version') or '1.0.0'),
                        'source_lang': str(item.get('source_lang') or '\u4e2d\u6587'),
                        'glossary_lang': str(item.get('glossary_lang') or item.get('target_lang') or '\u5916\u6587'),
                        'target_lang': str(item.get('target_lang') or item.get('glossary_lang') or '\u5916\u6587'),
                    }
                )
                seen_paths.add(filename)
        except Exception as e:
            log.warning('Failed to read installed official glossary manifest: %s', e)

    # Keep dictionaries installed by older AuraPro releases usable until the
    # independent package is installed. Missing bundled files are never listed.
    for item in OFFICIAL_GLOSSARIES:
        item_path = str(item['path'])
        if item_path not in seen_paths and resolve_glossary_path(item_path).exists():
            configs.append(item)
            seen_paths.add(item_path)

    _official_configs_cache_signature = manifest_signature
    _official_configs_cache = [dict(item) for item in configs]
    return configs


def is_official_glossary_path(path: str | Path) -> bool:
    relative = make_relative_glossary_path(path)
    normalized = relative.replace('\\', '/').strip()
    available_paths = {item['path'] for item in _available_official_glossary_configs()}
    if LEGACY_OFFICIAL_GLOSSARY_PATH.exists():
        available_paths.add('glossary.json')
    return normalized.removeprefix('./') in available_paths


def _official_glossary_config(path: str | Path = '', glossary_id: str = '') -> Optional[dict[str, str]]:
    normalized_path = make_relative_glossary_path(path).replace('\\', '/').strip() if path else ''
    if normalized_path in {'glossary.json', './glossary.json'} and LEGACY_OFFICIAL_GLOSSARY_PATH.exists():
        return DEFAULT_OFFICIAL_GLOSSARY
    for item in _available_official_glossary_configs():
        if glossary_id == item['id'] or normalized_path in {item['path'], f'./{item["path"]}'}:
            return item
    return None


def _official_glossary_item(
    source_lang: str,
    glossary_lang: str,
    target_lang: str,
    version: str = '1.0.0',
    config: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    config = config or DEFAULT_OFFICIAL_GLOSSARY
    return {
        'id': config['id'],
        'name': config['name'],
        'path': config['path'],
        'version': version if version != '1.0.0' else config['version'],
        'source_lang': source_lang or config['source_lang'],
        'glossary_lang': glossary_lang or config['glossary_lang'],
        'target_lang': target_lang or config['target_lang'],
        'official': True,
    }


def _official_glossary_items() -> list[dict[str, Any]]:
    return [
        _official_glossary_item(
            item['source_lang'],
            item['glossary_lang'],
            item['target_lang'],
            item['version'],
            item,
        )
        for item in _available_official_glossary_configs()
    ]


def _personal_glossary_item() -> dict[str, Any]:
    return {
        'id': DEFAULT_GLOSSARY_ID,
        'name': DEFAULT_GLOSSARY_NAME,
        'path': DEFAULT_GLOSSARY_RELATIVE_PATH,
        'version': '1.0.0',
        'source_lang': DEFAULT_SETTINGS['source_lang'],
        'glossary_lang': DEFAULT_SETTINGS['glossary_lang'],
        'target_lang': DEFAULT_SETTINGS['target_lang'],
        'official': False,
    }


def normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    legacy_path = settings.get('glossary_path') or DEFAULT_GLOSSARY_PATH
    legacy_source_lang = settings.get('source_lang') or DEFAULT_SETTINGS['source_lang']
    legacy_glossary_lang = settings.get('glossary_lang') or DEFAULT_SETTINGS['glossary_lang']
    legacy_target_lang = settings.get('target_lang') or DEFAULT_SETTINGS['target_lang']
    glossaries = settings.get('glossaries')

    available_official = _official_glossary_items()
    if not isinstance(glossaries, list) or not glossaries:
        glossaries = [_personal_glossary_item(), *available_official]
    else:
        normalized = []
        seen: set[str] = set()
        for index, glossary in enumerate(glossaries):
            if not isinstance(glossary, dict):
                continue
            name = str(glossary.get('name') or glossary.get('id') or f'Glossary {index + 1}').strip()
            glossary_id = _safe_glossary_id(str(glossary.get('id') or name))
            glossary_path = glossary.get('path') or glossary.get('glossary_path') or f'glossaries/{glossary_id}.json'
            official_config = _official_glossary_config(glossary_path, glossary_id)
            is_official = (
                bool(glossary.get('official'))
                or (glossary_id == 'default' and is_official_glossary_path(glossary_path))
                or official_config is not None
            )
            if is_official and official_config is None and not resolve_glossary_path(glossary_path).exists():
                continue
            if is_official:
                if official_config is not None:
                    glossary_id = official_config['id']
                    name = official_config['name']
                    glossary_path = official_config['path']
            base_id = glossary_id
            suffix = 2
            while glossary_id in seen:
                glossary_id = f'{base_id}-{suffix}'
                suffix += 1
            seen.add(glossary_id)
            normalized.append(
                {
                    'id': glossary_id,
                    'name': name or glossary_id,
                    'path': make_relative_glossary_path(glossary_path),
                    'version': str((official_config or {}).get('version') or glossary.get('version') or '1.0.0'),
                    'source_lang': str(
                        (official_config or {}).get('source_lang')
                        or glossary.get('source_lang')
                        or DEFAULT_SETTINGS['source_lang']
                    ),
                    'glossary_lang': str(
                        (official_config or {}).get('glossary_lang')
                        or glossary.get('glossary_lang')
                        or legacy_glossary_lang
                    ),
                    'target_lang': str(
                        (official_config or {}).get('target_lang') or glossary.get('target_lang') or legacy_target_lang
                    ),
                    'official': is_official,
                }
            )
        for item in _available_official_glossary_configs():
            if item['id'] not in seen:
                normalized.append(
                    _official_glossary_item(
                        item['source_lang'],
                        item['glossary_lang'],
                        item['target_lang'],
                        item['version'],
                        item,
                    )
                )
                seen.add(item['id'])
        glossaries = normalized or available_official or [_personal_glossary_item()]

    active_id = str(settings.get('active_glossary_id') or glossaries[0]['id'])
    if active_id == 'default' and any(item.get('id') == DEFAULT_GLOSSARY_ID for item in glossaries):
        active_id = DEFAULT_GLOSSARY_ID
    active = next((item for item in glossaries if item.get('id') == active_id), glossaries[0])
    settings['glossaries'] = glossaries
    settings['active_glossary_id'] = active['id']
    settings['glossary_path'] = make_relative_glossary_path(active['path'])
    settings['glossary_version'] = str(active.get('version') or '1.0.0')
    settings['source_lang'] = active['source_lang']
    settings['glossary_lang'] = active['glossary_lang']
    settings['target_lang'] = active['target_lang']
    return settings


def active_glossary(settings: dict[str, Any]) -> dict[str, Any]:
    settings = normalize_settings(settings)
    return next(item for item in settings['glossaries'] if item.get('id') == settings['active_glossary_id'])


def is_official_glossary(settings: dict[str, Any]) -> bool:
    return is_official_glossary_path(active_glossary(settings).get('path') or DEFAULT_GLOSSARY_PATH)


async def fork_official_glossary_for_edit(settings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    settings = normalize_settings(settings or await read_settings())
    active = active_glossary(settings)
    if not is_official_glossary_path(active.get('path') or DEFAULT_GLOSSARY_PATH):
        return settings

    glossaries = list(settings.get('glossaries') or [])
    existing_ids = {item.get('id') for item in glossaries if isinstance(item, dict)}
    base_id = _safe_glossary_id(f'{active.get("id") or "default"}-edited')
    glossary_id = base_id
    suffix = 2
    while glossary_id in existing_ids:
        glossary_id = f'{base_id}-{suffix}'
        suffix += 1

    edited_name = f'{active.get("name") or DEFAULT_GLOSSARY_NAME}-已编辑'
    relative_path = make_relative_glossary_path(f'glossaries/{glossary_id}.json')
    source_path = get_effective_glossary_path(settings)
    target_path = resolve_glossary_path(relative_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if source_path.exists():
        source_text = await asyncio.to_thread(source_path.read_text, encoding='utf-8')
        await asyncio.to_thread(target_path.write_text, source_text, encoding='utf-8')
    else:
        await asyncio.to_thread(target_path.write_text, '{}\n', encoding='utf-8')

    glossaries.append(
        {
            'id': glossary_id,
            'name': edited_name,
            'path': relative_path,
            'version': str(active.get('version') or settings.get('glossary_version') or '1.0.0'),
            'source_lang': str(active.get('source_lang') or settings.get('source_lang')),
            'glossary_lang': str(active.get('glossary_lang') or settings.get('glossary_lang')),
            'target_lang': str(active.get('target_lang') or settings.get('target_lang')),
        }
    )

    return await write_settings({'active_glossary_id': glossary_id, 'glossaries': glossaries})


def get_effective_glossary_path(settings: Optional[dict[str, Any]] = None) -> Path:
    settings = normalize_settings(settings or dict(DEFAULT_SETTINGS))
    configured = settings.get('glossary_path') or os.environ.get('GLOSSARY_PATH') or DEFAULT_GLOSSARY_PATH
    path = resolve_glossary_path(configured)
    if is_official_glossary_path(configured) and not path.exists() and LEGACY_OFFICIAL_GLOSSARY_PATH.exists():
        return LEGACY_OFFICIAL_GLOSSARY_PATH
    return path


async def read_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    has_saved_path = False
    if SETTINGS_PATH.exists():
        try:
            raw = await asyncio.to_thread(SETTINGS_PATH.read_text, encoding='utf-8')
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                settings.update(
                    {
                        k: v
                        for k, v in loaded.items()
                        if k in DEFAULT_SETTINGS or k in {'active_glossary_id', 'glossaries'}
                    }
                )
                has_saved_path = bool(str(loaded.get('glossary_path') or '').strip())
        except Exception as e:
            log.warning('Failed to read glossary settings: %s', e)

    if os.environ.get('GLOSSARY_PATH') and not has_saved_path:
        settings['glossary_path'] = os.environ['GLOSSARY_PATH']

    return normalize_settings(settings)


async def write_settings(values: dict[str, Any]) -> dict[str, Any]:
    current = await read_settings()
    for key, value in values.items():
        if (key in DEFAULT_SETTINGS or key in {'active_glossary_id', 'glossaries'}) and value is not None:
            current[key] = value

    current = normalize_settings(current)
    active = active_glossary(current)
    if 'glossary_path' in values and values['glossary_path'] is not None:
        active['path'] = make_relative_glossary_path(str(values['glossary_path']).strip())
    if 'source_lang' in values and values['source_lang'] is not None:
        active['source_lang'] = values['source_lang']
    if 'glossary_lang' in values and values['glossary_lang'] is not None:
        active['glossary_lang'] = values['glossary_lang']
    if 'target_lang' in values and values['target_lang'] is not None:
        active['target_lang'] = values['target_lang']
    if 'glossary_version' in values and values['glossary_version'] is not None:
        active['version'] = str(values['glossary_version']).strip() or '1.0.0'
    current = normalize_settings(current)

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix='glossary-settings-', suffix='.json', dir=str(SETTINGS_PATH.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp:
            json.dump(current, tmp, ensure_ascii=False, indent=2)
            tmp.write('\n')
        await asyncio.to_thread(os.replace, tmp_name, SETTINGS_PATH)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    await invalidate_cache()
    return current


async def ensure_glossary_settings_persisted() -> dict[str, Any]:
    settings = await read_settings()
    if SETTINGS_PATH.exists():
        try:
            raw_text = await asyncio.to_thread(SETTINGS_PATH.read_text, encoding='utf-8')
            raw = json.loads(raw_text) if raw_text else {}
        except Exception:
            raw = None

        if raw is None:
            await write_settings({})
        else:
            normalized = normalize_settings(raw if isinstance(raw, dict) else {})
            if raw != normalized:
                await write_settings({})
    else:
        await write_settings({})
    return settings


async def invalidate_cache() -> None:
    global _cache_path, _cache_signature, _cache_entries
    async with _cache_lock:
        _cache_path = None
        _cache_signature = None
        _cache_entries = {}


def _parse_glossary(raw: Any) -> dict[str, str]:
    entries: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            source = normalize_term(str(key))
            target = str(value).strip()
            if source and target:
                entries[source] = target
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            source = normalize_term(str(item.get('source', '')))
            target = str(item.get('target', '')).strip()
            if source and target:
                entries[source] = target
    return entries


async def read_entries(settings: Optional[dict[str, Any]] = None) -> tuple[dict[str, str], Optional[float]]:
    global _cache_path, _cache_signature, _cache_entries

    settings = settings or await read_settings()
    path = get_effective_glossary_path(settings)
    if not path.exists():
        return {}, None

    stat = await asyncio.to_thread(path.stat)
    signature = (stat.st_mtime, stat.st_size)
    path_key = str(path.resolve())

    async with _cache_lock:
        if _cache_path == path_key and _cache_signature == signature:
            return dict(_cache_entries), stat.st_mtime

        raw_text = await asyncio.to_thread(path.read_text, encoding='utf-8')
        raw = json.loads(raw_text)
        entries = _parse_glossary(raw)
        _cache_path = path_key
        _cache_signature = signature
        _cache_entries = entries
        return dict(entries), stat.st_mtime


async def write_entries(entries: dict[str, str], settings: Optional[dict[str, Any]] = None) -> None:
    settings = settings or await read_settings()
    path = get_effective_glossary_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix='glossary-', suffix='.json', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp:
            json.dump(dict(entries.items()), tmp, ensure_ascii=False, indent=2)
            tmp.write('\n')
        await asyncio.to_thread(os.replace, tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    await invalidate_cache()


def _language_key(value: str) -> str:
    return re.sub(r'\s+', '', str(value or '').strip().casefold())


def _is_chinese_language(value: str) -> bool:
    key = _language_key(value)
    return key in {'中文', '汉语', '简体中文', '繁体中文', '普通话', 'chinese', 'mandarin', 'zh', 'zh-cn', 'zh_cn'}


def _glossary_language_pair(settings: dict[str, Any]) -> tuple[str, str]:
    source_lang = str(settings.get('source_lang') or DEFAULT_SETTINGS['source_lang']).strip()
    target_lang = str(
        settings.get('target_lang') or settings.get('glossary_lang') or DEFAULT_SETTINGS['target_lang']
    ).strip()
    return source_lang, target_lang


def _lang_to_code(lang_name: str) -> str:
    lang_name = lang_name.strip()

    if lang_name == "中":
        return "zh"
    elif lang_name == "英":
        return "en" 

    try:
        lang = langcodes.find(lang_name)
        return lang.language.lower()
    except LookupError:
        return lang_name.casefold()

def _find_explicit_command(text: str, lang_name: str, window: int = 20) -> bool:
    candidates = [f'翻译成{lang_name}', f'翻译{lang_name}', f'translate to {lang_name}',
                  f'translate into {lang_name}']
    head = text[:window]
    tail = text[-window:] if len(text) > window else text

    for cmd in candidates:
        cmd_cf = cmd.casefold()
        if cmd_cf in head.casefold() or cmd_cf in tail.casefold():
            return True
    return False

def _smart_language_detect(text: str, source_lang: str, target_lang: str) -> tuple[str, str]:
    source_lang = source_lang.strip() or DEFAULT_SETTINGS['source_lang']
    target_lang = target_lang.strip() or DEFAULT_SETTINGS['target_lang']

    if _find_explicit_command(text, source_lang):
        return target_lang, source_lang
    if _find_explicit_command(text, target_lang):
        return source_lang, target_lang
    
    from fast_langdetect import detect_language

    detected_code = detect_language(text).lower()
    source_code = _lang_to_code(source_lang)
    target_code = _lang_to_code(target_lang)

    if detected_code == source_code:
        return source_lang, target_lang
    if detected_code == target_code:
        return target_lang, source_lang
    if detected_code != source_code and detected_code != target_code:
        # 因为中文检测性较高，为了避免一些小语种检测错误，所以返回的时候应该以小语种优先
        return target_lang, source_lang         

    return source_lang, target_lang
    
    # explicit_to_source = [f'翻译成{source_lang}', f'翻译{source_lang}', f'translate to {source_lang}']
    # explicit_to_target = [f'翻译成{target_lang}', f'翻译{target_lang}', f'translate to {target_lang}']
    # if any(cmd.casefold() in text.casefold() for cmd in explicit_to_source):
    #     return target_lang, source_lang
    # if any(cmd.casefold() in text.casefold() for cmd in explicit_to_target):
    #     return source_lang, target_lang

    # source_is_chinese = _is_chinese_language(source_lang)
    # target_is_chinese = _is_chinese_language(target_lang)
    # chinese_chars = re.findall(r'[\u4e00-\u9fa5]', text)
    # valid_length = len(re.sub(r'\s+', '', text))
    # chinese_ratio = len(chinese_chars) / valid_length if valid_length > 0 else 0

    # if source_is_chinese != target_is_chinese:
    #     chinese_side = source_lang if source_is_chinese else target_lang
    #     other_side = target_lang if source_is_chinese else source_lang
    #     explicit_to_chinese = ['翻译中文', '翻译成中文']
    #     explicit_to_other = [f'翻译成{other_side}', f'翻译{other_side}']
    #     if any(cmd in text for cmd in explicit_to_chinese):
    #         return other_side, chinese_side
    #     if any(cmd in text for cmd in explicit_to_other):
    #         return chinese_side, other_side
    #     if chinese_ratio > 0.35:
    #         return chinese_side, other_side
    #     return other_side, chinese_side

    # return source_lang, target_lang



@dataclass
class ParsedSide:
    aliases: list[str]
    mode: str


class GlossaryMatcher:
    BRACKET_PAIRS = {
        '（': '）',
        '(': ')',
        '【': '】',
        '[': ']',
        '「': '」',
        '『': '』',
        '"': '"',
        "'": "'",
    }

    _LEFT = ''.join(re.escape(k) for k in BRACKET_PAIRS.keys())
    _RIGHT = ''.join(re.escape(v) for v in BRACKET_PAIRS.values())
    BRACKET_PATTERN = re.compile(f'([{_LEFT}])([^{_RIGHT}]*)([{_RIGHT}])')
    ALIAS_SEPARATORS = re.compile(r'[/／、,|｜]')

    def __init__(self):
        self._cc_t2s = LazyModel(_load_opencc_t2s, 'OpenCC t2s converter', log)

        # 候选词典缓存: entries id → candidates
        self._candidates_cache: dict[str, tuple[dict, list]] = {}
        self._max_candidate_caches = 8

    def _is_matched_pair(self, left: str, right: str) -> bool:
        return self.BRACKET_PAIRS.get(left) == right

    def _normalize_spaces(self, text: str) -> str:
        return ' '.join(text.split())

    def _normalize_brackets(self, text: str) -> str:
        """统一括号为中文括号"""
        bracket_map = {
            '(': '（',
            ')': '）',
            '[': '【',
            ']': '】',
            '「': '「',
            '」': '」',
        }
        for src, dst in bracket_map.items():
            text = text.replace(src, dst)
        return text

    def _normalize_term(self, text: str) -> str:
        """标准化词条：unicode、括号、空格、繁简、大小写"""
        text = unicodedata.normalize('NFKC', text)
        text = self._normalize_brackets(text)
        text = self._normalize_spaces(text)
        text = self._cc_t2s.convert(text)
        return text

    def _strip_brackets(self, text: str) -> str:
        """去除括号及内容"""

        def replace(m: re.Match) -> str:
            return '' if self._is_matched_pair(m.group(1), m.group(3)) else m.group(0)

        return self._normalize_spaces(self.BRACKET_PATTERN.sub(replace, text))

    def _strip_bracket_markers(self, text: str) -> str:
        """只去掉括号符号，保留括号内的内容"""

        def replace(m: re.Match) -> str:
            return m.group(2) if self._is_matched_pair(m.group(1), m.group(3)) else m.group(0)

        return self._normalize_spaces(self.BRACKET_PATTERN.sub(replace, text))

    def _split_aliases(self, text: str) -> list[str]:
        """按别名分隔符拆分"""
        parts = self.ALIAS_SEPARATORS.split(text)
        seen = set()
        result = []
        for p in parts:
            p = self._normalize_term(p.strip())
            if p and p not in seen:
                seen.add(p)
                result.append(p)
        return result

    def _has_bracket_aliases(self, text: str) -> bool:
        """判断括号内是否存在别名分隔符"""
        m = self.BRACKET_PATTERN.search(text)
        if not m or not self._is_matched_pair(m.group(1), m.group(3)):
            return False
        inner = m.group(2)
        return bool(self.ALIAS_SEPARATORS.search(inner))

    def _expand_bracket_aliases(self, text: str) -> list[str]:
        """
        在分割别名之前，先展开括号内的多别名：
        "上课（学习/睡觉）" → ["上课（学习）", "上课（睡觉）"]
        "上课（学习）"      → ["上课（学习）"]  不变
        "上午/下午"         → ["上午/下午"]     不变，无括号
        """
        # 找第一个含分隔符的括号
        for m in self.BRACKET_PATTERN.finditer(text):
            if not self._is_matched_pair(m.group(1), m.group(3)):
                continue
            inner = m.group(2)
            inner_aliases = self.ALIAS_SEPARATORS.split(inner)
            if len(inner_aliases) <= 1:
                continue  # 这个括号内没有分隔符，找下一个

            # 找到了，展开这个括号
            prefix = text[: m.start()]
            suffix = text[m.end() :]

            expanded = []
            for alias in inner_aliases:
                alias = alias.strip()
                if alias:
                    # 括号替换为内容本身（去掉括号符号）
                    variant = f'{prefix}{alias}{suffix}'
                    expanded.append(variant)

            # 对每个展开结果递归处理（可能还有其他括号）
            result = []
            for variant in expanded:
                result.extend(self._expand_bracket_aliases(variant))
            return result

        # 没有找到含分隔符的括号，直接返回
        return [text]

    def _expand_bracket_variants(self, term: str) -> list[str]:
        """
        展开括号变体:
        "上午（这个）太阳" → ["上午（这个）太阳", "上午这个太阳", "上午太阳"]
        """
        variants = [term]
        stripped_markers = self._strip_bracket_markers(term)
        if stripped_markers != term:
            variants.append(stripped_markers)
        stripped_all = self._strip_brackets(term)
        if stripped_all != term and stripped_all not in variants:
            variants.append(stripped_all)
        return variants

    # ── 候选词典构建 ────────────────────────────────────────
    @staticmethod
    def _entries_hash(entries: dict[str, str]) -> str:
        """用内容 hash 做缓存 key，比 id() 可靠"""
        h = hashlib.sha256(
            json.dumps(sorted(entries.items()), ensure_ascii=False).encode()
        ).hexdigest()
        return h

    def _parse_side(self, raw: str) -> ParsedSide:
        """
        alias: 普通别名分隔
        bracket: 括号内有分隔符 → bracket 展开
        """
        if self._has_bracket_aliases(raw):
            return ParsedSide(aliases=self._expand_bracket_aliases(raw), mode='bracket')
        else:
            return ParsedSide(aliases=self._split_aliases(raw), mode='alias')

    def _pair_keys_values(self, key_parsed, value_parsed):
        """
        alias: 一一对应，数量不等时最后一个 value 兜底
        bracket: 展开 或 1→n：全部 key 对应第一个 value
        """
        keys = key_parsed.aliases
        vals = value_parsed.aliases

        if key_parsed.mode == 'alias' and value_parsed.mode == 'alias':
            if len(vals) == len(keys):
                return [(k, vals[i]) for i, k in enumerate(keys)]

        if len(vals) == 1:
            primary = vals[0]
        elif len(vals) > 1:
            primary = vals
        else:
            primary = None
        return [(k, primary) for k in keys]

    def build_candidates(
        self,
        key: str,
        entries: dict[str, str],
        reverse: bool = False,
    ) -> tuple[dict, list]:
        """构建标准化后的候选词典"""
        if key in self._candidates_cache:
            return self._candidates_cache[key]
        candidates: dict[str, list] = {}

        for source, target in entries.items():
            raw_key = target if reverse else source
            raw_value = source if reverse else target
            if not raw_key or not raw_value:
                continue

            # 根据分隔符进行分割出多个字典
            # 1. 根据分割符号划分，分为1->n、n->n
            # 2. 根据括号和分割符号 (do / did) homework 1->n, n1->n2
            # 目前就一种情况是单对单，是只有分割符号的情况下。
            key_parsed = self._parse_side(raw_key)
            value_parsed = self._parse_side(raw_value)
            pairs = self._pair_keys_values(key_parsed, value_parsed)

            for key_alias, value in pairs:
                key_variants = self._expand_bracket_variants(key_alias)

                for i, key_variant in enumerate(key_variants):
                    key_form = re.sub(r'\s+', '', key_variant)
                    if not key_form:
                        continue

                    key_form = self._normalize_spaces(key_form)
                    slot = candidates.setdefault(key_form, [])
                    values_to_add = value if isinstance(value, list) else [value]
                    for v in values_to_add:
                        if v and v not in slot:
                            slot.append(v)

        sorted_result = sorted(candidates.items(), key=lambda x: len(x[0]), reverse=True)
        if len(self._candidates_cache) >= self._max_candidate_caches:
            self._candidates_cache.pop(next(iter(self._candidates_cache)))
        self._candidates_cache[key] = (candidates, sorted_result)
        return candidates, sorted_result

    def invalidate_cache(self) -> None:
        """词典内容变化时手动清除缓存"""
        self._candidates_cache.clear()

    def _remove_covered_terms(self, text: str, matches) -> list:
        # 目前是通过未知判断是否重叠，如果不重叠则添加到里面，一开始会先根据精准和模糊排序，
        # 但排序对于优先级还是不能确定，排序优先级是先根据长度排序的，所以有一些模糊搜索的内容也会列在前面
        # 可以在覆盖里，再比对一下优先级，如果说是精准匹配则保留，如果不是精准匹配，则保留精准匹配的，这样准确性会更高一些

        # 先根据精准匹配情况以及字符长短进行排序，先匹配短语，然后再是单词
        # sorted_matches = sorted(
        #     matches,
        #     key=lambda x: (x[2], len(x[0]), x[3]),  # (is_exact, length, score)
        #     reverse=True
        # )
        # covered_ranges: list[tuple[int, int]] = []
        #
        # def is_covered(start: int, end: int) -> bool:
        #     return any(c_start <= start and end <= c_end for c_start, c_end in covered_ranges)
        #
        # result = []
        # for term, mapped, is_exact, score, positions in sorted_matches:
        #     if any(not is_covered(s, e) for s, e in positions):
        #         result.append((term, mapped))
        #         covered_ranges.extend(positions)
        #
        # return result

        text_len = len(text)
        covered = bytearray(text_len)  # 位图，O(1) 查询和标记

        sorted_matches = sorted(matches, key=lambda x: (x[2], len(x[0]), x[3]), reverse=True)

        result = []
        for term, mapped, is_exact, score, positions in sorted_matches:
            # 检查是否有任意位置未被覆盖
            has_uncovered = any(
                not all(covered[s:e])  # bytearray 切片比循环快
                for s, e in positions
                if s >= 0 and e <= text_len
            )
            if has_uncovered:
                result.append((term, mapped))
                for s, e in positions:
                    if 0 <= s < e <= text_len:
                        covered[s:e] = b'\x01' * (e - s)

        return result

    def find_matches(
        self,
        text: str,
        key: str,
        entries: dict[str, str],
        max_terms: int,
        reverse: bool = False,
        fuzzy_threshold: int = 70,
    ) -> list[tuple[str, str]]:
        candidates, sorted_candidates = self.build_candidates(key, entries, reverse)

        clean_text = re.sub(r'\s+', '', text)
        text_char_set = set(clean_text)
        all_hits: list[tuple[str, list, bool, float, list]] = []
        seen_keys: set[str] = set()
        for term, mapped in sorted_candidates:
            key = term.casefold()
            if key in seen_keys:
                continue

            # 精确匹配
            if term in clean_text:
                positions = self._find_all_positions(term, clean_text)
                all_hits.append((term, mapped, True, 100.0, positions))
                seen_keys.add(key)
            else:
                # 字符集重叠率，比 fuzz 快 10x 以上
                term_chars = set(term)
                overlap = len(term_chars & text_char_set) / max(len(term_chars), 1)
                if overlap < 0.6:
                    continue

                # 模糊匹配，直接获取分数和位置
                alignment = fuzz.partial_ratio_alignment(term, clean_text)
                if alignment is not None and alignment.score >= fuzzy_threshold:
                    positions = [(alignment.dest_start, alignment.dest_end)]
                    all_hits.append((term, mapped, False, alignment.score, positions))
                    seen_keys.add(key)

            if len(all_hits) >= max_terms:
                break

        return self._remove_covered_terms(clean_text, all_hits)

    def _find_all_positions(self, term: str, text: str) -> list[tuple[int, int]]:
        """找出 term 在 text 中所有精确出现的位置"""
        positions = []
        start = 0
        while True:
            idx = text.find(term, start)
            if idx == -1:
                break
            positions.append((idx, idx + len(term)))
            start = idx + 1
        return positions

    def _find_fuzzy_positions(self, term: str, text: str) -> list[tuple[int, int]]:
        """
        模糊命中时，用滑动窗口找最相似的子串位置
        窗口大小 = term长度 ± 2（容忍助词插入/删除）
        """
        best_score = 0
        best_pos = None
        tlen = len(term)

        for window in range(max(1, tlen - 2), tlen + 3):
            for start in range(len(text) - window + 1):
                sub = text[start : start + window]
                score = fuzz.ratio(term, sub)
                if score > best_score:
                    best_score = score
                    best_pos = (start, start + window)

        return [best_pos] if best_pos else []


matcher = GlossaryMatcher()


class BilingualKnowledgeReader:
    BILINGUAL_TYPE = 'bilingual'

    def __init__(self, bm25_index_ttl: float = 300.0, max_bm25_indexes: int = 16) -> None:
        self._bm25_cache: dict[str, dict] = {}
        self._bm25_locks: dict[str, asyncio.Lock] = {}
        self._bm25_index_ttl = bm25_index_ttl
        self._max_bm25_indexes = max_bm25_indexes

    def invalidate_bm25_cache(self, collection_name: Optional[str] = None):
        """文档发生变化（新增/删除/更新）时调用，强制下次查询重建索引"""
        if collection_name is None:
            self._bm25_cache.clear()
        else:
            self._bm25_cache.pop(collection_name, None)

    
    async def _get_or_build_bm25_index(
        self,
        collection_name: str,
        fetch_fn,
    ) -> Optional[dict]:
        from rank_bm25 import BM25Okapi

        now = time.monotonic()
        cached = self._bm25_cache.get(collection_name)
        if cached and now - cached['built_at'] < self._bm25_index_ttl:
            return cached

        lock = self._bm25_locks.setdefault(collection_name, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._bm25_cache.get(collection_name)
            if cached and now - cached['built_at'] < self._bm25_index_ttl:
                return cached

            collection_result = await fetch_fn(collection_name)
            if not collection_result or not collection_result.documents or not collection_result.documents[0]:
                return None

            texts = collection_result.documents[0]
            metadatas = collection_result.metadatas[0]
            tokenized = [self._tokenize(t) for t in texts]
            entry = {
                'bm25': BM25Okapi(tokenized),
                'texts': texts,
                'metadatas': metadatas,
                'built_at': now,
            }

            expired = [
                key
                for key, value in self._bm25_cache.items()
                if now - value['built_at'] >= self._bm25_index_ttl
            ]
            for key in expired:
                self._bm25_cache.pop(key, None)
                old_lock = self._bm25_locks.get(key)
                if old_lock is not None and not old_lock.locked():
                    self._bm25_locks.pop(key, None)

            while len(self._bm25_cache) >= self._max_bm25_indexes:
                oldest = min(self._bm25_cache, key=lambda key: self._bm25_cache[key]['built_at'])
                self._bm25_cache.pop(oldest, None)
                old_lock = self._bm25_locks.get(oldest)
                if old_lock is not None and not old_lock.locked():
                    self._bm25_locks.pop(oldest, None)

            self._bm25_cache[collection_name] = entry
            return entry
 

    @classmethod
    def name_to_langcode(cls, name: str, default: str = 'en') -> str:
        if not name or not isinstance(name, str):
            return default

        if name == "中":
            return "zh"
        elif name == "英":
            return "en" 

        try:
            name = name.strip()
            lang = langcodes.find(name)
            code = lang.language
            return code
        except:
            manual_map = {
                '英语': 'en',
                '英文': 'en',
                '尼泊尔语': 'ne',
                '尼泊尔': 'ne',
                '中文': 'zh',
                '汉语': 'zh',
                '日语': 'ja',
                '韩语': 'ko',
            }
            return manual_map.get(name, default)

    async def cleanup_duplicate_collection_content(self, collection_name: str) -> dict[str, int]:
        if not collection_name:
            return {'removed_count': 0, 'kept_count': 0}

        collection = None
        access_errors: list[Exception] = []

        try:
            collection = VECTOR_DB_CLIENT.client.get_collection(collection_name)
        except Exception as exc:
            access_errors.append(exc)
        if collection is None:
            return {'removed_count': 0, 'kept_count': 0}

        read_batch_size = 200
        seen: set[str] = set()
        keep_ids: list[str] = []
        remove_ids: list[str] = []
        offset = 0
        total_read = 0

        while True:
            try:
                result = collection.get(include=['documents', 'metadatas'], limit=read_batch_size, offset=offset)
            except TypeError:
                break

            documents = result.get('documents') or []
            metadatas = result.get('metadatas') or []
            ids = result.get('ids') or list(range(offset, offset + len(documents)))
            if not documents:
                break

            for item_id, content, meta in zip(ids, documents, metadatas):
                normalized = self._normalize_candidate_text(content)
                if not normalized:
                    remove_ids.append(item_id)
                    continue
                if normalized in seen:
                    remove_ids.append(item_id)
                    continue
                seen.add(normalized)
                keep_ids.append(item_id)

            total_read += len(documents)
            if len(documents) < read_batch_size:
                break
            offset += read_batch_size

        if remove_ids:
            deleted_count = 0
            remaining_ids = list(remove_ids)
            batch_size = 50
            try:
                while remaining_ids:
                    batch_ids = remaining_ids[:batch_size]
                    while True:
                        try:
                            await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=collection_name, ids=batch_ids)
                            deleted_count += len(batch_ids)
                            remaining_ids = remaining_ids[batch_size:]
                            break
                        except Exception as exc:
                            if batch_size <= 1:
                                raise
                            batch_size = max(1, batch_size // 2)
                            batch_ids = batch_ids[:batch_size]
            except Exception as exc:
                log.warning('Failed to delete duplicate items from collection %s: %s', collection_name, exc)
                return {'removed_count': 0, 'kept_count': len(keep_ids)}

            return {'removed_count': deleted_count, 'kept_count': len(keep_ids)}

        return {'removed_count': len(remove_ids), 'kept_count': len(keep_ids)}

    def lcs_overlap_score(self, a: str, b: str, min_overlap: int = 15) -> float:
        if not a or not b:
            return 0.0
        matcher = SequenceMatcher(None, a, b, autojunk=False)
        match = matcher.find_longest_match(0, len(a), 0, len(b))
        lcs_len = match.size
        if lcs_len > min_overlap:
            return 100.0
        return 0.0
    
    
    _jieba = LazyModel(_load_jieba, 'jieba tokenizer', log)

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        jieba = cls._jieba.get()
        return [token.strip() for token in jieba.cut_for_search(text) if token.strip()]
 

    async def _bm25_recall(
        self,
        collection_name: str,
        query: str,
        k: int,
        fetch_fn,
    ) -> list[tuple[float, str, dict]]:
        entry = await self._get_or_build_bm25_index(collection_name, fetch_fn)
        if entry is None:
            return []
 
        tokenized_query = self._tokenize(query)
        scores = entry['bm25'].get_scores(tokenized_query)
 
        ranked = sorted(zip(scores, entry['texts'], entry['metadatas']), key=lambda x: x[0], reverse=True)
        ranked = [(s, t, m) for s, t, m in ranked if s > 0]
        if not ranked:
            return []
 
        max_score = ranked[0][0]
        return [(s / max_score, t, m) for s, t, m in ranked[:k]]

    async def _embedding_recall(
        self,
        collection_name: str,
        query: str,
        embedding_function,
        k: int,
    ) -> list[tuple[float, str, dict]]:
        retriever = VectorSearchRetriever(
            collection_name=collection_name,
            embedding_function=embedding_function,
            top_k=k,
        )
        docs = await retriever.ainvoke(query)
        return [
            (doc.metadata.get('score', doc.metadata.get('distance', 0.0)), doc.page_content, doc.metadata)
            for doc in docs
        ]
 
    def _rerank_recall(
        self,
        query: str,
        candidates: list[tuple[float, str, dict]],
        reranking_function,
        top_n: int,
        r_score: float = 0.0,
    ) -> list[tuple[float, str, dict]]:
        if not candidates:
            return []
        if reranking_function is None:
            return self._deduplicate_candidates(sorted(candidates, key=lambda x: x[0], reverse=True))[:top_n]

        documents = [Document(page_content=c[1], metadata=c[2]) for c in candidates]
        scores = reranking_function(query, documents)
        reranked = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
        
        scored = [(score, cand[1], cand[2]) for score, cand in reranked]
        above_threshold = [c for c in scored if c[0] >= r_score]

        # 保底：如果閾值篩完數量不夠 min_results，就放寬閾值，
        if len(above_threshold) >= top_n:
            result = above_threshold
        else:
            result = scored[:max(top_n, len(above_threshold))]
        return self._deduplicate_candidates(result)[:top_n]
    
    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    @staticmethod
    def _normalize_candidate_text(text: str) -> str:
        return re.sub(r'\s+', ' ', str(text or '').strip()).casefold()

    def _deduplicate_candidates(
        self,
        candidates: list[tuple[float, str, dict]],
    ) -> list[tuple[float, str, dict]]:
        if not candidates:
            return []

        deduped: list[tuple[float, str, dict]] = []
        seen: set[str] = set()
        for score, text, meta in sorted(candidates, key=lambda x: x[0], reverse=True):
            normalized = self._normalize_candidate_text(text)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append((score, text, meta))
        return deduped
 
    def _fuse(
        self,
        bm25_results: list[tuple[float, str, dict]],
        embedding_results: list[tuple[float, str, dict]],
        bm25_weight: float,
    ) -> list[tuple[float, str, dict]]:
        combined: dict[str, tuple[float, str, dict]] = {}
 
        for score, text, meta in bm25_results:
            h = self._content_hash(text)
            combined[h] = (score * (1 - bm25_weight), text, meta)
 
        for score, text, meta in embedding_results:
            h = self._content_hash(text)
            weighted = score * bm25_weight
            if h in combined:
                prev_score, prev_text, prev_meta = combined[h]
                combined[h] = (prev_score + weighted, prev_text, prev_meta)
            else:
                combined[h] = (weighted, text, meta)
 
        fused = sorted(combined.values(), key=lambda x: x[0], reverse=True)
        return self._deduplicate_candidates([(score, text, meta) for score, text, meta in fused])

 
    async def _retrieve_from_collection(
        self,
        collection_name: str,
        query: str,
        embedding_function,
        reranking_function,
        fetch_fn,
        k_recall: int,
        k_final: int,
        r: float,
        hybrid_bm25_weight: float,
    ) -> list[tuple[float, str, dict]]:
        bm25_results = (
            await self._bm25_recall(collection_name, query, k_recall, fetch_fn)
            if hybrid_bm25_weight > 0
            else []
        )
        embedding_results = (
            await self._embedding_recall(collection_name, query, embedding_function, k_recall)
            if hybrid_bm25_weight < 1
            else []
        )
 
        if hybrid_bm25_weight <= 0:
            fused = embedding_results
        elif hybrid_bm25_weight >= 1:
            fused = bm25_results
        else:
            fused = self._fuse(bm25_results, embedding_results, hybrid_bm25_weight)
 
        return self._rerank_recall(query, fused, reranking_function, top_n=k_final, r_score=r)
 
    async def retrieve(
        self,
        collection_names: list[str],
        queries: list[str],
        embedding_function,
        reranking_function,
        k_recall: int,
        k_final: int,
        r: float,
        hybrid_bm25_weight: float,
    ) -> dict:
        async def fetch_fn(name: str):
            try:
                return await ASYNC_VECTOR_DB_CLIENT.get(collection_name=name)
            except Exception as e:
                log.exception(f'Failed to fetch collection {name}: {e}')
                return None
 
        async def _run(collection_name: str, query: str):
            try:
                return await self._retrieve_from_collection(
                    collection_name=collection_name,
                    query=query,
                    embedding_function=embedding_function,
                    reranking_function=reranking_function,
                    fetch_fn=fetch_fn,
                    k_recall=k_recall,
                    k_final=k_final,
                    r=r,
                    hybrid_bm25_weight=hybrid_bm25_weight,
                )
            except Exception as e:
                log.exception(f'Error retrieving from {collection_name}: {e}')
                return None
 
        tasks = [(name, q) for name in collection_names for q in queries]
        all_results = await asyncio.gather(*[_run(name, q) for name, q in tasks])
 
        merged: dict[str, tuple[float, str, dict]] = {}
        for result in all_results:
            if not result:
                continue
            for score, text, meta in result:
                h = self._content_hash(text)
                if h not in merged or score > merged[h][0]:
                    merged[h] = (score, text, meta)
 
        final = self._deduplicate_candidates(list(merged.values()))[:k_final]
        if not final:
            return {'distances': [[]], 'documents': [[]], 'metadatas': [[]]}
 
        distances, documents, metadatas = zip(*final)
        return {
            'distances': [list(distances)],
            'documents': [list(documents)],
            'metadatas': [list(metadatas)],
        }
 
    async def find_matches(self, request, queries, source_lang, target_lang, user):
        try:
            async with get_async_db() as session:
                knowledges = await Knowledges.get_knowledge_bases(db=session)
                filter_knowledges = [
                    item for item in knowledges
                    if isinstance(getattr(item, 'meta', None), dict)
                    and item.meta.get('knowledge_type') == 'bilingual'
                ]
 
            collection_names = [k.id for k in filter_knowledges]
            knowledge_name_by_id = {
                k.id: (getattr(k, 'name', None) or k.id) for k in filter_knowledges
            }
            embedding_function = lambda query, prefix: request.app.state.EMBEDDING_FUNCTION(query, prefix=prefix, user=user)
            reranking_function = (
                (lambda query, documents: request.app.state.RERANKING_FUNCTION(query, documents, user=user))
                if request.app.state.RERANKING_FUNCTION else None
            )
 
            retrieval_config = await Config.get_many(
                'rag.top_k', 'rag.relevance_threshold', 'rag.hybrid_bm25_weight',
            )
 
            k_final = retrieval_config.get('rag.top_k', 3)
            k_recall = max(k_final * 5, 20)

            for collection_name in collection_names:
                await self.cleanup_duplicate_collection_content(collection_name)
 
            raw_result = await self.retrieve(
                collection_names=collection_names,
                queries=[queries],
                embedding_function=embedding_function,
                reranking_function=reranking_function,
                k_recall=k_recall,
                k_final=k_final, 
                r=retrieval_config.get('rag.relevance_threshold', 0.0),
                hybrid_bm25_weight=retrieval_config.get('rag.hybrid_bm25_weight', 0.5),
            )
 
            metadatas = raw_result.get('metadatas', [[]])[0]
            is_match = False

            # 判断当前字符是否和搜索出来的字符重叠
            for metadata in metadatas:
                source = metadata.get('parent_content', '')
                score = self.combined_similarity(queries, source)
                if score >= 99:
                    is_match = True
                    break

            match is_match:
                case True:
                    return await self.find_matches_sentence(raw_result, source_lang, target_lang, collection_names, queries, knowledge_name_by_id)
                case False:
                    return await self.find_matches_words(raw_result, source_lang, target_lang, collection_names, knowledge_name_by_id)

        except Exception as e:
            log.warning('BilingualKnowledgeReader: 查询知识库失败: %s', e)
            return []
        
    async def find_matches_words(self, raw_result, source_lang, target_lang, collection_names, knowledge_name_by_id):
        documents = raw_result.get('documents', [[]])[0]
        metadatas = raw_result.get('metadatas', [[]])[0]

        source_lang_code = self.name_to_langcode(source_lang)
        target_lang_code = self.name_to_langcode(target_lang)
        
        sentence_collection = []
        for collection_name in collection_names:
            for _, meta in zip(documents, metadatas):

                bilingual_id = meta.get('bilingual_id')
                para_idx = meta.get('para_index')
                sentence_index = meta.get('sentence_index')

                collection = VECTOR_DB_CLIENT.client.get_collection(collection_name)  # noqa: F821
                result = collection.get(
                    where={'$and': [
                        {'bilingual_id': {'$eq': bilingual_id}},
                        {'para_index': {'$eq': para_idx}},
                        {'sentence_index': {'$eq': sentence_index}},
                    ]},
                    include=['metadatas'],
                )

                if result:
                    sentence_collection.append((collection_name, result))
                    break

        words_list = []
        sources_by_collection: dict[str, dict] = {}
        for collection_name, result in sentence_collection:
            metadatas = result.get("metadatas", [])
            for meta in metadatas:
                word_langs = meta.get("words", {})
                if isinstance(word_langs, str):
                    word_langs = ast.literal_eval(word_langs)
                words = word_langs.get(target_lang_code, '')
                words_list.extend(words)

                bilingual_id = meta.get('bilingual_id')
                para_idx = meta.get('para_index')
                align_score = meta.get('align_score', 0)
                langs = meta.get('langs', {})
                if isinstance(langs, str):
                    langs = ast.literal_eval(langs)

                src_text = langs.get(source_lang_code, '')
                tgt_text = langs.get(target_lang_code, '')
                if not collection_name:
                    continue
            
                knowledge_name = knowledge_name_by_id.get(collection_name, collection_name)
                bucket = sources_by_collection.setdefault(
                    collection_name,
                    {
                        'source': {
                            'id': collection_name,
                            'name': knowledge_name,
                            'type': 'bilingual',
                        },
                        'document': [],
                        'metadata': [],
                    },
                )
                bucket['document'].append(f'{src_text} -> {tgt_text}')
                bucket['metadata'].append(
                    {
                        'source': knowledge_name,
                        'name': knowledge_name,
                        'bilingual_id': bilingual_id,
                        'para_index': para_idx,
                        'align_score': align_score,
                    }
                )
        return words_list, list(sources_by_collection.values()), False

    async def find_matches_sentence(self, raw_result, source_lang, target_lang, collection_names, queries, knowledge_name_by_id):
        source_lang_code = self.name_to_langcode(source_lang)
        target_lang_code = self.name_to_langcode(target_lang)

        documents = raw_result.get('documents', [[]])[0]
        metadatas = raw_result.get('metadatas', [[]])[0]
        distances = raw_result.get('distances', [[]])[0]

        para_list = set()
        for _, meta, _ in zip(documents, metadatas, distances):
            bilingual_id = meta.get('bilingual_id')
            para_idx = meta.get('para_index')
            if para_idx is None:
                continue
            key = (bilingual_id, para_idx)
            if key not in para_list:
                para_list.add(key)

        para_source_collection: dict[tuple, str] = {}
        full_para_map = {}
        for key in list(para_list):
            per_collection = await self._fetch_paragraph_sentences(
                key, collection_names, source_lang_code, target_lang_code,
            )
            full_para_map[key] = []
            for collection_name, sent_map, _ in per_collection:
                filtered = {}
                for sent_idx, item in sent_map.items():
                    best_sim = self.combined_similarity(queries, item['source'])
                    if best_sim < 50:
                        continue
                    filtered[sent_idx] = item
                if filtered:
                    full_para_map[key].append(filtered)
                    para_source_collection[key] = collection_name

        bilingual_tuple = []
        sources_by_collection: dict[str, dict] = {}
        for key, para_items in full_para_map.items():
            collection_name = para_source_collection.get(key)
            knowledge_name = knowledge_name_by_id.get(collection_name, collection_name) if collection_name else '双语知识库'

            for sentence_items in para_items:
                for _, item in sentence_items.items():
                    src_text = item['source']
                    tgt_text = item['target']
                    bilingual_tuple.append((src_text, tgt_text))

                    if not collection_name:
                        continue

                    bucket = sources_by_collection.setdefault(
                        collection_name,
                        {
                            'source': {
                                'id': collection_name,
                                'name': knowledge_name,
                                'type': 'bilingual',
                            },
                            'document': [],
                            'metadata': [],
                        },
                    )
                    bucket['document'].append(f'{src_text} -> {tgt_text}')
                    bucket['metadata'].append(
                        {
                            'source': knowledge_name,
                            'name': knowledge_name,
                            'bilingual_id': key[0],
                            'para_index': key[1],
                            'align_score': item.get('align_score'),
                        }
                    )

        return bilingual_tuple, list(sources_by_collection.values()), True

    async def _fetch_paragraph_sentences(
        self,
        key: tuple,
        collection_names: list[str],
        source_lang_code: str,
        target_lang_code: str,
    ) -> list[tuple[str, dict, dict]]:
        """
        取出某个段落在各 collection 下的全部句子（不做相似度过滤），
        返回 [(collection_name, sent_map), ...]，sent_map: {sentence_index: {'source','target','align_score'}}
        句子/词组两条路径共用，词组路径需要完整段落上下文，不能只挑相似句子。
        """
        bilingual_id, para_idx = key
        per_collection = []
        for collection_name in collection_names:
            collection = VECTOR_DB_CLIENT.client.get_collection(collection_name)  # noqa: F821
            result = collection.get(
                where={'$and': [
                    {'bilingual_id': {'$eq': bilingual_id}},
                    {'para_index': {'$eq': para_idx}},
                ]},
                include=['metadatas'],
            )
            sent_map = {}
            for meta in result.get('metadatas', []):
                sent_idx = meta.get('sentence_index')
                if sent_idx is None:
                    continue
                langs = meta.get('langs', {})
                if isinstance(langs, str):
                    langs = ast.literal_eval(langs)
                source = langs.get(source_lang_code, '')
                target = langs.get(target_lang_code, '')
                align_score = meta.get('align_score', 0)
                sent_map[sent_idx] = {'source': source, 'target': target, 'align_score': align_score}
            if sent_map:
                per_collection.append((collection_name, sent_map, result.get('metadatas', [])))
        return per_collection

    def combined_similarity(self, a: str, b: str) -> float:
        partial = fuzz.partial_ratio(a, b)
        overlap = self.lcs_overlap_score(a, b)
        return max(partial, overlap)


bilingual_matcher = BilingualKnowledgeReader()


def _build_glossary_block(
    text: str,
    entries: dict[str, str],
    settings: dict[str, Any],
    source_lang: str,
    target_lang: str,
) -> str:
    max_terms = int(settings.get('max_terms_injected') or 10)
    configured_source, configured_target = _glossary_language_pair(settings)
    reverse = _language_key(source_lang) == _language_key(configured_target) and \
              _language_key(target_lang) == _language_key(configured_source)
    forward = _language_key(source_lang) == _language_key(configured_source) and \
              _language_key(target_lang) == _language_key(configured_target)
    if not (forward or reverse):
        return ''

    key = settings.get("glossary_path")
    hits = matcher.find_matches(text, key, entries, max_terms=max_terms, reverse=reverse)
    if not hits:
        return ''

    def format_mapped(mapped):
        if len(mapped) == 1:
            return mapped[0]
        return f'{"  /  ".join(mapped)}'

    term_lines = '\n'.join(f'  {term} -> {format_mapped(mapped)}' for term, mapped in hits)
    return (
        f'\n{term_lines}\n 以上词典为 {configured_source} -> {configured_target}。'
        f'本次翻译方向为 {source_lang} -> {target_lang}。翻译时请优先使用词典词汇翻译。'
        f'词典中部分词条包含多个候选译文，用 [ ] 标注并以 / 分隔。'
    )


async def _build_bilingual_block(request, text, settings: dict[str, Any], source_lang: str, target_lang: str, user):
    max_terms = int(settings.get('max_terms_injected') or 10)
    configured_source, configured_target = _glossary_language_pair(settings)
    reverse = _language_key(source_lang) == _language_key(configured_target) and _language_key(
        target_lang
    ) == _language_key(configured_source)
    forward = _language_key(source_lang) == _language_key(configured_source) and _language_key(
        target_lang
    ) == _language_key(configured_target)
    if not (forward or reverse):
        return ''

    hits, sources, is_matched = await bilingual_matcher.find_matches(request, text, source_lang, target_lang, user)
    if not hits:
        return ''

    if is_matched:
        term_lines = '\n'.join(f'  {term} -> {mapped}' for term, mapped in hits)
        block = (
            f'\n{term_lines}\n 以上译文对照为 {configured_source} -> {configured_target}。'
            f'本次翻译方向为 {source_lang} -> {target_lang}。翻译时请优先使用词典词汇翻译。'
        )
        return block, sources, is_matched
    else:
        term_lines = '\n'.join(f'  {term} -> {mapped}' for term, mapped in hits)
        block = (
            f'\n{term_lines}\n 以上译文对照为 {configured_source} -> {configured_target}。'
            f'本次翻译方向为 {source_lang} -> {target_lang}。翻译时请优先使用词典词汇翻译。'
        )
        return block, sources, is_matched


def _strict_target_language_rule(source_lang: str, target_lang: str) -> str:
    return f'最终输出只使用自然、标准的{target_lang}。不要混入非目标语言。'


def _set_message_text(message: dict[str, Any], value: str, part_index: Optional[int] = None) -> None:
    content = message.get('content')
    if isinstance(content, str):
        message['content'] = value
    elif isinstance(content, list):
        if part_index is not None:
            content[part_index]['text'] = value
        else:
            content.insert(0, {'type': 'text', 'text': value})


def _latest_user_text_ref(messages: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], Optional[int], str]:
    for message in reversed(messages):
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return message, None, content
        if isinstance(content, list):
            for idx in range(len(content) - 1, -1, -1):
                part = content[idx]
                if isinstance(part, dict) and part.get('type') == 'text':
                    return message, idx, part.get('text', '')
            # Latest user message has no text part (e.g. audio-only voice
            # message) — still return it so mode prompts can be inserted.
            return message, None, ''
    return None, None, ''


def _message_has_audio(message: Optional[dict[str, Any]]) -> bool:
    if not message:
        return False
    content = message.get('content')
    if not isinstance(content, list):
        return False
    return any(isinstance(part, dict) and part.get('type') == 'input_audio' for part in content)


TRANSLATION_NAME_RULES = (
    '翻译额外规则：当把外语翻译为中文时，必须完整翻译所有普通词汇。'
    '只有明确属于专有名词的人名、城市名、地名、机构名、作品名、品牌名、组织名，'
    '才可以在中文译名后用括号保留原文，如“马德里（Madrid）”。'
    '动词、形容词等普通词语不要保留原文，必须完整自然翻译成中文。\n\n'
    '组织、职场等场景中的岗位、职级和成员称呼应按实际语义正常翻译，不作为人名处理。\n\n'
    '若翻译内容为多人对话，尽量区分说话人是谁。'
    '中文原文缺少标点或用空格代替标点时，先根据语义补全断句，再翻译。'
)


def _translation_name_rules_for_target(target_lang: str) -> str:
    if not _is_chinese_language(target_lang):
        return ''
    return f'{TRANSLATION_NAME_RULES}\n'


def build_translation_prompt(text: str, entries: dict[str, str], settings: dict[str, Any]) -> str:
    configured_source, configured_target = _glossary_language_pair(settings)
    source_lang, target_lang = _smart_language_detect(text, configured_source, configured_target)
    glossary_block = _build_glossary_block(text, entries, settings, source_lang, target_lang)

    return (
        f'[命令：请将下面的【原文】翻译成{target_lang}，只要翻译结果，不要语言对照。'
        f'如原文有错别字，请结合上下文自动纠正并通顺地翻译要保证易读性。]\n'
        f'{_strict_target_language_rule(source_lang, target_lang)}\n'
        f'{glossary_block}\n'
        f'{_translation_name_rules_for_target(target_lang)}\n'
        f'【原文】\n{text}'
    )


def _manuscript_bold_rule(entries: dict[str, str]) -> str:
    """A forceful, concrete instruction to bold glossary terms in the output.

    Smaller models routinely ignore a soft "please bold" line, so this states
    the requirement as mandatory formatting (part of the translation itself),
    shows the literal ** ** form, and includes a concrete example taken from
    the active glossary so the model has the exact target string to emit.
    """
    example = ''
    if entries:
        sample_src, sample_tgt = next(iter(entries.items()))
        sample_tgt = re.split(r'[\[/]', str(sample_tgt))[0].strip() or str(sample_tgt).strip()
        if sample_src and sample_tgt:
            example = (
                f'例如词典中「{sample_src} -> {sample_tgt}」，译文里就必须写成 **{sample_tgt}**（连同两侧的两个星号）。'
            )
    return (
        '【强制格式 · 必须执行，不可省略】译文中只要出现上方词典里的术语，'
        '就必须把它的目标语言译文用 Markdown 粗体包起来，写成 **译文** 这种带两个星号的形式。'
        f'{example}'
        '加粗是译文本身的一部分，不是额外说明或注释。'
        '只加粗命中词典的术语本身，不要加粗整句、未命中词典的普通词语或解释文字。\n'
    )


def build_manuscript_translation_prompt(text: str, entries: dict[str, str], settings: dict[str, Any]) -> str:
    configured_source, configured_target = _glossary_language_pair(settings)
    source_lang, target_lang = _smart_language_detect(text, configured_source, configured_target)
    glossary_block = _build_glossary_block(text, entries, settings, source_lang, target_lang)

    glossary_bold_rule = _manuscript_bold_rule(entries) if glossary_block else ''
    bold_reminder = (
        '（再次提醒：译文中所有命中词典的术语都必须用 **粗体** 标记，不能漏掉。）\n' if glossary_block else ''
    )

    return (
        f'[命令：请将下面的【文稿原文】完整翻译成{target_lang}，只输出译文，不要语言对照、说明或总结。'
        f'请尽量保留原文段落、换行、列表和标题层级。'
        f'如原文有错别字、缺少标点或表达不连贯，请结合上下文自动修正并通顺翻译，保证适合正式文稿阅读。]\n'
        f'{_strict_target_language_rule(source_lang, target_lang)}\n'
        f'{glossary_block}\n'
        f'{glossary_bold_rule}'
        f'{_translation_name_rules_for_target(target_lang)}\n'
        f'{bold_reminder}'
        f'【文稿原文】\n{text}'
    )


def build_interpretation_prompt(text: str, entries: dict[str, str], settings: dict[str, Any]) -> str:
    configured_source, configured_target = _glossary_language_pair(settings)
    source_lang, target_lang = _smart_language_detect(text, configured_source, configured_target)
    glossary_block = _build_glossary_block(text, entries, settings, source_lang, target_lang)

    return (
        f'你擅长在{configured_source}和{configured_target}之间做快速同声传译。\n'
        f'[强制命令：请将接下来的输入极速同传翻译或解析为【{target_lang}】，绝对不要输出其他内容。]\n'
        f'如果输入是单词，请给出最实用的{target_lang}含义；如果输入是句子，请直接翻译。速度要快，表达要自然。\n'
        f'{_strict_target_language_rule(source_lang, target_lang)}\n'
        f'{glossary_block}\n'
        f'{_translation_name_rules_for_target(target_lang)}\n'
        f'【输入内容】\n{text}'
    )


def build_learning_prompt(text: str, entries: dict[str, str], settings: dict[str, Any]) -> str:
    configured_source, configured_target = _glossary_language_pair(settings)
    source_lang, target_lang = _smart_language_detect(text, configured_source, configured_target)
    glossary_block = _build_glossary_block(text, entries, settings, source_lang, target_lang)

    return (
        f'你是一位专业的语言教师，擅长{configured_source}和{configured_target}。\n'
        f'[强制命令：当前输入主要是【{source_lang}】，请围绕【{target_lang}】给出学习解析。]\n'
        f'如果输入是单词，请给出含义、词性、常见用法和例句；如果输入是句子，请解释意思、语法、关键单词或短语。'
        f'不要寒暄，不要确认收到，直接进入解析。\n'
        f'{_strict_target_language_rule(source_lang, target_lang)}\n'
        f'{glossary_block}\n'
        f'【输入内容】\n{text}'
    )


def _audio_user_note(user_text: str) -> str:
    return f'\n【用户补充说明】\n{user_text.strip()}\n' if user_text.strip() else ''


def _build_glossary_block_for_audio(user_text: str, entries: dict[str, str], settings: dict[str, Any]) -> str:
    """Glossary block for voice input where the spoken content is unknown.

    When the user typed a caption alongside the audio, match terms against it.
    Otherwise inject the whole glossary if it is small enough — we cannot
    pre-filter terms against speech we have not heard yet.
    """
    max_terms = int(settings.get('max_terms_injected') or 10)
    configured_source, configured_target = _glossary_language_pair(settings)

    if user_text.strip():
        block = _build_glossary_block(user_text, entries, settings, configured_source, configured_target)
        if block:
            return block

    if not entries or len(entries) > max_terms:
        return ''

    term_lines = '\n'.join(f'  {source} -> {target}' for source, target in entries.items())
    return (
        f'\n词典（{configured_source} -> {configured_target}，双向适用）：\n'
        f'{term_lines}\n'
        '若语音中出现词典词汇，请优先使用词典译法。'
        '部分词条包含多个候选译文，用 [ ] 标注并以 / 分隔，请按语境选择最自然的一个。\n'
    )


def _audio_bilingual_rule(configured_source: str, configured_target: str) -> str:
    """Output BOTH languages instead of guessing the translation direction.

    Multimodal audio models are unreliable at deciding which way to translate
    (they frequently just echo the spoken language). Emitting both the
    source-language and target-language versions side by side sidesteps the
    problem: whichever language the user wanted is always present, and the
    result doubles as a bilingual reference.
    """
    return (
        f'请听懂语音内容，然后同时给出{configured_source}和{configured_target}两个版本，做双语对照。\n'
        f'严格按下面两行的格式输出，每种语言一行，不要有任何多余内容、解释或前后缀：\n'
        f'{configured_source}：（这里写{configured_source}版本）\n'
        f'{configured_target}：（这里写{configured_target}版本）\n'
    )


def build_translation_audio_prompt(user_text: str, entries: dict[str, str], settings: dict[str, Any]) -> str:
    configured_source, configured_target = _glossary_language_pair(settings)
    glossary_block = _build_glossary_block_for_audio(user_text, entries, settings)

    return (
        f'[命令：附件音频是一段语音发言，语言是{configured_source}或{configured_target}其中之一。]\n'
        f'{_audio_bilingual_rule(configured_source, configured_target)}'
        '语音如有口误、重复、缺标点或不连贯，请结合上下文自动修正，保证两种语言都通顺易读。\n'
        f'{glossary_block}'
        f'{_audio_user_note(user_text)}'
    )


def build_manuscript_translation_audio_prompt(user_text: str, entries: dict[str, str], settings: dict[str, Any]) -> str:
    configured_source, configured_target = _glossary_language_pair(settings)
    glossary_block = _build_glossary_block_for_audio(user_text, entries, settings)

    glossary_bold_rule = _manuscript_bold_rule(entries) if glossary_block else ''

    return (
        f'[命令：附件音频是一段需要整理成文稿译文的语音，语言是{configured_source}或{configured_target}其中之一。\n'
        f'{_audio_bilingual_rule(configured_source, configured_target)}'
        '请让输出适合正式文稿阅读，自动修正口误、重复、缺标点或不连贯表达。\n'
        f'{glossary_block}'
        f'{glossary_bold_rule}'
        f'{_audio_user_note(user_text)}'
    )


def build_interpretation_audio_prompt(user_text: str, entries: dict[str, str], settings: dict[str, Any]) -> str:
    configured_source, configured_target = _glossary_language_pair(settings)
    glossary_block = _build_glossary_block_for_audio(user_text, entries, settings)

    return (
        f'你擅长在{configured_source}和{configured_target}之间做快速同声传译。\n'
        f'[强制命令：附件音频是用户的语音输入，语言是{configured_source}或{configured_target}其中之一。]\n'
        f'{_audio_bilingual_rule(configured_source, configured_target)}'
        '速度要快，表达要自然。如果语音是单词，对照行给出最实用的含义。\n'
        f'{glossary_block}'
        f'{_audio_user_note(user_text)}'
    )


def build_learning_audio_prompt(user_text: str, entries: dict[str, str], settings: dict[str, Any]) -> str:
    configured_source, configured_target = _glossary_language_pair(settings)
    glossary_block = _build_glossary_block_for_audio(user_text, entries, settings)

    return (
        f'你是一位专业的语言教师，擅长{configured_source}和{configured_target}。\n'
        '附件音频是用户的语音输入。请先简要写出听到的语音内容，再给出学习解析：'
        f'若语音是{configured_source}，围绕学习{configured_target}来讲解；'
        f'若语音是{configured_target}，用{configured_source}讲解。'
        '不要寒暄，不要确认收到，直接进入内容。\n'
        f'{glossary_block}'
        f'{_audio_user_note(user_text)}'
    )


async def build_rag_translation_prompt(request, text: str, settings: dict[str, Any], user) -> tuple[str, list]:
    configured_source, configured_target = _glossary_language_pair(settings)
    source_lang, target_lang = _smart_language_detect(text, configured_source, configured_target)
    glossary_block, sources, is_matched = await _build_bilingual_block(request, text, settings, source_lang, target_lang, user)


    if is_matched:
        prompt = (
            f'命令：请将下面的【原文】翻译成{target_lang}，只要翻译结果，不要语言对照。\n'
            f'以下是翻译记忆库中已确认的句子对照（{source_lang} -> {target_lang})\n\n'
            '\n【部分参考】以下内容仅部分与原文重叠，整句对照不一定完全适用。'
            '请自行判断原文中具体是哪一部分内容与下列原文重叠，'
            '只参考重叠部分对应的译文片段，不要把整条译文不加甄别地套用到原文的其他部分\n'
            f'{_strict_target_language_rule(source_lang, target_lang)}\n\n'
            f'{glossary_block}\n\n'
            f'{_translation_name_rules_for_target(target_lang)}\n\n'
            f'【原文】\n{text}'
        )
        return prompt, sources
    else:
        prompt = (
            f'[命令：请将下面的【原文】翻译成{target_lang}，只要翻译结果，不要语言对照。'
            f'如原文有错别字，请结合上下文自动纠正并通顺地翻译要保证易读性。]\n'
            f'{_strict_target_language_rule(source_lang, target_lang)}\n'
            f'{glossary_block}\n'
            f'{_translation_name_rules_for_target(target_lang)}\n'
            f'【原文】\n{text}'
        )
        return prompt, sources


def _count_text_tokens(text: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding('o200k_base').encode(text))
    except Exception:
        return max(1, len(text) // 3)


def _message_token_count(message: dict[str, Any]) -> int:
    content = message.get('content', '')
    if isinstance(content, str):
        return _count_text_tokens(content)
    if isinstance(content, list):
        return sum(_count_text_tokens(str(item.get('text', ''))) for item in content if item.get('type') == 'text')
    return 0


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _context_token_budget(token_limit: int) -> int:
    if token_limit <= 0:
        return 0
    reserve = min(max(1024, token_limit // 10), max(0, token_limit - 512))
    return max(1, token_limit - reserve)


def _split_system_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system_messages = [message for message in messages if message.get('role') == 'system']
    body_messages = [message for message in messages if message.get('role') != 'system']
    return system_messages, body_messages


def _apply_turn_limit(messages: list[dict[str, Any]], max_turns: int) -> list[dict[str, Any]]:
    if max_turns <= 0:
        return messages

    system_messages, body_messages = _split_system_messages(messages)
    body_limit = max_turns * 2 + 1
    if len(body_messages) > body_limit:
        body_messages = body_messages[-body_limit:]

    return [*system_messages, *body_messages]


def _trim_text_to_token_budget(text: str, token_budget: int) -> str:
    if token_budget <= 0 or _count_text_tokens(text) <= token_budget:
        return text

    notice = '[Earlier content was automatically omitted to fit the model context.]\n'
    notice_tokens = _count_text_tokens(notice)
    body_budget = max(1, token_budget - notice_tokens)
    low = 0
    high = len(text)
    best = ''

    while low <= high:
        mid = (low + high) // 2
        candidate = text[-mid:] if mid > 0 else ''
        if _count_text_tokens(candidate) <= body_budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1

    return f'{notice}{best.lstrip()}'


def _trim_message_to_token_budget(message: dict[str, Any], token_budget: int) -> dict[str, Any]:
    if token_budget <= 0 or _message_token_count(message) <= token_budget:
        return message

    trimmed = dict(message)
    content = trimmed.get('content', '')
    if isinstance(content, str):
        trimmed['content'] = _trim_text_to_token_budget(content, token_budget)
    elif isinstance(content, list):
        text_items = [
            item
            for item in content
            if isinstance(item, dict) and item.get('type') == 'text' and str(item.get('text') or '').strip()
        ]
        if text_items:
            last_text_item = text_items[-1]
            new_content = []
            for item in content:
                if item is last_text_item:
                    copied = dict(item)
                    copied['text'] = _trim_text_to_token_budget(str(item.get('text') or ''), token_budget)
                    new_content.append(copied)
                elif isinstance(item, dict) and item.get('type') == 'text':
                    continue
                else:
                    new_content.append(item)
            trimmed['content'] = new_content

    return trimmed


def _apply_token_limit(messages: list[dict[str, Any]], token_limit: int) -> list[dict[str, Any]]:
    budget = _context_token_budget(token_limit)
    if budget <= 0:
        return messages

    system_messages, body_messages = _split_system_messages(messages)
    kept_reversed: list[dict[str, Any]] = []
    used_tokens = 0

    for message in reversed(body_messages):
        tokens = _message_token_count(message)
        if kept_reversed and used_tokens + tokens > budget:
            break
        if not kept_reversed and tokens > budget:
            trimmed_message = _trim_message_to_token_budget(message, budget)
            kept_reversed.append(trimmed_message)
            used_tokens += _message_token_count(trimmed_message)
            break
        kept_reversed.append(message)
        used_tokens += tokens

    kept_body = list(reversed(kept_reversed))
    remaining_tokens = budget - used_tokens
    kept_system: list[dict[str, Any]] = []
    for message in system_messages:
        tokens = _message_token_count(message)
        if tokens <= remaining_tokens:
            kept_system.append(message)
            remaining_tokens -= tokens

    return [*kept_system, *kept_body]


def _truncate_messages(
    messages: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    use_max_turns: bool = True,
    token_limit_override: Optional[int] = None,
) -> list[dict[str, Any]]:
    configured_max_turns = _safe_int(settings.get('max_turns'), DEFAULT_SETTINGS['max_turns'])
    max_turns = min(configured_max_turns, 5) if configured_max_turns > 0 else 3
    token_limit = token_limit_override or _safe_int(settings.get('token_limit'), DEFAULT_SETTINGS['token_limit'])
    result = messages

    if use_max_turns:
        result = _apply_turn_limit(result, max_turns)

    result = _apply_token_limit(result, token_limit)

    return result


def _context_limit_from_form_data(form_data: dict[str, Any], settings: dict[str, Any]) -> int:
    configured_limit = _safe_int(settings.get('token_limit'), DEFAULT_SETTINGS['token_limit'])
    candidates: list[int] = []

    for key in ('num_ctx', 'n_ctx', 'ctx_size', 'context_size', 'max_context', 'max_context_size'):
        value = form_data.get(key)
        parsed = _safe_int(value, 0)
        if parsed > 0:
            candidates.append(parsed)

    options = form_data.get('options')
    if isinstance(options, dict):
        for key in ('num_ctx', 'n_ctx', 'ctx_size', 'context_size'):
            parsed = _safe_int(options.get(key), 0)
            if parsed > 0:
                candidates.append(parsed)

    # If the provider did not expose its context size, use a conservative local
    # default. This prevents a 16k WebUI/glossary setting from overfeeding an
    # 8k llama.cpp model and making even fresh chats fail.
    provider_limit = min(candidates) if candidates else 8192
    return min(configured_limit, provider_limit) if configured_limit > 0 else provider_limit


async def apply_context_cleanup(form_data: dict[str, Any]) -> dict[str, Any]:
    settings = await read_settings()
    messages = form_data.get('messages') or []
    form_data['messages'] = _truncate_messages(
        messages,
        settings,
        use_max_turns=False,
        token_limit_override=_context_limit_from_form_data(form_data, settings),
    )
    return form_data


async def apply_rag_translation_mode(request, form_data: dict[str, Any], user) -> tuple[dict[str, Any], Any]:
    settings = await read_settings()
    messages = form_data.get('messages') or []
    message, part_index, text = _latest_user_text_ref(messages)
    if message and text.strip():
        prompt, sources = await build_rag_translation_prompt(request, text, settings, user)
        _set_message_text(message, prompt, part_index)

    form_data['messages'] = _truncate_messages(messages, settings)
    return form_data, sources


async def apply_translation_mode(form_data: dict[str, Any]) -> dict[str, Any]:
    settings = await read_settings()
    entries, _updated_at = await read_entries(settings)
    messages = form_data.get('messages') or []
    message, part_index, text = _latest_user_text_ref(messages)
    if _message_has_audio(message):
        # Voice input: the spoken content is what needs translating; any typed
        # text rides along as a supplementary note.
        _set_message_text(message, build_translation_audio_prompt(text, entries, settings), part_index)
    elif message and text.strip():
        _set_message_text(message, build_translation_prompt(text, entries, settings), part_index)

    form_data['messages'] = _truncate_messages(messages, settings)
    return form_data


async def apply_manuscript_translation_mode(form_data: dict[str, Any]) -> dict[str, Any]:
    settings = await read_settings()
    entries, _updated_at = await read_entries(settings)
    messages = form_data.get('messages') or []
    message, part_index, text = _latest_user_text_ref(messages)
    if _message_has_audio(message):
        _set_message_text(message, build_manuscript_translation_audio_prompt(text, entries, settings), part_index)
    elif message and text.strip():
        _set_message_text(message, build_manuscript_translation_prompt(text, entries, settings), part_index)

    form_data['messages'] = _truncate_messages(messages, settings)
    return form_data


async def apply_interpretation_mode(form_data: dict[str, Any]) -> dict[str, Any]:
    settings = await read_settings()
    entries, _updated_at = await read_entries(settings)
    messages = form_data.get('messages') or []
    message, part_index, text = _latest_user_text_ref(messages)
    if _message_has_audio(message):
        _set_message_text(message, build_interpretation_audio_prompt(text, entries, settings), part_index)
    elif message and text.strip():
        _set_message_text(message, build_interpretation_prompt(text, entries, settings), part_index)

    form_data['messages'] = _truncate_messages(messages, settings)
    return form_data


async def apply_learning_mode(form_data: dict[str, Any]) -> dict[str, Any]:
    settings = await read_settings()
    entries, _updated_at = await read_entries(settings)
    messages = form_data.get('messages') or []
    message, part_index, text = _latest_user_text_ref(messages)
    if _message_has_audio(message):
        _set_message_text(message, build_learning_audio_prompt(text, entries, settings), part_index)
    elif message and text.strip():
        _set_message_text(message, build_learning_prompt(text, entries, settings), part_index)

    return form_data
