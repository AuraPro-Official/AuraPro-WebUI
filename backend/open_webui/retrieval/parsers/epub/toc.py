"""EPUB2 NCX and EPUB3 NAV TOC parsers."""

from __future__ import annotations

from html.parser import HTMLParser
import xml.etree.ElementTree as ET

from .model import TocEntry
from .package import EpubPackageError, resolve_href


def parse_ncx(data: bytes, ncx_path: str, book_title: str) -> tuple[TocEntry, ...]:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise EpubPackageError("NCX contains a forbidden DTD/entity declaration")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise EpubPackageError(f"invalid NCX: {error}") from error
    result: list[TocEntry] = []
    def visit(parent: ET.Element, path: tuple[str, ...]) -> None:
        for node in parent:
            if _local(node.tag) != "navPoint":
                continue
            label = next((" ".join((item.text or "").split()) for item in node.iter() if _local(item.tag) == "text" and item.text), "Section")
            current = path + (label,)
            content = next((item for item in node if _local(item.tag) == "content"), None)
            if content is not None and content.get("src"):
                href, fragment = resolve_href(ncx_path, content.get("src", ""))
                result.append(TocEntry(href, fragment, current, "ncx"))
            visit(node, current)
    nav_map = next((item for item in root.iter() if _local(item.tag) == "navMap"), None)
    if nav_map is not None:
        # `book_title` is a separate result field.  Breadcrumbs describe the
        # document's own hierarchy and must therefore not duplicate it.
        visit(nav_map, ())
    return tuple(result)


class _NavParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nav_depth = 0
        self.toc_depth: int | None = None
        self.li_paths: list[tuple[str, ...]] = []
        self.anchor: dict[str, object] | None = None
        self.entries: list[tuple[tuple[str, ...], str]] = []
    def handle_starttag(self, tag, attrs):
        tag = tag.lower(); values = dict(attrs)
        if tag == "nav":
            self.nav_depth += 1
            if self.toc_depth is None and ({"toc", "doc-toc"} & {values.get("epub:type", ""), values.get("role", ""), values.get("type", "")}):
                self.toc_depth = self.nav_depth
        elif self.toc_depth is not None and tag == "li":
            self.li_paths.append(())
        elif self.toc_depth is not None and tag == "a" and self.li_paths:
            self.anchor = {"href": values.get("href"), "text": []}
    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor["text"].append(data)  # type: ignore[index]
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a" and self.anchor is not None:
            label = " ".join("".join(self.anchor["text"]).split())  # type: ignore[index]
            href = self.anchor["href"]
            if label and isinstance(href, str):
                parent = self.li_paths[-2] if len(self.li_paths) > 1 else ()
                self.li_paths[-1] = parent + (label,)
                self.entries.append((self.li_paths[-1], href))
            self.anchor = None
        elif tag == "li" and self.toc_depth is not None and self.li_paths:
            self.li_paths.pop()
        elif tag == "nav":
            if self.toc_depth == self.nav_depth:
                self.toc_depth = None
            self.nav_depth -= 1


def parse_nav(data: str, nav_path: str, book_title: str) -> tuple[TocEntry, ...]:
    parser = _NavParser(); parser.feed(data); parser.close()
    return tuple(TocEntry(*resolve_href(nav_path, href), path, "nav") for path, href in parser.entries)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
