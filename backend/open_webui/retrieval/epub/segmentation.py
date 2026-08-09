"""Query-side word segmentation for Tier-1 concept matching.

CJK text has no spaces, so a substring search over a natural-language query
happily anchors a concept in the middle of an unrelated word: ``律`` inside
``规律``, or the one-character alias ``锚`` inside ``锚点``.  A segmenter gives
the matcher the one thing it is missing — where the query's own words begin and
end — so a CJK term can be required to sit on a word boundary the way a Latin
term already is.

Two decisions are load-bearing and deliberate:

* **Only the query is segmented, never the concept terms.**  Segmentation here
  supplies a *boundary predicate*, not a pattern set.  A term like
  ``枢对锚站的校验`` is a phrase no segmenter will ever emit as one token;
  rebuilding the trie over tokens would fragment it into pieces that no longer
  exist as a unit, and the term would stop matching the very query it names.
  The trie keeps matching whole terms; segmentation only decides which of its
  hits land on a boundary.
* **The tokenizer instance is private.**  ``jieba`` exposes a process-global
  default tokenizer (``jieba.dt``) that other parts of this application already
  configure for their own purposes.  Owning a separate
  :class:`jieba.Tokenizer` means nothing here can change how glossary
  translation or bilingual alignment tokenizes, and nothing there can change
  how search resolves a concept.

The dependency is a local, deterministic, offline dictionary tokenizer — it
performs no inference and opens no socket — so falling back to unsegmented
matching when it is missing is a recall decision, not a cloud fallback.  The
fallback is still reported as degraded rather than taken silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TokenBoundaries:
    """Where the words of one specific text begin and end, in code points.

    ``starts`` and ``ends`` are half-open-interval endpoints: a token covering
    ``text[a:b]`` contributes ``a`` to ``starts`` and ``b`` to ``ends``.  A
    match ``[start, end)`` is boundary-valid when both endpoints are members,
    which allows a term to span several adjacent tokens (``枢对锚站的校验``
    covers four) while rejecting one that begins or ends mid-token (``律``
    inside ``规律``).

    ``0`` and ``len(text)`` are always members, whatever the tokenizer said.
    Without that, a text consisting of exactly one term could fail its own
    boundary test, and Tier-2 re-validation — which re-matches the canonical
    name the local resolver returned — would reject every answer it got.
    """

    starts: frozenset[int]
    ends: frozenset[int]

    @classmethod
    def from_tokens(cls, length: int, tokens: 'list[tuple[int, int]]') -> 'TokenBoundaries':
        starts = {0, length}
        ends = {0, length}
        for start, end in tokens:
            if 0 <= start <= length and 0 <= end <= length:
                starts.add(start)
                ends.add(end)
        return cls(starts=frozenset(starts), ends=frozenset(ends))

    def aligned(self, start: int, end: int) -> bool:
        return start in self.starts and end in self.ends


class QuerySegmenter(Protocol):
    """Boundary source injected into search, mirroring the model services.

    It is a protocol for the same reason ``EmbeddingService`` is: search must
    stay testable and desktop-installable without assuming any one
    tokenizer is present.
    """

    def boundaries(self, text: str) -> TokenBoundaries: ...


class SegmenterUnavailable(RuntimeError):
    """The local segmenter could not be imported or initialized."""


class JiebaQuerySegmenter:
    """Boundaries from a private ``jieba`` tokenizer with the default dictionary.

    The dictionary is deliberately the stock one.  Seeding it from the concept
    vocabulary is a separate change that needs its own measurement, and it is
    actively dangerous below two code points: teaching the tokenizer that ``枢``
    is a word would split ``枢对`` and re-admit exactly the spurious one
    character alias matches this module exists to stop.
    """

    def __init__(self) -> None:
        try:
            import jieba
        except Exception as error:  # pragma: no cover - import environment
            raise SegmenterUnavailable(f'jieba is not importable: {error}') from error
        try:
            # A private instance, never ``jieba.dt``: the global tokenizer is
            # shared with utils/glossary_translation.py and
            # utils/bilingual/word_aligner.py, and search must not be able to
            # change what either of them sees.
            tokenizer = jieba.Tokenizer()
            # Building the prefix dictionary costs roughly a third of a second,
            # which is two orders of magnitude more than a query.  Pay it once,
            # here, so a caller that caches this object pays it once per
            # process rather than once per request.
            tokenizer.initialize()
        except Exception as error:  # pragma: no cover - runtime environment
            raise SegmenterUnavailable(f'jieba failed to initialize: {error}') from error
        self._tokenizer = tokenizer

    def boundaries(self, text: str) -> TokenBoundaries:
        tokens = [(start, end) for _, start, end in self._tokenizer.tokenize(text)]
        return TokenBoundaries.from_tokens(len(text), tokens)


def load_query_segmenter() -> tuple[QuerySegmenter | None, str | None]:
    """Return the local segmenter, or ``None`` and the reason it is unavailable.

    Failure is returned rather than raised because an unsegmented query is a
    usable, if over-broad, search; the caller is responsible for reporting the
    reason as a degraded component so the loss of precision is visible.
    """
    try:
        return JiebaQuerySegmenter(), None
    except SegmenterUnavailable as error:
        return None, str(error)
