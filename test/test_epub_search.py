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
    ships with disagreed about where `神对约伯的试炼的意义` breaks, which is the
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
    the earliest span in the book is a bare `神` in a preface sentence about
    nothing.  `独一无二的神` legitimately has 20 grounded `HAS_PART` children,
    so expanding through it drags in spans that have no particular connection
    to the query — that hub is why `神的权柄` reaches 778 spans.  And `方舟`
    has a single child, so expanding through it says something.

    The queries here name the hub by its canonical `独一无二的神` rather than
    leaning on its one-character `神` alias.  That alias is still in the
    fixture and still anchors the preface span, but whether it *resolves* from
    a given query is now a question about that query's word boundaries — and
    these tests are about ranking, which must not be hostage to how a tokenizer
    happens to break one phrase.  In book order the preface span still comes
    first; that is the reported symptom, and it was never arbitrary — it was
    the front of the book.
    """

    PREFACE = "写在前面的话：本书是神的话语的选编，供人阅读。"
    FLOOD = "神要用洪水灭世，嘱咐挪亚造方舟，为要保全一家人的性命。"
    JUDGMENT = "洪水灭世之后的人类与各样活物都得以保全，为神下一步的经营作了铺垫。"
    HUB_CHILD = "神的公义性情是人无法测透的，人只能从神所作的事上认识神的实质与所有所是。"
    ARK_CHILD = "方舟造成之后，神吩咐挪亚将各样活物带进方舟里面。"

    @staticmethod
    def _passage(passage_id: str, chapter: str, content: str) -> dict[str, object]:
        return {
            "passage_id": passage_id,
            "book_title": "神的作工",
            "toc_path": (chapter,),
            "content": content,
            "content_sha256": _hash(content),
        }

    def __init__(self) -> None:
        super().__init__()
        self.passages = {
            "preface": self._passage("preface", "写在前面的话", self.PREFACE),
            "flood": self._passage("flood", "第四章", self.FLOOD),
            "judgment": self._passage("judgment", "第四章", self.JUDGMENT),
            "righteousness": self._passage("righteousness", "第九章", self.HUB_CHILD),
            "ark": self._passage("ark", "第四章", self.ARK_CHILD),
        }
        self.terms = [
            {"concept_id": "hub", "canonical_name": "独一无二的神", "term": "独一无二的神"},
            {"concept_id": "hub", "canonical_name": "独一无二的神", "term": "神"},
            {"concept_id": "flood", "canonical_name": "洪水灭世", "term": "洪水灭世"},
            {"concept_id": "ark", "canonical_name": "方舟", "term": "方舟"},
        ]
        # Book order, deliberately opening with the preface: the front-matter
        # span is the earliest mention of the hub in the whole book.
        self.occurrences = [
            {**self.passages["preface"], "concept_id": "hub", "canonical_name": "独一无二的神",
             "start_codepoint": 10, "end_codepoint": 11},
            {**self.passages["flood"], "concept_id": "flood", "canonical_name": "洪水灭世",
             "start_codepoint": 0, "end_codepoint": 15},
            {**self.passages["flood"], "concept_id": "hub", "canonical_name": "独一无二的神",
             "start_codepoint": 0, "end_codepoint": 15},
            {**self.passages["judgment"], "concept_id": "flood", "canonical_name": "洪水灭世",
             "start_codepoint": 0, "end_codepoint": 13},
            # Reached only by expanding out of the hub, yet earlier in the book
            # than nothing and longer than the flood span.  Book order and span
            # length both favour it; relation cost must not.
            {**self.passages["righteousness"], "concept_id": "righteousness",
             "canonical_name": "神的公义性情", "start_codepoint": 0, "end_codepoint": 35},
            {**self.passages["ark"], "concept_id": "ark_animals", "canonical_name": "各样活物",
             "start_codepoint": 0, "end_codepoint": 23},
        ]
        # 20 children under the hub, one child under `方舟`: one hop out of the
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


class GenericTermFakeSource(FakeSource):
    """One concept the whole book is about, and one that names a single thing.

    ``独一无二的神`` is reachable by its full canonical name *and* by the
    one-character alias ``神`` a model proposed, has 175 mentions, and has 20
    grounded ``HAS_PART`` children.  ``洪水灭世`` is reachable by a four-character
    name, has two mentions, and has one child.  Both must resolve; only the
    second may seed a walk.

    The specificity columns are supplied exactly as the SQLite store supplies
    them.  :class:`FakeSource` deliberately supplies none of them, which is how
    the tests keep proving that a repository answering the older three-column
    shape still expands exactly as it did.
    """

    HUB = "神所作的事无人能测透，人只能从中认识神。"
    HUB_CHILD = "神的公义性情是人无法测透的。"
    FLOOD = "神要用洪水灭世，嘱咐挪亚造方舟。"
    ARK = "方舟造成之后，各样活物都进去了。"

    def __init__(self) -> None:
        super().__init__()
        self.passages = {
            "hub": self._passage("hub", "第一章", self.HUB),
            "righteousness": self._passage("righteousness", "第九章", self.HUB_CHILD),
            "flood": self._passage("flood", "第四章", self.FLOOD),
            "ark": self._passage("ark", "第四章", self.ARK),
        }
        self.terms = [
            {"concept_id": "hub", "canonical_name": "独一无二的神", "term": "独一无二的神",
             "term_source": "MODEL", "mention_count": 175, "has_part_fanout": 20},
            {"concept_id": "hub", "canonical_name": "独一无二的神", "term": "神",
             "term_source": "MODEL", "mention_count": 175, "has_part_fanout": 20},
            {"concept_id": "flood", "canonical_name": "洪水灭世", "term": "洪水灭世",
             "term_source": "MODEL", "mention_count": 2, "has_part_fanout": 1},
        ]
        self.occurrences = [
            {**self.passages["hub"], "concept_id": "hub", "canonical_name": "独一无二的神",
             "start_codepoint": 0, "end_codepoint": 6},
            {**self.passages["righteousness"], "concept_id": "righteousness",
             "canonical_name": "神的公义性情", "start_codepoint": 0, "end_codepoint": 6},
            {**self.passages["flood"], "concept_id": "flood", "canonical_name": "洪水灭世",
             "start_codepoint": 0, "end_codepoint": 8},
            {**self.passages["ark"], "concept_id": "ark_animals", "canonical_name": "各样活物",
             "start_codepoint": 0, "end_codepoint": 6},
        ]
        self.relations = [
            {"subject_concept_id": "hub", "predicate": "HAS_PART",
             "object_concept_id": "righteousness"},
            {"subject_concept_id": "flood", "predicate": "HAS_PART",
             "object_concept_id": "ark_animals"},
        ]
        self.units = {}

    def matched_concept_names(self, passage_id, concept_ids):
        return sorted(
            {
                row["canonical_name"]
                for row in self.occurrences
                if row["passage_id"] == passage_id and row["concept_id"] in concept_ids
            }
        )


class TocFakeSource(FakeSource):
    """The acceptance book's TOC shape, reduced to the four rules that bind it.

    ``人一生所必经的六个关口`` is a real node in the parsed ``toc_nodes`` with
    the six 关口 as its children, while the concept ``六个关口`` is a
    one-mention island with no relation of any predicate.  Before TOC
    expansion, the query that names it returned exactly one span — the heading
    — and the sections that answer it were unreachable.

    Everything else in this fixture exists to be *refused*:

    * ``方舟`` was decomposed by the model, so its TOC children must stay out of
      the way entirely — ``方舟的木料`` sits in its child node and must never
      appear.
    * ``贯穿的话`` is mentioned inside a 关口 section *and* somewhere else, so it
      is bound to neither and is admitted from neither.
    * ``众人的结局`` binds to a node whose children hold more concepts than the
      budget allows, so its expansion is skipped whole rather than cut short.

    The specificity columns are supplied here exactly as the SQLite store
    supplies them.  :class:`FakeSource` deliberately supplies none of them and
    has no ``list_toc_child_concepts``, which is how the tests keep proving that
    a repository answering the older shape still behaves as it did.
    """

    FILLERS = 65

    def __init__(self) -> None:
        super().__init__()
        self.passages = {
            "gates": self._passage("gates", "人一生所必经的六个关口", "人一生所必经的六个关口"),
            "birth": self._passage("birth", "第一关　人的出生", "一个人生在何处，生在什么家庭。"),
            "growth": self._passage("growth", "第二关　人的成长", "一个人的成长背景决定很多事。"),
            "ark": self._passage("ark", "方舟", "方舟造成之后，神吩咐挪亚。"),
            "timber": self._passage("timber", "方舟的木料", "歌斐木是造方舟所用的木料。"),
            "animals": self._passage("animals", "各样活物", "各样活物都进了方舟里面。"),
            "elsewhere": self._passage("elsewhere", "另一章", "贯穿的话也出现在别处。"),
            "crowd": self._passage("crowd", "众人的结局", "众人的结局各不相同。"),
            "crowd_body": self._passage("crowd_body", "结局细目", "细目一二三四五六七八九十。"),
        }
        # concept -> the one TOC node every one of its mentions falls under.
        # ``everywhere`` is absent because it is mentioned under two nodes,
        # which is exactly what makes it unbound.
        self.bindings = {
            "gates": "n-gates",
            "birth": "n-gate-one",
            "growth": "n-gate-two",
            "ark": "n-ark",
            "timber": "n-timber",
            "animals": "n-elsewhere",
            "crowd": "n-crowd",
            **{f"filler-{index}": "n-crowd-detail" for index in range(self.FILLERS)},
        }
        self.toc_children = {
            "n-gates": ("n-gate-one", "n-gate-two"),
            "n-ark": ("n-timber",),
            "n-crowd": ("n-crowd-detail",),
        }
        self.terms = [
            self._term("gates", "六个关口", "六个关口", mentions=1, fanout=0),
            self._term("birth", "人的出生", "人的出生", mentions=1, fanout=0),
            self._term("growth", "人的成长", "人的成长", mentions=1, fanout=0),
            # Decomposed by the model: TOC structure must defer to that.
            self._term("ark", "方舟", "方舟", mentions=2, fanout=1),
            self._term("crowd", "众人的结局", "众人的结局", mentions=1, fanout=0),
        ]
        self.occurrences = [
            self._occurrence("gates", "六个关口", "gates", 6, 10),
            self._occurrence("birth", "人的出生", "birth", 0, 6),
            self._occurrence("growth", "人的成长", "growth", 0, 6),
            self._occurrence("ark", "方舟", "ark", 0, 2),
            self._occurrence("timber", "方舟的木料", "timber", 0, 3),
            self._occurrence("animals", "各样活物", "animals", 0, 4),
            self._occurrence("everywhere", "贯穿的话", "birth", 7, 9),
            self._occurrence("everywhere", "贯穿的话", "elsewhere", 0, 4),
            self._occurrence("crowd", "众人的结局", "crowd", 0, 5),
        ] + [
            self._occurrence(f"filler-{index}", f"细目{index}", "crowd_body", index, index + 1)
            for index in range(self.FILLERS)
        ]
        self.relations = [
            {"subject_concept_id": "ark", "predicate": "HAS_PART", "object_concept_id": "animals"},
        ]
        self.units = {}

    @staticmethod
    def _term(concept_id: str, canonical: str, term: str, *, mentions: int, fanout: int) -> dict:
        return {
            "concept_id": concept_id,
            "canonical_name": canonical,
            "term": term,
            "term_source": "MODEL",
            "mention_count": mentions,
            "has_part_fanout": fanout,
        }

    def _occurrence(
        self, concept_id: str, canonical: str, passage_id: str, start: int, end: int
    ) -> dict:
        return {
            **self.passages[passage_id],
            "concept_id": concept_id,
            "canonical_name": canonical,
            "start_codepoint": start,
            "end_codepoint": end,
        }

    def matched_concept_names(self, passage_id, concept_ids):
        return sorted(
            {
                row["canonical_name"]
                for row in self.occurrences
                if row["passage_id"] == passage_id and row["concept_id"] in concept_ids
            }
        )

    def list_toc_child_concepts(self, concept_ids):
        """The store's join, written out: bind the seed, then bind each child.

        Both directions use the same ``bindings`` table, which is the point of
        the rule — a concept the book spreads across two nodes is neither a
        seed nor a child, no matter which end of the join it is on.
        """
        rows = []
        for concept_id in concept_ids:
            node = self.bindings.get(concept_id)
            if node is None:
                continue
            children = self.toc_children.get(node, ())
            for other, other_node in sorted(self.bindings.items()):
                if other != concept_id and other_node in children:
                    rows.append(
                        {
                            "seed_concept_id": concept_id,
                            "seed_toc_node_id": node,
                            "concept_id": other,
                        }
                    )
        return rows


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
        matches merely because its characters appear: `义` is a real concept
        name and it is *inside* `意义`, which is not a mention of it.  A term
        may still span several tokens — `神对约伯的试炼` covers four — because
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

        job = "神对约伯的试炼的意义"
        trial = ConceptTermMatcher(
            [
                ConceptTerm("meaning", "意义", "义"),
                ConceptTerm("covenant", "神与人立约", "约"),
                ConceptTerm("god", "独一无二的神", "神"),
                ConceptTerm("job", "神对约伯的试炼", "神对约伯的试炼"),
            ]
        )
        self.assertEqual(
            [term.concept_id for term in trial.match(job, boundaries=_boundaries(job))],
            ["job"],
        )
        # Without a segmenter the older substring rule applies and `义` is
        # admitted again from inside `意义` — the exact defect, restored.  The
        # fallback is not the old behaviour in full: `约` and `神` stay out,
        # because longest-match suppression needs no boundaries to see that
        # both sit inside `神对约伯的试炼`.  Pinning this keeps the degraded
        # path from rotting unnoticed, and marks how much of the fix survives
        # a missing tokenizer.
        self.assertEqual(
            sorted(term.concept_id for term in trial.match(job)),
            ["job", "meaning"],
        )

    @unittest.skipIf(SEGMENTER is None, f"local query segmenter unavailable: {SEGMENTER_REASON}")
    def test_a_shorter_term_inside_a_longer_matched_term_is_suppressed(self) -> None:
        """Containment decides, and only strict containment.

        `约` is boundary-valid nowhere in `神对约伯的试炼的意义`, but even where
        a short alias is boundary-valid it must lose to a longer term that
        covers it — otherwise the long phrase and its own fragments would both
        resolve.  Two terms that matched the *identical* characters are not in
        a containment relation and both survive, which is the rule the store
        already applies to overlapping source spans.
        """
        matcher = ConceptTermMatcher(
            [
                ConceptTerm("flood", "洪水", "洪水"),
                ConceptTerm("flood_world", "洪水灭世", "洪水灭世"),
                ConceptTerm("flood_alias", "大洪水", "洪水灭世"),
            ]
        )
        query = "洪水灭世的经过"
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
                ConceptTerm("god", "独一无二的神", "神"),
                ConceptTerm("god", "独一无二的神", "独一无二的神"),
            ]
        )
        query = "神的作工与独一无二的神"
        spans = matcher.match_spans(query, boundaries=_boundaries(query))
        self.assertEqual(len(spans), 1)
        self.assertEqual(query[spans[0].start : spans[0].end], "独一无二的神")

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

    def test_toc_children_answer_a_concept_the_model_never_decomposed(self) -> None:
        """The book's own hierarchy reaches sections no relation points at.

        ``六个关口`` is a one-mention island: before this, the query that names
        it returned the heading span and nothing else, because there is no
        ``HAS_PART`` edge anywhere in the graph to walk out of it.  Its TOC node
        has the sections that answer the question, and that hierarchy is
        structural provenance the parser read out of the EPUB — no model
        proposed it and none may revise it.

        Three things are asserted together because they are one claim.  The
        child sections are reached; they are labelled ``structure:TOC_CHILD``
        and *not* ``relation:``, so a reader can tell a structural edge from a
        semantic one at a glance; and ``resolved_concepts`` still names only
        the concept the query actually contained, because a TOC child is
        expansion-derived and was never a Tier-1 match.

        ``贯穿的话`` is mentioned inside 第一关 but also elsewhere in the book,
        so it binds to no node and is admitted from neither — that is what keeps
        this channel from becoming a second hub.
        """
        source = TocFakeSource()
        response = EpubSearchService(source=source).search("六个关口", graph_limit=10)

        self.assertEqual(response.resolved_concepts, ("六个关口",))
        self.assertEqual(response.graph_total, 3)
        self.assertEqual(
            [hit.passage_id for hit in response.graph_results], ["gates", "birth", "growth"]
        )
        self.assertEqual(response.graph_results[0].provenance, ("graph",))
        self.assertEqual(
            response.graph_results[1].provenance, ("graph", "structure:TOC_CHILD:1")
        )
        self.assertEqual(
            response.graph_results[2].provenance, ("graph", "structure:TOC_CHILD:1")
        )
        self.assertNotIn("贯穿的话", {name for hit in response.graph_results for name in hit.matched_concepts})

    def test_a_model_decomposed_concept_never_falls_back_to_toc_structure(self) -> None:
        """Where the semantic graph exists it is authoritative; TOC is the fallback.

        ``方舟`` has a grounded ``HAS_PART`` child, so the model answered the
        "what are its parts?" question and this channel must not answer it
        again.  Its TOC child node holds ``方舟的木料``, which is a perfectly
        real concept and still must not appear: a structural edge competing
        with a semantic one would make the two orderings disagree about what a
        decomposition even is.
        """
        source = TocFakeSource()
        response = EpubSearchService(source=source).search("方舟", graph_limit=10)

        names = {name for hit in response.graph_results for name in hit.matched_concepts}
        self.assertIn("各样活物", names)
        self.assertNotIn("方舟的木料", names)
        self.assertEqual(
            [hit.provenance for hit in response.graph_results],
            [("graph",), ("graph", "relation:HAS_PART:1")],
        )

    def test_a_generic_term_still_resolves_but_seeds_no_expansion(self) -> None:
        """Resolution and expansion are different questions about the same match.

        ``独一无二的神`` is matched by its full canonical name, so it resolves
        and contributes every one of its own spans — nothing here is capped,
        hidden, or down-weighted.  What it does not do is seed a walk: at 175
        mentions it is what the whole book is about, and one hop out of it
        returns spans about something else entirely, which is how a query about
        one thing came back holding a hub child the reader never named.

        The same concept reached through its one-character model alias ``神`` is
        refused for two independent reasons at once, and must behave
        identically — the guard is about the concept's specificity, not about
        which surface form happened to win.
        """
        service = EpubSearchService(source=GenericTermFakeSource())

        for query in ("独一无二的神", "神"):
            with self.subTest(query=query):
                response = service.search(query, graph_limit=10)
                self.assertEqual(response.resolved_concepts, ("独一无二的神",))
                # Its own span, and none of its 20 children's.
                self.assertEqual(response.graph_total, 1)
                names = {name for hit in response.graph_results for name in hit.matched_concepts}
                self.assertNotIn("神的公义性情", names)
                self.assertEqual(
                    {hit.provenance for hit in response.graph_results}, {("graph",)}
                )

        # The contrast is what makes the rule a rule rather than a cap: a
        # four-character name on a two-mention concept seeds its walk exactly as
        # it did before, and its child arrives with relation provenance.
        specific = service.search("洪水灭世", graph_limit=10)
        self.assertEqual(specific.graph_total, 2)
        self.assertEqual(
            specific.graph_results[1].provenance, ("graph", "relation:HAS_PART:1")
        )

    def test_an_oversized_toc_node_is_skipped_whole_and_reported(self) -> None:
        """A budget that truncated would leave ``graph_total`` unexplainable.

        ``众人的结局`` binds to a node whose children hold more concepts than
        the channel will accept.  Returning the first 64 of them would come back
        as a smaller number with nothing anywhere saying which source had been
        dropped, and paging to the end would simply stop early.  So the seed's
        expansion is skipped entirely — the query behaves exactly as it did
        before this channel existed — and the skip is reported as a degraded
        component so the number has an explanation attached to it.
        """
        source = TocFakeSource()
        response = EpubSearchService(source=source).search("众人的结局", graph_limit=100)

        self.assertEqual(response.graph_total, 1)
        self.assertEqual(response.graph_results[0].provenance, ("graph",))
        skipped = [entry for entry in response.degraded if entry.component == "toc-child-expansion"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("65", str(skipped[0].reason))
        self.assertIn("skipped whole", str(skipped[0].reason))

    def test_a_toc_expanded_page_walk_still_ends_at_graph_total(self) -> None:
        """Channel A's contract is unchanged by where the concept ids came from.

        TOC expansion only lengthens the tuple handed to the occurrence
        queries; it never touches passages, spans, or the predicate the count
        and the pages share.  That is an argument, and this is the check that
        the argument holds: walk the TOC-expanded set one span at a time and it
        must visit each distinct source span exactly once and stop at
        ``graph_total``.
        """
        source = TocFakeSource()
        service = EpubSearchService(source=source)
        total = service.search("六个关口", graph_limit=1).graph_total

        walked = []
        for offset in range(total):
            page = service.search("六个关口", graph_offset=offset, graph_limit=1)
            walked.extend(
                (hit.passage_id, hit.excerpt.start_codepoint, hit.excerpt.end_codepoint)
                for hit in page.graph_results
            )

        self.assertEqual(len(walked), total)
        self.assertEqual(len(set(walked)), total)
        self.assertEqual(
            service.search("六个关口", graph_offset=total, graph_limit=1).graph_results, ()
        )

    def test_a_repository_without_specificity_columns_expands_exactly_as_before(self) -> None:
        """The new guards read what is offered and invent nothing.

        :class:`FakeSource` answers the three-column vocabulary and has no
        ``list_toc_child_concepts`` at all, which is the shape a read model that
        has not caught up would present.  It must keep today's behaviour rather
        than raise or silently stop expanding: ``父主题`` still reaches its
        ``HAS_PART`` child, and the mention ceiling — which nothing has declared
        — refuses nothing.
        """
        source = FakeSource()
        service = EpubSearchService(source=source)

        self.assertEqual(service._expansion_seed_ids(
            service._concept_matcher().match_spans("父主题", boundaries=_boundaries("父主题"))
        ), ("parent",))
        response = service.search("父主题", graph_limit=10)
        self.assertEqual(response.graph_total, 2)
        self.assertEqual(response.graph_results[1].provenance, ("graph", "relation:HAS_PART:1"))

    def test_graph_pages_stay_exhaustive_once_the_channel_is_ranked(self) -> None:
        """Ranking reorders the result set; it must not change what is in it.

        Relevance is one stable total order over every span, applied before the
        page is cut, so walking one span at a time still visits each span once
        and still ends at ``graph_total``.  A per-page rerank would have passed
        an ordering test and quietly made page 2 meaningless, so the walk — not
        a recomputed expectation — is what this asserts.
        """
        source = HubFakeSource()
        query = "独一无二的神，洪水灭世，挪亚造方舟"
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

        `神的公义性情` is a real `HAS_PART` child of `独一无二的神`, and its span
        is the longest in the fixture — under book order or under length alone
        it would lead the page.  It is still last, because one hop out of a hub
        that fans out to 20 children costs `1 + log2(20)`, while one hop out of
        `方舟`, which names a single part, costs a plain hop.  Nothing is
        dropped: both expanded spans are still counted and still reachable, so
        `graph_total` is unchanged by the weighting.
        """
        source = HubFakeSource()
        response = EpubSearchService(source=source).search(
            "独一无二的神，洪水灭世，挪亚造方舟", graph_limit=10
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

        A `洪水灭世` query used to open its graph panel on the book's preface,
        because the preface is where the hub concept is first mentioned and the
        channel was ordered by book position.  The ordering was never arbitrary
        — it was the front of the book — but it was useless.  The single
        character `神` in a sentence about how the book was compiled must now
        rank below the spans that actually discuss the flood, and the panel's
        first result must be the span carrying both queried concepts.
        """
        source = HubFakeSource()
        response = EpubSearchService(source=source).search("独一无二的神，洪水灭世", graph_limit=3)

        first = response.graph_results[0]
        self.assertEqual(first.passage_id, "flood")
        self.assertEqual(first.matched_concepts, ("洪水灭世", "独一无二的神"))
        self.assertEqual(first.excerpt.content, "神要用洪水灭世，嘱咐挪亚造方舟")
        self.assertEqual(first.content, HubFakeSource.FLOOD)
        # Ranking demoted the preface span; it did not remove it.  It is still
        # counted, still paged, and still cites its complete passage.
        self.assertEqual([hit.passage_id for hit in response.graph_results][:3], ["flood", "judgment", "preface"])
        preface = response.graph_results[2]
        self.assertEqual(preface.excerpt.content, "神")
        self.assertEqual(preface.content, HubFakeSource.PREFACE)
        self.assertEqual(
            preface.content[preface.excerpt.start_codepoint : preface.excerpt.end_codepoint], "神"
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

        service.search("独一无二的神，洪水灭世，挪亚造方舟", graph_limit=1, graph_fusion_limit=4)
        panel_call, fusion_call = source.occurrence_calls
        self.assertEqual((panel_call["offset"], panel_call["limit"]), (0, 1))
        self.assertEqual((fusion_call["offset"], fusion_call["limit"]), (0, 4))

        source.occurrence_calls.clear()
        service.search("独一无二的神，洪水灭世，挪亚造方舟", graph_offset=3, graph_limit=2, graph_fusion_limit=4)
        panel_call, fusion_call = source.occurrence_calls
        self.assertEqual((panel_call["offset"], panel_call["limit"]), (3, 2))
        self.assertEqual((fusion_call["offset"], fusion_call["limit"]), (0, 4))

        # A page that already contains the ranked top of the channel is reused
        # rather than read twice.
        source.occurrence_calls.clear()
        service.search("独一无二的神，洪水灭世，挪亚造方舟", graph_limit=10, graph_fusion_limit=4)
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
