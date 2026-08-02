"""Typed output contract for the pure EPUB parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ContentKind = Literal["paragraph", "heading", "list_item", "blockquote", "pre", "fallback"]


@dataclass(frozen=True, slots=True)
class ParserWarning:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class TocEntry:
    href: str
    fragment: str | None
    path: tuple[str, ...]
    source: Literal["nav", "ncx"]


@dataclass(frozen=True, slots=True)
class ParsedPassage:
    """One immutable, non-overlapping visible EPUB text unit."""

    ordinal: int
    content: str
    content_kind: ContentKind
    toc_path: tuple[str, ...]
    source_path: str
    source_fragment: str | None
    spine_index: int


@dataclass(frozen=True, slots=True)
class EpubParseResult:
    format_version: int
    book_title: str
    opf_path: str
    passages: tuple[ParsedPassage, ...]
    toc: tuple[TocEntry, ...]
    warnings: tuple[ParserWarning, ...] = field(default_factory=tuple)
