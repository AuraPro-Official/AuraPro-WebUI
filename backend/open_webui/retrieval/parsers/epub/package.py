"""EPUB container and OPF package parsing."""

from __future__ import annotations

from dataclasses import dataclass
import posixpath
import xml.etree.ElementTree as ET


class EpubPackageError(ValueError):
    """The EPUB package metadata is missing or malformed."""


@dataclass(frozen=True, slots=True)
class ManifestItem:
    identifier: str
    href: str
    media_type: str
    properties: frozenset[str]


@dataclass(frozen=True, slots=True)
class PackageDocument:
    title: str | None
    manifest: dict[str, ManifestItem]
    spine: tuple[str, ...]
    nav_item: ManifestItem | None
    ncx_item: ManifestItem | None


def parse_container(data: bytes) -> str:
    root = _safe_xml(data, 'container.xml')
    for item in root.iter():
        if _local(item.tag) == 'rootfile' and item.get('full-path'):
            return _normalise(item.get('full-path', ''))
    raise EpubPackageError('META-INF/container.xml contains no OPF rootfile')


def parse_opf(data: bytes) -> PackageDocument:
    root = _safe_xml(data, 'OPF package')
    title = None
    manifest: dict[str, ManifestItem] = {}
    spine: list[str] = []
    spine_toc = None
    for item in root.iter():
        name = _local(item.tag)
        if name == 'title' and title is None and item.text:
            title = item.text
        elif name == 'item' and item.get('id') and item.get('href'):
            identifier = item.get('id', '')
            manifest[identifier] = ManifestItem(
                identifier,
                item.get('href', ''),
                item.get('media-type', ''),
                frozenset(item.get('properties', '').split()),
            )
        elif name == 'itemref' and item.get('idref'):
            spine.append(item.get('idref', ''))
        elif name == 'spine' and item.get('toc'):
            spine_toc = item.get('toc')
    if not manifest or not spine:
        raise EpubPackageError('OPF package must contain a manifest and reading spine')
    nav = next((item for item in manifest.values() if 'nav' in item.properties), None)
    ncx = manifest.get(spine_toc) if spine_toc else None
    if ncx is None:
        ncx = next((item for item in manifest.values() if item.media_type == 'application/x-dtbncx+xml'), None)
    return PackageDocument(title, manifest, tuple(spine), nav, ncx)


def resolve_href(base_path: str, href: str) -> tuple[str, str | None]:
    path, sep, fragment = href.partition('#')
    resolved = base_path if not path else posixpath.join(posixpath.dirname(base_path), path)
    return _normalise(resolved), fragment if sep and fragment else None


def _normalise(path: str) -> str:
    normalised = posixpath.normpath(path.replace('\\', '/'))
    if normalised in {'', '.'} or normalised.startswith('../') or normalised.startswith('/'):
        raise EpubPackageError(f'unsafe package path: {path!r}')
    return normalised


def _safe_xml(data: bytes, description: str) -> ET.Element:
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise EpubPackageError(f'{description} contains a forbidden DTD/entity declaration')
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise EpubPackageError(f'invalid {description}: {error}') from error


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]
