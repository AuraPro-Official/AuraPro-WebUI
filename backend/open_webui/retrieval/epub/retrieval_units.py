"""Faithful derived-window planning for EPUB vector retrieval.

The planner deliberately operates on Python string indices: those are Unicode
code-point offsets, which is the offset contract used by the EPUB canonical
store.  It never normalizes or otherwise changes the source passage.
"""

from __future__ import annotations

from dataclasses import dataclass


TARGET_CODEPOINTS = 800
OVERLAP_CODEPOINTS = 150
CHINESE_SENTENCE_ENDINGS = frozenset('。！？；')
ENGLISH_SENTENCE_ENDINGS = frozenset('.!?;')


@dataclass(frozen=True)
class RetrievalWindow:
    """One continuous, source-addressable retrieval window."""

    start_codepoint: int
    end_codepoint: int


def plan_retrieval_windows(
    content: str,
    *,
    target_codepoints: int = TARGET_CODEPOINTS,
    overlap_codepoints: int = OVERLAP_CODEPOINTS,
) -> tuple[RetrievalWindow, ...]:
    """Plan overlapping source windows without changing ``content``.

    Short passages produce one full-passage window.  For longer text the end
    nearest the target is selected at a Chinese sentence boundary first and an
    English one second.  A small target tolerance prevents a nearby English
    full stop from being discarded merely because an unrelated Chinese boundary
    is much farther away.  If no sentence boundary remains at all, a character
    boundary is the last resort.

    A next window starts approximately ``overlap_codepoints`` before the prior
    end.  Its end must be later than the prior end, so overlap cannot cause a
    repeated window or an infinite loop.
    """
    if not isinstance(content, str):
        raise TypeError('retrieval-unit source content must be text')
    if not content:
        return ()
    if target_codepoints < 1:
        raise ValueError('target_codepoints must be positive')
    if not 0 <= overlap_codepoints < target_codepoints:
        raise ValueError('overlap_codepoints must be non-negative and smaller than target')
    if len(content) <= target_codepoints:
        return (RetrievalWindow(0, len(content)),)

    chinese = _sentence_endings(content, CHINESE_SENTENCE_ENDINGS)
    english = _sentence_endings(content, ENGLISH_SENTENCE_ENDINGS)
    windows: list[RetrievalWindow] = []
    start = 0
    previous_end = 0

    while start < len(content):
        desired_end = min(start + target_codepoints, len(content))
        if desired_end == len(content):
            end = len(content)
        else:
            end = _choose_sentence_boundary(
                chinese=chinese,
                english=english,
                desired_end=desired_end,
                previous_end=previous_end,
                tolerance=overlap_codepoints,
            )
            if end is None:
                # There is no usable sentence boundary after the previous
                # window, so only now is an exact character boundary allowed.
                end = desired_end

        if end <= start:
            raise RuntimeError('retrieval window planner did not make progress')
        windows.append(RetrievalWindow(start, end))
        if end == len(content):
            break
        previous_end = end
        start = max(0, end - overlap_codepoints)

    return tuple(windows)


def _sentence_endings(content: str, endings: frozenset[str]) -> tuple[int, ...]:
    """Return exclusive code-point offsets immediately after sentence marks."""
    return tuple(index + 1 for index, character in enumerate(content) if character in endings)


def _choose_sentence_boundary(
    *,
    chinese: tuple[int, ...],
    english: tuple[int, ...],
    desired_end: int,
    previous_end: int,
    tolerance: int,
) -> int | None:
    """Choose a forward sentence boundary close to the target window end."""
    # Excluding the prior end is essential because the overlapped next window
    # begins before it and must advance beyond it.
    chinese_candidates = tuple(offset for offset in chinese if offset > previous_end)
    english_candidates = tuple(offset for offset in english if offset > previous_end)
    if not chinese_candidates and not english_candidates:
        return None

    near_chinese = _within_tolerance(chinese_candidates, desired_end, tolerance)
    if near_chinese:
        return _nearest(near_chinese, desired_end)
    near_english = _within_tolerance(english_candidates, desired_end, tolerance)
    if near_english:
        return _nearest(near_english, desired_end)

    # Do not make a character split while a sentence boundary exists.  When
    # neither language has a boundary close to target, choose the closest one;
    # tie-breaking preserves the Chinese-first rule.
    nearest_chinese = _nearest(chinese_candidates, desired_end) if chinese_candidates else None
    nearest_english = _nearest(english_candidates, desired_end) if english_candidates else None
    if nearest_english is None:
        return nearest_chinese
    if nearest_chinese is None:
        return nearest_english
    if abs(nearest_chinese - desired_end) <= abs(nearest_english - desired_end):
        return nearest_chinese
    return nearest_english


def _within_tolerance(values: tuple[int, ...], target: int, tolerance: int) -> tuple[int, ...]:
    return tuple(value for value in values if abs(value - target) <= tolerance)


def _nearest(values: tuple[int, ...], target: int) -> int:
    # Prefer an earlier break when equidistant to avoid unexpectedly oversized
    # windows, while still allowing an oversized source sentence when needed.
    return min(values, key=lambda value: (abs(value - target), value))
