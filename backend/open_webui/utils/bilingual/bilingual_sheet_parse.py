import csv
import io
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_DOC_ID_RE = re.compile(r'/document/d/([a-zA-Z0-9_-]+)')
_SHEET_ID_RE = re.compile(r'/spreadsheets/d/([a-zA-Z0-9_-]+)')
_GID_RE = re.compile(r'[?&#]gid=(\d+)')

_LOGIN_WALL_MARKERS = (
    'accounts.google.com/ServiceLogin',
    'accounts.google.com/signin',
    '<title>Sign in',
)

# 语言对表头格式: 源语言-目标语言，如 zh-ja / zh-en / en-fr
_LANG_PAIR_RE = re.compile(r'^([a-zA-Z]+)-([a-zA-Z]+)$')

_NAME_COL_KEYS = {'name', 'title', 'basename', '标题', '名称', '章节'}


class GoogleBilingualImportError(Exception):
    """本模块所有可预期错误的基类，携带用户可读的中文提示。"""


def extract_doc_id(url: str) -> str:
    m = _DOC_ID_RE.search(url)
    if not m:
        raise GoogleBilingualImportError(f'无法从链接中解析出 Google 文档 ID: {url}')
    return m.group(1)


def extract_sheet_id(url: str) -> str:
    m = _SHEET_ID_RE.search(url)
    if not m:
        raise GoogleBilingualImportError(f'无法从链接中解析出 Google 表格 ID: {url}')
    return m.group(1)


def extract_gid(url: str) -> Optional[str]:
    m = _GID_RE.search(url)
    return m.group(1) if m else None


def parse_lang_pair(header_cell: str) -> tuple[str, str]:
    """把表头单元格（如 'zh-ja'）解析成 (源语言, 目标语言)。"""
    cell = header_cell.strip().lower()
    m = _LANG_PAIR_RE.match(cell)
    if not m:
        raise GoogleBilingualImportError(f"表头单元格 '{header_cell}' 不是合法的语言对格式（应形如 zh-ja / zh-en）。")
    return m.group(1), m.group(2)


def _looks_like_login_wall(text: str) -> bool:
    head = text[:2000]
    return any(marker in head for marker in _LOGIN_WALL_MARKERS)


@dataclass
class ParagraphRow:
    index: int
    langs: dict[str, str] = field(default_factory=dict)  # lang -> 该段落在该语言下的文本

    def to_dict(self) -> dict:
        return {'index': self.index, 'langs': self.langs}


@dataclass
class BilingualFileResult:
    id: str
    baseName: str
    languages: list[str] = field(default_factory=list)
    paragraphs: list[ParagraphRow] = field(default_factory=list)
    primaryLang: Optional[str] = None
    primaryText: str = ''  # 主语言全文（各段落用 \n\n 拼接），供整体 embedding 使用
    docLinks: dict[str, str] = field(default_factory=dict)  # "zh-ja" -> 文档链接，便于追溯
    errors: dict[str, str] = field(default_factory=dict)  # "zh-ja" -> 抓取/解析失败原因

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'baseName': self.baseName,
            'languages': self.languages,
            'paragraphs': [p.to_dict() for p in self.paragraphs],
            'primaryLang': self.primaryLang,
            'primaryText': self.primaryText,
            'docLinks': self.docLinks,
            'errors': self.errors,
        }


@dataclass
class BilingualImportResult:
    files: list[BilingualFileResult]
    primaryLang: str
    languages: list[str]
    totalFiles: int
    rowErrors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'files': [f.to_dict() for f in self.files],
            'primaryLang': self.primaryLang,
            'languages': self.languages,
            'totalFiles': self.totalFiles,
            'rowErrors': self.rowErrors,
        }


_TIMEOUT = aiohttp.ClientTimeout(total=30)
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
    )
}


async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True) as resp:
        text = await resp.text(errors='ignore')
        if resp.status != 200:
            snippet = text[:300].replace('\n', ' ').strip()
            raise GoogleBilingualImportError(
                f'请求失败（HTTP {resp.status}）: {url}' + (f'\n响应内容片段: {snippet}' if snippet else '')
            )
        if _looks_like_login_wall(text):
            raise GoogleBilingualImportError(
                '该文档/表格未公开分享（需要登录），请将分享权限设置为“知道链接的任何人可查看”后重试。'
            )
        return text


async def fetch_sheet_csv(session: aiohttp.ClientSession, sheet_url: str) -> str:
    sheet_id = extract_sheet_id(sheet_url)
    gid = extract_gid(sheet_url) or '0'
    export_url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}'
    return await _fetch_text(session, export_url)


async def fetch_sheet_html(session: aiohttp.ClientSession, sheet_url: str) -> str:
    sheet_id = extract_sheet_id(sheet_url)
    gid = extract_gid(sheet_url) or '0'
    export_url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:html&gid={gid}'
    return await _fetch_text(session, export_url)


async def fetch_doc_html(session: aiohttp.ClientSession, doc_url: str) -> str:
    doc_id = extract_doc_id(doc_url)
    export_url = f'https://docs.google.com/document/d/{doc_id}/export?format=html'
    return await _fetch_text(session, export_url)


def parse_doc_table_pairs(html: str, skip_first_table_row: bool = False) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, 'html.parser')
    tables = soup.find_all('table')
    if not tables:
        raise GoogleBilingualImportError('文档中没有找到表格，无法提取原文/译文对照内容。')

    pairs: list[tuple[str, str]] = []
    for t_idx, table in enumerate(tables):
        rows = table.find_all('tr')
        for r_idx, tr in enumerate(rows):
            cells = tr.find_all(['td', 'th'])
            if len(cells) < 2:
                continue
            if t_idx == 0 and r_idx == 0 and skip_first_table_row:
                continue
            source_text = cells[0].get_text('\n').strip()
            target_text = cells[1].get_text('\n').strip()
            if not source_text and not target_text:
                continue
            pairs.append((source_text, target_text))

    if not pairs:
        raise GoogleBilingualImportError('表格中没有解析到任何有效的原文/译文段落。')

    return pairs


def _unwrap_google_redirect(href: str) -> str:
    if 'google.com/url' not in href:
        return href
    try:
        from urllib.parse import urlparse, parse_qs, unquote

        query = parse_qs(urlparse(href).query)
        real = query.get('q', [None])[0]
        return unquote(real) if real else href
    except Exception:
        return href


def _cell_display_text(cell) -> str:
    return cell.get_text(' ', strip=True)


def _cell_link_or_text(cell) -> str:
    a = cell.find('a')
    if a and a.get('href'):
        return _unwrap_google_redirect(a['href'].strip())
    return _cell_display_text(cell)


def parse_sheet_html(html: str) -> tuple[list[tuple[str, str, int]], list[dict[str, str]]]:
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table')
    if not table:
        raise GoogleBilingualImportError('未能读取到表格内容，请确认该 Google Sheet 已设置为“知道链接的任何人可查看”。')

    trs = table.find_all('tr')
    if not trs:
        raise GoogleBilingualImportError('表格内容为空。')

    header_index = 1
    header_cells = trs[header_index].find_all(['td', 'th'])
    header = [_cell_display_text(c) for c in header_cells]
    if not header or all(not h for h in header):
        raise GoogleBilingualImportError('表格第一行（表头）为空，无法确定语言对列。')

    lang_pair_columns: list[tuple[str, str, int]] = []
    col_key_map: dict[int, str] = {}

    for idx, col_name in enumerate(header):
        if not col_name:
            continue
        lower = col_name.lower()
        if lower in _NAME_COL_KEYS:
            col_key_map[idx] = 'name'
            continue
        src, tgt = parse_lang_pair(col_name)  # 若格式不对会在此抛错，提示具体是哪个表头单元格
        lang_pair_columns.append((src, tgt, idx))
        col_key_map[idx] = col_name  # 用原始表头文本（如 "zh-ja"）作为 key，便于回显/追溯

    if not lang_pair_columns:
        raise GoogleBilingualImportError("未能从表头中识别出任何 '源语言-目标语言' 格式的列，例如 zh-ja、zh-en。")

    rows: list[dict[str, str]] = []
    for tr in trs[header_index + 1 :]:
        cells = tr.find_all(['td', 'th'])
        if not cells or all(not _cell_display_text(c) for c in cells):
            continue
        row_dict: dict[str, str] = {}
        for idx, key in col_key_map.items():
            if idx >= len(cells):
                continue
            if key == 'name':
                value = _cell_display_text(cells[idx])
            else:
                value = _cell_link_or_text(cells[idx])
            if value:
                row_dict[key] = value
        rows.append(row_dict)

    return lang_pair_columns, rows


async def import_bilingual_from_google_sheet(
    sheet_url: str,
    primary_lang: Optional[str] = None,
    max_concurrent_fetches: int = 8,
    skip_first_table_row: bool = False,
) -> BilingualImportResult:
    import asyncio

    async with aiohttp.ClientSession() as session:
        sheet_html = await fetch_sheet_html(session, sheet_url)
        lang_pair_columns, rows = parse_sheet_html(sheet_html)

        all_languages: list[str] = []
        for src, tgt, _ in lang_pair_columns:
            for lang in (src, tgt):
                if lang not in all_languages:
                    all_languages.append(lang)

        resolved_primary = (primary_lang or lang_pair_columns[0][0]).lower()
        if resolved_primary not in all_languages:
            raise GoogleBilingualImportError(
                f"指定的主语言 '{resolved_primary}' 不在表格检测到的语言 {all_languages} 中。"
            )

        semaphore = asyncio.Semaphore(max_concurrent_fetches)
        row_errors: list[dict] = []
        results: list[BilingualFileResult] = []

        async def fetch_one_doc(header_key: str, src: str, tgt: str, link: str):
            """抓取单个语言对文档，返回 (header_key, src, tgt, pairs 或 None, error 或 None)"""
            async with semaphore:
                try:
                    html = await fetch_doc_html(session, link)
                    pairs = parse_doc_table_pairs(html, skip_first_table_row=skip_first_table_row)
                    return header_key, src, tgt, pairs, None
                except GoogleBilingualImportError as e:
                    return header_key, src, tgt, None, str(e)
                except Exception as e:  # noqa: BLE001
                    log.exception('抓取/解析 Google 文档表格失败: url=%s', link)
                    return header_key, src, tgt, None, f'抓取失败: {e}'

        for row_idx, row in enumerate(rows):
            base_name = row.get('name') or f'row_{row_idx + 1}'
            file_result = BilingualFileResult(
                id=f'{base_name}__gdoc_{row_idx}',
                baseName=base_name,
                languages=list(all_languages),
                primaryLang=resolved_primary,
            )

            tasks = []
            for src, tgt, col_idx in lang_pair_columns:
                header_key = None
                for k in row.keys():
                    if k.lower() == f'{src}-{tgt}':
                        header_key = k
                        break
                if header_key is None:
                    continue
                link = row.get(header_key)
                if not link or not link.startswith('https://'):
                    continue
                file_result.docLinks[header_key] = link
                tasks.append(fetch_one_doc(header_key, src, tgt, link))

            if not tasks:
                row_errors.append({'row': row_idx + 1, 'baseName': base_name, 'reason': '该行没有任何有效的文档链接'})
                results.append(file_result)
                continue

            fetched = await asyncio.gather(*tasks)

            # 用段落索引对齐合并：不同语言对文档假设行序对应同一份原文的切分
            merged: dict[int, dict[str, str]] = {}
            max_len_seen = 0
            length_mismatch = False
            first_len = None

            for header_key, src, tgt, pairs, error in fetched:
                if error is not None:
                    file_result.errors[header_key] = error
                    continue
                if first_len is None:
                    first_len = len(pairs)
                elif len(pairs) != first_len:
                    length_mismatch = True
                max_len_seen = max(max_len_seen, len(pairs))
                for p_idx, (source_text, target_text) in enumerate(pairs):
                    bucket = merged.setdefault(p_idx, {})
                    # 同一份原文可能被多个语言对列重复提供（如 zh-ja 和 zh-en 的左列都是 zh），
                    # 以先到者为准，不覆盖，避免不同文档间原文有细微差异时互相覆盖。
                    bucket.setdefault(src, source_text)
                    bucket[tgt] = target_text

            if length_mismatch:
                row_errors.append(
                    {
                        'row': row_idx + 1,
                        'baseName': base_name,
                        'reason': f'该行不同语言对文档的段落数不一致（已按各自行序对齐，最长 {max_len_seen} 段），请检查是否有漏译或拆分不一致的段落。',
                    }
                )

            for p_idx in sorted(merged.keys()):
                file_result.paragraphs.append(ParagraphRow(index=p_idx, langs=merged[p_idx]))

            primary_paragraphs = [p.langs.get(resolved_primary, '') for p in file_result.paragraphs]
            file_result.primaryText = '\n\n'.join(t for t in primary_paragraphs if t)

            if not file_result.primaryText:
                row_errors.append(
                    {'row': row_idx + 1, 'baseName': base_name, 'reason': '主语言在该行所有文档中均未抓取到内容'}
                )

            results.append(file_result)

        return BilingualImportResult(
            files=results,
            primaryLang=resolved_primary,
            languages=all_languages,
            totalFiles=len(results),
            rowErrors=row_errors,
        )
