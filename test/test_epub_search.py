"""Acceptance tests for faithful local-only EPUB concept search."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import math
from pathlib import Path
import sys
import types
import unittest


EPUB_DIR = Path(__file__).resolve().parents[1] / "backend/open_webui/retrieval/epub"
PACKAGE_NAME = "epub_search_sdd_test_package"
PACKAGE = types.ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(EPUB_DIR)]  # type: ignore[attr-defined]
sys.modules[PACKAGE_NAME] = PACKAGE


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.{name}", EPUB_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INFERENCE = _load("inference")
VECTOR_INDEX = _load("vector_index")
SEGMENTATION = _load("segmentation")
SEARCH = _load("search")

ConceptTerm = SEARCH.ConceptTerm
ConceptTermMatcher = SEARCH.ConceptTermMatcher
EpubSearchService = SEARCH.EpubSearchService
ModelAvailability = INFERENCE.ModelAvailability
DerivedVectorRecord = VECTOR_INDEX.DerivedVectorRecord
TokenBoundaries = SEGMENTATION.TokenBoundaries
load_query_segmenter = SEGMENTATION.load_query_segmenter

SEGMENTER, SEGMENTER_REASON = load_query_segmenter()


def _boundaries(text: str):
    """Real segmentation for ``text``, so the tests pin the shipped tokenizer.

    A stub would let the boundary rule pass while the tokenizer this actually
    ships with disagreed about where `枢对锚站的校验的规律` breaks, which is the
    only thing that decides whether the fix works.
    """
    assert SEGMENTER is not None
    return SEGMENTER.boundaries(text)


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _span_length(span) -> int:
    """An unanchored mention is not a span, so it has no length to rank by."""
    if span["start_codepoint"] is None or span["end_codepoint"] is None:
        return 0
    return int(span["end_codepoint"]) - int(span["start_codepoint"])


def _contains(start, end, other_start, other_end) -> bool:
    """Whether ``[start, end)`` covers ``[other_start, other_end)``; equal counts."""
    if start is None or end is None:
        return other_start is None or other_end is None
    if other_start is None or other_end is None:
        return False
    return other_start >= start and other_end <= end


class FakeSource:
    def __init__(self) -> None:
        self.passages = {
            "p1": self._passage("p1", "第一章", "TCP 是传输控制协议。原文完整保留。"),
            "p2": self._passage("p2", "第二章", "HTTP 与 TCP 不同。第二段完整保留。"),
            "p3": self._passage("p3", "第三章", "概念检索也应返回完整原文段落。"),
        }
        self.terms = [
            {"concept_id": "tcp", "canonical_name": "TCP", "term": "TCP"},
            {"concept_id": "tcp", "canonical_name": "TCP", "term": "Transmission Control Protocol"},
            {"concept_id": "中文", "canonical_name": "检索", "term": "检索"},
            {"concept_id": "parent", "canonical_name": "父主题", "term": "父主题"},
            {"concept_id": "child", "canonical_name": "子主题", "term": "子主题"},
        ]
        self.occurrences = [
            {**self.passages["p1"], "concept_id": "tcp", "canonical_name": "TCP", "start_codepoint": 0, "end_codepoint": 3},
            {**self.passages["p2"], "concept_id": "tcp", "canonical_name": "TCP", "start_codepoint": 7, "end_codepoint": 10},
            {**self.passages["p2"], "concept_id": "tcp", "canonical_name": "TCP", "start_codepoint": None, "end_codepoint": None},
            {**self.passages["p1"], "concept_id": "parent", "canonical_name": "父主题", "start_codepoint": 0, "end_codepoint": 3},
            {**self.passages["p3"], "concept_id": "child", "canonical_name": "子主题", "start_codepoint": 0, "end_codepoint": 2},
        ]
        self.relations = [{"subject_concept_id": "parent", "predicate": "HAS_PART", "object_concept_id": "child"}]
        self.units = {
            "u1": self._unit("u1", "p1", 0, 8),
            "u2": self._unit("u2", "p2", 0, 9),
            "u3": self._unit("u3", "p3", 0, 8),
        }
        # Every ``list_concept_occurrences`` call, so a test can assert what
        # relevance the service declared as well as what came back.
        self.occurrence_calls: list[dict[str, object]] = []
        # How often the vocabulary was actually re-read, so a test can tell a
        # reused matcher from a rebuilt one without timing anything.
        self.term_reads = 0

    @staticmethod
    def _passage(passage_id: str, chapter: str, content: str) -> dict[str, object]:
        return {
            "passage_id": passage_id,
            "book_title": "网络原理",
            "toc_path": (chapter,),
            "content": content,
            "content_sha256": _hash(content),
        }

    def _unit(self, unit_id: str, passage_id: str, start: int, end: int) -> dict[str, object]:
        content = str(self.passages[passage_id]["content"])[start:end]
        return {
            "retrieval_unit_id": unit_id,
            "passage_id": passage_id,
            "start_codepoint": start,
            "end_codepoint": end,
            "content": content,
            "content_sha256": _hash(content),
        }

    def list_concept_terms(self):
        self.term_reads += 1
        return self.terms

    def concept_term_fingerprint(self):
        """Stand in for the store's aggregate: any change to the vocabulary moves it."""
        return (len(self.terms), tuple(sorted(term["term"] for term in self.terms)))

    def _occurrence_spans(self, concept_ids, concept_costs=None):
        """Mirror the store contract: one row per distinct surviving source span.

        The graph channel enumerates distinct source spans, not mention rows,
        so exact duplicates collapse and a span wholly inside another span of
        the same passage is dropped in favour of its container.  The surviving
        row carries every concept that anchored on it.  Count and page are both
        derived from this one list, exactly as the SQL store derives them from
        one shared predicate.

        The list is then ordered exactly as the store's ``ORDER BY`` does:
        cheapest relation cost, then most queried concepts on the span, then
        longest span, then book order — which is this fixture's occurrence
        order.  Ranking is applied to the whole list before it is sliced, so a
        page here means what a page means in SQL.  ``concept_costs`` never
        reaches the count, because ordering cannot change how many spans exist.
        """
        rows = [row for row in self.occurrences if row["concept_id"] in concept_ids]
        spans = []
        for passage_id, start, end in dict.fromkeys(
            (row["passage_id"], row["start_codepoint"], row["end_codepoint"]) for row in rows
        ):
            contained_by_another = any(
                other["passage_id"] == passage_id
                and other["start_codepoint"] is not None
                and start is not None
                and other["start_codepoint"] <= start
                and other["end_codepoint"] >= end
                and (other["start_codepoint"] < start or other["end_codepoint"] > end)
                for other in rows
            )
            if contained_by_another:
                continue
            attributed = [
                row
                for row in rows
                if row["passage_id"] == passage_id
                and _contains(start, end, row["start_codepoint"], row["end_codepoint"])
            ]
            spans.append(
                {
                    **self.passages[passage_id],
                    "start_codepoint": start,
                    "end_codepoint": end,
                    "concept_ids": tuple(sorted({row["concept_id"] for row in attributed})),
                    "canonical_names": tuple(sorted({row["canonical_name"] for row in attributed})),
                }
            )
        costs = concept_costs or {}
        spans.sort(
            key=lambda span: (
                min((costs.get(concept, 0.0) for concept in span["concept_ids"]), default=0.0),
                -len(span["concept_ids"]),
                -_span_length(span),
            )
        )
        return spans

    def count_concept_occurrences(self, concept_ids):
        return len(self._occurrence_spans(concept_ids))

    def list_concept_occurrences(self, concept_ids, *, offset, limit, concept_costs=None):
        self.occurrence_calls.append(
            {"concept_ids": tuple(concept_ids), "offset": offset, "limit": limit, "costs": dict(concept_costs or {})}
        )
        return self._occurrence_spans(concept_ids, concept_costs)[offset : offset + limit]

    def get_search_passage(self, passage_id):
        return self.passages.get(passage_id)

    def get_retrieval_unit(self, retrieval_unit_id):
        return self.units.get(retrieval_unit_id)

    def matched_concept_names(self, passage_id, concept_ids):
        return ["TCP"] if passage_id in {"p1", "p2"} and "tcp" in concept_ids else []

    def list_concept_relation_neighbors(self, concept_ids, *, predicates=("HAS_PART",)):
        return [
            relation
            for relation in self.relations
            if relation["subject_concept_id"] in concept_ids and relation["predicate"] in predicates
        ]


class HubFakeSource(FakeSource):
    """The acceptance book's shape, reduced to what ranking has to get right.

    Three things about that book matter here.  It opens with front matter, so
    the earliest span in the book is a bare `枢` in a preface sentence about
    nothing.  `全域潮汐枢纽` legitimately has 20 grounded `HAS_PART` children,
    so expanding through it drags in spans that have no particular connection
    to the query — that hub is why `枢纽的权重` reaches 778 spans.  And `锚站`
    has a single child, so expanding through it says something.

    The queries here name the hub by its canonical `全域潮汐枢纽` rather than
    leaning on its one-character `枢` alias.  That alias is still in the
    fixture and still anchors the preface span, but whether it *resolves* from
    a given query is now a question about that query's word boundaries — and
    these tests are about ranking, which must not be hostage to how a tokenizer
    happens to break one phrase.  In book order the preface span still comes
    first; that is the reported symptom, and it was never arbitrary — it was
    the front of the book.
    """

    PREFACE = "前言：本册所收录的是枢的运行记录选编，供人查阅。"
    FLOOD = "枢在汛期观测，通知值守员建锚站，以保全流量记录的完整。"
    JUDGMENT = "汛期观测之后的水位与浮标阵列读数都得以留存，为下一轮标定作了铺垫。"
    HUB_CHILD = "枢纽的基准线由长期观测归算得到，只能从历次校验的残差中读出它的稳定程度。"
    ARK_CHILD = "锚站建成之后，枢即调度值守员将浮标阵列布放到位。"

    @staticmethod
    def _passage(passage_id: str, chapter: str, content: str) -> dict[str, object]:
        return {
            "passage_id": passage_id,
            "book_title": "潮汐观测总志",
            "toc_path": (chapter,),
            "content": content,
            "content_sha256": _hash(content),
        }

    def __init__(self) -> None:
        super().__init__()
        self.passages = {
            "preface": self._passage("preface", "前言", self.PREFACE),
            "flood": self._passage("flood", "第四章", self.FLOOD),
            "judgment": self._passage("judgment", "第四章", self.JUDGMENT),
            "righteousness": self._passage("righteousness", "第九章", self.HUB_CHILD),
            "ark": self._passage("ark", "第四章", self.ARK_CHILD),
        }
        self.terms = [
            {"concept_id": "hub", "canonical_name": "全域潮汐枢纽", "term": "全域潮汐枢纽"},
            {"concept_id": "hub", "canonical_name": "全域潮汐枢纽", "term": "枢"},
            {"concept_id": "flood", "canonical_name": "汛期观测", "term": "汛期观测"},
            {"concept_id": "ark", "canonical_name": "锚站", "term": "锚站"},
        ]
        # Book order, deliberately opening with the preface: the front-matter
        # span is the earliest mention of the hub in the whole book.
        self.occurrences = [
            {**self.passages["preface"], "concept_id": "hub", "canonical_name": "全域潮汐枢纽",
             "start_codepoint": 10, "end_codepoint": 11},
            {**self.passages["flood"], "concept_id": "flood", "canonical_name": "汛期观测",
             "start_codepoint": 0, "end_codepoint": 15},
            {**self.passages["flood"], "concept_id": "hub", "canonical_name": "全域潮汐枢纽",
             "start_codepoint": 0, "end_codepoint": 15},
            {**self.passages["judgment"], "concept_id": "flood", "canonical_name": "汛期观测",
             "start_codepoint": 0, "end_codepoint": 13},
            # Reached only by expanding out of the hub, yet earlier in the book
            # than nothing and longer than the flood span.  Book order and span
            # length both favour it; relation cost must not.
            {**self.passages["righteousness"], "concept_id": "righteousness",
             "canonical_name": "枢纽的基准线", "start_codepoint": 0, "end_codepoint": 35},
            {**self.passages["ark"], "concept_id": "ark_animals", "canonical_name": "浮标阵列",
             "start_codepoint": 0, "end_codepoint": 23},
        ]
        # 20 children under the hub, one child under `锚站`: one hop out of the
        # hub is worth far less than one hop out of a concept that names a
        # single part.
        self.relations = [
            {"subject_concept_id": "hub", "predicate": "HAS_PART", "object_concept_id": name}
            for name in ("righteousness", *(f"hub-child-{index}" for index in range(19)))
        ] + [{"subject_concept_id": "ark", "predicate": "HAS_PART", "object_concept_id": "ark_animals"}]
        self.units = {}

    def matched_concept_names(self, passage_id, concept_ids):
        return sorted(
            {
                row["canonical_name"]
                for row in self.occurrences
                if row["passage_id"] == passage_id and row["concept_id"] in concept_ids
            }
        )


class FakeEmbeddings:
    profile = "private-embed-v1"

    def __init__(self, *, available: bool = True):
        self.available = available
        self.calls: list[list[str]] = []

    def availability(self):
        return (
            ModelAvailability.ready("local-embedding")
            if self.available
            else ModelAvailability.degraded("local-embedding", "runtime stopped")
        )

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0]]


class FakeReranker:
    profile = "private-rerank-v1"

    def __init__(self, *, available: bool = True):
        self.available = available
        self.calls: list[tuple[str, list[str]]] = []

    def availability(self):
        return (
            ModelAvailability.ready("local-reranker")
            if self.available
            else ModelAvailability.degraded("local-reranker", "runtime stopped")
        )

    def score(self, query, documents):
        self.calls.append((query, list(documents)))
        return [0.80, 0.95, 0.70][: len(documents)]


class FusionEmbeddings(FakeEmbeddings):
    """Return one query vector and deterministic vectors for graph excerpts."""

    def embed(self, texts):
        self.calls.append(list(texts))
        if len(texts) == 1 and texts[0] == "TCP":
            return [[1.0, 0.0]]
        return [[0.0, 1.0] for _ in texts]


class FusionReranker(FakeReranker):
    def score(self, query, documents):
        self.calls.append((query, list(documents)))
        # The first call serves the legacy vector channel.  The second call
        # must contain candidates from both retrieval channels.
        return [0.40] if len(documents) == 1 else [0.95, 0.80][: len(documents)]


class FakeResolver:
    profile = "private-resolver-v1"

    def __init__(self, result: str | None, *, available: bool = True):
        self.result = result
        self.available = available
        self.calls: list[tuple[str, list[str]]] = []

    def availability(self):
        return (
            ModelAvailability.ready("local-concept-resolver")
            if self.available
            else ModelAvailability.degraded("local-concept-resolver", "runtime stopped")
        )

    def resolve(self, query, candidates):
        self.calls.append((query, list(candidates)))
        return self.result


class FakeVectorBackend:
    def __init__(self, records):
        self.records = list(records)
        self.calls: list[tuple[tuple[float, ...], str, int]] = []

    def search(self, query_vector, *, embedding_profile, limit):
        self.calls.append((tuple(query_vector), embedding_profile, limit))
        return self.records[:limit]


def _record(source: FakeSource, unit_id: str, vector: tuple[float, ...]) -> object:
    unit = source.units[unit_id]
    return DerivedVectorRecord(
        retrieval_unit_id=unit_id,
        passage_id=str(unit["passage_id"]),
        start_codepoint=int(unit["start_codepoint"]),
        end_codepoint=int(unit["end_codepoint"]),
        content_sha256=str(unit["content_sha256"]),
        embedding_profile="private-embed-v1",
        vector=vector,
    )


class EpubSearchTest(unittest.TestCase):
    @unittest.skipIf(SEGMENTER is None, f"local query segmenter unavailable: {SEGMENTER_REASON}")
    def test_tier_one_trie_uses_latin_boundaries_and_token_aligned_cjk_matching(self) -> None:
        """A CJK term must sit on the query's own word boundaries, not anywhere.

        The Latin rule is unchanged.  What changes is that a CJK term no longer
        matches merely because its characters appear: `律` is a real concept
        name and it is *inside* `规律`, which is not a mention of it.  A term
        may still span several tokens — `枢对锚站的校验` covers four — because
        the query is segmented to supply boundaries, never to supply patterns.
        """
        matcher = ConceptTermMatcher(
            [
                ConceptTerm("tcp", "TCP", "TCP"),
                ConceptTerm("search", "检索", "检索"),
            ]
        )
        latin = "用 TCP 做检索"
        self.assertEqual(
            [term.concept_id for term in matcher.match(latin, boundaries=_boundaries(latin))],
            ["tcp", "search"],
        )
        self.assertEqual(matcher.match("TCPIP", boundaries=_boundaries("TCPIP")), ())
        # `检索` inside `中文检索词` still matches: the segmenter breaks that
        # query into `中文`/`检索`/`词`, so the match is token-aligned.  The
        # assertion is unchanged from the direct-matching rule; its reason is not.
        inside = "中文检索词"
        self.assertEqual(
            [term.concept_id for term in matcher.match(inside, boundaries=_boundaries(inside))],
            ["search"],
        )

        job = "枢对锚站的校验的规律"
        trial = ConceptTermMatcher(
            [
                ConceptTerm("meaning", "规律", "律"),
                ConceptTerm("covenant", "枢与站点约定", "锚"),
                ConceptTerm("the hub", "全域潮汐枢纽", "枢"),
                ConceptTerm("job", "枢对锚站的校验", "枢对锚站的校验"),
            ]
        )
        self.assertEqual(
            [term.concept_id for term in trial.match(job, boundaries=_boundaries(job))],
            ["job"],
        )
        # Without a segmenter the older substring rule applies and `律` is
        # admitted again from inside `规律` — the exact defect, restored.  The
        # fallback is not the old behaviour in full: `锚` and `枢` stay out,
        # because longest-match suppression needs no boundaries to see that
        # both sit inside `枢对锚站的校验`.  Pinning this keeps the degraded
        # path from rotting unnoticed, and marks how much of the fix survives
        # a missing tokenizer.
        self.assertEqual(
            sorted(term.concept_id for term in trial.match(job)),
            ["job", "meaning"],
        )

    @unittest.skipIf(SEGMENTER is None, f"local query segmenter unavailable: {SEGMENTER_REASON}")
    def test_a_shorter_term_inside_a_longer_matched_term_is_suppressed(self) -> None:
        """Containment decides, and only strict containment.

        `锚` is boundary-valid nowhere in `枢对锚站的校验的规律`, but even where
        a short alias is boundary-valid it must lose to a longer term that
        covers it — otherwise the long phrase and its own fragments would both
        resolve.  Two terms that matched the *identical* characters are not in
        a containment relation and both survive, which is the rule the store
        already applies to overlapping source spans.
        """
        matcher = ConceptTermMatcher(
            [
                ConceptTerm("flood", "汛情", "汛情"),
                ConceptTerm("flood_world", "汛期观测", "汛期观测"),
                ConceptTerm("flood_alias", "主汛情", "汛期观测"),
            ]
        )
        query = "汛期观测的经过"
        resolved = sorted(term.concept_id for term in matcher.match(query, boundaries=_boundaries(query)))
        self.assertEqual(resolved, ["flood_alias", "flood_world"])

        spans = matcher.match_spans(query, boundaries=_boundaries(query))
        self.assertEqual({(span.start, span.end) for span in spans}, {(0, 4)})

    @unittest.skipIf(SEGMENTER is None, f"local query segmenter unavailable: {SEGMENTER_REASON}")
    def test_a_concept_keeps_its_longest_span_not_its_first(self) -> None:
        """Deduplication by concept keeps the longest span, a change from the first.

        The matcher used to discard position before anything could compare two
        hits, so a concept matched early by a short alias kept that hit even
        when its full name matched later.  A later stage ranks by how much of
        the query a concept accounted for, and that answer must not depend on
        scan order.
        """
        matcher = ConceptTermMatcher(
            [
                ConceptTerm("the hub", "全域潮汐枢纽", "枢"),
                ConceptTerm("the hub", "全域潮汐枢纽", "全域潮汐枢纽"),
            ]
        )
        query = "枢纽的运行与全域潮汐枢纽"
        spans = matcher.match_spans(query, boundaries=_boundaries(query))
        self.assertEqual(len(spans), 1)
        self.assertEqual(query[spans[0].start : spans[0].end], "全域潮汐枢纽")

    def test_the_cached_matcher_is_reused_but_never_serves_a_changed_vocabulary(self) -> None:
        """Caching is keyed on the store, so an alias edit is visible on the next query.

        Both the matcher and the segmenter are now held on the service instead
        of being rebuilt per request, because the segmenter's dictionary costs
        far more than a query does.  That is only safe if the cache cannot go
        stale, so the key is a store fingerprint that moves on every write that
        could change the vocabulary.  This asserts the fingerprint and the
        resulting behaviour rather than any wall-clock time.
        """
        source = FakeSource()
        service = EpubSearchService(source=source)

        before = source.concept_term_fingerprint()
        self.assertEqual(service.search("TCP").resolved_concepts, ("TCP",))
        self.assertEqual(source.term_reads, 1)
        self.assertEqual(service.search("TCP").resolved_concepts, ("TCP",))
        self.assertEqual(source.term_reads, 1)

        source.terms.append(
            {"concept_id": "tcp", "canonical_name": "TCP", "term": "传输控制协议"}
        )
        self.assertNotEqual(source.concept_term_fingerprint(), before)
        self.assertEqual(service.search("传输控制协议").resolved_concepts, ("TCP",))
        self.assertEqual(source.term_reads, 2)

    def test_a_missing_segmenter_is_reported_degraded_and_never_silently_broadens(self) -> None:
        """Falling back to unsegmented matching is a precision loss, so it is declared.

        The segmenter is a local, deterministic dictionary tokenizer, so
        running without it is a recall/precision trade rather than a forbidden
        cloud fallback — but it must not happen invisibly, and it must be
        reported on every response that ran without one, not only the first.
        """
        source = FakeSource()
        service = EpubSearchService(source=source, segmenter=None)
        service._segmenter_loaded = True  # simulate a failed load
        service._segmenter = None
        service._segmenter_reason = "jieba is not importable"

        for _ in range(2):
            response = service.search("TCP")
            reported = [item for item in response.degraded if item.component == "query-segmenter"]
            self.assertEqual(len(reported), 1)
            self.assertEqual(reported[0].reason, "jieba is not importable")
            self.assertFalse(reported[0].available)

    def test_graph_channel_is_exhaustive_over_distinct_spans_and_never_returns_only_excerpt(self) -> None:
        # Exhaustive, but over distinct source spans rather than mention rows:
        # ``graph_total`` counts the spans this query can page through.
        source = FakeSource()
        response = EpubSearchService(source=source).search("TCP", graph_offset=1, graph_limit=1)

        self.assertEqual(response.resolved_concepts, ("TCP",))
        self.assertEqual(response.graph_total, 3)
        self.assertEqual(len(response.graph_results), 1)
        hit = response.graph_results[0]
        self.assertEqual(hit.passage_id, "p2")
        self.assertEqual(hit.content, source.passages["p2"]["content"])
        self.assertEqual(hit.content[hit.excerpt.start_codepoint : hit.excerpt.end_codepoint], hit.excerpt.content)
        self.assertEqual(hit.excerpt.content, "TCP")
        self.assertEqual(hit.provenance, ("graph",))

    def test_tier_two_local_resolver_only_accepts_existing_concepts_and_degrades_without_fallback(self) -> None:
        source = FakeSource()
        resolver = FakeResolver("TCP")
        response = EpubSearchService(source=source, concept_resolver=resolver).search("传输层连接")
        self.assertEqual(response.resolved_concepts, ("TCP",))
        self.assertEqual(len(resolver.calls), 1)

        unavailable = FakeResolver("TCP", available=False)
        response = EpubSearchService(source=source, concept_resolver=unavailable).search("传输层连接")
        self.assertEqual(response.resolved_concepts, ())
        self.assertEqual(unavailable.calls, [])
        self.assertIn("runtime stopped", response.degraded[0].reason or "")

    def test_graph_expands_grounded_has_part_children_with_relation_provenance(self) -> None:
        source = FakeSource()
        response = EpubSearchService(source=source).search("父主题", graph_limit=10)

        self.assertEqual(response.resolved_concepts, ("父主题",))
        self.assertEqual(response.graph_total, 2)
        self.assertEqual([hit.passage_id for hit in response.graph_results], ["p1", "p3"])
        self.assertEqual(response.graph_results[0].provenance, ("graph",))
        self.assertEqual(response.graph_results[1].provenance, ("graph", "relation:HAS_PART:1"))

    def test_one_source_span_shared_by_two_concepts_is_one_hit_naming_both(self) -> None:
        """The graph channel returns a piece of source once, not once per concept.

        ``TCP`` and ``父主题`` are both anchored on p1[0:3].  That is one
        distinct source span, so it is one hit carrying both names — and it is
        counted once.  Relation-derived spans keep their own provenance.
        """
        source = FakeSource()
        response = EpubSearchService(source=source).search("TCP 父主题", graph_limit=10)

        self.assertEqual(response.resolved_concepts, ("TCP", "父主题"))
        self.assertEqual(response.graph_total, 4)
        self.assertEqual(len(response.graph_results), 4)
        self.assertEqual(
            [hit.passage_id for hit in response.graph_results], ["p1", "p2", "p2", "p3"]
        )
        shared = response.graph_results[0]
        self.assertEqual(shared.matched_concepts, ("TCP", "父主题"))
        self.assertEqual(shared.excerpt.content, "TCP")
        self.assertEqual(shared.provenance, ("graph",))
        self.assertEqual(response.graph_results[3].matched_concepts, ("子主题",))
        self.assertEqual(response.graph_results[3].provenance, ("graph", "relation:HAS_PART:1"))

    def test_graph_pages_stay_exhaustive_once_the_channel_is_ranked(self) -> None:
        """Ranking reorders the result set; it must not change what is in it.

        Relevance is one stable total order over every span, applied before the
        page is cut, so walking one span at a time still visits each span once
        and still ends at ``graph_total``.  A per-page rerank would have passed
        an ordering test and quietly made page 2 meaningless, so the walk — not
        a recomputed expectation — is what this asserts.
        """
        source = HubFakeSource()
        query = "全域潮汐枢纽，汛期观测，值守员造锚站"
        total = EpubSearchService(source=source).search(query).graph_total
        self.assertEqual(total, 5)

        walked: list[object] = []
        for offset in range(total + 1):
            page = EpubSearchService(source=source).search(query, graph_offset=offset, graph_limit=1)
            self.assertEqual(page.graph_total, total)
            if not page.graph_results:
                break
            hit = page.graph_results[0]
            walked.append((hit.passage_id, hit.excerpt.start_codepoint, hit.excerpt.end_codepoint))

        self.assertEqual(len(walked), total)
        self.assertEqual(len(set(walked)), total)
        whole = EpubSearchService(source=source).search(query, graph_limit=total)
        self.assertEqual(
            walked,
            [(hit.passage_id, hit.excerpt.start_codepoint, hit.excerpt.end_codepoint) for hit in whole.graph_results],
        )

    def test_a_directly_matched_span_outranks_one_reached_through_a_hub(self) -> None:
        """Reaching a concept through a 20-child hub is weak evidence, and ranks so.

        `枢纽的基准线` is a real `HAS_PART` child of `全域潮汐枢纽`, and its span
        is the longest in the fixture — under book order or under length alone
        it would lead the page.  It is still last, because one hop out of a hub
        that fans out to 20 children costs `1 + log2(20)`, while one hop out of
        `锚站`, which names a single part, costs a plain hop.  Nothing is
        dropped: both expanded spans are still counted and still reachable, so
        `graph_total` is unchanged by the weighting.
        """
        source = HubFakeSource()
        response = EpubSearchService(source=source).search(
            "全域潮汐枢纽，汛期观测，值守员造锚站", graph_limit=10
        )

        self.assertEqual(response.graph_total, 5)
        self.assertEqual(
            [hit.passage_id for hit in response.graph_results],
            ["flood", "judgment", "preface", "ark", "righteousness"],
        )
        # Both expanded spans keep their hop count in provenance; only their
        # ranking weight differs, so a reader still sees how each was reached.
        self.assertEqual(response.graph_results[3].provenance, ("graph", "relation:HAS_PART:1"))
        self.assertEqual(response.graph_results[4].provenance, ("graph", "relation:HAS_PART:1"))
        costs = source.occurrence_calls[0]["costs"]
        self.assertEqual(costs["hub"], 0.0)
        self.assertEqual(costs["ark_animals"], 1.0)
        self.assertAlmostEqual(costs["righteousness"], 1.0 + math.log2(20))

    def test_a_front_matter_hub_span_does_not_outrank_a_topical_span(self) -> None:
        """The reported symptom, asserted directly.

        A `汛期观测` query used to open its graph panel on the book's preface,
        because the preface is where the hub concept is first mentioned and the
        channel was ordered by book position.  The ordering was never arbitrary
        — it was the front of the book — but it was useless.  The single
        character `枢` in a sentence about how the book was compiled must now
        rank below the spans that actually discuss the flood, and the panel's
        first result must be the span carrying both queried concepts.
        """
        source = HubFakeSource()
        response = EpubSearchService(source=source).search("全域潮汐枢纽，汛期观测", graph_limit=3)

        first = response.graph_results[0]
        self.assertEqual(first.passage_id, "flood")
        self.assertEqual(first.matched_concepts, ("汛期观测", "全域潮汐枢纽"))
        self.assertEqual(first.excerpt.content, "枢在汛期观测，通知值守员建锚站")
        self.assertEqual(first.content, HubFakeSource.FLOOD)
        # Ranking demoted the preface span; it did not remove it.  It is still
        # counted, still paged, and still cites its complete passage.
        self.assertEqual([hit.passage_id for hit in response.graph_results][:3], ["flood", "judgment", "preface"])
        preface = response.graph_results[2]
        self.assertEqual(preface.excerpt.content, "枢")
        self.assertEqual(preface.content, HubFakeSource.PREFACE)
        self.assertEqual(
            preface.content[preface.excerpt.start_codepoint : preface.excerpt.end_codepoint], "枢"
        )

    def test_a_small_graph_page_no_longer_starves_the_fused_channel(self) -> None:
        """`graph_limit` sizes the panel's page, not the fused channel's recall.

        Fusion used to be handed whatever page the panel had asked for, so a
        `graph_limit` of 1 gave the local Cross-Encoder exactly one graph
        candidate — and with the channel ordered by book position that one was
        the front of the book.  Fusion now always reads the top of the ranked
        channel from offset 0, so paging the panel cannot change the fused
        answer either.
        """
        source = HubFakeSource()
        service = EpubSearchService(source=source)

        service.search("全域潮汐枢纽，汛期观测，值守员造锚站", graph_limit=1, graph_fusion_limit=4)
        panel_call, fusion_call = source.occurrence_calls
        self.assertEqual((panel_call["offset"], panel_call["limit"]), (0, 1))
        self.assertEqual((fusion_call["offset"], fusion_call["limit"]), (0, 4))

        source.occurrence_calls.clear()
        service.search("全域潮汐枢纽，汛期观测，值守员造锚站", graph_offset=3, graph_limit=2, graph_fusion_limit=4)
        panel_call, fusion_call = source.occurrence_calls
        self.assertEqual((panel_call["offset"], panel_call["limit"]), (3, 2))
        self.assertEqual((fusion_call["offset"], fusion_call["limit"]), (0, 4))

        # A page that already contains the ranked top of the channel is reused
        # rather than read twice.
        source.occurrence_calls.clear()
        service.search("全域潮汐枢纽，汛期观测，值守员造锚站", graph_limit=10, graph_fusion_limit=4)
        self.assertEqual(len(source.occurrence_calls), 1)

    def test_vector_candidates_are_cross_encoder_reranked_then_mmr_diversified(self) -> None:
        source = FakeSource()
        backend = FakeVectorBackend(
            [
                _record(source, "u1", (1.0, 0.0)),
                _record(source, "u2", (0.99, 0.01)),
                _record(source, "u3", (0.0, 1.0)),
            ]
        )
        embeddings = FakeEmbeddings()
        reranker = FakeReranker()
        response = EpubSearchService(
            source=source,
            vector_backend=backend,
            embeddings=embeddings,
            reranker=reranker,
            mmr_lambda=0.5,
        ).search("连接协议", vector_limit=2, vector_candidate_limit=3)

        # u2 is most relevant. u3 is selected next because MMR penalizes the
        # nearly identical u1 vector, proving rerank precedes diversification.
        self.assertEqual([hit.passage_id for hit in response.vector_results], ["p2", "p3"])
        self.assertEqual(response.vector_results[0].content, source.passages["p2"]["content"])
        self.assertEqual(response.vector_results[0].excerpt.content, source.units["u2"]["content"])
        self.assertEqual(response.vector_results[0].provenance, ("vector", "cross-encoder", "mmr"))
        self.assertEqual(embeddings.calls, [["连接协议"]])
        self.assertEqual(len(reranker.calls), 1)

    def test_tampered_vector_window_or_missing_local_reranker_is_degraded_not_cloud_fallback(self) -> None:
        source = FakeSource()
        backend = FakeVectorBackend([_record(source, "u1", (1.0, 0.0))])
        response = EpubSearchService(
            source=source, vector_backend=backend, embeddings=FakeEmbeddings(), reranker=FakeReranker()
        ).search("TCP")
        self.assertEqual(len(response.vector_results), 1)

        source.units["u1"]["content"] = "tampered"
        response = EpubSearchService(
            source=source, vector_backend=backend, embeddings=FakeEmbeddings(), reranker=FakeReranker()
        ).search("TCP")
        self.assertEqual(response.vector_results, ())
        self.assertIn("does not equal", response.degraded[-1].reason or "")

        response = EpubSearchService(source=source).search("TCP")
        self.assertEqual(response.vector_results, ())
        self.assertEqual(response.degraded[-1].component, "local-vector-search")

    def test_graph_and_vector_candidates_share_local_cross_encoder_and_mmr_fusion(self) -> None:
        source = FakeSource()
        backend = FakeVectorBackend([_record(source, "u2", (1.0, 0.0))])
        embeddings = FusionEmbeddings()
        reranker = FusionReranker()

        response = EpubSearchService(
            source=source,
            vector_backend=backend,
            embeddings=embeddings,
            reranker=reranker,
            mmr_lambda=1.0,
        ).search(
            "TCP",
            graph_limit=1,
            # Bound the graph side of the fusion explicitly: the display page
            # no longer bounds it, so this test names the one graph candidate
            # it is about instead of relying on the panel's page size.
            graph_fusion_limit=1,
            vector_limit=2,
            vector_candidate_limit=2,
        )

        # The legacy fields stay independently usable, while fused_results
        # ranks the exact graph excerpt and exact vector window together.
        self.assertEqual([hit.passage_id for hit in response.graph_results], ["p1"])
        self.assertEqual([hit.passage_id for hit in response.vector_results], ["p2"])
        self.assertEqual([hit.passage_id for hit in response.fused_results], ["p1", "p2"])
        graph_hit, vector_hit = response.fused_results
        self.assertEqual(graph_hit.content, source.passages["p1"]["content"])
        self.assertEqual(graph_hit.excerpt.content, "TCP")
        self.assertEqual(graph_hit.provenance, ("graph", "cross-encoder", "mmr", "fused"))
        self.assertEqual(vector_hit.content, source.passages["p2"]["content"])
        self.assertEqual(vector_hit.excerpt.content, source.units["u2"]["content"])
        self.assertEqual(vector_hit.provenance, ("vector", "cross-encoder", "mmr", "fused"))
        self.assertEqual(reranker.calls[-1][1], ["TCP", source.units["u2"]["content"]])
        self.assertEqual(embeddings.calls, [["TCP"], ["TCP"]])

    def test_malformed_local_graph_embedding_fails_closed_for_fused_channel(self) -> None:
        source = FakeSource()
        response = EpubSearchService(
            source=source,
            vector_backend=FakeVectorBackend([]),
            # This existing fake returns one vector for a two-excerpt graph
            # batch.  It simulates a malformed local runtime response.
            embeddings=FakeEmbeddings(),
            reranker=FakeReranker(),
        ).search("TCP", graph_limit=2)

        self.assertEqual(response.graph_results[0].content, source.passages["p1"]["content"])
        self.assertEqual(response.fused_results, ())
        self.assertEqual(response.degraded[-1].component, "local-fused-search")
        self.assertIn("every graph candidate", response.degraded[-1].reason or "")


if __name__ == "__main__":
    unittest.main()
