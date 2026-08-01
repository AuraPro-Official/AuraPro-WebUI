"""ZIP archive validation for EPUB input."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import stat
import zipfile


class EpubArchiveError(ValueError):
    """The archive cannot safely be treated as an EPUB."""


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_entries: int = 10_000
    max_compressed_bytes: int = 200 * 1024 * 1024
    max_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_expansion_ratio: int = 100


def validate_archive(archive: zipfile.ZipFile, limits: ArchiveLimits) -> None:
    infos = archive.infolist()
    if len(infos) > limits.max_entries:
        raise EpubArchiveError("EPUB archive has too many entries")
    if sum(info.compress_size for info in infos) > limits.max_compressed_bytes:
        raise EpubArchiveError("EPUB archive compressed size exceeds the limit")
    if sum(info.file_size for info in infos) > limits.max_uncompressed_bytes:
        raise EpubArchiveError("EPUB archive expanded size exceeds the limit")
    seen_names: set[str] = set()
    for info in infos:
        if info.filename in seen_names:
            # ZipFile.read(name) resolves an ambiguous duplicate member.  That
            # makes the imported reading text depend on an implementation
            # detail, so duplicate archive members are not a safe EPUB input.
            raise EpubArchiveError(f"duplicate EPUB archive member: {info.filename!r}")
        seen_names.add(info.filename)
        path = PurePosixPath(info.filename)
        if not info.filename or "\x00" in info.filename or path.is_absolute() or ".." in path.parts:
            raise EpubArchiveError(f"unsafe EPUB archive path: {info.filename!r}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise EpubArchiveError(f"symbolic links are not allowed in EPUB archives: {info.filename!r}")
        if info.file_size and info.compress_size == 0:
            raise EpubArchiveError(f"invalid compressed EPUB entry: {info.filename!r}")
        if info.compress_size and info.file_size / info.compress_size > limits.max_expansion_ratio:
            raise EpubArchiveError(f"EPUB entry expansion ratio exceeds the limit: {info.filename!r}")
