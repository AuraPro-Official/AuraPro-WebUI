"""Top-level pure EPUB parser, format version 1."""

from __future__ import annotations

import os
from pathlib import Path
import re
import zipfile

from .archive import ArchiveLimits, EpubArchiveError, validate_archive
from .model import EpubParseResult, ParsedPassage, ParserWarning, TocEntry
from .package import EpubPackageError, parse_container, parse_opf, resolve_href
from .toc import parse_nav, parse_ncx
from .toc_mapping import toc_path_for_unit
from .xhtml import extract_xhtml_text

PARSER_FORMAT_VERSION = 1


class EPUBParser:
    """Safe, persistence-free parser for EPUB visible reading text."""

    def __init__(self, file_path: str | os.PathLike[str], *, archive_limits: ArchiveLimits | None = None) -> None:
        self.file_path = Path(file_path)
        self.archive_limits = archive_limits or ArchiveLimits()

    def parse_book(self) -> EpubParseResult:
        if not zipfile.is_zipfile(self.file_path):
            raise EpubArchiveError(f'not a valid EPUB ZIP archive: {self.file_path}')
        with zipfile.ZipFile(self.file_path) as archive:
            validate_archive(archive, self.archive_limits)
            names = set(archive.namelist())
            if 'META-INF/container.xml' not in names:
                raise EpubPackageError('EPUB is missing META-INF/container.xml')
            opf_path = parse_container(archive.read('META-INF/container.xml'))
            if opf_path not in names:
                raise EpubPackageError(f'EPUB OPF rootfile is missing: {opf_path}')
            package = parse_opf(archive.read(opf_path))
            title = (package.title or self.file_path.stem).strip() or self.file_path.stem
            warnings: list[ParserWarning] = []
            nav_entries = self._nav(archive, names, opf_path, package.nav_item, title, warnings)
            ncx_entries = self._ncx(archive, names, opf_path, package.ncx_item, title, warnings)
            toc_entries = nav_entries or ncx_entries
            if nav_entries and ncx_entries and _signature(nav_entries) != _signature(ncx_entries):
                warnings.append(
                    ParserWarning('nav_ncx_disagreement', 'EPUB NAV takes precedence over differing NCX entries')
                )
            if not toc_entries:
                warnings.append(ParserWarning('toc_missing', 'EPUB has no usable NAV or NCX table of contents'))

            passages: list[ParsedPassage] = []
            for spine_index, item_id in enumerate(package.spine):
                item = package.manifest.get(item_id)
                if item is None:
                    warnings.append(
                        ParserWarning('spine_item_missing', f'spine references absent manifest item {item_id!r}')
                    )
                    continue
                path, _ = resolve_href(opf_path, item.href)
                if path not in names:
                    warnings.append(ParserWarning('spine_resource_missing', 'spine resource is missing', path))
                    continue
                extraction = extract_xhtml_text(_decode(archive.read(path), path, warnings), path)
                warnings.extend(extraction.warnings)
                for unit in extraction.units:
                    passages.append(
                        ParsedPassage(
                            ordinal=len(passages),
                            content=unit.content,
                            content_kind=unit.content_kind,
                            toc_path=toc_path_for_unit(path, unit, extraction.anchors, toc_entries),
                            source_path=path,
                            source_fragment=unit.source_fragment,
                            spine_index=spine_index,
                        )
                    )
        return EpubParseResult(
            PARSER_FORMAT_VERSION, title, opf_path, tuple(passages), tuple(toc_entries), tuple(warnings)
        )

    def parse(self) -> dict[str, object]:
        """Temporary compatibility adapter for the legacy loader API."""
        result = self.parse_book()
        passages = [
            {
                'passage_id': f'{result.book_title}_P{passage.ordinal:05d}',
                'book_title': result.book_title,
                'toc_path': list(passage.toc_path),
                'content': passage.content,
                'content_kind': passage.content_kind,
                'source_path': passage.source_path,
                'source_fragment': passage.source_fragment,
                'parent_context': f'[{" > ".join(passage.toc_path)}] {passage.content}',
            }
            for passage in result.passages
        ]
        return {
            'book_title': result.book_title,
            'passages': passages,
            'total_passages': len(passages),
            'warnings': [{'code': item.code, 'message': item.message, 'path': item.path} for item in result.warnings],
            'parser_format_version': result.format_version,
        }

    @staticmethod
    def _nav(archive, names, opf_path, item, title, warnings):
        if item is None:
            return ()
        path, _ = resolve_href(opf_path, item.href)
        if path not in names:
            warnings.append(ParserWarning('nav_missing', 'NAV manifest resource is missing', path))
            return ()
        entries = parse_nav(_decode(archive.read(path), path, warnings), path, title)
        if not entries:
            warnings.append(ParserWarning('nav_unusable', 'NAV contains no usable toc links', path))
        return entries

    @staticmethod
    def _ncx(archive, names, opf_path, item, title, warnings):
        if item is None:
            return ()
        path, _ = resolve_href(opf_path, item.href)
        if path not in names:
            warnings.append(ParserWarning('ncx_missing', 'NCX manifest resource is missing', path))
            return ()
        return parse_ncx(archive.read(path), path, title)


def parse_epub(file_path: str | os.PathLike[str], *, archive_limits: ArchiveLimits | None = None) -> EpubParseResult:
    return EPUBParser(file_path, archive_limits=archive_limits).parse_book()


def _decode(data: bytes, path: str, warnings: list[ParserWarning]) -> str:
    match = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([^\"']+)[\"']", data[:256], re.I)
    if match is None:
        match = re.search(rb"<meta[^>]+charset=[\"']?([^\"'\s/>]+)", data[:2048], re.I)
    encoding = match.group(1).decode('ascii', 'replace') if match else 'utf-8'
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        warnings.append(
            ParserWarning(
                'xhtml_decode_recovered', f'could not decode declared {encoding!r}; used UTF-8 replacement', path
            )
        )
        return data.decode('utf-8', errors='replace')


def _signature(entries: tuple[TocEntry, ...]) -> tuple[tuple[str, str | None, tuple[str, ...]], ...]:
    return tuple((item.href, item.fragment, item.path) for item in entries)
