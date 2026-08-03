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
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .inference import ConceptResolver, EmbeddingService, ModelAvailability, RerankerService
from .vector_index import DerivedVectorRecord


class SearchError(ValueError):
    """The caller supplied an invalid search request or source invariant failed."""


@dataclass(frozen=True, slots=True)
class ConceptTerm:
    """One canonical concept/alias term available to the Tier-1 matcher."""

    concept_id: str
    canonical_name: str
    term: str


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
    degraded: tuple[ModelAvailability, ...]


class EpubSearchRepository(Protocol):
    """Canonical-store read surface used by search, independent of SQLite."""

    def list_concept_terms(self) -> list[Mapping[str, Any]]: ...

    def count_concept_occurrences(self, concept_ids: Sequence[str]) -> int: ...

    def list_concept_occurrences(
        self, concept_ids: Sequence[str], *, offset: int, limit: int
    ) -> list[Mapping[str, Any]]: ...

    def get_search_passage(self, passage_id: str) -> Mapping[str, Any] | None: ...

    def get_retrieval_unit(self, retrieval_unit_id: str) -> Mapping[str, Any] | None: ...

    def matched_concept_names(
        self, passage_id: str, concept_ids: Sequence[str]
    ) -> list[str]: ...


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


class ConceptTermMatcher:
    """In-memory multi-pattern matcher with Latin-token boundary semantics.

    It is a trie rather than a dependency on a particular Aho-Corasick package:
    all aliases are scanned in one pass, which is equivalent for the search
    contract and keeps desktop deployments dependency-light.  CJK phrases are
    matched directly; terms starting/ending in ASCII word characters require a
    corresponding token boundary.
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

    def match(self, text: str) -> tuple[ConceptTerm, ...]:
        if not text:
            return ()
        folded = text.casefold()
        matched: dict[str, ConceptTerm] = {}
        node = self._root
        for end, character in enumerate(folded, start=1):
            while node is not self._root and character not in node.children:
                assert node.failure is not None
                node = node.failure
            node = node.children.get(character, self._root)
            for term in node.terms:
                term_folded = term.term.casefold()
                start = end - len(term_folded)
                if start >= 0 and self._valid_boundaries(folded, start, end, term_folded):
                    matched.setdefault(term.concept_id, term)
        return tuple(matched.values())

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
    def _valid_boundaries(text: str, start: int, end: int, term: str) -> bool:
        # Only Latin-style terms require boundaries.  CJK terms intentionally
        # match directly inside natural-language queries.
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

    def search(
        self,
        query: str,
        *,
        graph_offset: int = 0,
        graph_limit: int = 20,
        vector_limit: int = 10,
        vector_candidate_limit: int = 50,
    ) -> SearchResponse:
        if not isinstance(query, str) or not query.strip():
            raise SearchError("query must be non-empty text")
        if graph_offset < 0 or graph_limit < 1 or vector_limit < 1 or vector_candidate_limit < vector_limit:
            raise SearchError("search pagination limits are invalid")

        terms = self._concept_terms()
        matcher = ConceptTermMatcher(terms)
        matched = matcher.match(query)
        degraded: list[ModelAvailability] = []
        if not matched:
            matched = self._resolve_tier_two(query, matcher, degraded)
        concept_ids = tuple(term.concept_id for term in matched)
        resolved_names = tuple(dict.fromkeys(term.canonical_name for term in matched))

        graph_total = self._source.count_concept_occurrences(concept_ids) if concept_ids else 0
        graph_rows = (
            self._source.list_concept_occurrences(concept_ids, offset=graph_offset, limit=graph_limit)
            if concept_ids
            else []
        )
        graph_results = tuple(
            self._graph_hit(row, resolved_names) for row in graph_rows
        )
        vector_results = self._vector_hits(
            query,
            concept_ids=concept_ids,
            candidate_limit=vector_candidate_limit,
            result_limit=vector_limit,
            degraded=degraded,
        )
        return SearchResponse(
            query=query,
            resolved_concepts=resolved_names,
            graph_total=graph_total,
            graph_offset=graph_offset,
            graph_results=graph_results,
            vector_results=vector_results,
            degraded=tuple(degraded),
        )

    def _concept_terms(self) -> list[ConceptTerm]:
        terms: list[ConceptTerm] = []
        for row in self._source.list_concept_terms():
            concept_id = row.get("concept_id")
            canonical = row.get("canonical_name")
            term = row.get("term")
            if all(isinstance(value, str) and value for value in (concept_id, canonical, term)):
                terms.append(ConceptTerm(concept_id, canonical, term))
        return terms

    def _resolve_tier_two(
        self,
        query: str,
        matcher: ConceptTermMatcher,
        degraded: list[ModelAvailability],
    ) -> tuple[ConceptTerm, ...]:
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
        accepted = matcher.match(resolved)
        if not accepted:
            degraded.append(
                ModelAvailability.degraded("local-concept-resolver", "returned an unknown concept")
            )
        return accepted

    def _graph_hit(self, row: Mapping[str, Any], resolved_names: Sequence[str]) -> SearchHit:
        passage = self._passage_from_row(row)
        start = row.get("start_codepoint")
        end = row.get("end_codepoint")
        excerpt = _verified_excerpt(passage["content"], passage["content_sha256"], start, end)
        name = row.get("canonical_name")
        matched = (str(name),) if isinstance(name, str) and name else tuple(resolved_names)
        return SearchHit(
            passage_id=passage["passage_id"],
            book_title=passage["book_title"],
            toc_path=passage["toc_path"],
            content=passage["content"],
            content_sha256=passage["content_sha256"],
            matched_concepts=matched,
            provenance=("graph",),
            excerpt=excerpt,
        )

    def _vector_hits(
        self,
        query: str,
        *,
        concept_ids: Sequence[str],
        candidate_limit: int,
        result_limit: int,
        degraded: list[ModelAvailability],
    ) -> tuple[SearchHit, ...]:
        if self._vector_backend is None or self._embeddings is None or self._reranker is None:
            degraded.append(ModelAvailability.degraded("local-vector-search", "not fully configured"))
            return ()
        embedding_availability = self._embeddings.availability()
        if not embedding_availability.available:
            degraded.append(embedding_availability)
            return ()
        reranker_availability = self._reranker.availability()
        if not reranker_availability.available:
            degraded.append(reranker_availability)
            return ()
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
            documents = [self._validated_candidate_window(candidate)["content"] for candidate in candidates]
            scores = self._reranker.score(query, documents)
            if len(scores) != len(candidates):
                raise SearchError("local reranker must score every vector candidate")
            ranked = sorted(
                zip(candidates, scores), key=lambda pair: pair[1], reverse=True
            )
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


def _ascii_word(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character == "_")


def _validated_vector(vector: Sequence[float]) -> tuple[float, ...]:
    if not vector:
        raise SearchError("local embedding returned an empty query vector")
    result: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SearchError("local embedding returned a non-finite query vector")
        result.append(float(value))
    return tuple(result)


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
