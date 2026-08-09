"""Faithful, local-only EPUB concept search orchestration.

This module deliberately has no database or HTTP-client dependency.  Its
repository and vector protocols make the canonical EPUB store responsible for
reading immutable source rows, while model adapters are constrained by the
local/private policy in :mod:`inference`.  A derived retrieval window is never
returned as a citation: every hit renders its complete parent passage and a
verified continuous excerpt.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import math
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .inference import ConceptResolver, EmbeddingService, ModelAvailability, RerankerService
from .segmentation import QuerySegmenter, TokenBoundaries, load_query_segmenter
from .vector_index import DerivedVectorRecord


class SearchError(ValueError):
    """The caller supplied an invalid search request or source invariant failed."""


# How a concept was reached when it was not matched directly.  The prefix is
# load-bearing: ``relation:`` is a semantic claim a model made and an
# administrator can revise, ``structure:`` is the book's own table of contents,
# which no model authored.  A reader who sees the second must not be able to
# mistake it for the first.
_RELATION_HAS_PART = "relation:HAS_PART"
_STRUCTURE_TOC_CHILD = "structure:TOC_CHILD"

# When one span is attributed to several expansion-derived concepts at the same
# hop count, this order decides which edge explains it.  The semantic graph
# wins wherever it exists; TOC structure is the fallback, never a competitor.
_LABEL_PRIORITY = (_RELATION_HAS_PART, _STRUCTURE_TOC_CHILD)

_EMPTY_LABELS: Mapping[str, str] = MappingProxyType({})

# A concept matched only by a short or model-invented surface form still
# resolves and still contributes its own spans; it just does not get to seed
# expansion.  See :meth:`EpubSearchService._expansion_seed_ids` for why the test
# is about the term and not about the concept behind it.
_MIN_SEED_TERM_LENGTH = 2

# A TOC node whose children hold more bound concepts than this contributes no
# expansion at all.  Skipping is deliberate: truncating would leave
# ``graph_total`` a number with no explanation of what was left out.
_MAX_TOC_CHILD_CONCEPTS = 64


@dataclass(frozen=True, slots=True)
class ConceptTerm:
    """One canonical concept/alias term available to the Tier-1 matcher.

    The first three fields are all the matcher itself ever reads.  The last
    three describe the concept rather than the match, and each has one reader:

    * ``term_source`` distinguishes a surface form a model invented from one a
      seed list or an administrator supplied, and
      :meth:`EpubSearchService._expansion_seed_ids` refuses to seed expansion
      from a single character a model proposed.
    * ``has_part_fanout`` is how
      :meth:`EpubSearchService._expand_toc_child_concepts` knows the model
      already decomposed a concept and that TOC structure must stay out of the
      way.
    * ``mention_count`` currently gates nothing.  It was tried as a proxy for a
      generic term and removed, because frequency is a fact about the book and
      not about what the reader asked for — see
      :meth:`EpubSearchService._expansion_seed_ids`.  It is kept because it is
      free alongside the fan-out the store already computes, and because a
      future specificity signal will want it; it is deliberately not deleted
      and re-added.

    All three default to ``None``, meaning "this repository did not say".  A
    repository that supplies none of them — a test double, a future PostgreSQL
    read model that has not caught up — gets exactly today's behaviour instead
    of an error, because every guard reads them only when present.
    """

    concept_id: str
    canonical_name: str
    term: str
    term_source: str | None = None
    mention_count: int | None = None
    has_part_fanout: int | None = None


@dataclass(frozen=True, slots=True)
class SearchExcerpt:
    content: str
    start_codepoint: int
    end_codepoint: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A faithful source result.  ``content`` is always the full passage."""

    passage_id: str
    book_title: str
    toc_path: tuple[str, ...]
    content: str
    content_sha256: str
    matched_concepts: tuple[str, ...]
    provenance: tuple[str, ...]
    excerpt: SearchExcerpt
    score: float | None = None


@dataclass(frozen=True, slots=True)
class SearchResponse:
    query: str
    resolved_concepts: tuple[str, ...]
    graph_total: int
    graph_offset: int
    graph_results: tuple[SearchHit, ...]
    vector_results: tuple[SearchHit, ...]
    fused_results: tuple[SearchHit, ...]
    degraded: tuple[ModelAvailability, ...]


@dataclass(frozen=True, slots=True)
class _RelationExpansion:
    """The concept set a query reaches, with how far and how cheaply it got there.

    ``depths`` is hop count and is what a hit's provenance reports.  ``costs``
    is the retrieval-ranking signal and is deliberately *not* hop count: one
    hop out of a concept with 20 ``HAS_PART`` children says far less about a
    span than one hop out of a concept with a single child.  Keeping the two
    apart means bounding a hub changes ranking without rewriting the provenance
    a reader is shown.

    ``labels`` records *by what kind of edge* a concept was reached, for the
    concepts that were not matched directly.  A semantic ``HAS_PART`` edge a
    model proposed and an administrator can revise, and a structural TOC edge
    the parser read out of the book, are both one hop, but they are not the
    same claim, and a reader is shown which one they got.
    """

    concept_ids: tuple[str, ...]
    depths: Mapping[str, int]
    costs: Mapping[str, float]
    labels: Mapping[str, str] = _EMPTY_LABELS


@dataclass(frozen=True, slots=True)
class _FusedCandidate:
    """A locally-rankable, source-verified candidate from either channel.

    ``hit`` keeps the canonical passage and exact excerpt that will be
    rendered.  ``vector`` is only used locally for MMR diversification; it is
    never exposed as source evidence.
    """

    hit: SearchHit
    vector: tuple[float, ...]


class EpubSearchRepository(Protocol):
    """Canonical-store read surface used by search, independent of SQLite."""

    def list_concept_terms(self) -> list[Mapping[str, Any]]: ...

    # A cheap value that changes whenever ``list_concept_terms`` would return
    # something different, so the Tier-1 matcher can be reused across requests
    # without ever serving a vocabulary the administrator has already changed.
    # ``None`` declares that this repository cannot make that promise, and
    # costs it the reuse rather than the freshness.
    def concept_term_fingerprint(self) -> tuple[Any, ...] | None: ...

    # Both occurrence methods enumerate *distinct source spans*, never mention
    # rows: exact duplicates and spans nested inside another span collapse into
    # one row that carries ``concept_ids``/``canonical_names`` for every
    # concept it absorbed.  The count must apply that same rule, or the total
    # disagrees with the pages.
    def count_concept_occurrences(self, concept_ids: Sequence[str]) -> int: ...

    # ``concept_costs`` declares how expensive it was to reach each concept, so
    # the store can rank before it truncates.  It affects ``ORDER BY`` only:
    # the predicate the count and the pages share is untouched.
    def list_concept_occurrences(
        self,
        concept_ids: Sequence[str],
        *,
        offset: int,
        limit: int,
        concept_costs: Mapping[str, float] | None = None,
    ) -> list[Mapping[str, Any]]: ...

    def get_search_passage(self, passage_id: str) -> Mapping[str, Any] | None: ...

    def get_retrieval_unit(self, retrieval_unit_id: str) -> Mapping[str, Any] | None: ...

    def matched_concept_names(
        self, passage_id: str, concept_ids: Sequence[str]
    ) -> list[str]: ...

    def list_concept_relation_neighbors(
        self, concept_ids: Sequence[str], *, predicates: Sequence[str] = ("HAS_PART",)
    ) -> list[Mapping[str, Any]]: ...

    # Structural, deterministic decomposition read from the book's own table of
    # contents, for concepts the model never decomposed.  Search calls it only
    # when the repository actually has it, so a read model that cannot answer
    # the question is served today's behaviour rather than an AttributeError.
    def list_toc_child_concepts(
        self, concept_ids: Sequence[str]
    ) -> list[Mapping[str, Any]]: ...


class VectorCandidateBackend(Protocol):
    """Derived-vector query boundary.  Implementations may be sqlite-vec/pgvector."""

    def search(
        self, query_vector: Sequence[float], *, embedding_profile: str, limit: int
    ) -> list[DerivedVectorRecord]: ...


class _TrieNode:
    __slots__ = ("children", "terms", "failure")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.terms: list[ConceptTerm] = []
        self.failure: _TrieNode | None = None


@dataclass(frozen=True, slots=True)
class _TermMatch:
    """One boundary-valid hit, with the span it covered in the searched text."""

    start: int
    end: int
    term: ConceptTerm


class ConceptTermMatcher:
    """In-memory multi-pattern matcher with token-boundary semantics.

    It is a trie rather than a dependency on a particular Aho-Corasick package:
    all aliases are scanned in one pass, which is equivalent for the search
    contract and keeps desktop deployments dependency-light.  For the same
    reason it does not segment anything itself: a caller that has word
    boundaries for the searched text passes them in, and a caller that has none
    still gets a working, if broader, match.  Terms starting/ending in ASCII
    word characters require a corresponding token boundary; terms containing
    CJK require the supplied boundaries to agree with both of their ends.
    """

    def __init__(self, terms: Iterable[ConceptTerm]):
        self._root = _TrieNode()
        self._terms: list[ConceptTerm] = []
        seen: set[tuple[str, str]] = set()
        for term in terms:
            if not term.term.strip():
                continue
            key = (term.concept_id, term.term.casefold())
            if key in seen:
                continue
            seen.add(key)
            self._terms.append(term)
            node = self._root
            for character in term.term.casefold():
                node = node.children.setdefault(character, _TrieNode())
            node.terms.append(term)
        self._build_failure_links()

    @property
    def terms(self) -> tuple[ConceptTerm, ...]:
        return tuple(self._terms)

    def match(self, text: str, *, boundaries: TokenBoundaries | None = None) -> tuple[ConceptTerm, ...]:
        """Concepts this text mentions, one per concept."""
        return tuple(hit.term for hit in self.match_spans(text, boundaries=boundaries))

    def match_spans(
        self, text: str, *, boundaries: TokenBoundaries | None = None
    ) -> tuple[_TermMatch, ...]:
        """Concepts this text mentions, each with the span that won it.

        Positions are kept all the way through because two rules need them.
        Longest-match suppression drops a hit that sits *strictly inside*
        another surviving hit: with the query ``神对约伯的试炼的意义``, the
        alias ``约`` is boundary-valid on its own but is covered by the longer
        ``神对约伯的试炼``, and a reader asking about the trial of Job is not
        asking about every covenant in the book.  Equal spans are not
        contained, so two aliases that collide on identical characters both
        survive — the same rule the store applies when it collapses overlapping
        source spans.

        Deduplication then keeps the *longest* surviving span per concept.
        This is a deliberate change from keeping the first: position used to be
        discarded before anything could compare two hits, so a concept matched
        by a one-character alias early in the query kept that hit even when its
        full canonical name matched later.
        """
        if not text:
            return ()
        folded = text.casefold()
        # Offsets are into the folded text, while ``boundaries`` describes the
        # original.  Casefolding is length-preserving for CJK and for every
        # script this store indexes, but not universally (``ß`` folds to two
        # characters).  Where it is not, the offsets cannot be compared, so the
        # boundary rule is dropped rather than applied to the wrong positions.
        if boundaries is not None and len(folded) != len(text):
            boundaries = None
        hits: list[_TermMatch] = []
        node = self._root
        for end, character in enumerate(folded, start=1):
            while node is not self._root and character not in node.children:
                assert node.failure is not None
                node = node.failure
            node = node.children.get(character, self._root)
            for term in node.terms:
                term_folded = term.term.casefold()
                start = end - len(term_folded)
                if start >= 0 and self._valid_boundaries(folded, start, end, term_folded, boundaries):
                    hits.append(_TermMatch(start=start, end=end, term=term))
        surviving = [hit for hit in hits if not _strictly_contained(hit, hits)]
        best: dict[str, _TermMatch] = {}
        for hit in surviving:
            incumbent = best.get(hit.term.concept_id)
            if incumbent is None or (hit.end - hit.start) > (incumbent.end - incumbent.start):
                best[hit.term.concept_id] = hit
        return tuple(best.values())

    def _build_failure_links(self) -> None:
        """Compile the trie into an Aho-Corasick automaton once per request."""
        self._root.failure = self._root
        queue: deque[_TrieNode] = deque()
        for child in self._root.children.values():
            child.failure = self._root
            queue.append(child)
        while queue:
            node = queue.popleft()
            for character, child in node.children.items():
                failure = node.failure or self._root
                while failure is not self._root and character not in failure.children:
                    assert failure.failure is not None
                    failure = failure.failure
                child.failure = failure.children.get(character, self._root)
                # A terminal reached through a suffix failure is also a match.
                child.terms.extend(child.failure.terms)
                queue.append(child)

    @staticmethod
    def _valid_boundaries(
        text: str, start: int, end: int, term: str, boundaries: TokenBoundaries | None
    ) -> bool:
        # A term containing CJK is valid only where the searched text's own
        # word boundaries agree with both of its ends.  CJK is written without
        # spaces, so without this a term matches anywhere it appears as a
        # substring: `义` lands inside `意义`, and the one-character alias `约`
        # lands inside the name `约伯`, pulling in concepts the query never
        # mentioned.  Boundaries come from the caller because this matcher does
        # not own a segmenter; when the caller has none, the older rule applies
        # and the loss of precision is reported as degraded rather than hidden.
        if boundaries is not None and any(_is_cjk(character) for character in term):
            if not boundaries.aligned(start, end):
                return False
        # The Latin rule is unchanged: a term with no ASCII word characters has
        # nothing further to check, and a mixed term must satisfy both.
        if not any(_ascii_word(character) for character in term):
            return True
        if _ascii_word(term[0]) and start > 0 and _ascii_word(text[start - 1]):
            return False
        if _ascii_word(term[-1]) and end < len(text) and _ascii_word(text[end]):
            return False
        return True


class EpubSearchService:
    """Combines graph and local-vector retrieval without cloud fallback."""

    def __init__(
        self,
        *,
        source: EpubSearchRepository,
        vector_backend: VectorCandidateBackend | None = None,
        embeddings: EmbeddingService | None = None,
        reranker: RerankerService | None = None,
        concept_resolver: ConceptResolver | None = None,
        segmenter: QuerySegmenter | None = None,
        mmr_lambda: float = 0.7,
    ) -> None:
        if not 0.0 <= mmr_lambda <= 1.0:
            raise SearchError("mmr_lambda must be between 0 and 1")
        self._source = source
        self._vector_backend = vector_backend
        self._embeddings = embeddings
        self._reranker = reranker
        self._concept_resolver = concept_resolver
        self._mmr_lambda = mmr_lambda
        # An explicitly injected segmenter is taken as configured and is never
        # replaced by the loaded one; ``None`` means "load the local default on
        # first use", which is not the same thing as "run without one".
        self._segmenter = segmenter
        self._segmenter_loaded = segmenter is not None
        self._segmenter_reason: str | None = None
        self._matcher: ConceptTermMatcher | None = None
        self._matcher_fingerprint: tuple[Any, ...] | None = None

    def search(
        self,
        query: str,
        *,
        graph_offset: int = 0,
        graph_limit: int = 20,
        graph_fusion_limit: int = 20,
        vector_limit: int = 10,
        vector_candidate_limit: int = 50,
    ) -> SearchResponse:
        if not isinstance(query, str) or not query.strip():
            raise SearchError("query must be non-empty text")
        if (
            graph_offset < 0
            or graph_limit < 1
            or graph_fusion_limit < 1
            or vector_limit < 1
            or vector_candidate_limit < vector_limit
        ):
            raise SearchError("search pagination limits are invalid")

        degraded: list[ModelAvailability] = []
        matcher = self._concept_matcher()
        segmenter = self._query_segmenter(degraded)
        matches = matcher.match_spans(query, boundaries=_boundaries(segmenter, query))
        if not matches:
            matches = self._resolve_tier_two(query, matcher, segmenter, degraded)
        matched = tuple(hit.term for hit in matches)
        concept_ids = tuple(dict.fromkeys(term.concept_id for term in matched))
        resolved_names = tuple(dict.fromkeys(term.canonical_name for term in matched))
        # Resolution and expansion are two different questions.  Every matched
        # concept is resolved and contributes its own spans; only the subset
        # that names something specific gets to pull the graph behind it.
        seed_ids = self._expansion_seed_ids(matches)
        expansion = self._expand_relation_concepts(concept_ids, seed_ids)
        expansion = self._expand_toc_child_concepts(expansion, matches, seed_ids, degraded)
        graph_concept_ids = expansion.concept_ids

        graph_total = self._source.count_concept_occurrences(graph_concept_ids) if graph_concept_ids else 0
        graph_results = self._graph_page(
            expansion, resolved_names, offset=graph_offset, limit=graph_limit
        )
        # The fused channel reads the *top* of the ranked graph channel, never
        # the page the panel happens to be showing.  Feeding it the display
        # page made ``graph_limit`` a recall ceiling on a ranked channel: with
        # the default 20 against a ``graph_total`` of 778, fusion only ever saw
        # the 20 spans nearest the front of the book, and its results looked
        # sound only because 50 vector candidates carried them.  Paging the
        # panel must not change what the fused answer is, either.
        fusion_graph_results = (
            graph_results[:graph_fusion_limit]
            if graph_offset == 0 and graph_limit >= graph_fusion_limit
            else self._graph_page(expansion, resolved_names, offset=0, limit=graph_fusion_limit)
        )
        vector_candidates = self._vector_candidates(
            query,
            candidate_limit=vector_candidate_limit,
            degraded=degraded,
        )
        vector_results = self._vector_hits(
            query,
            vector_candidates=vector_candidates,
            concept_ids=graph_concept_ids,
            result_limit=vector_limit,
            degraded=degraded,
        )
        fused_results = (
            self._fused_hits(
                query,
                graph_results=fusion_graph_results,
                vector_candidates=vector_candidates,
                concept_ids=graph_concept_ids,
                result_limit=vector_limit,
                degraded=degraded,
            )
            if fusion_graph_results
            else _mark_vector_results_fused(vector_results)
        )
        return SearchResponse(
            query=query,
            resolved_concepts=resolved_names,
            graph_total=graph_total,
            graph_offset=graph_offset,
            graph_results=graph_results,
            vector_results=vector_results,
            fused_results=fused_results,
            degraded=tuple(degraded),
        )

    def _expansion_seed_ids(self, matches: Sequence[_TermMatch]) -> tuple[str, ...]:
        """Which resolved concepts are allowed to *seed* expansion.

        The question is about the **matched term**, never about the concept
        behind it.  A query that names something brings its neighbourhood with
        it; a query that merely happens to contain a single very common
        character does not, because expanding out of a concept the reader never
        actually named returns spans about something else entirely — which is
        how one query came back citing ``神对亚当的嘱咐``, a hub *child* reached
        by walking out of a concept nobody had asked for.

        This is a **resolution** rule, not a ranking one, and it is the only
        place the two could be confused.  Ranking still down-weights a
        high-degree concept and never caps it: everything a seed reaches is
        still counted and still pageable.  What changes here is which concepts
        are seeds at all.  The concept itself stays resolved either way and
        contributes every one of its own spans — that is what makes this safe.

        Two conditions, both about the term's shape, and both must hold:

        1. **The winning matched term is at least two code points.**  Measured
           on the query, not on the vocabulary: ``match_spans`` keeps the
           longest surviving span per concept, so a concept whose full name the
           query spelled out is judged on that name even if a one-character
           alias also matched somewhere.
        2. **Not a one-character alias a model invented.**  Implied by (1)
           today, and stated anyway: (1) is about how much of the query the
           term covered, this is about how much the term is worth, and a future
           relaxation of the first must not silently readmit the second.

        Deliberately *not* a condition: **how many mentions the concept has.**
        That was tried as a proxy for a generic term and it misfires exactly
        where it matters.  ``神的权柄`` is matched by its full five-code-point
        name; it is not generic, it is a specific topic that a book about God
        naturally discusses often, and a reader who searches for it by name
        almost certainly wants its sub-topics.  A ceiling on the count refused
        them, cutting that query from 174 spans to 42.  Frequency is a fact
        about the book, not about what the reader asked for.

        The accepted consequence is that naming the hub outright —
        ``独一无二的神``, spelled in full — expands to its whole subtree.  That
        is correct: you asked for it by name.  What stays blocked is its
        one-character alias ``神``, by rule (1), so an incidental 神 inside an
        unrelated query still drags nothing in.

        Also deliberately *not* a condition: what fraction of the query the
        term covered.  That number moves with phrasing rather than with
        meaning, and longest-match suppression already discards the short alias
        that sits inside a longer one.
        """
        seeds: list[str] = []
        for hit in matches:
            term = hit.term
            if (hit.end - hit.start) < _MIN_SEED_TERM_LENGTH:
                continue
            if term.term_source == "MODEL" and len(term.term) < _MIN_SEED_TERM_LENGTH:
                continue
            seeds.append(term.concept_id)
        return tuple(dict.fromkeys(seeds))

    def _expand_relation_concepts(
        self, concept_ids: Sequence[str], seed_ids: Sequence[str] | None = None, *, max_depth: int = 2
    ) -> _RelationExpansion:
        """Follow a bounded containment graph without turning relations into citations.

        Coverage is unchanged by ranking: every concept reachable within
        ``max_depth`` is still queried, and ``graph_total`` still counts every
        span they occur in.  A high-degree concept is *down-weighted*, never
        dropped — capping the fan-out would delete source a reader could
        otherwise page to, and it would do so invisibly, since the count would
        simply come back smaller with no way to tell why.

        The weight is the price of the walk: one hop costs ``1 + log2(k)``
        where ``k`` is how many children that parent fanned out to.  A parent
        with one child costs a plain hop; ``独一无二的神``, with 20 grounded
        ``HAS_PART`` children in the acceptance book, costs 5.32 — so its
        children's spans sort below spans reached by two hops through narrow
        parents, which is exactly the relative confidence those two paths
        deserve.  Because a hub can make a one-hop path dearer than a two-hop
        one, cost takes the cheapest path found while ``depths`` keeps the hop
        count, which is what provenance reports.

        ``seed_ids`` is which of the matched concepts may *start* a walk, and
        defaults to all of them.  It changes only the frontier: ``depths`` and
        ``costs`` still open at zero for every directly matched concept, and
        ``concept_ids`` still contains every one of them, so a concept that
        cannot seed expansion is in no way less resolved than before and
        ``resolved_concepts`` is untouched.
        """
        depths = {concept_id: 0 for concept_id in concept_ids}
        costs: dict[str, float] = {concept_id: 0.0 for concept_id in concept_ids}
        labels: dict[str, str] = {}
        frontier = list(concept_ids if seed_ids is None else seed_ids)
        for depth in range(1, max_depth + 1):
            if not frontier:
                break
            edges = self._source.list_concept_relation_neighbors(frontier, predicates=("HAS_PART",))
            children: dict[str, set[str]] = {}
            for edge in edges:
                subject, target = edge.get("subject_concept_id"), edge.get("object_concept_id")
                if isinstance(subject, str) and isinstance(target, str) and subject and target:
                    children.setdefault(subject, set()).add(target)
            step_costs: dict[str, float] = {}
            for edge in edges:
                subject, target = edge.get("subject_concept_id"), edge.get("object_concept_id")
                if not isinstance(target, str) or not target:
                    continue
                fanout = len(children.get(subject, ())) if isinstance(subject, str) else 0
                parent_cost = costs.get(subject, 0.0) if isinstance(subject, str) else 0.0
                candidate = parent_cost + 1.0 + math.log2(max(fanout, 1))
                if candidate < step_costs.get(target, math.inf):
                    step_costs[target] = candidate
            next_frontier: list[str] = []
            for target, cost in step_costs.items():
                if target not in depths:
                    depths[target] = depth
                    labels[target] = _RELATION_HAS_PART
                    next_frontier.append(target)
                if cost < costs.get(target, math.inf):
                    costs[target] = cost
            frontier = next_frontier
        return _RelationExpansion(
            concept_ids=tuple(depths), depths=depths, costs=costs, labels=labels
        )

    def _expand_toc_child_concepts(
        self,
        expansion: _RelationExpansion,
        matches: Sequence[_TermMatch],
        seed_ids: Sequence[str],
        degraded: list[ModelAvailability],
    ) -> _RelationExpansion:
        """Add the concepts a seed's TOC child sections hold, where the model added none.

        The book states its own hierarchy.  ``人一生所必经的六个关口`` has six
        child sections in the parsed ``toc_nodes``, one per 关口, and the query
        ``人生六个关口是什么`` used to return exactly one span — the heading —
        because ``六个关口`` is a one-mention island with no relation of any
        predicate.  Structural provenance the parser read out of the EPUB is
        not something a model has to be asked for, and it is not something a
        model may overrule.

        It is also not allowed to compete with the semantic graph.  Three gates,
        in this order:

        * The seed must be **expansion-eligible** by the same rule the relation
          walk uses, so a concept the query only brushed cannot reach TOC
          structure either.
        * The seed must have **``HAS_PART`` out-degree 0**.  Where a model did
          decompose a concept, that decomposition is authoritative and this
          fallback stays out of the way entirely.  A repository that does not
          report the degree cannot answer the question, so it gets no TOC
          expansion rather than an assumed zero.
        * The seed must be **bound to exactly one TOC node**, and only concepts
          themselves fully bound inside that node's children are admitted.  The
          repository enforces both; see
          :meth:`SQLiteEpubStore.list_toc_child_concepts` for why an
          all-mentions-in-one-node rule and not a majority.

        Only child nodes, never siblings — measured on the acceptance book, a
        node's siblings hold a median of 10 bound concepts against a median of
        0 for its children, so siblings are association rather than
        decomposition.

        A child set larger than ``_MAX_TOC_CHILD_CONCEPTS`` is **skipped
        whole**, and the skip is reported as a degraded component.  Truncating
        it would be worse than not expanding: ``graph_total`` would come back a
        smaller number with nothing anywhere saying what had been dropped.

        The hop is priced exactly as a ``HAS_PART`` hop is —
        ``parent_cost + 1 + log2(children)`` over the concepts the hop actually
        reached — so these concepts drop straight into ``costs`` and are ordered
        by the one existing ranking, with no second ordering to keep in step.

        Nothing new reaches the store's occurrence queries but a longer tuple of
        concept ids: this method never touches passages, spans, or offsets, so
        the count and the pages still share their one predicate.
        """
        children_of = getattr(self._source, "list_toc_child_concepts", None)
        if children_of is None or not seed_ids:
            return expansion
        fanouts = {hit.term.concept_id: hit.term.has_part_fanout for hit in matches}
        undecomposed = [
            concept_id for concept_id in seed_ids if fanouts.get(concept_id) == 0
        ]
        if not undecomposed:
            return expansion
        by_seed: dict[str, list[str]] = {}
        for row in children_of(undecomposed):
            seed = row.get("seed_concept_id")
            child = row.get("concept_id")
            if isinstance(seed, str) and seed and isinstance(child, str) and child:
                by_seed.setdefault(seed, []).append(child)
        depths = dict(expansion.depths)
        costs = dict(expansion.costs)
        labels = dict(expansion.labels)
        for seed, children in by_seed.items():
            reached = tuple(dict.fromkeys(children))
            if len(reached) > _MAX_TOC_CHILD_CONCEPTS:
                degraded.append(
                    ModelAvailability.degraded(
                        "toc-child-expansion",
                        f"{len(reached)} concepts under one node exceeds the "
                        f"{_MAX_TOC_CHILD_CONCEPTS} budget; skipped whole",
                    )
                )
                continue
            depth = depths.get(seed, 0) + 1
            cost = costs.get(seed, 0.0) + 1.0 + math.log2(max(len(reached), 1))
            for child in reached:
                if child not in depths:
                    depths[child] = depth
                    labels[child] = _STRUCTURE_TOC_CHILD
                if cost < costs.get(child, math.inf):
                    costs[child] = cost
        return _RelationExpansion(
            concept_ids=tuple(depths), depths=depths, costs=costs, labels=labels
        )

    def _graph_page(
        self,
        expansion: _RelationExpansion,
        resolved_names: Sequence[str],
        *,
        offset: int,
        limit: int,
    ) -> tuple[SearchHit, ...]:
        """Read one page of the ranked graph channel."""
        if not expansion.concept_ids:
            return ()
        rows = self._source.list_concept_occurrences(
            expansion.concept_ids,
            offset=offset,
            limit=limit,
            concept_costs=expansion.costs,
        )
        return tuple(self._graph_hit(row, resolved_names, expansion) for row in rows)

    def _concept_matcher(self) -> ConceptTermMatcher:
        """Return the Tier-1 matcher, rebuilding it only when the vocabulary moved.

        The matcher used to be rebuilt for every request.  That was affordable
        on its own — a few milliseconds — but it is not affordable next to a
        segmenter, whose dictionary costs a third of a second to build, so both
        are now held on the service.

        Freshness is not traded away for it.  The key is a store fingerprint
        that changes on every write that could change what
        ``list_concept_terms`` returns — a merge, a split, an alias, an ingest
        — so an administrator who edits an alias sees the effect on the next
        query, not after a restart.  A repository that answers ``None`` is
        declaring it cannot make that promise, and is served a freshly built
        matcher every time rather than a possibly stale one.
        """
        fingerprint = self._source.concept_term_fingerprint()
        if (
            self._matcher is not None
            and fingerprint is not None
            and fingerprint == self._matcher_fingerprint
        ):
            return self._matcher
        matcher = ConceptTermMatcher(self._concept_terms())
        self._matcher = matcher
        self._matcher_fingerprint = fingerprint
        return matcher

    def _query_segmenter(self, degraded: list[ModelAvailability]) -> QuerySegmenter | None:
        """Return the query segmenter, loading it at most once per service.

        It is not keyed on the store fingerprint the matcher uses, and that is
        deliberate: the segmenter reads only the stock dictionary, so no write
        to this store can change what it returns.  Rebuilding it when an alias
        changes would pay a third of a second to obtain an identical object.

        An unavailable segmenter is a real reduction in precision — CJK terms
        fall back to matching anywhere they appear — so it is reported on every
        response that ran without one, not only on the request that discovered
        it was missing.
        """
        if not self._segmenter_loaded:
            self._segmenter, self._segmenter_reason = load_query_segmenter()
            self._segmenter_loaded = True
        if self._segmenter is None:
            degraded.append(
                ModelAvailability.degraded(
                    "query-segmenter", self._segmenter_reason or "not configured"
                )
            )
        return self._segmenter

    def _concept_terms(self) -> list[ConceptTerm]:
        """Read the vocabulary, keeping the specificity columns when offered.

        The three identity columns are required; the three specificity columns
        are read only if the repository supplied them, and a value of the wrong
        type is treated as absent rather than as an error.  That is what lets
        one search service run against both the SQLite store and a repository
        that answers the older three-column shape.
        """
        terms: list[ConceptTerm] = []
        for row in self._source.list_concept_terms():
            concept_id = row.get("concept_id")
            canonical = row.get("canonical_name")
            term = row.get("term")
            if all(isinstance(value, str) and value for value in (concept_id, canonical, term)):
                terms.append(
                    ConceptTerm(
                        concept_id,
                        canonical,
                        term,
                        term_source=_optional_string(row.get("term_source")),
                        mention_count=_optional_int(row.get("mention_count")),
                        has_part_fanout=_optional_int(row.get("has_part_fanout")),
                    )
                )
        return terms

    def _resolve_tier_two(
        self,
        query: str,
        matcher: ConceptTermMatcher,
        segmenter: QuerySegmenter | None,
        degraded: list[ModelAvailability],
    ) -> tuple[_TermMatch, ...]:
        if self._concept_resolver is None:
            degraded.append(ModelAvailability.degraded("local-concept-resolver", "not configured"))
            return ()
        availability = self._concept_resolver.availability()
        if not availability.available:
            degraded.append(availability)
            return ()
        candidates = tuple(dict.fromkeys(term.canonical_name for term in matcher.terms))
        if not candidates:
            return ()
        try:
            resolved = self._concept_resolver.resolve(query, candidates)
        except Exception as error:
            degraded.append(ModelAvailability.degraded("local-concept-resolver", _safe_reason(error)))
            return ()
        if not resolved:
            return ()
        # The local LLM can select only an existing canonical/alias term; it
        # never creates a new graph node or trusts generated free-form text.
        # The boundaries are the resolver's own answer segmented, not the
        # query's: the offsets being validated are offsets into ``resolved``.
        # A bare canonical name is one span from 0 to its length, and those two
        # endpoints are always boundaries, so re-validation still accepts it.
        # Spans, not bare terms, because the expansion guard measures how much
        # of the *answer* each winning term covered, exactly as it does for a
        # Tier-1 match.
        accepted = matcher.match_spans(resolved, boundaries=_boundaries(segmenter, resolved))
        if not accepted:
            degraded.append(
                ModelAvailability.degraded("local-concept-resolver", "returned an unknown concept")
            )
        return accepted

    def _graph_hit(
        self, row: Mapping[str, Any], resolved_names: Sequence[str], expansion: _RelationExpansion
    ) -> SearchHit:
        passage = self._passage_from_row(row)
        start = row.get("start_codepoint")
        end = row.get("end_codepoint")
        excerpt = _verified_excerpt(passage["content"], passage["content_sha256"], start, end)
        # A graph row is one distinct source span, not one mention: the store
        # collapses duplicate and nested spans, so a single row can carry every
        # concept that anchored on that span.
        names = _row_strings(row, "canonical_names")
        matched = names or tuple(resolved_names)
        concept_ids = _row_strings(row, "concept_ids")
        # A span that a directly matched concept anchored is a direct hit even
        # when it also absorbed an expansion-derived concept, so the shallowest
        # depth wins.  A span reached only by expansion keeps the provenance of
        # the edge that reached it, and the edge *kind* is part of that: a
        # semantic ``HAS_PART`` the model proposed and a structural TOC edge the
        # parser read out of the book are both one hop and are not the same
        # claim.  Where both explain the same span at the same depth, the
        # semantic edge is reported, because the model's decomposition is
        # authoritative wherever it exists.
        depths = [expansion.depths.get(concept_id, 0) for concept_id in concept_ids]
        relation_depth = min(depths) if depths else 0
        label = _best_label(expansion, concept_ids, relation_depth)
        provenance = ("graph",) if not relation_depth else ("graph", f"{label}:{relation_depth}")
        return SearchHit(
            passage_id=passage["passage_id"],
            book_title=passage["book_title"],
            toc_path=passage["toc_path"],
            content=passage["content"],
            content_sha256=passage["content_sha256"],
            matched_concepts=matched,
            provenance=provenance,
            excerpt=excerpt,
        )

    def _vector_candidates(
        self,
        query: str,
        *,
        candidate_limit: int,
        degraded: list[ModelAvailability],
    ) -> tuple[DerivedVectorRecord, ...] | None:
        """Read only source-validated local vector candidates once per query.

        The same immutable candidate set is used by the legacy vector channel
        and by the fused graph/vector channel.  A failure returns ``None`` so
        callers do not accidentally reinterpret an unavailable local model as
        an empty-but-successful result.
        """
        if self._vector_backend is None or self._embeddings is None or self._reranker is None:
            degraded.append(ModelAvailability.degraded("local-vector-search", "not fully configured"))
            return None
        embedding_availability = self._embeddings.availability()
        if not embedding_availability.available:
            degraded.append(embedding_availability)
            return None
        reranker_availability = self._reranker.availability()
        if not reranker_availability.available:
            degraded.append(reranker_availability)
            return None
        try:
            vectors = self._embeddings.embed([query])
            if len(vectors) != 1:
                raise SearchError("local embedding must return exactly one query vector")
            query_vector = _validated_vector(vectors[0])
            candidates = self._vector_backend.search(
                query_vector,
                embedding_profile=self._embeddings.profile,
                limit=candidate_limit,
            )
            if not candidates:
                return ()
            for candidate in candidates:
                if candidate.embedding_profile != self._embeddings.profile:
                    raise SearchError("vector backend returned a candidate from another embedding profile")
                # Validate query/index dimension compatibility even for a
                # backend that returned only one result (where MMR would not
                # otherwise calculate a cosine similarity).
                _cosine(query_vector, candidate.vector)
            # Validate windows before either channel sends their text to the
            # local Cross-Encoder.  This rejects a stale/tampered derived
            # vector record before it can influence any rank.
            for candidate in candidates:
                self._validated_candidate_window(candidate)
        except Exception as error:
            degraded.append(ModelAvailability.degraded("local-vector-search", _safe_reason(error)))
            return None
        return tuple(candidates)

    def _vector_hits(
        self,
        query: str,
        *,
        vector_candidates: tuple[DerivedVectorRecord, ...] | None,
        concept_ids: Sequence[str],
        result_limit: int,
        degraded: list[ModelAvailability],
    ) -> tuple[SearchHit, ...]:
        """Preserve the legacy vector-only ranking contract."""
        if not vector_candidates:
            return ()
        assert self._reranker is not None
        try:
            documents = [self._validated_candidate_window(candidate)["content"] for candidate in vector_candidates]
            scores = _validated_scores(self._reranker.score(query, documents), expected=len(vector_candidates))
            ranked = sorted(zip(vector_candidates, scores), key=lambda pair: pair[1], reverse=True)
            selected = _mmr_select(ranked, limit=result_limit, lambda_value=self._mmr_lambda)
        except Exception as error:
            degraded.append(ModelAvailability.degraded("local-vector-search", _safe_reason(error)))
            return ()

        results: list[SearchHit] = []
        rendered_passages: set[str] = set()
        for candidate, score in selected:
            if candidate.passage_id in rendered_passages:
                continue
            rendered_passages.add(candidate.passage_id)
            unit = self._validated_candidate_window(candidate)
            passage = self._search_passage(candidate.passage_id)
            excerpt = _verified_excerpt(
                passage["content"], passage["content_sha256"],
                unit["start_codepoint"], unit["end_codepoint"],
            )
            matched = tuple(self._source.matched_concept_names(candidate.passage_id, concept_ids))
            results.append(
                SearchHit(
                    passage_id=passage["passage_id"],
                    book_title=passage["book_title"],
                    toc_path=passage["toc_path"],
                    content=passage["content"],
                    content_sha256=passage["content_sha256"],
                    matched_concepts=matched,
                    provenance=("vector", "cross-encoder", "mmr"),
                    excerpt=excerpt,
                    score=float(score),
                )
            )
            if len(results) == result_limit:
                break
        return tuple(results)

    def _fused_hits(
        self,
        query: str,
        *,
        graph_results: Sequence[SearchHit],
        vector_candidates: tuple[DerivedVectorRecord, ...] | None,
        concept_ids: Sequence[str],
        result_limit: int,
        degraded: list[ModelAvailability],
    ) -> tuple[SearchHit, ...]:
        """Rank graph and vector candidates together with local models only.

        Graph matches are not merely prepended to semantic results: their
        precise excerpts enter the same private Cross-Encoder and MMR pass as
        verified vector windows.  If any local operation is unavailable or
        malformed, this new derived channel fails closed while the independent
        graph/vector response fields retain their normal compatibility.
        """
        if self._embeddings is None or self._reranker is None:
            # _vector_candidates already reported the configuration state.
            return ()
        if vector_candidates is None:
            return ()
        try:
            candidates = self._fused_candidates(graph_results, vector_candidates, concept_ids=concept_ids)
            if not candidates:
                return ()
            scores = _validated_scores(
                self._reranker.score(query, [candidate.hit.excerpt.content for candidate in candidates]),
                expected=len(candidates),
            )
            ranked = sorted(zip(candidates, scores), key=lambda pair: pair[1], reverse=True)
            selected = _mmr_select_fused(ranked, limit=result_limit, lambda_value=self._mmr_lambda)
        except Exception as error:
            degraded.append(ModelAvailability.degraded("local-fused-search", _safe_reason(error)))
            return ()

        results: list[SearchHit] = []
        rendered_passages: set[str] = set()
        for candidate, score in selected:
            # A result always cites one complete immutable parent passage.  We
            # do not render two windows from it as separate top-level answers.
            if candidate.hit.passage_id in rendered_passages:
                continue
            rendered_passages.add(candidate.hit.passage_id)
            results.append(
                SearchHit(
                    passage_id=candidate.hit.passage_id,
                    book_title=candidate.hit.book_title,
                    toc_path=candidate.hit.toc_path,
                    content=candidate.hit.content,
                    content_sha256=candidate.hit.content_sha256,
                    matched_concepts=candidate.hit.matched_concepts,
                    provenance=(*candidate.hit.provenance, "cross-encoder", "mmr", "fused"),
                    excerpt=candidate.hit.excerpt,
                    score=float(score),
                )
            )
            if len(results) == result_limit:
                break
        return tuple(results)

    def _fused_candidates(
        self,
        graph_results: Sequence[SearchHit],
        vector_candidates: Sequence[DerivedVectorRecord],
        *,
        concept_ids: Sequence[str],
    ) -> tuple[_FusedCandidate, ...]:
        """Deduplicate identical exact excerpts before locally ranking them."""
        vector_by_excerpt: dict[tuple[str, int, int], _FusedCandidate] = {}
        for vector_candidate in vector_candidates:
            unit = self._validated_candidate_window(vector_candidate)
            passage = self._search_passage(vector_candidate.passage_id)
            excerpt = _verified_excerpt(
                passage["content"], passage["content_sha256"],
                unit["start_codepoint"], unit["end_codepoint"],
            )
            hit = SearchHit(
                passage_id=passage["passage_id"],
                book_title=passage["book_title"],
                toc_path=passage["toc_path"],
                content=passage["content"],
                content_sha256=passage["content_sha256"],
                matched_concepts=tuple(
                    self._source.matched_concept_names(vector_candidate.passage_id, concept_ids)
                ),
                provenance=("vector",),
                excerpt=excerpt,
            )
            vector_by_excerpt[(hit.passage_id, excerpt.start_codepoint, excerpt.end_codepoint)] = _FusedCandidate(
                hit=hit, vector=_validated_vector(vector_candidate.vector)
            )

        # Preserve graph ordering as a stable tie-breaker, then append vector
        # candidates that do not share the same exact source excerpt.
        merged: list[_FusedCandidate] = []
        seen: set[tuple[str, int, int]] = set()
        graph_without_vector: list[SearchHit] = []
        for graph_hit in graph_results:
            key = (graph_hit.passage_id, graph_hit.excerpt.start_codepoint, graph_hit.excerpt.end_codepoint)
            vector_match = vector_by_excerpt.get(key)
            if vector_match is None:
                graph_without_vector.append(graph_hit)
                continue
            seen.add(key)
            merged.append(
                _FusedCandidate(
                    hit=SearchHit(
                        passage_id=graph_hit.passage_id,
                        book_title=graph_hit.book_title,
                        toc_path=graph_hit.toc_path,
                        content=graph_hit.content,
                        content_sha256=graph_hit.content_sha256,
                        matched_concepts=_merged_strings(
                            graph_hit.matched_concepts, vector_match.hit.matched_concepts
                        ),
                        provenance=_merged_provenance(graph_hit.provenance, vector_match.hit.provenance),
                        excerpt=graph_hit.excerpt,
                    ),
                    vector=vector_match.vector,
                )
            )

        if graph_without_vector:
            # Embeddings are calculated only for graph excerpts that have no
            # already-indexed vector.  This is entirely local/private and lets
            # MMR compare both retrieval channels in the same vector space.
            graph_vectors = self._embeddings.embed([hit.excerpt.content for hit in graph_without_vector])
            if len(graph_vectors) != len(graph_without_vector):
                raise SearchError("local embedding must score every graph candidate")
            for graph_hit, graph_vector in zip(graph_without_vector, graph_vectors):
                graph_vector = _validated_vector(graph_vector)
                # Enforce a single embedding space before MMR; vectors from
                # indexed candidates have already been profile-validated.
                if vector_by_excerpt:
                    _cosine(graph_vector, next(iter(vector_by_excerpt.values())).vector)
                merged.append(_FusedCandidate(hit=graph_hit, vector=graph_vector))

        for key, vector_candidate in vector_by_excerpt.items():
            if key not in seen:
                merged.append(vector_candidate)
        return tuple(merged)

    def _validated_candidate_window(self, candidate: DerivedVectorRecord) -> Mapping[str, Any]:
        unit = self._source.get_retrieval_unit(candidate.retrieval_unit_id)
        if unit is None:
            raise SearchError("vector candidate refers to a missing retrieval unit")
        if (
            unit.get("passage_id") != candidate.passage_id
            or unit.get("content_sha256") != candidate.content_sha256
            or unit.get("start_codepoint") != candidate.start_codepoint
            or unit.get("end_codepoint") != candidate.end_codepoint
        ):
            raise SearchError("vector candidate does not match its derived source record")
        passage = self._search_passage(candidate.passage_id)
        excerpt = _verified_excerpt(
            passage["content"], passage["content_sha256"],
            candidate.start_codepoint,
            candidate.end_codepoint,
        )
        if unit.get("content") != excerpt.content:
            raise SearchError("retrieval-unit text does not equal its source excerpt")
        return unit

    def _search_passage(self, passage_id: str) -> dict[str, Any]:
        row = self._source.get_search_passage(passage_id)
        if row is None:
            raise SearchError("search result refers to a missing passage")
        return self._passage_from_row(row)

    @staticmethod
    def _passage_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
        passage_id = row.get("passage_id")
        title = row.get("book_title")
        content = row.get("content")
        content_sha256 = row.get("content_sha256")
        raw_path = row.get("toc_path", ())
        if not all(isinstance(value, str) and value for value in (passage_id, title, content, content_sha256)):
            raise SearchError("search passage has invalid mandatory source fields")
        if not isinstance(raw_path, (tuple, list)) or any(not isinstance(item, str) for item in raw_path):
            raise SearchError("search passage has an invalid TOC path")
        if _hash_text(content) != content_sha256:
            raise SearchError("search passage source hash does not match its content")
        return {
            "passage_id": passage_id,
            "book_title": title,
            "toc_path": tuple(raw_path),
            "content": content,
            "content_sha256": content_sha256,
        }


def _verified_excerpt(
    content: str, content_sha256: str, start: Any, end: Any
) -> SearchExcerpt:
    if _hash_text(content) != content_sha256:
        raise SearchError("source content hash does not match returned passage")
    if start is None and end is None:
        return SearchExcerpt(content=content, start_codepoint=0, end_codepoint=len(content))
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start or end > len(content):
        raise SearchError("excerpt offsets must identify a non-empty source substring")
    excerpt = content[start:end]
    if excerpt != content[start:end]:  # Retained as an explicit contract guard.
        raise SearchError("excerpt must be a continuous source substring")
    return SearchExcerpt(content=excerpt, start_codepoint=start, end_codepoint=end)


def _mmr_select(
    ranked: Sequence[tuple[DerivedVectorRecord, float]], *, limit: int, lambda_value: float
) -> list[tuple[DerivedVectorRecord, float]]:
    """Select reranked candidates with maximum marginal relevance.

    The reranker supplies relevance; cosine similarity between local embeddings
    supplies the diversity penalty.  Stable tie-breaking keeps results
    deterministic for a fixed backend order.
    """
    remaining = list(ranked)
    selected: list[tuple[DerivedVectorRecord, float]] = []
    while remaining and len(selected) < limit:
        best_index = 0
        best_mmr = -math.inf
        for index, (candidate, relevance) in enumerate(remaining):
            diversity = max(
                (_cosine(candidate.vector, existing.vector) for existing, _ in selected), default=0.0
            )
            score = lambda_value * relevance - (1.0 - lambda_value) * diversity
            if score > best_mmr:
                best_index, best_mmr = index, score
        selected.append(remaining.pop(best_index))
    return selected


def _mmr_select_fused(
    ranked: Sequence[tuple[_FusedCandidate, float]], *, limit: int, lambda_value: float
) -> list[tuple[_FusedCandidate, float]]:
    """MMR for candidates whose vectors may come from either retrieval path."""
    remaining = list(ranked)
    selected: list[tuple[_FusedCandidate, float]] = []
    while remaining and len(selected) < limit:
        best_index: int | None = None
        best_mmr = -math.inf
        for index, (candidate, relevance) in enumerate(remaining):
            if any(existing.hit.passage_id == candidate.hit.passage_id for existing, _ in selected):
                continue
            diversity = max(
                (_cosine(candidate.vector, existing.vector) for existing, _ in selected), default=0.0
            )
            score = lambda_value * relevance - (1.0 - lambda_value) * diversity
            if score > best_mmr:
                best_index, best_mmr = index, score
        if best_index is None:
            break
        selected.append(remaining.pop(best_index))
    return selected


def _ascii_word(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character == "_")


def _boundaries(segmenter: QuerySegmenter | None, text: str) -> TokenBoundaries | None:
    """Word boundaries for one text, or ``None`` when there is no segmenter.

    A segmenter that raises is treated as absent for this text rather than
    failing the search: the fallback is a broader match over the same immutable
    source, and the missing-segmenter degradation is already reported.
    """
    if segmenter is None:
        return None
    try:
        return segmenter.boundaries(text)
    except Exception:
        return None


def _is_cjk(character: str) -> bool:
    """Whether this character belongs to a script written without word spaces.

    Deliberately narrow: only Han ideographs.  The token-boundary rule exists
    because such a script gives a substring search nothing to anchor on, and
    applying it to punctuation or to Latin-with-symbols terms (``TCP/IP``)
    would reject matches the Latin rule already handles correctly.
    """
    code = ord(character)
    return (
        0x3400 <= code <= 0x4DBF  # CJK Unified Ideographs Extension A
        or 0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0xF900 <= code <= 0xFAFF  # CJK Compatibility Ideographs
        or 0x20000 <= code <= 0x3134F  # Extensions B onwards
    )


def _strictly_contained(hit: _TermMatch, hits: Sequence[_TermMatch]) -> bool:
    """Whether a longer hit wholly covers this one.

    Equal spans are not contained, so two aliases that matched the identical
    characters both survive; this mirrors the containment rule the store uses
    when it collapses overlapping source spans, so recall and citation agree
    about what "the same piece of text" means.
    """
    length = hit.end - hit.start
    return any(
        other.start <= hit.start and hit.end <= other.end and (other.end - other.start) > length
        for other in hits
    )


def _validated_vector(vector: Sequence[float]) -> tuple[float, ...]:
    if not vector:
        raise SearchError("local embedding returned an empty query vector")
    result: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SearchError("local embedding returned a non-finite query vector")
        result.append(float(value))
    return tuple(result)


def _validated_scores(scores: Sequence[float], *, expected: int) -> tuple[float, ...]:
    if len(scores) != expected:
        raise SearchError("local reranker must score every candidate")
    result: list[float] = []
    for score in scores:
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise SearchError("local reranker returned a non-finite score")
        result.append(float(score))
    return tuple(result)


def _optional_string(value: Any) -> str | None:
    """A repository column that is text when present and absent otherwise."""
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    """A repository column that is a count when present and absent otherwise.

    ``bool`` is excluded on purpose: it is an ``int`` in Python, and a
    repository answering ``True`` for a count has answered nothing.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _best_label(
    expansion: _RelationExpansion, concept_ids: Sequence[str], depth: int
) -> str:
    """Which kind of edge explains a span reached at ``depth``.

    Only the concepts that were actually reached at that depth get a say, so a
    span carrying both a one-hop concept and a two-hop one is described by the
    one-hop edge.  Among those, ``_LABEL_PRIORITY`` decides, and it is an
    explicit order rather than whatever ``sorted`` would do to the strings:
    a semantic relation the model asserted outranks a structural TOC edge, and
    that must stay true if either label is ever renamed.
    """
    candidates = {
        expansion.labels[concept_id]
        for concept_id in concept_ids
        if expansion.depths.get(concept_id) == depth and concept_id in expansion.labels
    }
    for label in _LABEL_PRIORITY:
        if label in candidates:
            return label
    return _RELATION_HAS_PART


def _row_strings(row: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Read a repository row's list-valued column of non-empty strings."""
    value = row.get(key)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _merged_strings(*values: Sequence[str]) -> tuple[str, ...]:
    """Stable deduplication for canonical concept labels or provenance labels."""
    return tuple(dict.fromkeys(item for value in values for item in value))


def _merged_provenance(graph: Sequence[str], vector: Sequence[str]) -> tuple[str, ...]:
    """Identify both retrieval origins before local rank-stage provenance."""
    return _merged_strings(graph, vector)


def _mark_vector_results_fused(results: Sequence[SearchHit]) -> tuple[SearchHit, ...]:
    """Avoid a duplicate local rerank when there is no graph candidate.

    With no graph result, the vector-only Cross-Encoder/MMR result is exactly
    the fused candidate set.  The marker makes that derivation explicit while
    retaining one local model invocation and stable legacy vector behavior.
    """
    return tuple(
        SearchHit(
            passage_id=hit.passage_id,
            book_title=hit.book_title,
            toc_path=hit.toc_path,
            content=hit.content,
            content_sha256=hit.content_sha256,
            matched_concepts=hit.matched_concepts,
            provenance=(*hit.provenance, "fused"),
            excerpt=hit.excerpt,
            score=hit.score,
        )
        for hit in results
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise SearchError("vector candidate dimensions do not match query/index dimensions")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _hash_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _safe_reason(error: Exception) -> str:
    return (str(error).strip() or type(error).__name__)[:240]
