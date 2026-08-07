"""Map spine XHTML units to TOC entries."""

from .model import TocEntry
from .xhtml import ExtractedUnit


def toc_path_for_unit(
    source_path: str, unit: ExtractedUnit, anchors: dict[str, int], entries: tuple[TocEntry, ...]
) -> tuple[str, ...]:
    matches: list[tuple[int, int, TocEntry]] = []
    for index, entry in enumerate(entries):
        if entry.href != source_path:
            continue
        position = -1 if entry.fragment is None else anchors.get(entry.fragment)
        if position is not None and position <= unit.source_order:
            matches.append((position, index, entry))
    return max(matches, key=lambda item: (item[0], item[1]))[2].path if matches else ()
