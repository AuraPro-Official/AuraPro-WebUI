"""Stable visible-text extraction from textual XHTML documents."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re

from .model import ContentKind, ParserWarning

_IGNORED = {"head", "script", "style", "template", "noscript", "metadata"}
_KINDS: dict[str, ContentKind] = {
    "p": "paragraph", "h1": "heading", "h2": "heading", "h3": "heading", "h4": "heading", "h5": "heading", "h6": "heading",
    "li": "list_item", "blockquote": "blockquote", "pre": "pre",
}
_UNSUPPORTED = {"img", "image", "svg", "table", "thead", "tbody", "tfoot", "tr", "td", "th"}
_FLOW_WS = re.compile(r"[\t\n\r\f ]+")


@dataclass(frozen=True, slots=True)
class ExtractedUnit:
    content: str
    content_kind: ContentKind
    source_fragment: str | None
    source_order: int


@dataclass(frozen=True, slots=True)
class XhtmlExtraction:
    units: tuple[ExtractedUnit, ...]
    anchors: dict[str, int]
    warnings: tuple[ParserWarning, ...]


@dataclass(slots=True)
class _Block:
    tag: str
    kind: ContentKind
    order: int
    fragment: str | None
    parts: list[str]
    nested: bool = False


class _VisibleTextParser(HTMLParser):
    """Collect leaf evidence blocks, never overlapping parent and child text."""

    def __init__(self, path: str) -> None:
        super().__init__(convert_charrefs=True)
        self.path = path
        self.ignored = self.unsupported = 0
        self.blocks: list[_Block] = []
        self.units: list[ExtractedUnit] = []
        self.anchors: dict[str, int] = {}
        self.warnings: list[ParserWarning] = []
        self.warned: set[str] = set()
        self.order = 0
        self.fragment: str | None = None
        self.fallback: list[str] = []
        self.fallback_order: int | None = None
        self.fallback_fragment: str | None = None
        self.used_fallback = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self.order += 1
        values = dict(attrs)
        identifier = values.get("id") or values.get("name")
        if identifier:
            self.anchors.setdefault(identifier, self.order)
            self.fragment = identifier
        if tag in _IGNORED:
            self.ignored += 1
            return
        if self.ignored:
            return
        if tag in _UNSUPPORTED:
            self.unsupported += 1
            category = "table" if tag in {"table", "thead", "tbody", "tfoot", "tr", "td", "th"} else "image"
            if category not in self.warned:
                self.warned.add(category)
                self.warnings.append(ParserWarning(f"{category}_content_ignored", f"{category} content is out of scope for textual EPUB import", self.path))
            return
        if self.unsupported:
            return
        if tag == "br":
            self._append("\uFFF0")
            return
        kind = _KINDS.get(tag)
        if kind:
            if not self.blocks:
                self._flush_fallback()
            if self.blocks:
                self.blocks[-1].nested = True
            self.blocks.append(_Block(tag, kind, self.order, identifier or self.fragment, []))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED and self.ignored:
            self.ignored -= 1
            return
        if self.ignored:
            return
        if tag in _UNSUPPORTED and self.unsupported:
            self.unsupported -= 1
            return
        if self.unsupported:
            return
        if tag in _KINDS and self.blocks and self.blocks[-1].tag == tag:
            block = self.blocks.pop()
            if not block.nested:
                content = _normalise(block.parts, block.kind)
                if content:
                    self.units.append(ExtractedUnit(content, block.kind, block.fragment, block.order))

    def handle_data(self, data: str) -> None:
        if self.ignored or self.unsupported:
            return
        if self.blocks:
            self._append(data)
        else:
            self.fallback.append(data)
            if self.fallback_order is None:
                self.fallback_order = self.order
                self.fallback_fragment = self.fragment

    def close(self) -> None:
        super().close()
        while self.blocks:
            block = self.blocks.pop()
            if not block.nested:
                content = _normalise(block.parts, block.kind)
                if content:
                    self.units.append(ExtractedUnit(content, block.kind, block.fragment, block.order))
            self.warnings.append(ParserWarning("malformed_xhtml", f"unclosed <{block.tag}> recovered", self.path))

    def result(self) -> XhtmlExtraction:
        self._flush_fallback()
        units = sorted(self.units, key=lambda item: item.source_order)
        if self.used_fallback:
            self.warnings.append(ParserWarning("fallback_text_block", "used conservative fallback text block", self.path))
        return XhtmlExtraction(tuple(units), self.anchors, tuple(self.warnings))

    def _append(self, value: str) -> None:
        for block in self.blocks:
            block.parts.append(value)

    def _flush_fallback(self) -> None:
        content = _normalise(self.fallback, "fallback")
        if content:
            self.units.append(ExtractedUnit(
                content,
                "fallback",
                self.fallback_fragment,
                self.fallback_order if self.fallback_order is not None else self.order,
            ))
            self.used_fallback = True
        self.fallback.clear()
        self.fallback_order = None
        self.fallback_fragment = None


def extract_xhtml_text(source: str, source_path: str) -> XhtmlExtraction:
    parser = _VisibleTextParser(source_path)
    parser.feed(source)
    parser.close()
    return parser.result()


def _normalise(parts: list[str], kind: ContentKind) -> str:
    raw = "".join(parts)
    if kind == "pre":
        return raw
    # Explicit <br> has a dedicated sentinel; all source flow whitespace is
    # collapsed according to the confirmed visible-text rule.
    return "\n".join(_FLOW_WS.sub(" ", piece).strip(" ") for piece in raw.split("\uFFF0")).strip(" ")
