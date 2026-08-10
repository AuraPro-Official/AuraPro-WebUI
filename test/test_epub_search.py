"""Acceptance tests for faithful local-only EPUB concept search."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import math
from pathlib import Path
import sys
import types
import unittest


EPUB_DIR = Path(__file__).resolve().parents[1] / 'backend/open_webui/retrieval/epub'
PACKAGE_NAME = 'epub_search_sdd_test_package'
PACKAGE = types.ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(EPUB_DIR)]  # type: ignore[attr-defined]
sys.modules[PACKAGE_NAME] = PACKAGE


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f'{PACKAGE_NAME}.{name}', EPUB_DIR / f'{name}.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INFERENCE = _load('inference')
VECTOR_INDEX = _load('vector_index')
SEGMENTATION = _load('segmentation')
SEARCH = _load('search')

ConceptTerm = SEARCH.ConceptTerm
ConceptTermMatcher = SEARCH.ConceptTermMatcher
EpubSearchService = SEARCH.EpubSearchService
ModelAvailability = INFERENCE.ModelAvailability
DerivedVectorRecord = VECTOR_INDEX.DerivedVectorRecord
TokenBoundaries = SEGMENTATION.TokenBoundaries
load_query_segmenter = SEGMENTATION.load_query_segmenter

SEGMENTER, SEGMENTER_REASON = load_query_segmenter()


# ---------------------------------------------------------------------------
# Why the Chinese fixtures below look the way they do
# ---------------------------------------------------------------------------
#
# The EPUB acceptance corpus is a copyrighted book, so none of its text,
# headings or concept names appear in this repository.  Every Chinese fixture
# here is invented: a fictional tidal-observation network built around a
# scheduler (``全域潮汐枢纽``), its measurement stations, and a calibration
# procedure.  It refers to no real book, person, organisation or place.
#
# The substitution is structural, not decorative.  Each fixture reproduces one
# property of the real corpus that a test depends on, and changing a name
# without preserving that property silently stops the test from testing
# anything:
#
#   * ``全域潮汐枢纽`` + the one-code-point alias ``枢`` — a hub with 20
#     grounded ``HAS_PART`` children reachable by a single character.  This is
#     the case the resolution-stage expansion guard exists for: naming the hub
#     in full expands to its subtree, brushing it with the alias does not.
#   * ``枢纽的基准线`` — a grounded child of that hub carrying the *longest*
#     span in the fixture, so relation cost has to beat both book order and
#     span length rather than agreeing with them.
#   * ``汛期观测`` / ``汛情`` / ``主汛情`` — a two-code-point name strictly
#     inside a four-code-point one (longest-match suppression), plus two terms
#     matching identical characters, which are *not* in a containment relation
#     and must both survive.
#   * ``锚站`` → ``浮标阵列`` — a model-decomposed concept with exactly one
#     child, so one hop costs a plain hop against ``1 + log2(20)`` out of the
#     hub.  ``锚站的缆索`` sits in its TOC child and must never surface.
#   * ``枢对锚站的校验`` — a seven-code-point phrase that spans several jieba
#     tokens and that no segmenter emits as one token, which is why only the
#     query is segmented and never the terms.
#   * ``律`` inside ``规律`` — a one-code-point concept name landing inside a
#     common two-code-point word: rejected with a segmenter, admitted without
#     one.  Both halves are pinned, so the degraded path cannot rot unnoticed.
#   * ``锚`` inside ``枢对锚站的校验`` — a one-code-point alias suppressed by
#     containment whether or not boundaries are available.
#   * ``观测网所必经的六道闸门`` — a TOC parent with six leaf children, whose
#     concept ``六道闸门`` is a one-mention island with no relation of any
#     predicate, so only TOC structure can reach the sections that answer it.
#   * ``巡检记录`` — mentioned under two TOC nodes, therefore bound to neither
#     and admitted from neither; the rule that stops the TOC channel becoming
#     a second hub.
#   * ``全网的结算`` plus 65 fillers — an over-budget TOC node whose expansion
#     is skipped whole and reported degraded, never truncated.
#
# Counts that appear in prose below (``174 spans``, ``778``, ``851``, ``20``
# children) are real measurements against the real store.  The names they are
# attached to are not.


def _boundaries(text: str):
    """Real segmentation for ``text``, so the tests pin the shipped tokenizer.

    A stub would let the boundary rule pass while the tokenizer this actually
    ships with disagreed about where `枢对锚站的校验的规律` breaks, which is the
    only thing that decides whether the fix works.
    """
    assert SEGMENTER is not None
    return SEGMENTER.boundaries(text)


def _hash(value: str) -> str:
    return sha256(value.encode('utf-8')).hexdigest()


def _span_length(span) -> int:
    """An unanchored mention is not a span, so it has no length to rank by."""
    if span['start_codepoint'] is None or span['end_codepoint'] is None:
        return 0
    return int(span['end_codepoint']) - int(span['start_codepoint'])


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
            'p1': self._passage('p1', '第一章', 'TCP 是传输控制协议。原文完整保留。'),
            'p2': self._passage('p2', '第二章', 'HTTP 与 TCP 不同。第二段完整保留。'),
            'p3': self._passage('p3', '第三章', '概念检索也应返回完整原文段落。'),
        }
        self.terms = [
            {'concept_id': 'tcp', 'canonical_name': 'TCP', 'term': 'TCP'},
            {'concept_id': 'tcp', 'canonical_name': 'TCP', 'term': 'Transmission Control Protocol'},
            {'concept_id': '中文', 'canonical_name': '检索', 'term': '检索'},
            {'concept_id': 'parent', 'canonical_name': '父主题', 'term': '父主题'},
            {'concept_id': 'child', 'canonical_name': '子主题', 'term': '子主题'},
        ]
        self.occurrences = [
            {
                **self.passages['p1'],
                'concept_id': 'tcp',
                'canonical_name': 'TCP',
                'start_codepoint': 0,
                'end_codepoint': 3,
            },
            {
                **self.passages['p2'],
                'concept_id': 'tcp',
                'canonical_name': 'TCP',
                'start_codepoint': 7,
                'end_codepoint': 10,
            },
            {
                **self.passages['p2'],
                'concept_id': 'tcp',
                'canonical_name': 'TCP',
                'start_codepoint': None,
                'end_codepoint': None,
            },
            {
                **self.passages['p1'],
                'concept_id': 'parent',
                'canonical_name': '父主题',
                'start_codepoint': 0,
                'end_codepoint': 3,
            },
            {
                **self.passages['p3'],
                'concept_id': 'child',
                'canonical_name': '子主题',
                'start_codepoint': 0,
                'end_codepoint': 2,
            },
        ]
        self.relations = [{'subject_concept_id': 'parent', 'predicate': 'HAS_PART', 'object_concept_id': 'child'}]
        self.units = {
            'u1': self._unit('u1', 'p1', 0, 8),
            'u2': self._unit('u2', 'p2', 0, 9),
            'u3': self._unit('u3', 'p3', 0, 8),
        }
        # Every ``list_concept_occurrences`` call, so a test can assert what
        # relevance the service declared as well as what came back.
        self.occurrence_calls: list[dict[str, object]] = []
        # Every ``list_concept_relation_neighbors`` call, so a test can assert
        # which predicates were walked, in which direction, and out of what —
        # the last of which is how "depth 1 is a hard stop" is checked without
        # inferring it from the concepts that came back.
        self.relation_calls: list[dict[str, object]] = []
        # How often the vocabulary was actually re-read, so a test can tell a
        # reused matcher from a rebuilt one without timing anything.
        self.term_reads = 0

    @staticmethod
    def _passage(passage_id: str, chapter: str, content: str) -> dict[str, object]:
        return {
            'passage_id': passage_id,
            'book_title': '网络原理',
            'toc_path': (chapter,),
            'content': content,
            'content_sha256': _hash(content),
        }

    def _unit(self, unit_id: str, passage_id: str, start: int, end: int) -> dict[str, object]:
        content = str(self.passages[passage_id]['content'])[start:end]
        return {
            'retrieval_unit_id': unit_id,
            'passage_id': passage_id,
            'start_codepoint': start,
            'end_codepoint': end,
            'content': content,
            'content_sha256': _hash(content),
        }

    def list_concept_terms(self):
        self.term_reads += 1
        return self.terms

    def concept_term_fingerprint(self):
        """Stand in for the store's aggregate: any change to the vocabulary moves it."""
        return (len(self.terms), tuple(sorted(term['term'] for term in self.terms)))

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
        rows = [row for row in self.occurrences if row['concept_id'] in concept_ids]
        spans = []
        for passage_id, start, end in dict.fromkeys(
            (row['passage_id'], row['start_codepoint'], row['end_codepoint']) for row in rows
        ):
            contained_by_another = any(
                other['passage_id'] == passage_id
                and other['start_codepoint'] is not None
                and start is not None
                and other['start_codepoint'] <= start
                and other['end_codepoint'] >= end
                and (other['start_codepoint'] < start or other['end_codepoint'] > end)
                for other in rows
            )
            if contained_by_another:
                continue
            attributed = [
                row
                for row in rows
                if row['passage_id'] == passage_id
                and _contains(start, end, row['start_codepoint'], row['end_codepoint'])
            ]
            spans.append(
                {
                    **self.passages[passage_id],
                    'start_codepoint': start,
                    'end_codepoint': end,
                    'concept_ids': tuple(sorted({row['concept_id'] for row in attributed})),
                    'canonical_names': tuple(sorted({row['canonical_name'] for row in attributed})),
                }
            )
        costs = concept_costs or {}
        spans.sort(
            key=lambda span: (
                min((costs.get(concept, 0.0) for concept in span['concept_ids']), default=0.0),
                -len(span['concept_ids']),
                -_span_length(span),
            )
        )
        return spans

    def count_concept_occurrences(self, concept_ids):
        return len(self._occurrence_spans(concept_ids))

    def list_concept_occurrences(self, concept_ids, *, offset, limit, concept_costs=None):
        self.occurrence_calls.append(
            {'concept_ids': tuple(concept_ids), 'offset': offset, 'limit': limit, 'costs': dict(concept_costs or {})}
        )
        return self._occurrence_spans(concept_ids, concept_costs)[offset : offset + limit]

    def get_search_passage(self, passage_id):
        return self.passages.get(passage_id)

    def get_retrieval_unit(self, retrieval_unit_id):
        return self.units.get(retrieval_unit_id)

    def matched_concept_names(self, passage_id, concept_ids):
        return ['TCP'] if passage_id in {'p1', 'p2'} and 'tcp' in concept_ids else []

    def list_concept_relation_neighbors(self, concept_ids, *, predicates=('HAS_PART',), direction='outgoing'):
        """Mirror the store contract, including which end of an edge was asked about.

        The row shape is the same for every direction — subject, predicate,
        object as stored — because deciding which end is the *neighbour* is the
        caller's job and not the repository's.  ``both`` returns the edge once
        even when both of its ends were queried, since it is one edge.
        """
        self.relation_calls.append({'concept_ids': tuple(concept_ids), 'predicates': tuple(predicates), 'direction': direction})
        matches = {
            'outgoing': lambda relation: relation['subject_concept_id'] in concept_ids,
            'incoming': lambda relation: relation['object_concept_id'] in concept_ids,
            'both': lambda relation: relation['subject_concept_id'] in concept_ids
            or relation['object_concept_id'] in concept_ids,
        }[direction]
        return [
            relation for relation in self.relations if matches(relation) and relation['predicate'] in predicates
        ]


class HubFakeSource(FakeSource):
    """The acceptance corpus's shape, reduced to what ranking has to get right.

    Names are invented (see the substitution note at the top of this module);
    the shape is the real one.  Three things about that corpus matter here.  It opens with front matter, so
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

    PREFACE = '前言：本册所收录的是枢的运行记录选编，供人查阅。'
    FLOOD = '枢在汛期观测，通知值守员建锚站，以保全流量记录的完整。'
    JUDGMENT = '汛期观测之后的水位与浮标阵列读数都得以留存，为下一轮标定作了铺垫。'
    HUB_CHILD = '枢纽的基准线由长期观测归算得到，只能从历次校验的残差中读出它的稳定程度。'
    ARK_CHILD = '锚站建成之后，枢即调度值守员将浮标阵列布放到位。'

    @staticmethod
    def _passage(passage_id: str, chapter: str, content: str) -> dict[str, object]:
        return {
            'passage_id': passage_id,
            'book_title': '潮汐观测总志',
            'toc_path': (chapter,),
            'content': content,
            'content_sha256': _hash(content),
        }

    def __init__(self) -> None:
        super().__init__()
        self.passages = {
            'preface': self._passage('preface', '前言', self.PREFACE),
            'flood': self._passage('flood', '第四章', self.FLOOD),
            'judgment': self._passage('judgment', '第四章', self.JUDGMENT),
            'righteousness': self._passage('righteousness', '第九章', self.HUB_CHILD),
            'ark': self._passage('ark', '第四章', self.ARK_CHILD),
        }
        self.terms = [
            {'concept_id': 'hub', 'canonical_name': '全域潮汐枢纽', 'term': '全域潮汐枢纽'},
            {'concept_id': 'hub', 'canonical_name': '全域潮汐枢纽', 'term': '枢'},
            {'concept_id': 'flood', 'canonical_name': '汛期观测', 'term': '汛期观测'},
            {'concept_id': 'ark', 'canonical_name': '锚站', 'term': '锚站'},
        ]
        # Book order, deliberately opening with the preface: the front-matter
        # span is the earliest mention of the hub in the whole book.
        self.occurrences = [
            {
                **self.passages['preface'],
                'concept_id': 'hub',
                'canonical_name': '全域潮汐枢纽',
                'start_codepoint': 10,
                'end_codepoint': 11,
            },
            {
                **self.passages['flood'],
                'concept_id': 'flood',
                'canonical_name': '汛期观测',
                'start_codepoint': 0,
                'end_codepoint': 15,
            },
            {
                **self.passages['flood'],
                'concept_id': 'hub',
                'canonical_name': '全域潮汐枢纽',
                'start_codepoint': 0,
                'end_codepoint': 15,
            },
            {
                **self.passages['judgment'],
                'concept_id': 'flood',
                'canonical_name': '汛期观测',
                'start_codepoint': 0,
                'end_codepoint': 13,
            },
            # Reached only by expanding out of the hub, yet earlier in the book
            # than nothing and longer than the flood span.  Book order and span
            # length both favour it; relation cost must not.
            {
                **self.passages['righteousness'],
                'concept_id': 'righteousness',
                'canonical_name': '枢纽的基准线',
                'start_codepoint': 0,
                'end_codepoint': 35,
            },
            {
                **self.passages['ark'],
                'concept_id': 'ark_animals',
                'canonical_name': '浮标阵列',
                'start_codepoint': 0,
                'end_codepoint': 23,
            },
        ]
        # 20 children under the hub, one child under `锚站`: one hop out of the
        # hub is worth far less than one hop out of a concept that names a
        # single part.
        self.relations = [
            {'subject_concept_id': 'hub', 'predicate': 'HAS_PART', 'object_concept_id': name}
            for name in ('righteousness', *(f'hub-child-{index}' for index in range(19)))
        ] + [{'subject_concept_id': 'ark', 'predicate': 'HAS_PART', 'object_concept_id': 'ark_animals'}]
        self.units = {}

    def matched_concept_names(self, passage_id, concept_ids):
        return sorted(
            {
                row['canonical_name']
                for row in self.occurrences
                if row['passage_id'] == passage_id and row['concept_id'] in concept_ids
            }
        )


class GenericTermFakeSource(FakeSource):
    """One concept reachable by two very different surface forms.

    ``全域潮汐枢纽`` is what the book is about: 175 mentions and 20 grounded
    ``HAS_PART`` children.  It is reachable by its full canonical name and by
    the one-character alias ``枢`` a model proposed.  The whole resolution guard
    is the claim that those two matches are not the same event — naming it in
    full is a request for it, while a stray 枢 inside some other question is
    not — so the fixture exists to make a single concept answer both ways.

    ``汛期观测`` is the ordinary case: a four-character name on a two-mention
    concept with one child, which has always expanded and must keep expanding.

    The specificity columns are supplied exactly as the SQLite store supplies
    them.  :class:`FakeSource` deliberately supplies none of them, which is how
    the tests keep proving that a repository answering the older three-column
    shape still expands exactly as it did.
    """

    HUB = '枢纽调度的时序无人能凭直觉复现，只能从记录中还原。'
    HUB_CHILD = '枢纽的基准线由长期观测归算得到。'
    FLOOD = '枢在汛期观测，通知值守员建锚站。'
    ARK = '锚站建成之后，浮标阵列都已就位。'

    def __init__(self) -> None:
        super().__init__()
        self.passages = {
            'hub': self._passage('hub', '第一章', self.HUB),
            'righteousness': self._passage('righteousness', '第九章', self.HUB_CHILD),
            'flood': self._passage('flood', '第四章', self.FLOOD),
            'ark': self._passage('ark', '第四章', self.ARK),
        }
        self.terms = [
            {
                'concept_id': 'hub',
                'canonical_name': '全域潮汐枢纽',
                'term': '全域潮汐枢纽',
                'term_source': 'MODEL',
                'mention_count': 175,
                'has_part_fanout': 20,
            },
            {
                'concept_id': 'hub',
                'canonical_name': '全域潮汐枢纽',
                'term': '枢',
                'term_source': 'MODEL',
                'mention_count': 175,
                'has_part_fanout': 20,
            },
            {
                'concept_id': 'flood',
                'canonical_name': '汛期观测',
                'term': '汛期观测',
                'term_source': 'MODEL',
                'mention_count': 2,
                'has_part_fanout': 1,
            },
        ]
        self.occurrences = [
            {
                **self.passages['hub'],
                'concept_id': 'hub',
                'canonical_name': '全域潮汐枢纽',
                'start_codepoint': 0,
                'end_codepoint': 6,
            },
            {
                **self.passages['righteousness'],
                'concept_id': 'righteousness',
                'canonical_name': '枢纽的基准线',
                'start_codepoint': 0,
                'end_codepoint': 6,
            },
            {
                **self.passages['flood'],
                'concept_id': 'flood',
                'canonical_name': '汛期观测',
                'start_codepoint': 0,
                'end_codepoint': 8,
            },
            {
                **self.passages['ark'],
                'concept_id': 'ark_animals',
                'canonical_name': '浮标阵列',
                'start_codepoint': 0,
                'end_codepoint': 6,
            },
        ]
        self.relations = [
            {'subject_concept_id': 'hub', 'predicate': 'HAS_PART', 'object_concept_id': 'righteousness'},
            {'subject_concept_id': 'flood', 'predicate': 'HAS_PART', 'object_concept_id': 'ark_animals'},
        ]
        self.units = {}

    def matched_concept_names(self, passage_id, concept_ids):
        return sorted(
            {
                row['canonical_name']
                for row in self.occurrences
                if row['passage_id'] == passage_id and row['concept_id'] in concept_ids
            }
        )


class TocFakeSource(FakeSource):
    """The acceptance corpus's TOC shape, reduced to the four rules that bind it.

    ``观测网所必经的六道闸门`` is a real node in the parsed ``toc_nodes`` with
    the six 关口 as its children, while the concept ``六道闸门`` is a
    one-mention island with no relation of any predicate.  Before TOC
    expansion, the query that names it returned exactly one span — the heading
    — and the sections that answer it were unreachable.

    Everything else in this fixture exists to be *refused*:

    * ``锚站`` was decomposed by the model, so its TOC children must stay out of
      the way entirely — ``锚站的缆索`` sits in its child node and must never
      appear.
    * ``巡检记录`` is mentioned inside a 关口 section *and* somewhere else, so it
      is bound to neither and is admitted from neither.
    * ``全网的结算`` binds to a node whose children hold more concepts than the
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
            'gates': self._passage('gates', '观测网所必经的六道闸门', '观测网所必经的六道闸门'),
            'birth': self._passage('birth', '第一闸门　流量校准', '流量校准记录见于巡检记录之中。'),
            'growth': self._passage('growth', '第二闸门　基线复核', '基线复核决定后续标定的取值。'),
            'ark': self._passage('ark', '锚站', '锚站建成之后，值守员随即布放。'),
            'timber': self._passage('timber', '锚站的缆索', '缆索是锚站所用的主要构件。'),
            'animals': self._passage('animals', '浮标阵列', '浮标阵列都已布入锚站周边。'),
            'elsewhere': self._passage('elsewhere', '另一章', '巡检记录也出现在别处。'),
            'crowd': self._passage('crowd', '全网的结算', '全网的结算各不相同。'),
            'crowd_body': self._passage('crowd_body', '结算细目', '细目一二三四五六七八九十。'),
        }
        # concept -> the one TOC node every one of its mentions falls under.
        # ``everywhere`` is absent because it is mentioned under two nodes,
        # which is exactly what makes it unbound.
        self.bindings = {
            'gates': 'n-gates',
            'birth': 'n-gate-one',
            'growth': 'n-gate-two',
            'ark': 'n-ark',
            'timber': 'n-timber',
            'animals': 'n-elsewhere',
            'crowd': 'n-crowd',
            **{f'filler-{index}': 'n-crowd-detail' for index in range(self.FILLERS)},
        }
        self.toc_children = {
            'n-gates': ('n-gate-one', 'n-gate-two'),
            'n-ark': ('n-timber',),
            'n-crowd': ('n-crowd-detail',),
        }
        self.terms = [
            self._term('gates', '六道闸门', '六道闸门', mentions=1, fanout=0),
            self._term('birth', '流量校准', '流量校准', mentions=1, fanout=0),
            self._term('growth', '基线复核', '基线复核', mentions=1, fanout=0),
            # Decomposed by the model: TOC structure must defer to that.
            self._term('ark', '锚站', '锚站', mentions=2, fanout=1),
            self._term('crowd', '全网的结算', '全网的结算', mentions=1, fanout=0),
        ]
        self.occurrences = [
            self._occurrence('gates', '六道闸门', 'gates', 7, 11),
            self._occurrence('birth', '流量校准', 'birth', 0, 4),
            self._occurrence('growth', '基线复核', 'growth', 0, 4),
            self._occurrence('ark', '锚站', 'ark', 0, 2),
            self._occurrence('timber', '锚站的缆索', 'timber', 0, 3),
            self._occurrence('animals', '浮标阵列', 'animals', 0, 4),
            self._occurrence('everywhere', '巡检记录', 'birth', 8, 12),
            self._occurrence('everywhere', '巡检记录', 'elsewhere', 0, 4),
            self._occurrence('crowd', '全网的结算', 'crowd', 0, 5),
        ] + [
            self._occurrence(f'filler-{index}', f'细目{index}', 'crowd_body', index, index + 1)
            for index in range(self.FILLERS)
        ]
        self.relations = [
            {'subject_concept_id': 'ark', 'predicate': 'HAS_PART', 'object_concept_id': 'animals'},
        ]
        self.units = {}

    @staticmethod
    def _term(concept_id: str, canonical: str, term: str, *, mentions: int, fanout: int) -> dict:
        return {
            'concept_id': concept_id,
            'canonical_name': canonical,
            'term': term,
            'term_source': 'MODEL',
            'mention_count': mentions,
            'has_part_fanout': fanout,
        }

    def _occurrence(self, concept_id: str, canonical: str, passage_id: str, start: int, end: int) -> dict:
        return {
            **self.passages[passage_id],
            'concept_id': concept_id,
            'canonical_name': canonical,
            'start_codepoint': start,
            'end_codepoint': end,
        }

    def matched_concept_names(self, passage_id, concept_ids):
        return sorted(
            {
                row['canonical_name']
                for row in self.occurrences
                if row['passage_id'] == passage_id and row['concept_id'] in concept_ids
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
                            'seed_concept_id': concept_id,
                            'seed_toc_node_id': node,
                            'concept_id': other,
                        }
                    )
        return rows


class EnumerationFakeSource(FakeSource):
    """One enumerated TOC node, in the shape fused ranking has to survive.

    Names are invented (see the substitution note at the top of this module).
    The shape is the acceptance book's, and every part of it was measured on the
    query that motivated the sibling rule:

    * ``观测网所必经的六道闸门`` is a TOC parent with **six** child sections and
      is the node TOC-child expansion reaches.  All six enter the candidate pool
      and only five used to come back.
    * The parent node carries **two passages of its own** — the heading and a
      framing sentence.  Both outscore most of the sections, and neither is an
      item of the list, so neither may be a sibling of one.
    * ``第一闸门`` and ``第六闸门`` each hold **two passages**, one reached by the
      graph channel and one only by the vector channel.  This is the case that
      breaks a per-candidate provenance test: on the real book the best-scoring
      span of most sections came from the channel that labels nothing, so
      membership has to be a property of the *node*, not of the retrieval path.
    * ``1）量程分档`` is a **sub-node of the fourth section**, so a hit in it is
      one level too deep to be a seventh item.
    * ``另一组`` is a second parent with three child sections that TOC-child
      expansion never touched.  It exists to be treated as ordinary: sharing a
      TOC parent is association, not decomposition, and only expansion says
      otherwise.

    ``spine_index``/``ordinal`` are supplied exactly as the SQLite store
    supplies them, because the order sections come back in is part of the
    answer.  :class:`FakeSource` supplies neither, which is how the tests keep
    proving a repository without them still returns the same citations.
    """

    CHAPTER = '闸门总述'
    PARENT = '观测网所必经的六道闸门'
    SECTIONS = (
        ('gate-1', '第一闸门　流量校准', '流量校准'),
        ('gate-2', '第二闸门　基线复核', '基线复核'),
        ('gate-3', '第三闸门　相位对齐', '相位对齐'),
        ('gate-4', '第四闸门　量程标定', '量程标定'),
        ('gate-5', '第五闸门　残差归算', '残差归算'),
        ('gate-6', '第六闸门　停机封存', '停机封存'),
    )

    def __init__(self) -> None:
        super().__init__()
        self.passages = {
            'gates': self._ordered('gates', (self.CHAPTER, self.PARENT), '观测网所必经的六道闸门', 0, 10),
            'gates_body': self._ordered(
                'gates_body', (self.CHAPTER, self.PARENT), '每一个观测网都要经过几道关键闸门。', 0, 11
            ),
            'g1': self._ordered('g1', self._path(1), '流量校准决定后续标定的取值。', 0, 20),
            'g1b': self._ordered('g1b', self._path(1), '流量校准的复核在每季度进行一次。', 0, 21),
            'g2': self._ordered('g2', self._path(2), '基线复核由长期观测归算得到。', 0, 30),
            'g3': self._ordered('g3', self._path(3), '相位对齐要求各站共用同一时基。', 0, 40),
            'g4': self._ordered('g4', self._path(4), '量程标定按分档逐级完成。', 0, 50),
            'g4sub': self._ordered('g4sub', (*self._path(4), '1）量程分档'), '量程分档的档位由历史极值决定。', 0, 55),
            'g5': self._ordered('g5', self._path(5), '残差归算给出本轮校验的判定。', 0, 60),
            'g6': self._ordered('g6', self._path(6), '停机封存', 0, 70),
            'g6v': self._ordered('g6v', self._path(6), '停机封存之前要留存最后一组读数。', 0, 71),
            'o1': self._ordered('o1', ('另一章', '另一组', '甲节'), '甲节的记录另作说明。', 1, 10),
            'o2': self._ordered('o2', ('另一章', '另一组', '乙节'), '乙节的记录另作说明。', 1, 11),
            'o3': self._ordered('o3', ('另一章', '另一组', '丙节'), '丙节的记录另作说明。', 1, 12),
        }
        self.bindings = {'gates': 'n-gates', **{concept: node for node, _, concept in self.SECTIONS}}
        self.toc_children = {'n-gates': tuple(node for node, _, _ in self.SECTIONS)}
        self.terms = [
            TocFakeSource._term('gates', '六道闸门', '六道闸门', mentions=1, fanout=0),
            *(TocFakeSource._term(concept, concept, concept, mentions=1, fanout=0) for _, _, concept in self.SECTIONS),
        ]
        # One graph span per section, plus the seed's own heading span.  The
        # vector-only passages carry no mention at all, which is the whole point
        # of ``g1b``/``g6v``/``g4sub``: they are reachable by embedding and by
        # nothing else.
        self.occurrences = [
            self._occurrence('gates', '六道闸门', 'gates', 7, 11),
            *(
                self._occurrence(concept, concept, f'g{index}', 0, len(concept))
                for index, (_, _, concept) in enumerate(self.SECTIONS, start=1)
            ),
        ]
        self.relations = []
        self.units = {
            passage_id: self._whole(passage_id)
            for passage_id in ('gates_body', 'g1b', 'g4sub', 'g6v', 'o1', 'o2', 'o3')
        }

    def _path(self, section: int) -> tuple[str, ...]:
        return (self.CHAPTER, self.PARENT, self.SECTIONS[section - 1][1])

    def _ordered(self, passage_id, toc_path, content, spine_index, ordinal) -> dict[str, object]:
        return {
            'passage_id': passage_id,
            'book_title': '潮汐观测总志',
            'toc_path': tuple(toc_path),
            'content': content,
            'content_sha256': _hash(content),
            'spine_index': spine_index,
            'ordinal': ordinal,
        }

    def _whole(self, passage_id: str) -> dict[str, object]:
        content = str(self.passages[passage_id]['content'])
        return {
            'retrieval_unit_id': passage_id,
            'passage_id': passage_id,
            'start_codepoint': 0,
            'end_codepoint': len(content),
            'content': content,
            'content_sha256': _hash(content),
        }

    _occurrence = TocFakeSource._occurrence
    matched_concept_names = TocFakeSource.matched_concept_names
    list_toc_child_concepts = TocFakeSource.list_toc_child_concepts



class PredicateGraphFakeSource(FakeSource):
    """All six stored predicates on one seed, shaped to the traversal each earns.

    Names are invented (see the substitution note at the top of this module).
    The *shape* is the acceptance graph's: six predicates, a containment
    subtree deeper than two hops, an ``ELABORATES`` edge whose stored direction
    points back at the seed, and a sequential chain a query can land in the
    middle of.  Measured on the real store the six predicates are distributed
    roughly 160 / 64 / 37 / 16 / 10 / 3, so five of them together carry about as
    many edges as ``HAS_PART`` does alone; walking only the first left half the
    graph unreachable at query time.

    Every branch below exists to pin one boundary, and each one is a boundary a
    plain "walk everything" implementation crosses:

    * ``全域潮汐枢纽`` → ``枢纽的基准线`` → ``浮标阵列`` → ``锚站的缆索`` — a
      containment chain three deep.  Two hops are reached, the third is not.
    * ``巡检记录`` **elaborates** ``全域潮汐枢纽``, so it is reachable from the
      hub only by walking that edge backwards, and ``历年归档`` beyond it is not
      reachable at all.  The stored direction of ``ELABORATES`` does not survive
      inspection on the real graph — its edges lean about 2:1 towards
      subject-as-topic, which is the opposite of what the predicate's name
      asserts, and two pairs are stated both ways — so both ends are walked and
      the second hop is refused instead.
    * ``第一闸门`` → ``第二闸门`` → ``第三闸门`` → ``第四闸门`` — a sequential
      chain.  Seeded on the *middle* link it must reach both neighbours, which
      is the whole reason these predicates are bidirectional, and must not reach
      the fourth, which is what "an associative hop is never expanded from"
      means concretely.
    * ``泥沙淤积`` **causes** ``基线漂移``; ``高潮位`` **contrasts** ``低潮位``;
      ``校验流程`` is a **prerequisite** of ``读数核对``.  Three more
      bidirectional pairs, present so the remaining predicates are exercised by
      name rather than assumed to behave like the one that was.
    * The hub also **causes** ``枢纽的基准线``, which containment already
      reaches more cheaply.  One concept, two predicates, one hop each: the
      provenance a reader is shown has to name the edge that won.
    """

    def __init__(self) -> None:
        super().__init__()
        self.passages = {
            concept_id: self._passage(concept_id, chapter, content)
            for concept_id, chapter, content in (
                ('hub', '第一章', '全域潮汐枢纽按班次编排全网的观测时序。'),
                ('baseline', '第二章', '枢纽的基准线由长期观测归算得到。'),
                ('array', '第三章', '浮标阵列沿岸线布放，逐点回报水位。'),
                ('cable', '第四章', '锚站的缆索每季度换新一次。'),
                ('log', '第五章', '巡检记录按站汇总，随月报一并归档。'),
                ('archive', '第六章', '历年归档只在复核时调阅。'),
                ('gate_one', '第七章', '第一闸门先行放水，为后续留出余量。'),
                ('gate_two', '第八章', '第二闸门在此之后开启。'),
                ('gate_three', '第九章', '第三闸门收束尾水。'),
                ('gate_four', '第十章', '第四闸门只在大汛时启用。'),
                ('silt', '第十一章', '泥沙淤积改变了断面形状。'),
                ('drift', '第十二章', '基线漂移由长期淤积累积而成。'),
                ('tide_high', '第十三章', '高潮位出现在每日两次的峰段。'),
                ('tide_low', '第十四章', '低潮位是同一周期的谷段。'),
                ('calib', '第十五章', '校验流程须在核对之前走完。'),
                ('reading', '第十六章', '读数核对以校验后的基准为准。'),
            )
        }
        self.terms = [
            self._named_term(concept_id, name)
            for concept_id, name in (
                ('hub', '全域潮汐枢纽'),
                ('gate_two', '第二闸门'),
                ('drift', '基线漂移'),
                ('tide_low', '低潮位'),
                ('reading', '读数核对'),
                ('log', '巡检记录'),
            )
        ]
        # One span per concept, so ``graph_total`` counts reached concepts and a
        # blow-up in the walk shows up directly in the number.
        self.occurrences = [
            {
                **self.passages[concept_id],
                'concept_id': concept_id,
                'canonical_name': name,
                'start_codepoint': 0,
                'end_codepoint': len(name),
            }
            for concept_id, name in (
                ('hub', '全域潮汐枢纽'),
                ('baseline', '枢纽的基准线'),
                ('array', '浮标阵列'),
                ('cable', '锚站的缆索'),
                ('log', '巡检记录'),
                ('archive', '历年归档'),
                ('gate_one', '第一闸门'),
                ('gate_two', '第二闸门'),
                ('gate_three', '第三闸门'),
                ('gate_four', '第四闸门'),
                ('silt', '泥沙淤积'),
                ('drift', '基线漂移'),
                ('tide_high', '高潮位'),
                ('tide_low', '低潮位'),
                ('calib', '校验流程'),
                ('reading', '读数核对'),
            )
        ]
        self.relations = [
            {'subject_concept_id': subject, 'predicate': predicate, 'object_concept_id': object_}
            for subject, predicate, object_ in (
                ('hub', 'HAS_PART', 'baseline'),
                ('baseline', 'HAS_PART', 'array'),
                ('array', 'HAS_PART', 'cable'),
                ('hub', 'CAUSES', 'baseline'),
                ('log', 'ELABORATES', 'hub'),
                ('log', 'ELABORATES', 'archive'),
                ('gate_one', 'PRECEDES', 'gate_two'),
                ('gate_two', 'PRECEDES', 'gate_three'),
                ('gate_three', 'PRECEDES', 'gate_four'),
                ('silt', 'CAUSES', 'drift'),
                ('tide_high', 'CONTRASTS', 'tide_low'),
                ('calib', 'PREREQUISITE', 'reading'),
            )
        ]
        self.units = {}

    @staticmethod
    def _passage(passage_id: str, chapter: str, content: str) -> dict[str, object]:
        return {
            'passage_id': passage_id,
            'book_title': '潮汐观测总志',
            'toc_path': (chapter,),
            'content': content,
            'content_sha256': _hash(content),
        }

    @staticmethod
    def _named_term(concept_id: str, name: str) -> dict[str, object]:
        return {
            'concept_id': concept_id,
            'canonical_name': name,
            'term': name,
            'term_source': 'MODEL',
            'mention_count': 1,
            # Every seed here was decomposed by the model, so the TOC fallback
            # stays out of the way and these tests measure the relation walk and
            # nothing else.
            'has_part_fanout': 1,
        }

    def matched_concept_names(self, passage_id, concept_ids):
        return sorted(
            {
                row['canonical_name']
                for row in self.occurrences
                if row['passage_id'] == passage_id and row['concept_id'] in concept_ids
            }
        )

    def reached(self, query: str, **kwargs) -> set[str]:
        """The canonical names one query's graph channel reaches, spans and all."""
        response = EpubSearchService(source=self).search(query, graph_limit=50, **kwargs)
        return {name for hit in response.graph_results for name in hit.matched_concepts}


class TierTwoFakeSource(FakeSource):
    """A store whose Tier-1 answers are weak in three structurally different ways.

    Names are invented (see the substitution note at the top of this module).
    Tier 2 exists for the reader whose wording the vocabulary does not contain,
    so every fixture here is about a *quality* of a Tier-1 answer rather than
    about a particular word, and each one isolates exactly one of the three
    conditions the weak-match gate tests:

    * ``锚站`` — the **strong** case, and the control.  Named in full, so it
      seeds expansion; three of its own spans plus its ``HAS_PART`` child clear
      the ``graph_total`` floor comfortably.  A query naming it must never reach
      the resolver at all.
    * ``全域潮汐枢纽`` reached by its one-code-point alias ``枢`` — a match that
      **survives suppression and may not seed**.  The old one-character-alias
      defect fired inside a longer word and token-aligned matching has removed
      that; a query that *is* the single character is still boundary-valid, so
      the weak match is reproduced without depending on the behaviour that was
      fixed.  Three front-matter spans deliberately clear the ``graph_total``
      floor, so this fixture can only trip the seed condition.
    * ``巡检记录`` — a match that is **strong in every way except the answer**.
      Its full four-code-point name, so it seeds; no relation of any predicate
      and one appendix mention, so the whole graph channel is one span.  Only
      the floor condition can see this one.
    * ``漂移改正`` — the concept nothing in the fixture's queries spells, living
      in a passage the vector channel finds.  It is what a working Tier 2
      resolves *to*.
    * ``细目``-style fillers, 80 of them in one passage — more concepts under one
      vector hit than the candidate budget allows, so the shortlist has to be
      bounded rather than merely smaller, and the whole vocabulary is over
      budget, so a store with no vector channel has to refuse rather than fall
      back to sending all of it.
    """

    FILLERS = 80

    STATION_A = '锚站的布放先要确认底质与水深。'
    STATION_B = '锚站在汛期前后各校验一次。'
    STATION_C = '锚站与浮标阵列一同构成前端。'
    PREFACE_A = '枢的运行记录选编，供人查阅。'
    PREFACE_B = '枢所辖各站的沿革附于卷末。'
    PREFACE_C = '枢历年调度的时刻表另行刊印。'
    DRIFT = '漂移改正把缆索走位带来的偏差从读数里扣除。'
    LOG = '巡检记录只在附录里出现过一次。'
    BULK = '细目一二三四五六七八九十。'

    @staticmethod
    def _passage(passage_id: str, chapter: str, content: str) -> dict[str, object]:
        return {
            'passage_id': passage_id,
            'book_title': '潮汐观测总志',
            'toc_path': (chapter,),
            'content': content,
            'content_sha256': _hash(content),
        }

    def __init__(self) -> None:
        super().__init__()
        self.passages = {
            'station-a': self._passage('station-a', '第二章', self.STATION_A),
            'station-b': self._passage('station-b', '第二章', self.STATION_B),
            'station-c': self._passage('station-c', '第二章', self.STATION_C),
            'preface-a': self._passage('preface-a', '前言', self.PREFACE_A),
            'preface-b': self._passage('preface-b', '前言', self.PREFACE_B),
            'preface-c': self._passage('preface-c', '前言', self.PREFACE_C),
            'drift': self._passage('drift', '第五章', self.DRIFT),
            'log': self._passage('log', '附录', self.LOG),
            'bulk': self._passage('bulk', '附录', self.BULK),
        }
        self.terms = [
            self._term('station', '锚站', '锚站', fanout=1),
            self._term('hub', '全域潮汐枢纽', '全域潮汐枢纽', fanout=1),
            self._term('hub', '全域潮汐枢纽', '枢', fanout=1),
            self._term('drift', '漂移改正', '漂移改正', fanout=0),
            self._term('log', '巡检记录', '巡检记录', fanout=0),
            self._term('array', '浮标阵列', '浮标阵列', fanout=0),
        ] + [self._term(f'filler-{index}', f'细目{index}', f'细目{index}', fanout=0) for index in range(self.FILLERS)]
        self.occurrences = [
            self._occurrence('station', '锚站', 'station-a', 0, 2),
            self._occurrence('station', '锚站', 'station-b', 0, 2),
            self._occurrence('station', '锚站', 'station-c', 0, 2),
            self._occurrence('array', '浮标阵列', 'station-c', 3, 7),
            self._occurrence('hub', '全域潮汐枢纽', 'preface-a', 0, 1),
            self._occurrence('hub', '全域潮汐枢纽', 'preface-b', 0, 1),
            self._occurrence('hub', '全域潮汐枢纽', 'preface-c', 0, 1),
            self._occurrence('baseline', '枢纽的基准线', 'preface-c', 4, 9),
            self._occurrence('drift', '漂移改正', 'drift', 0, 4),
            self._occurrence('log', '巡检记录', 'log', 0, 4),
        ] + [self._occurrence(f'filler-{index}', f'细目{index}', 'bulk', 0, 2) for index in range(self.FILLERS)]
        self.relations = [
            {'subject_concept_id': 'station', 'predicate': 'HAS_PART', 'object_concept_id': 'array'},
            {'subject_concept_id': 'hub', 'predicate': 'HAS_PART', 'object_concept_id': 'baseline'},
        ]
        self.units = {
            'u-drift': self._unit('u-drift', 'drift', 0, 10),
            'u-bulk': self._unit('u-bulk', 'bulk', 0, 6),
            'u-station': self._unit('u-station', 'station-c', 0, 7),
        }

    @staticmethod
    def _term(concept_id: str, canonical: str, term: str, *, fanout: int) -> dict:
        return {
            'concept_id': concept_id,
            'canonical_name': canonical,
            'term': term,
            'term_source': 'MODEL',
            'mention_count': 1,
            'has_part_fanout': fanout,
        }

    def _occurrence(self, concept_id: str, canonical: str, passage_id: str, start: int, end: int) -> dict:
        return {
            **self.passages[passage_id],
            'concept_id': concept_id,
            'canonical_name': canonical,
            'start_codepoint': start,
            'end_codepoint': end,
        }

    def matched_concept_names(self, passage_id, concept_ids):
        return sorted(
            {
                row['canonical_name']
                for row in self.occurrences
                if row['passage_id'] == passage_id and row['concept_id'] in concept_ids
            }
        )

    def list_passage_concept_names(self, passage_ids):
        """The store's read, written out: which concepts these passages mention.

        Deliberately unfiltered by concept, which is the whole difference from
        ``matched_concept_names``: the Tier-2 shortlist starts from passages the
        vector channel returned and asks what lives in them, not from concepts a
        query already resolved.
        """
        wanted = set(passage_ids)
        rows = []
        for passage_id, name in sorted(
            {(str(row['passage_id']), str(row['canonical_name'])) for row in self.occurrences}
        ):
            if passage_id in wanted:
                rows.append({'passage_id': passage_id, 'canonical_name': name})
        return rows


class FakeEmbeddings:
    profile = 'private-embed-v1'

    def __init__(self, *, available: bool = True):
        self.available = available
        self.calls: list[list[str]] = []

    def availability(self):
        return (
            ModelAvailability.ready('local-embedding')
            if self.available
            else ModelAvailability.degraded('local-embedding', 'runtime stopped')
        )

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0]]


class FakeReranker:
    profile = 'private-rerank-v1'

    def __init__(self, *, available: bool = True):
        self.available = available
        self.calls: list[tuple[str, list[str]]] = []

    def availability(self):
        return (
            ModelAvailability.ready('local-reranker')
            if self.available
            else ModelAvailability.degraded('local-reranker', 'runtime stopped')
        )

    def score(self, query, documents):
        self.calls.append((query, list(documents)))
        return [0.80, 0.95, 0.70][: len(documents)]


class FusionEmbeddings(FakeEmbeddings):
    """Return one query vector and deterministic vectors for graph excerpts."""

    def embed(self, texts):
        self.calls.append(list(texts))
        if len(texts) == 1 and texts[0] == 'TCP':
            return [[1.0, 0.0]]
        return [[0.0, 1.0] for _ in texts]


class FusionReranker(FakeReranker):
    def score(self, query, documents):
        self.calls.append((query, list(documents)))
        # The first call serves the legacy vector channel.  The second call
        # must contain candidates from both retrieval channels.
        return [0.40] if len(documents) == 1 else [0.95, 0.80][: len(documents)]


class FakeResolver:
    profile = 'private-resolver-v1'

    def __init__(self, result: str | None, *, available: bool = True):
        self.result = result
        self.available = available
        self.calls: list[tuple[str, list[str]]] = []

    def availability(self):
        return (
            ModelAvailability.ready('local-concept-resolver')
            if self.available
            else ModelAvailability.degraded('local-concept-resolver', 'runtime stopped')
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
        passage_id=str(unit['passage_id']),
        start_codepoint=int(unit['start_codepoint']),
        end_codepoint=int(unit['end_codepoint']),
        content_sha256=str(unit['content_sha256']),
        embedding_profile='private-embed-v1',
        vector=vector,
    )


class ScriptedEmbeddings(FakeEmbeddings):
    """Vectors named per text, so a test states the cosines it is about."""

    def __init__(self, vectors: dict[str, tuple[float, ...]], default: tuple[float, ...]) -> None:
        super().__init__()
        self.vectors = vectors
        self.default = default

    def embed(self, texts):
        self.calls.append(list(texts))
        return [self.vectors.get(text, self.default) for text in texts]


class ScriptedReranker(FakeReranker):
    """Relevance named per excerpt, so a test states the ranking it is about."""

    def __init__(self, scores: dict[str, float], default: float = 0.01) -> None:
        super().__init__()
        self.scores = scores
        self.default = default

    def score(self, query, documents):
        self.calls.append((query, list(documents)))
        return [self.scores.get(document, self.default) for document in documents]


ENUMERATION_QUERY = '六道闸门是什么'
# Three axes so "everything else" can be orthogonal to both groups a test names,
# rather than accidentally identical to one of them.
GATE_AXIS = (1.0, 0.0, 0.0)
OTHER_AXIS = (0.0, 1.0, 0.0)
UNRELATED_AXIS = (0.0, 0.0, 1.0)


def _enumeration_service(source, *, scores, vectors, **kwargs):
    """The production service, with only the two local models scripted.

    ``scores`` and ``vectors`` are keyed by the text a model would actually be
    handed — a graph excerpt or a whole vector window — so a test reads as the
    ranking it is about rather than as a table of identifiers.
    """
    vectors = {ENUMERATION_QUERY: GATE_AXIS, **vectors}
    backend = FakeVectorBackend(
        [
            _record(source, unit_id, vectors.get(str(source.units[unit_id]['content']), UNRELATED_AXIS))
            for unit_id in source.units
        ]
    )
    return EpubSearchService(
        source=source,
        vector_backend=backend,
        embeddings=ScriptedEmbeddings(vectors, default=UNRELATED_AXIS),
        reranker=ScriptedReranker(scores),
        **kwargs,
    )


def _sections(response) -> list[str]:
    """The TOC section each fused result sits in, in the order returned."""
    return [hit.toc_path[-1] for hit in response.fused_results]


class EpubSearchTest(unittest.TestCase):
    @unittest.skipIf(SEGMENTER is None, f'local query segmenter unavailable: {SEGMENTER_REASON}')
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
                ConceptTerm('tcp', 'TCP', 'TCP'),
                ConceptTerm('search', '检索', '检索'),
            ]
        )
        latin = '用 TCP 做检索'
        self.assertEqual(
            [term.concept_id for term in matcher.match(latin, boundaries=_boundaries(latin))],
            ['tcp', 'search'],
        )
        self.assertEqual(matcher.match('TCPIP', boundaries=_boundaries('TCPIP')), ())
        # `检索` inside `中文检索词` still matches: the segmenter breaks that
        # query into `中文`/`检索`/`词`, so the match is token-aligned.  The
        # assertion is unchanged from the direct-matching rule; its reason is not.
        inside = '中文检索词'
        self.assertEqual(
            [term.concept_id for term in matcher.match(inside, boundaries=_boundaries(inside))],
            ['search'],
        )

        job = '枢对锚站的校验的规律'
        trial = ConceptTermMatcher(
            [
                ConceptTerm('regularity', '规律', '律'),
                ConceptTerm('agreement', '枢与站点约定', '锚'),
                ConceptTerm('the hub', '全域潮汐枢纽', '枢'),
                ConceptTerm('job', '枢对锚站的校验', '枢对锚站的校验'),
            ]
        )
        self.assertEqual(
            [term.concept_id for term in trial.match(job, boundaries=_boundaries(job))],
            ['job'],
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
            ['job', 'regularity'],
        )

    @unittest.skipIf(SEGMENTER is None, f'local query segmenter unavailable: {SEGMENTER_REASON}')
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
                ConceptTerm('flood', '汛情', '汛情'),
                ConceptTerm('flood_world', '汛期观测', '汛期观测'),
                ConceptTerm('flood_alias', '主汛情', '汛期观测'),
            ]
        )
        query = '汛期观测的经过'
        resolved = sorted(term.concept_id for term in matcher.match(query, boundaries=_boundaries(query)))
        self.assertEqual(resolved, ['flood_alias', 'flood_world'])

        spans = matcher.match_spans(query, boundaries=_boundaries(query))
        self.assertEqual({(span.start, span.end) for span in spans}, {(0, 4)})

    @unittest.skipIf(SEGMENTER is None, f'local query segmenter unavailable: {SEGMENTER_REASON}')
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
                ConceptTerm('the hub', '全域潮汐枢纽', '枢'),
                ConceptTerm('the hub', '全域潮汐枢纽', '全域潮汐枢纽'),
            ]
        )
        query = '枢纽的运行与全域潮汐枢纽'
        spans = matcher.match_spans(query, boundaries=_boundaries(query))
        self.assertEqual(len(spans), 1)
        self.assertEqual(query[spans[0].start : spans[0].end], '全域潮汐枢纽')

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
        self.assertEqual(service.search('TCP').resolved_concepts, ('TCP',))
        self.assertEqual(source.term_reads, 1)
        self.assertEqual(service.search('TCP').resolved_concepts, ('TCP',))
        self.assertEqual(source.term_reads, 1)

        source.terms.append({'concept_id': 'tcp', 'canonical_name': 'TCP', 'term': '传输控制协议'})
        self.assertNotEqual(source.concept_term_fingerprint(), before)
        self.assertEqual(service.search('传输控制协议').resolved_concepts, ('TCP',))
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
        service._segmenter_reason = 'jieba is not importable'

        for _ in range(2):
            response = service.search('TCP')
            reported = [item for item in response.degraded if item.component == 'query-segmenter']
            self.assertEqual(len(reported), 1)
            self.assertEqual(reported[0].reason, 'jieba is not importable')
            self.assertFalse(reported[0].available)

    def test_graph_channel_is_exhaustive_over_distinct_spans_and_never_returns_only_excerpt(self) -> None:
        # Exhaustive, but over distinct source spans rather than mention rows:
        # ``graph_total`` counts the spans this query can page through.
        source = FakeSource()
        response = EpubSearchService(source=source).search('TCP', graph_offset=1, graph_limit=1)

        self.assertEqual(response.resolved_concepts, ('TCP',))
        self.assertEqual(response.graph_total, 3)
        self.assertEqual(len(response.graph_results), 1)
        hit = response.graph_results[0]
        self.assertEqual(hit.passage_id, 'p2')
        self.assertEqual(hit.content, source.passages['p2']['content'])
        self.assertEqual(hit.content[hit.excerpt.start_codepoint : hit.excerpt.end_codepoint], hit.excerpt.content)
        self.assertEqual(hit.excerpt.content, 'TCP')
        self.assertEqual(hit.provenance, ('graph',))

    def test_tier_two_local_resolver_only_accepts_existing_concepts_and_degrades_without_fallback(self) -> None:
        source = FakeSource()
        resolver = FakeResolver('TCP')
        response = EpubSearchService(source=source, concept_resolver=resolver).search('传输层连接')
        self.assertEqual(response.resolved_concepts, ('TCP',))
        self.assertEqual(len(resolver.calls), 1)
        # A five-concept vocabulary is already under the candidate budget, so it
        # is sent whole: shortlisting is a ceiling, not a requirement to own a
        # vector index.  See the shortlist tests for the store that is over it.
        self.assertEqual(resolver.calls[0][1], ['TCP', '检索', '父主题', '子主题'])

        unavailable = FakeResolver('TCP', available=False)
        response = EpubSearchService(source=source, concept_resolver=unavailable).search('传输层连接')
        self.assertEqual(response.resolved_concepts, ())
        self.assertEqual(unavailable.calls, [])
        # Asserted by component rather than by position: Channel B is now read
        # before Tier 2, so an unconfigured vector channel reports first.
        reported = [item for item in response.degraded if item.component == 'local-concept-resolver']
        self.assertEqual(len(reported), 1)
        self.assertIn('runtime stopped', reported[0].reason or '')

    def test_tier_two_fires_when_tier_one_is_weak_and_not_merely_when_it_is_empty(self) -> None:
        """The gate is weakness, which the SDD calls "no useful match".

        Tier 2 used to be consulted only when Tier 1 returned literally nothing,
        so one spurious match made the tuple truthy and the resolver was never
        asked on exactly the queries whose Tier-1 answer was worthless.  Three
        structurally different weak answers are pinned here, one per condition,
        each isolated so that a change to one condition cannot be masked by
        another:

        * ``锚站的布放`` names a concept in full — three of its own spans and a
          ``HAS_PART`` child — and is the control: strong, so the resolver is
          never reached and a configured local model costs this query nothing.
        * ``枢`` resolves ``全域潮汐枢纽`` through its one-code-point alias.  The
          match survives suppression and the concept has three front-matter
          spans, so only the *seed* condition can fire: the query brushed the
          concept without naming it.
        * ``巡检记录`` is named in full and does seed, so only the *floor*
          condition can fire: one appendix mention and no relation of any
          predicate means paging the whole graph channel shows one span.
        * A query containing no concept at all keeps the original condition.
        """
        source = TierTwoFakeSource()

        strong = FakeResolver('漂移改正')
        response = self._tier_two_service(source, strong).search('锚站的布放', graph_limit=10)
        self.assertEqual(response.resolved_concepts, ('锚站',))
        self.assertEqual(response.graph_total, 4)
        self.assertEqual(strong.calls, [])

        unseeded = FakeResolver(None)
        response = self._tier_two_service(source, unseeded).search('枢', graph_limit=10)
        self.assertEqual(response.resolved_concepts, ('全域潮汐枢纽',))
        # The weak match still resolves and still contributes every one of its
        # own spans; what the floor cannot see is that it reached nothing else.
        self.assertEqual(response.graph_total, 3)
        self.assertEqual(len(unseeded.calls), 1)

        thin = FakeResolver(None)
        response = self._tier_two_service(source, thin).search('巡检记录', graph_limit=10)
        self.assertEqual(response.resolved_concepts, ('巡检记录',))
        self.assertEqual(response.graph_total, 1)
        self.assertEqual(len(thin.calls), 1)

        empty = FakeResolver(None)
        response = self._tier_two_service(source, empty).search('缆索走位造成的偏差', graph_limit=10)
        self.assertEqual(response.resolved_concepts, ())
        self.assertEqual(len(empty.calls), 1)

    def test_tier_two_candidates_are_bounded_and_scoped_to_the_vector_channel(self) -> None:
        """The resolver is asked about a neighbourhood, never about the whole graph.

        The old payload was every distinct canonical name in the store — 1,113
        of them on the acceptance corpus, roughly 9k tokens, in front of a 3B
        model capped at 96 output tokens, which is why the tier was dead even
        where a model was running.  The shortlist is instead the concepts
        mentioned by the parent passages of Channel B's top vector candidates:
        work the request has already done, no new index, and the part of the
        system that already found where the answer lives.

        Two claims, and the fixture is built so that neither can pass by
        accident.  It is **scoped**: ``漂移改正`` comes first because the nearest
        vector candidate's passage mentions it, and ``巡检记录`` — a real concept
        in the vocabulary, in no returned passage — is absent.  And it is
        **bounded**: one of the returned passages alone mentions 80 concepts, so
        a shortlist that were merely smaller than the vocabulary would still be
        over budget.
        """
        source = TierTwoFakeSource()
        resolver = FakeResolver('漂移改正')
        backend = FakeVectorBackend([_record(source, 'u-drift', (1.0, 0.0)), _record(source, 'u-bulk', (0.9, 0.1))])
        response = EpubSearchService(
            source=source,
            vector_backend=backend,
            embeddings=FakeEmbeddings(),
            reranker=FakeReranker(),
            concept_resolver=resolver,
        ).search('缆索走位造成的偏差', graph_limit=10, vector_limit=1, vector_candidate_limit=2)

        self.assertEqual(len(resolver.calls), 1)
        candidates = resolver.calls[0][1]
        self.assertEqual(len(candidates), SEARCH._TIER_TWO_MAX_CANDIDATES)
        self.assertLess(len(candidates), len({term['canonical_name'] for term in source.terms}))
        self.assertEqual(candidates[0], '漂移改正')
        self.assertNotIn('巡检记录', candidates)
        self.assertEqual(response.resolved_concepts, ('漂移改正',))

    def test_tier_two_refuses_an_unbounded_candidate_list_rather_than_sending_it(self) -> None:
        """No vector channel and an over-budget vocabulary is a refusal, not a dump.

        The ceiling is the invariant here, not the vector channel.  A store
        whose whole vocabulary already fits under the budget is sent whole — the
        first Tier-2 test pins that — but a store over it has no shortlist to
        offer, and sending everything anyway is precisely the defect this
        removes.  The refusal is reported as a degraded component, because a
        tier that quietly declines to run is indistinguishable from one that ran
        and found nothing.
        """
        source = TierTwoFakeSource()
        resolver = FakeResolver('漂移改正')
        response = EpubSearchService(source=source, concept_resolver=resolver).search('缆索走位造成的偏差')

        self.assertEqual(resolver.calls, [])
        self.assertEqual(response.resolved_concepts, ())
        reported = [item for item in response.degraded if item.component == 'local-concept-resolver']
        self.assertEqual(len(reported), 1)
        self.assertFalse(reported[0].available)
        self.assertIn('no bounded candidate shortlist', reported[0].reason or '')

    def test_a_tier_two_answer_outside_the_vocabulary_is_rejected_by_re_validation(self) -> None:
        """The model may select an existing concept; it may never invent one.

        The answer is fed back through the same matcher that failed on the
        query, so a name the store does not hold resolves nothing and cannot
        reach the graph.  A model that answers with a concept plus commentary is
        also handled by the same pass — the matcher finds the concept inside the
        reply — which is why the re-validation is a match rather than an
        equality test.
        """
        source = TierTwoFakeSource()
        invented = FakeResolver('潮汐相位反演')
        response = self._tier_two_service(source, invented).search('缆索走位造成的偏差')
        self.assertEqual(response.resolved_concepts, ())
        reported = [item for item in response.degraded if item.component == 'local-concept-resolver']
        self.assertEqual([item.reason for item in reported], ['returned an unknown concept'])

        wordy = FakeResolver('这个问题问的应该是漂移改正。')
        response = self._tier_two_service(source, wordy).search('缆索走位造成的偏差')
        self.assertEqual(response.resolved_concepts, ('漂移改正',))

    def test_an_unavailable_tier_two_resolver_degrades_explicitly_on_a_weak_match(self) -> None:
        """Silence is not an acceptable answer, and neither is a cloud retry.

        The weak-match gate widens how often Tier 2 is consulted, so it also
        widens how often an unconfigured or stopped resolver is the reason a
        weak answer stayed weak.  Both states are reported on the response that
        ran without one — the reader is told the tier could have helped and did
        not run, rather than being shown one thin span with no explanation.
        """
        source = TierTwoFakeSource()

        response = EpubSearchService(source=source).search('巡检记录')
        self.assertEqual(response.resolved_concepts, ('巡检记录',))
        reported = [item for item in response.degraded if item.component == 'local-concept-resolver']
        self.assertEqual([item.reason for item in reported], ['not configured'])

        stopped = FakeResolver('漂移改正', available=False)
        response = self._tier_two_service(source, stopped).search('巡检记录')
        self.assertEqual(stopped.calls, [])
        reported = [item for item in response.degraded if item.component == 'local-concept-resolver']
        self.assertEqual([item.reason for item in reported], ['runtime stopped'])

    def test_tier_two_adds_to_a_weak_tier_one_match_and_never_replaces_it(self) -> None:
        """``resolved_concepts`` stays one coherent set across both tiers.

        A weak Tier-1 match is still a match: the SDD says a concept matched
        only by a short surface form still resolves and still contributes every
        one of its own spans, and the gate decides whether to *also* ask the
        resolver.  So ``枢`` keeps ``全域潮汐枢纽`` and gains whatever the model
        names, in that order.

        Deduplication is by concept and Tier 1 wins it, which is what keeps the
        expansion guard honest: a model naming the concept the query had already
        brushed does not turn a one-code-point span into a two-code-point one,
        so the hub still does not seed and its subtree stays out.  Whether a
        Tier-2 concept should be allowed to seed expansion at all is a separate
        question owned elsewhere; nothing here decides it.
        """
        source = TierTwoFakeSource()

        added = FakeResolver('漂移改正')
        response = self._tier_two_service(source, added).search('枢', graph_limit=10)
        self.assertEqual(response.resolved_concepts, ('全域潮汐枢纽', '漂移改正'))
        self.assertEqual(response.graph_total, 4)

        confirmed = FakeResolver('全域潮汐枢纽')
        response = self._tier_two_service(source, confirmed).search('枢', graph_limit=10)
        self.assertEqual(response.resolved_concepts, ('全域潮汐枢纽',))
        # Still the alias's span, so still not a seed: 枢纽的基准线 stays out.
        self.assertEqual(response.graph_total, 3)

    @staticmethod
    def _tier_two_service(source, resolver):
        """A service whose vector channel returns the passages Tier 2 is scoped to.

        ``TierTwoFakeSource`` deliberately holds more concepts than the candidate
        budget, so a Tier-2 test that wants the resolver called at all has to
        supply the vector channel the shortlist is derived from — which is the
        point of the design and not a fixture inconvenience.
        """
        backend = FakeVectorBackend([_record(source, 'u-drift', (1.0, 0.0)), _record(source, 'u-station', (0.9, 0.1))])
        return EpubSearchService(
            source=source,
            vector_backend=backend,
            embeddings=FakeEmbeddings(),
            reranker=FakeReranker(),
            concept_resolver=resolver,
        )

    def test_graph_expands_grounded_has_part_children_with_relation_provenance(self) -> None:
        """Containment still reads exactly as it did, and still says so.

        The literal ``relation:HAS_PART:1`` is the contract a reader and the UI
        both depend on.  Other predicates now name themselves in the same slot
        (see :meth:`test_provenance_names_the_predicate_that_won_the_concept`),
        which is precisely why this one is pinned as a literal rather than as a
        prefix match: a template change that quietly renamed the containment
        label would otherwise pass.
        """
        source = FakeSource()
        response = EpubSearchService(source=source).search('父主题', graph_limit=10)

        self.assertEqual(response.resolved_concepts, ('父主题',))
        self.assertEqual(response.graph_total, 2)
        self.assertEqual([hit.passage_id for hit in response.graph_results], ['p1', 'p3'])
        self.assertEqual(response.graph_results[0].provenance, ('graph',))
        self.assertEqual(response.graph_results[1].provenance, ('graph', 'relation:HAS_PART:1'))
        # The walk asked the repository for containment, downwards, and asked
        # for each other class separately: one query per class, never one query
        # for all six predicates at once.
        self.assertEqual(
            [(call['predicates'], call['direction']) for call in source.relation_calls],
            [
                (('HAS_PART',), 'outgoing'),
                (('HAS_PART',), 'outgoing'),
                (('ELABORATES',), 'both'),
                (('PRECEDES', 'PREREQUISITE', 'CAUSES', 'CONTRASTS'), 'both'),
            ],
        )

    def test_one_source_span_shared_by_two_concepts_is_one_hit_naming_both(self) -> None:
        """The graph channel returns a piece of source once, not once per concept.

        ``TCP`` and ``父主题`` are both anchored on p1[0:3].  That is one
        distinct source span, so it is one hit carrying both names — and it is
        counted once.  Relation-derived spans keep their own provenance.
        """
        source = FakeSource()
        response = EpubSearchService(source=source).search('TCP 父主题', graph_limit=10)

        self.assertEqual(response.resolved_concepts, ('TCP', '父主题'))
        self.assertEqual(response.graph_total, 4)
        self.assertEqual(len(response.graph_results), 4)
        self.assertEqual([hit.passage_id for hit in response.graph_results], ['p1', 'p2', 'p2', 'p3'])
        shared = response.graph_results[0]
        self.assertEqual(shared.matched_concepts, ('TCP', '父主题'))
        self.assertEqual(shared.excerpt.content, 'TCP')
        self.assertEqual(shared.provenance, ('graph',))
        self.assertEqual(response.graph_results[3].matched_concepts, ('子主题',))
        self.assertEqual(response.graph_results[3].provenance, ('graph', 'relation:HAS_PART:1'))

    def test_each_predicate_class_expands_to_the_depth_its_meaning_licenses(self) -> None:
        """Six predicates, three traversals, and every bound checked at its edge.

        Walking ``HAS_PART`` alone left roughly half the stored edges — about
        130 of 290 on the acceptance graph — unreachable at query time.  The fix
        is not to add the other five to the same walk: measured on that graph,
        one seed goes from 8 concepts / 184 spans (containment, two hops) to
        51 / 968 walking every predicate downwards to the same depth, and
        61 / 1,119 bidirectionally.  So each class travels only as far as what
        it asserts is worth.

        Containment travels two hops and stops: ``浮标阵列`` is reached,
        ``锚站的缆索`` one hop further is not.  ``ELABORATES`` travels one, and
        it travels it in both directions — ``巡检记录`` is reached although its
        stored edge points *at* the hub rather than away from it, while
        ``历年归档``, one hop past it, is not.  Nothing here is a coverage cap:
        a concept that is reached is counted and paged in full, and what is
        refused is refused before the walk, not truncated after it.
        """
        source = PredicateGraphFakeSource()
        reached = source.reached('全域潮汐枢纽')

        self.assertEqual(reached, {'全域潮汐枢纽', '枢纽的基准线', '浮标阵列', '巡检记录'})
        # Depth 2 is containment's bound and containment's alone; the third
        # containment hop and the second elaboration hop are both absent, and
        # they are absent for different reasons stated in the same set.
        self.assertNotIn('锚站的缆索', reached)
        self.assertNotIn('历年归档', reached)

    def test_a_sequential_chain_is_reachable_from_a_link_in_its_middle(self) -> None:
        """Why the associative predicates are walked in both directions.

        ``第二闸门`` names a link in the middle of a chain.  Downwards-only, the
        reader who asks about it is told what comes after and never what comes
        before, which for a sequence is half an answer — and for ``CONTRASTS``,
        where the stored subject is arbitrary, it is a coin toss.  Bidirectional
        at depth 1 is what makes a chain reachable from any link in it.

        The same hop count is what keeps it a neighbourhood.  ``第四闸门`` is
        two links away and is not returned: the concept that ``第二闸门``
        reached is not itself somewhere to walk on from, so a chain of any
        length still contributes exactly its two adjacent links.
        """
        source = PredicateGraphFakeSource()
        response = EpubSearchService(source=source).search('第二闸门', graph_limit=50)

        self.assertEqual(response.graph_total, 3)
        self.assertEqual(
            {name for hit in response.graph_results for name in hit.matched_concepts},
            {'第二闸门', '第一闸门', '第三闸门'},
        )
        self.assertEqual(
            [hit.provenance for hit in response.graph_results],
            [('graph',), ('graph', 'relation:PRECEDES:1'), ('graph', 'relation:PRECEDES:1')],
        )

    def test_an_associative_hop_is_never_itself_expanded_from(self) -> None:
        """Depth 1 is a hard stop, and this is the check that it is not a default.

        Two independent ways of getting this wrong are pinned together, because
        a walk that composes classes passes either one alone.

        First, by what came back: ``巡检记录`` elaborates the hub, so seeding on
        it reaches ``全域潮汐枢纽`` in one hop — and stops there.  The hub's
        whole containment subtree stays out, although containment is allowed two
        hops in its own right, because the concept an elaboration reached is not
        a seed for anything.

        Second, by what was asked.  The repository records every call, so the
        frontier itself is visible: after the first round, no query ever names a
        concept that a non-containment edge produced.  Asserting only the first
        would let an implementation that walked further but deduplicated the
        result pass.
        """
        source = PredicateGraphFakeSource()
        reached = source.reached('巡检记录')

        self.assertEqual(reached, {'巡检记录', '全域潮汐枢纽', '历年归档'})
        self.assertNotIn('枢纽的基准线', reached)
        self.assertNotIn('浮标阵列', reached)

        # ``全域潮汐枢纽`` and ``历年归档`` were both produced by an
        # ``ELABORATES`` hop.  No query may name either of them: not the second
        # round of the elaboration walk, which does not exist, and not the
        # containment walk, whose frontier is only ever what containment itself
        # reached.
        self.assertEqual(
            {call['concept_ids'] for call in source.relation_calls},
            {('log',)},
        )

    def test_provenance_names_the_predicate_that_won_the_concept(self) -> None:
        """One slot, six possible answers, and the cheapest edge is the one shown.

        ``relation:{predicate}:{depth}`` replaces a hardcoded ``HAS_PART``.  A
        reader told that a span was reached "by a relation" learns almost
        nothing; told it was reached by a containment edge rather than by a
        contrastive one, they can judge it.  Each predicate is asserted by name
        here rather than by prefix, because the point of the change is the
        predicate and a template that lost it would still match a prefix.

        The hub both *contains* and *causes* ``枢纽的基准线`` — one concept, two
        predicates, one hop each.  Containment is the cheaper hop and the
        stronger claim, so it is what provenance names.  The structural TOC
        label is unaffected and is pinned by
        :meth:`test_toc_children_answer_a_concept_the_model_never_decomposed`:
        no semantic predicate outranks it into silence and it outranks none of
        them.
        """
        source = PredicateGraphFakeSource()
        service = EpubSearchService(source=source)

        def provenance(query: str, concept: str) -> tuple[str, ...]:
            response = service.search(query, graph_limit=50)
            return next(hit.provenance for hit in response.graph_results if concept in hit.matched_concepts)

        self.assertEqual(provenance('全域潮汐枢纽', '枢纽的基准线'), ('graph', 'relation:HAS_PART:1'))
        self.assertEqual(provenance('全域潮汐枢纽', '浮标阵列'), ('graph', 'relation:HAS_PART:2'))
        self.assertEqual(provenance('全域潮汐枢纽', '巡检记录'), ('graph', 'relation:ELABORATES:1'))
        self.assertEqual(provenance('第二闸门', '第一闸门'), ('graph', 'relation:PRECEDES:1'))
        self.assertEqual(provenance('基线漂移', '泥沙淤积'), ('graph', 'relation:CAUSES:1'))
        self.assertEqual(provenance('低潮位', '高潮位'), ('graph', 'relation:CONTRASTS:1'))
        self.assertEqual(provenance('读数核对', '校验流程'), ('graph', 'relation:PREREQUISITE:1'))

    def test_multi_predicate_expansion_still_resolves_only_tier_one_matches(self) -> None:
        """Expansion reaches concepts; it does not claim the query named them.

        ``resolved_concepts`` is what the reader asked for, and adding five
        predicates to the walk must not put a single new name in it — otherwise
        a query about one thing reports itself as a query about its whole
        neighbourhood.  The concepts the new predicates reach are all present in
        the channel and all counted; they are simply not resolutions.
        """
        source = PredicateGraphFakeSource()
        response = EpubSearchService(source=source).search('第二闸门', graph_limit=50)

        self.assertEqual(response.resolved_concepts, ('第二闸门',))
        self.assertGreater(response.graph_total, 1)
        self.assertIn('第一闸门', {name for hit in response.graph_results for name in hit.matched_concepts})

    def test_a_multi_predicate_page_walk_still_ends_at_graph_total(self) -> None:
        """§4.3's shared-predicate contract, over a set five predicates helped build.

        Where a concept id came from is not a fact the occurrence queries know:
        expansion only lengthens the tuple handed to them, and the count and the
        pages still apply one predicate to it.  That is an argument, and this is
        the check that the argument survived six predicates rather than one.
        Walk the expanded set one span at a time and it must visit each distinct
        source span exactly once, stop at ``graph_total``, and return the same
        order the unpaged read does.
        """
        source = PredicateGraphFakeSource()
        service = EpubSearchService(source=source)
        query = '全域潮汐枢纽'
        total = service.search(query, graph_limit=1).graph_total
        self.assertEqual(total, 4)

        walked: list[object] = []
        for offset in range(total + 1):
            page = service.search(query, graph_offset=offset, graph_limit=1)
            self.assertEqual(page.graph_total, total)
            if not page.graph_results:
                break
            hit = page.graph_results[0]
            walked.append((hit.passage_id, hit.excerpt.start_codepoint, hit.excerpt.end_codepoint))

        self.assertEqual(len(walked), total)
        self.assertEqual(len(set(walked)), total)
        whole = service.search(query, graph_limit=total)
        self.assertEqual(
            walked,
            [(hit.passage_id, hit.excerpt.start_codepoint, hit.excerpt.end_codepoint) for hit in whole.graph_results],
        )

    def test_toc_children_answer_a_concept_the_model_never_decomposed(self) -> None:
        """The book's own hierarchy reaches sections no relation points at.

        ``六道闸门`` is a one-mention island: before this, the query that names
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

        ``巡检记录`` is mentioned inside 第一闸门 but also elsewhere in the book,
        so it binds to no node and is admitted from neither — that is what keeps
        this channel from becoming a second hub.
        """
        source = TocFakeSource()
        response = EpubSearchService(source=source).search('六道闸门', graph_limit=10)

        self.assertEqual(response.resolved_concepts, ('六道闸门',))
        self.assertEqual(response.graph_total, 3)
        self.assertEqual([hit.passage_id for hit in response.graph_results], ['gates', 'birth', 'growth'])
        self.assertEqual(response.graph_results[0].provenance, ('graph',))
        self.assertEqual(response.graph_results[1].provenance, ('graph', 'structure:TOC_CHILD:1'))
        self.assertEqual(response.graph_results[2].provenance, ('graph', 'structure:TOC_CHILD:1'))
        self.assertNotIn('巡检记录', {name for hit in response.graph_results for name in hit.matched_concepts})

    def test_a_model_decomposed_concept_never_falls_back_to_toc_structure(self) -> None:
        """Where the semantic graph exists it is authoritative; TOC is the fallback.

        ``锚站`` has a grounded ``HAS_PART`` child, so the model answered the
        "what are its parts?" question and this channel must not answer it
        again.  Its TOC child node holds ``锚站的缆索``, which is a perfectly
        real concept and still must not appear: a structural edge competing
        with a semantic one would make the two orderings disagree about what a
        decomposition even is.
        """
        source = TocFakeSource()
        response = EpubSearchService(source=source).search('锚站', graph_limit=10)

        names = {name for hit in response.graph_results for name in hit.matched_concepts}
        self.assertIn('浮标阵列', names)
        self.assertNotIn('锚站的缆索', names)
        self.assertEqual(
            [hit.provenance for hit in response.graph_results],
            [('graph',), ('graph', 'relation:HAS_PART:1')],
        )

    def test_seeding_follows_the_matched_term_and_not_the_concept(self) -> None:
        """One concept, two surface forms, two different answers.

        This is the whole rule, and both halves have to be pinned together or
        it reads as an arbitrary threshold on a hub.

        Naming ``全域潮汐枢纽`` in full **expands**.  It has 175 mentions and 20
        children, and that is not a reason to refuse: how often a book discusses
        something is a fact about the book, not about what the reader asked
        for.  Someone who spells out the hub's name is asking for its subtree
        and should get it.  An earlier version of this guard capped by mention
        count and refused here, which also cut ``枢纽的权重`` — a specific topic
        that the acceptance book simply mentions often — from 174 spans to 42.

        A query where only the one-character alias ``枢`` can match **does
        not** expand.  The concept still resolves and still contributes its own
        span; what it may not do is drag 20 children in behind an incidental
        character the reader never meant as a topic.  That is the case that
        produced a citation of ``枢对测点的授时`` on a query about something
        else.
        """
        service = EpubSearchService(source=GenericTermFakeSource())

        named = service.search('全域潮汐枢纽', graph_limit=10)
        self.assertEqual(named.resolved_concepts, ('全域潮汐枢纽',))
        self.assertEqual(named.graph_total, 2)
        self.assertEqual(
            [hit.provenance for hit in named.graph_results],
            [('graph',), ('graph', 'relation:HAS_PART:1')],
        )
        self.assertIn(
            '枢纽的基准线',
            {name for hit in named.graph_results for name in hit.matched_concepts},
        )

        brushed = service.search('枢', graph_limit=10)
        # Same concept, same `resolved_concepts`: refusing to seed expansion is
        # not refusing to resolve, and the concept's own span is still returned.
        self.assertEqual(brushed.resolved_concepts, ('全域潮汐枢纽',))
        self.assertEqual(brushed.graph_total, 1)
        self.assertEqual({hit.provenance for hit in brushed.graph_results}, {('graph',)})
        self.assertNotIn(
            '枢纽的基准线',
            {name for hit in brushed.graph_results for name in hit.matched_concepts},
        )

        # And the ordinary case is untouched throughout.
        specific = service.search('汛期观测', graph_limit=10)
        self.assertEqual(specific.graph_total, 2)
        self.assertEqual(specific.graph_results[1].provenance, ('graph', 'relation:HAS_PART:1'))

    def test_an_oversized_toc_node_is_skipped_whole_and_reported(self) -> None:
        """A budget that truncated would leave ``graph_total`` unexplainable.

        ``全网的结算`` binds to a node whose children hold more concepts than
        the channel will accept.  Returning the first 64 of them would come back
        as a smaller number with nothing anywhere saying which source had been
        dropped, and paging to the end would simply stop early.  So the seed's
        expansion is skipped entirely — the query behaves exactly as it did
        before this channel existed — and the skip is reported as a degraded
        component so the number has an explanation attached to it.
        """
        source = TocFakeSource()
        response = EpubSearchService(source=source).search('全网的结算', graph_limit=100)

        self.assertEqual(response.graph_total, 1)
        self.assertEqual(response.graph_results[0].provenance, ('graph',))
        skipped = [entry for entry in response.degraded if entry.component == 'toc-child-expansion']
        self.assertEqual(len(skipped), 1)
        self.assertIn('65', str(skipped[0].reason))
        self.assertIn('skipped whole', str(skipped[0].reason))

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
        total = service.search('六道闸门', graph_limit=1).graph_total

        walked = []
        for offset in range(total):
            page = service.search('六道闸门', graph_offset=offset, graph_limit=1)
            walked.extend(
                (hit.passage_id, hit.excerpt.start_codepoint, hit.excerpt.end_codepoint) for hit in page.graph_results
            )

        self.assertEqual(len(walked), total)
        self.assertEqual(len(set(walked)), total)
        self.assertEqual(service.search('六道闸门', graph_offset=total, graph_limit=1).graph_results, ())

    def test_a_repository_without_specificity_columns_expands_exactly_as_before(self) -> None:
        """The new guards read what is offered and invent nothing.

        :class:`FakeSource` answers the three-column vocabulary and has no
        ``list_toc_child_concepts`` at all, which is the shape a read model that
        has not caught up would present.  It must keep today's behaviour rather
        than raise or silently stop expanding: ``父主题`` still seeds, still
        reaches its ``HAS_PART`` child, and the alias-source rule — which
        nothing has declared — refuses nothing.
        """
        source = FakeSource()
        service = EpubSearchService(source=source)

        self.assertEqual(
            service._expansion_seed_ids(
                service._concept_matcher().match_spans('父主题', boundaries=_boundaries('父主题'))
            ),
            ('parent',),
        )
        response = service.search('父主题', graph_limit=10)
        self.assertEqual(response.graph_total, 2)
        self.assertEqual(response.graph_results[1].provenance, ('graph', 'relation:HAS_PART:1'))

    def test_graph_pages_stay_exhaustive_once_the_channel_is_ranked(self) -> None:
        """Ranking reorders the result set; it must not change what is in it.

        Relevance is one stable total order over every span, applied before the
        page is cut, so walking one span at a time still visits each span once
        and still ends at ``graph_total``.  A per-page rerank would have passed
        an ordering test and quietly made page 2 meaningless, so the walk — not
        a recomputed expectation — is what this asserts.
        """
        source = HubFakeSource()
        query = '全域潮汐枢纽，汛期观测，值守员造锚站'
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
        response = EpubSearchService(source=source).search('全域潮汐枢纽，汛期观测，值守员造锚站', graph_limit=10)

        self.assertEqual(response.graph_total, 5)
        self.assertEqual(
            [hit.passage_id for hit in response.graph_results],
            ['flood', 'judgment', 'preface', 'ark', 'righteousness'],
        )
        # Both expanded spans keep their hop count in provenance; only their
        # ranking weight differs, so a reader still sees how each was reached.
        self.assertEqual(response.graph_results[3].provenance, ('graph', 'relation:HAS_PART:1'))
        self.assertEqual(response.graph_results[4].provenance, ('graph', 'relation:HAS_PART:1'))
        costs = source.occurrence_calls[0]['costs']
        self.assertEqual(costs['hub'], 0.0)
        self.assertEqual(costs['ark_animals'], 1.0)
        self.assertAlmostEqual(costs['righteousness'], 1.0 + math.log2(20))

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
        response = EpubSearchService(source=source).search('全域潮汐枢纽，汛期观测', graph_limit=3)

        first = response.graph_results[0]
        self.assertEqual(first.passage_id, 'flood')
        self.assertEqual(first.matched_concepts, ('全域潮汐枢纽', '汛期观测'))
        self.assertEqual(first.excerpt.content, '枢在汛期观测，通知值守员建锚站')
        self.assertEqual(first.content, HubFakeSource.FLOOD)
        # Ranking demoted the preface span; it did not remove it.  It is still
        # counted, still paged, and still cites its complete passage.
        self.assertEqual([hit.passage_id for hit in response.graph_results][:3], ['flood', 'judgment', 'preface'])
        preface = response.graph_results[2]
        self.assertEqual(preface.excerpt.content, '枢')
        self.assertEqual(preface.content, HubFakeSource.PREFACE)
        self.assertEqual(preface.content[preface.excerpt.start_codepoint : preface.excerpt.end_codepoint], '枢')

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

        service.search('全域潮汐枢纽，汛期观测，值守员造锚站', graph_limit=1, graph_fusion_limit=4)
        panel_call, fusion_call = source.occurrence_calls
        self.assertEqual((panel_call['offset'], panel_call['limit']), (0, 1))
        self.assertEqual((fusion_call['offset'], fusion_call['limit']), (0, 4))

        source.occurrence_calls.clear()
        service.search('全域潮汐枢纽，汛期观测，值守员造锚站', graph_offset=3, graph_limit=2, graph_fusion_limit=4)
        panel_call, fusion_call = source.occurrence_calls
        self.assertEqual((panel_call['offset'], panel_call['limit']), (3, 2))
        self.assertEqual((fusion_call['offset'], fusion_call['limit']), (0, 4))

        # A page that already contains the ranked top of the channel is reused
        # rather than read twice.
        source.occurrence_calls.clear()
        service.search('全域潮汐枢纽，汛期观测，值守员造锚站', graph_limit=10, graph_fusion_limit=4)
        self.assertEqual(len(source.occurrence_calls), 1)

    def test_vector_candidates_are_cross_encoder_reranked_then_mmr_diversified(self) -> None:
        source = FakeSource()
        backend = FakeVectorBackend(
            [
                _record(source, 'u1', (1.0, 0.0)),
                _record(source, 'u2', (0.99, 0.01)),
                _record(source, 'u3', (0.0, 1.0)),
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
        ).search('连接协议', vector_limit=2, vector_candidate_limit=3)

        # u2 is most relevant. u3 is selected next because MMR penalizes the
        # nearly identical u1 vector, proving rerank precedes diversification.
        self.assertEqual([hit.passage_id for hit in response.vector_results], ['p2', 'p3'])
        self.assertEqual(response.vector_results[0].content, source.passages['p2']['content'])
        self.assertEqual(response.vector_results[0].excerpt.content, source.units['u2']['content'])
        self.assertEqual(response.vector_results[0].provenance, ('vector', 'cross-encoder', 'mmr'))
        self.assertEqual(embeddings.calls, [['连接协议']])
        self.assertEqual(len(reranker.calls), 1)

    def test_tampered_vector_window_or_missing_local_reranker_is_degraded_not_cloud_fallback(self) -> None:
        source = FakeSource()
        backend = FakeVectorBackend([_record(source, 'u1', (1.0, 0.0))])
        response = EpubSearchService(
            source=source, vector_backend=backend, embeddings=FakeEmbeddings(), reranker=FakeReranker()
        ).search('TCP')
        self.assertEqual(len(response.vector_results), 1)

        source.units['u1']['content'] = 'tampered'
        response = EpubSearchService(
            source=source, vector_backend=backend, embeddings=FakeEmbeddings(), reranker=FakeReranker()
        ).search('TCP')
        self.assertEqual(response.vector_results, ())
        self.assertIn('does not equal', response.degraded[-1].reason or '')

        response = EpubSearchService(source=source).search('TCP')
        self.assertEqual(response.vector_results, ())
        self.assertEqual(response.degraded[-1].component, 'local-vector-search')

    def test_graph_and_vector_candidates_share_local_cross_encoder_and_mmr_fusion(self) -> None:
        source = FakeSource()
        backend = FakeVectorBackend([_record(source, 'u2', (1.0, 0.0))])
        embeddings = FusionEmbeddings()
        reranker = FusionReranker()

        response = EpubSearchService(
            source=source,
            vector_backend=backend,
            embeddings=embeddings,
            reranker=reranker,
            mmr_lambda=1.0,
        ).search(
            'TCP',
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
        self.assertEqual([hit.passage_id for hit in response.graph_results], ['p1'])
        self.assertEqual([hit.passage_id for hit in response.vector_results], ['p2'])
        self.assertEqual([hit.passage_id for hit in response.fused_results], ['p1', 'p2'])
        graph_hit, vector_hit = response.fused_results
        self.assertEqual(graph_hit.content, source.passages['p1']['content'])
        self.assertEqual(graph_hit.excerpt.content, 'TCP')
        self.assertEqual(graph_hit.provenance, ('graph', 'cross-encoder', 'mmr', 'fused'))
        self.assertEqual(vector_hit.content, source.passages['p2']['content'])
        self.assertEqual(vector_hit.excerpt.content, source.units['u2']['content'])
        self.assertEqual(vector_hit.provenance, ('vector', 'cross-encoder', 'mmr', 'fused'))
        self.assertEqual(reranker.calls[-1][1], ['TCP', source.units['u2']['content']])
        self.assertEqual(embeddings.calls, [['TCP'], ['TCP']])

    def test_sibling_sections_of_one_enumerated_node_stop_crowding_each_other_out(self) -> None:
        """An enumeration's sections are complementary, so MMR must not net them off.

        This is the defect the sibling rule exists for, in the shape it was
        measured in.  Six sections reach the pool; the reranker likes all six;
        and MMR — which assumes a candidate resembling a selected one is
        *redundant* with it — spends the answer's slots on near-duplicates of
        the sections it already has and drops two sections entirely.  "The fifth
        gate" is not more of "the fourth", and a reader who asked what the gates
        are wants both.

        Four claims, asserted together because they are one rule:

        * all six sections come back;
        * ``g1b``, a second passage of a section already present, does **not**
          take a slot, though it outscores three sections that do.  Cosine calls
          it novel — 0.6 against its own section's span — and cosine is wrong:
          the unit an enumeration enumerates is the section;
        * ``g4sub``, one level deeper, does **not** take a slot either, though
          it outscores four sections that do.  A sub-section of the fourth gate
          is not a seventh gate;
        * ``g6v`` does, and it is reachable only by the vector channel.  The
          claim is about the node, not about which channel found the span.
        """
        source = EnumerationFakeSource()
        response = _enumeration_service(
            source,
            scores={
                '六道闸门': 0.92,
                '流量校准': 0.90,
                '每一个观测网都要经过几道关键闸门。': 0.88,
                '流量校准的复核在每季度进行一次。': 0.86,
                '量程分档的档位由历史极值决定。': 0.85,
                '基线复核': 0.84,
                '相位对齐': 0.82,
                '量程标定': 0.80,
                '残差归算': 0.78,
                '停机封存之前要留存最后一组读数。': 0.76,
                '停机封存': 0.30,
            },
            vectors={
                '六道闸门': OTHER_AXIS,
                '每一个观测网都要经过几道关键闸门。': (0.8, 0.6, 0.0),
                '流量校准的复核在每季度进行一次。': (0.6, 0.8, 0.0),
                **{text: GATE_AXIS for text in ('流量校准', '基线复核', '相位对齐', '量程标定', '残差归算', '停机封存')},
                '量程分档的档位由历史极值决定。': GATE_AXIS,
                '停机封存之前要留存最后一组读数。': GATE_AXIS,
            },
        ).search(ENUMERATION_QUERY, graph_limit=20, vector_limit=8, vector_candidate_limit=20)

        self.assertEqual(response.resolved_concepts, ('六道闸门',))
        self.assertEqual(
            [hit.passage_id for hit in response.fused_results],
            ['gates', 'g1', 'g2', 'g3', 'g4', 'g5', 'g6v', 'gates_body'],
        )
        self.assertEqual(
            _sections(response)[1:7], [title for _, title, _ in EnumerationFakeSource.SECTIONS]
        )
        # Both near-duplicates lose to sections they outscore, which is the
        # trade the rule is making and the reason it is worth making.
        self.assertNotIn('g1b', [hit.passage_id for hit in response.fused_results])
        self.assertNotIn('g4sub', [hit.passage_id for hit in response.fused_results])
        self.assertGreater(0.86, 0.76)

    def test_sibling_sections_come_back_in_book_order_among_themselves(self) -> None:
        """An enumeration reads in the book's order, not in the reranker's.

        MMR's selection order is a relevance order, and for a list of stages it
        is the wrong one: on the acceptance book the sections came back first,
        sixth, second, third, fifth.  Only the occupants of the group's own
        slots are permuted, so a section can never displace anything that
        outranked it — here the seed's heading keeps rank 1 and the parent's
        framing sentence keeps rank 8, exactly as ranking placed them.

        The scores are deliberately in reverse book order, so a result that came
        back in book order cannot have come back by relevance: the second result
        scores 0.70 and the seventh scores 0.90.
        """
        source = EnumerationFakeSource()
        response = _enumeration_service(
            source,
            scores={
                '六道闸门': 0.92,
                '停机封存之前要留存最后一组读数。': 0.90,
                '残差归算': 0.88,
                '量程标定': 0.86,
                '相位对齐': 0.84,
                '基线复核': 0.82,
                '流量校准': 0.70,
                '每一个观测网都要经过几道关键闸门。': 0.60,
            },
            vectors={
                '六道闸门': OTHER_AXIS,
                '每一个观测网都要经过几道关键闸门。': (0.8, 0.6, 0.0),
                **{
                    text: GATE_AXIS
                    for text in ('流量校准', '基线复核', '相位对齐', '量程标定', '残差归算', '停机封存')
                },
                # Every other span under this chapter reads on the same axis,
                # so nothing wins a slot merely by being unlike the answer.
                '停机封存之前要留存最后一组读数。': GATE_AXIS,
                '流量校准的复核在每季度进行一次。': GATE_AXIS,
                '量程分档的档位由历史极值决定。': GATE_AXIS,
            },
        ).search(ENUMERATION_QUERY, graph_limit=20, vector_limit=8, vector_candidate_limit=20)

        self.assertEqual(
            [hit.passage_id for hit in response.fused_results],
            ['gates', 'g1', 'g2', 'g3', 'g4', 'g5', 'g6v', 'gates_body'],
        )
        self.assertEqual(response.fused_results[1].score, 0.70)
        self.assertEqual(response.fused_results[6].score, 0.90)

    def test_a_minority_of_sections_keeps_its_relevance_order(self) -> None:
        """Book order is for an answer that *is* a list, not for two stray sections.

        Where one node's sections are a minority of the results they are not a
        list being read, they are supporting evidence among unrelated answers,
        and ordering them by book position is actively wrong.  Measured on the
        acceptance book: a query returned four sections of one node among six
        other results, the best of the four scoring 0.9734 at rank 1 and the
        weakest 0.0014 at rank 10 — and sorting those four by book position put
        the 0.0014 one first.

        Here the two sections are 1 of 6 and 6 of 6, in the reverse of book
        order, and they stay that way.
        """
        source = EnumerationFakeSource()
        response = _enumeration_service(
            source,
            scores={
                '六道闸门': 0.95,
                '残差归算': 0.90,
                '甲节的记录另作说明。': 0.60,
                '乙节的记录另作说明。': 0.55,
                '丙节的记录另作说明。': 0.50,
                '流量校准': 0.20,
                '每一个观测网都要经过几道关键闸门。': 0.15,
            },
            vectors={
                '六道闸门': OTHER_AXIS,
                '残差归算': GATE_AXIS,
                '流量校准': GATE_AXIS,
                '甲节的记录另作说明。': UNRELATED_AXIS,
                '乙节的记录另作说明。': (0.6, 0.0, 0.8),
                '丙节的记录另作说明。': (0.8, 0.0, 0.6),
                '每一个观测网都要经过几道关键闸门。': (0.8, 0.6, 0.0),
            },
        ).search(ENUMERATION_QUERY, graph_limit=20, vector_limit=6, vector_candidate_limit=20)

        # The fifth gate is two of six results ahead of the first gate, and it
        # stays there: it is the one the reader asked something answerable by.
        self.assertEqual(
            [hit.passage_id for hit in response.fused_results], ['gates', 'g5', 'o1', 'o2', 'o3', 'g1']
        )

    def test_sections_sharing_a_toc_parent_are_ordinary_without_toc_child_expansion(self) -> None:
        """Adjacency in the TOC is association; only expansion says decomposition.

        ``另一组`` has three child sections with a common parent, exactly like
        the enumerated node — and nothing about this query asked for its parts,
        so TOC-child expansion never labelled them and they get no exemption.
        They crowd each other out under ordinary cosine while the enumerated
        sections do not, in the same response, which is the whole scope of the
        change stated as one comparison.

        Read the other way round: this is what the enumerated group would still
        look like if the rule keyed on path adjacency alone, and it is why the
        rule does not.  A node's siblings hold a median of 10 bound concepts
        against a median of 0 for its children; exempting every chapter's
        sections from each other would trade the diversity of every query in the
        book for one query shape.
        """
        source = EnumerationFakeSource()
        response = _enumeration_service(
            source,
            scores={
                # The seed's own heading: relevant, in neither group, and the
                # candidate the unenumerated sections have to beat.
                '六道闸门': 0.60,
                '流量校准': 0.80,
                '甲节的记录另作说明。': 0.795,
                '基线复核': 0.79,
                '乙节的记录另作说明。': 0.79,
                '丙节的记录另作说明。': 0.785,
                '相位对齐': 0.78,
                '量程标定': 0.77,
            },
            vectors={
                **{text: GATE_AXIS for text in ('流量校准', '基线复核', '相位对齐', '量程标定')},
                **{text: OTHER_AXIS for text in ('甲节的记录另作说明。', '乙节的记录另作说明。', '丙节的记录另作说明。')},
            },
        ).search(ENUMERATION_QUERY, graph_limit=20, vector_limit=6, vector_candidate_limit=20)

        returned = [hit.passage_id for hit in response.fused_results]
        # Four enumerated sections, all of them; three unenumerated ones, one.
        self.assertEqual(returned[:5], ['g1', 'o1', 'g2', 'g3', 'g4'])
        self.assertNotIn('o2', returned)
        self.assertNotIn('o3', returned)

    def test_one_toc_node_cannot_take_every_slot_and_the_bound_is_reported(self) -> None:
        """The exemption is bounded, and a bound that binds is never silent.

        A parser can hand one node dozens of children — TOC-child expansion
        refuses only above 64 concepts — and an unbounded exemption would let
        such a node hold every slot on relevance alone, leaving nothing that
        stands *outside* the list.  Past the budget the group's remaining
        candidates go back to ordinary cosine rather than being dropped, so the
        reserve is a floor on other material and never an empty slot.

        The bound is reported through the same channel the TOC-child budget
        reports through, because the reader is owed the same thing in both
        cases: a number they can explain.
        """
        source = EnumerationFakeSource()
        response = _enumeration_service(
            source,
            scores={
                '六道闸门': 0.92,
                '流量校准': 0.90,
                '基线复核': 0.84,
                '相位对齐': 0.82,
                '量程标定': 0.80,
                '残差归算': 0.78,
                '每一个观测网都要经过几道关键闸门。': 0.88,
            },
            vectors={
                '六道闸门': OTHER_AXIS,
                '每一个观测网都要经过几道关键闸门。': (0.8, 0.6, 0.0),
                **{text: GATE_AXIS for text in ('流量校准', '基线复核', '相位对齐', '量程标定', '残差归算')},
            },
        ).search(ENUMERATION_QUERY, graph_limit=20, vector_limit=4, vector_candidate_limit=20)

        self.assertEqual(
            [hit.passage_id for hit in response.fused_results], ['gates', 'g1', 'g2', 'gates_body']
        )
        bound = [item for item in response.degraded if item.component == 'toc-sibling-diversity']
        self.assertEqual(len(bound), 1)
        self.assertIn('one TOC node', bound[0].reason or '')

    def test_malformed_local_graph_embedding_fails_closed_for_fused_channel(self) -> None:
        source = FakeSource()
        response = EpubSearchService(
            source=source,
            vector_backend=FakeVectorBackend([]),
            # This existing fake returns one vector for a two-excerpt graph
            # batch.  It simulates a malformed local runtime response.
            embeddings=FakeEmbeddings(),
            reranker=FakeReranker(),
        ).search('TCP', graph_limit=2)

        self.assertEqual(response.graph_results[0].content, source.passages['p1']['content'])
        self.assertEqual(response.fused_results, ())
        self.assertEqual(response.degraded[-1].component, 'local-fused-search')
        self.assertIn('every graph candidate', response.degraded[-1].reason or '')


if __name__ == '__main__':
    unittest.main()
