"""Focused, dependency-free tests for the canonical EPUB SQLite store."""

from __future__ import annotations

from dataclasses import replace
import json
import os
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest


# The store now shares its concept-key folding rule and its artifact contract
# with the pure ``overlay`` module, so it is loaded as a member of a synthetic
# package rather than as a lone file.  ``test_epub_search`` established this
# pattern; the tests stay independent of the OpenWebUI application package.
EPUB_DIR = Path(__file__).resolve().parents[1] / "backend/open_webui/retrieval/epub"
PACKAGE_NAME = "epub_store_sdd_test_package"
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


OVERLAY_MODULE = _load("overlay")
STORE_MODULE = _load("store")
IntegrityError = STORE_MODULE.IntegrityError
OverlayRejected = STORE_MODULE.OverlayRejected
SQLiteEpubStore = STORE_MODULE.SQLiteEpubStore
UnknownConceptError = STORE_MODULE.UnknownConceptError
OverlayError = OVERLAY_MODULE.OverlayError
OverlayMention = OVERLAY_MODULE.OverlayMention
PassageFingerprint = OVERLAY_MODULE.PassageFingerprint
parse_overlay_json = OVERLAY_MODULE.parse_overlay_json

# Every table with a foreign key to ``concepts(concept_id)``.  A merge that
# forgets one of these would orphan rows or trip the foreign key, so the tests
# below assert against this list rather than against the columns they happened
# to remember.
# ``concept_merges.target_concept_id`` is the audit row's own reference and
# always names the surviving concept, so a merge has to repoint it too when
# that concept is itself folded onward; the other four carry graph rows that a
# merge has to move.  ``concept_merges.source_concept_id`` is deliberately not
# a foreign key because the row it names is deleted by the merge itself.
CONCEPT_REFERENCING_COLUMNS = (
    ("concept_aliases", "concept_id"),
    ("concept_mentions", "concept_id"),
    ("concept_merges", "target_concept_id"),
    ("concept_relations", "object_concept_id"),
    ("concept_relations", "subject_concept_id"),
)


class SQLiteEpubStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteEpubStore(os.path.join(self.tempdir.name, "epub.db"))
        self.book_id = self.store.create_book("A faithful book", book_id="book-a")
        creation = self.store.create_book_version(
            self.book_id, epub_bytes=b"a complete test epub", version_id="version-a"
        )
        self.assertTrue(creation.created)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _passage(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "passage_id": "passage-a",
            "source_href": "chapter-1.xhtml",
            "source_fragment": "opening",
            "spine_index": 0,
            "ordinal": 0,
            "content_kind": "paragraph",
            "content": "原文，含标点。\nSecond line.",
        }
        base.update(overrides)
        return base

    def test_duplicate_hash_reuses_existing_canonical_version(self) -> None:
        other_book = self.store.create_book("Same title is irrelevant", book_id="book-b")
        duplicate = self.store.create_book_version(
            other_book, epub_bytes=b"a complete test epub", version_id="version-b"
        )

        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.version_id, "version-a")
        self.assertEqual(duplicate.book_id, "book-a")
        self.assertIsNone(self.store.get_version("version-b"))
        self.assertEqual(self.store.get_epub_bytes("version-a"), b"a complete test epub")

    def test_passage_and_derived_window_keep_exact_source_and_offsets(self) -> None:
        self.store.add_passages("version-a", [self._passage()])
        unit_id = self.store.add_retrieval_unit("passage-a", 4, 9, embedding_profile="local-v1")

        passage = self.store.get_passage("passage-a")
        unit = self.store.get_retrieval_unit(unit_id)
        self.assertIsNotNone(passage)
        self.assertIsNotNone(unit)
        assert passage is not None and unit is not None
        self.assertEqual(unit["content"], passage["content"][4:9])
        self.assertEqual(unit["content"], "标点。\nS")
        with self.assertRaises(IntegrityError):
            self.store.add_retrieval_unit("passage-a", 0, 999)

    def test_version_retrieval_units_have_stable_order_and_persist_index_state(self) -> None:
        self.store.add_passages("version-a", [self._passage()])
        unit_id = self.store.add_retrieval_unit("passage-a", 0, 3)

        rows = self.store.list_retrieval_units_for_version("version-a")
        self.assertEqual([row["retrieval_unit_id"] for row in rows], [unit_id])
        self.assertEqual(rows[0]["vector_state"], "PENDING")
        self.store.set_retrieval_unit_vector_state(unit_id, "READY")
        self.assertEqual(self.store.get_retrieval_unit(unit_id)["vector_state"], "READY")
        with self.assertRaisesRegex(IntegrityError, "invalid vector state"):
            self.store.set_retrieval_unit_vector_state(unit_id, "UNKNOWN")

    def test_source_passages_are_immutable_and_foreign_keys_are_safe(self) -> None:
        self.store.add_passages("version-a", [self._passage()])
        concept_id = self.store.upsert_concept("TCP", aliases=["Transmission Control Protocol"])
        mention_id = self.store.add_concept_mention(
            concept_id, "passage-a", start_codepoint=0, end_codepoint=2, evidence="原文"
        )
        self.assertTrue(mention_id)

        with self.assertRaises(IntegrityError):
            self.store.add_concept_mention("missing", "passage-a")
        with self.assertRaises(Exception) as raised:
            self.store._connection().execute(
                "UPDATE passages SET content = ? WHERE passage_id = ?", ("rewritten", "passage-a")
            )
        self.assertIn("immutable", str(raised.exception))
        self.store._connection().rollback()

        updated_concept_id = self.store.upsert_concept("TCP", aliases=["TCP/IP"], definition="updated")
        self.assertEqual(updated_concept_id, concept_id)
        mention = self.store._connection().execute(
            "SELECT concept_id FROM concept_mentions WHERE mention_id = ?", (mention_id,)
        ).fetchone()
        self.assertEqual(mention["concept_id"], concept_id)

    def test_the_concept_term_fingerprint_moves_whenever_the_vocabulary_does(self) -> None:
        """Search caches its Tier-1 matcher on this value, so it must never miss a write.

        A stale matcher is worse than a slow one: an administrator who adds an
        alias would keep getting the old answer with nothing to indicate why.
        Adding a concept, adding an alias to an existing concept, and merging
        two concepts each have to move it — the last one changes no row count
        at all, which is why ``MAX(updated_at)`` is in the value.
        """
        self.store.add_passages("version-a", [self._passage()])
        seen = {self.store.concept_term_fingerprint()}

        tcp = self.store.upsert_concept("TCP")
        self.assertNotIn(self.store.concept_term_fingerprint(), seen)
        seen.add(self.store.concept_term_fingerprint())

        self.store.upsert_concept("TCP", aliases=["Transmission Control Protocol"])
        self.assertNotIn(self.store.concept_term_fingerprint(), seen)
        seen.add(self.store.concept_term_fingerprint())

        udp = self.store.upsert_concept("UDP")
        self.store.add_concept_mention(tcp, "passage-a", start_codepoint=0, end_codepoint=2)
        self.store.add_concept_mention(udp, "passage-a", start_codepoint=4, end_codepoint=6)
        seen.add(self.store.concept_term_fingerprint())
        self.store.merge_concepts(
            target_concept_id=tcp, source_concept_id=udp, merged_by="admin"
        )
        self.assertNotIn(self.store.concept_term_fingerprint(), seen)

        # Reading twice without writing must give the identical value, or the
        # cache would rebuild on every request and buy nothing.
        self.assertEqual(
            self.store.concept_term_fingerprint(), self.store.concept_term_fingerprint()
        )

    def test_canonical_name_cannot_capture_another_concepts_alias(self) -> None:
        self.store.upsert_concept("TCP", aliases=["Transmission Control Protocol"])
        with self.assertRaisesRegex(IntegrityError, "already an alias"):
            self.store.upsert_concept("Transmission Control Protocol")

    def test_concept_relations_are_version_scoped_and_evidence_bound(self) -> None:
        self.store.add_passages("version-a", [self._passage()])
        parent = self.store.upsert_concept("父概念")
        child = self.store.upsert_concept("子概念")
        self.store.add_concept_mention(parent, "passage-a", start_codepoint=0, end_codepoint=2, evidence="原文")
        self.store.add_concept_mention(child, "passage-a", start_codepoint=4, end_codepoint=6, evidence="标点")

        relation_id = self.store.add_concept_relation(
            "version-a",
            parent,
            "HAS_PART",
            child,
            evidence=[
                {
                    "passage_id": "passage-a",
                    "start_codepoint": 0,
                    "end_codepoint": 2,
                    "evidence": "原文",
                }
            ],
        )
        relation = self.store.list_concept_relation_neighbors([parent])
        self.assertEqual(relation[0]["relation_id"], relation_id)
        self.assertEqual(relation[0]["object_concept_id"], child)

        self.store.create_book_version(
            self.book_id, epub_bytes=b"a second complete test epub", version_id="version-b"
        )
        self.store.add_passages("version-b", [self._passage(passage_id="passage-b")])
        self.store.add_concept_mention(parent, "passage-b", start_codepoint=0, end_codepoint=2, evidence="原文")
        self.store.add_concept_mention(child, "passage-b", start_codepoint=4, end_codepoint=6, evidence="标点")
        same_relation = self.store.add_concept_relation(
            "version-b",
            parent,
            "HAS_PART",
            child,
            evidence=[
                {
                    "passage_id": "passage-b",
                    "start_codepoint": 0,
                    "end_codepoint": 2,
                    "evidence": "原文",
                }
            ],
        )
        self.assertEqual(same_relation, relation_id)
        assertion_count = self.store._connection().execute(
            "SELECT COUNT(*) FROM concept_relation_assertions WHERE relation_id = ?", (relation_id,)
        ).fetchone()[0]
        self.assertEqual(assertion_count, 2)

        with self.assertRaisesRegex(IntegrityError, "immutable source substring"):
            self.store.add_concept_relation(
                "version-a",
                parent,
                "ELABORATES",
                child,
                evidence=[
                    {
                        "passage_id": "passage-a",
                        "start_codepoint": 0,
                        "end_codepoint": 2,
                        "evidence": "改写",
                    }
                ],
            )

    def test_batch_item_cannot_cross_book_versions(self) -> None:
        self.store.add_passages("version-a", [self._passage()])
        job_id = self.store.create_batch_job("version-a", provider="provider", profile_name="concept-v1")
        self.store.add_batch_item(job_id, "passage-a", custom_id="passage-a", request={"model": "local"})

        other_book = self.store.create_book("Other", book_id="book-b")
        self.store.create_book_version(other_book, epub_bytes=b"other epub", version_id="version-b")
        self.store.add_passages("version-b", [self._passage(passage_id="passage-b")])
        with self.assertRaises(IntegrityError):
            self.store.add_batch_item(job_id, "passage-b", custom_id="passage-b", request={})

    def test_search_read_surface_returns_stable_graph_rows_and_toc_hierarchy(self) -> None:
        self.store.add_toc_nodes(
            "version-a",
            [
                {
                    "toc_node_id": "chapter", "title": "第一章", "href": "chapter-1.xhtml",
                    "spine_index": 0, "ordinal": 0,
                },
                {
                    "toc_node_id": "section", "parent_toc_node_id": "chapter", "title": "概述",
                    "href": "chapter-1.xhtml", "fragment": "overview", "spine_index": 0, "ordinal": 1,
                },
            ],
        )
        self.store.add_passages("version-a", [self._passage(toc_node_id="section")])
        concept_id = self.store.upsert_concept("TCP", aliases=["Transmission Control Protocol"])
        self.store.add_concept_mention(concept_id, "passage-a", start_codepoint=0, end_codepoint=2)

        self.assertEqual(self.store.count_concept_occurrences([concept_id]), 1)
        row = self.store.list_concept_occurrences([concept_id], offset=0, limit=20)[0]
        self.assertEqual(row["book_title"], "A faithful book")
        self.assertEqual(row["toc_path"], ("第一章", "概述"))
        self.assertEqual(row["content"][0:2], "原文")
        self.assertEqual(
            self.store.matched_concept_names("passage-a", [concept_id]), ["TCP"]
        )
        self.assertEqual(
            {entry["term"] for entry in self.store.list_concept_terms()},
            {"TCP", "Transmission Control Protocol"},
        )

    def _graph_span_fixture(self) -> list[str]:
        """Mentions that duplicate, nest and partially overlap in two passages.

        The passages are inserted out of book order so the graph read surface
        has to sort by ``spine_index``/``ordinal`` rather than by insertion,
        which is the tie-break the ranking signals fall through to.
        """
        self.store.add_passages(
            "version-a",
            [
                self._passage(passage_id="passage-b", source_fragment="second", ordinal=1),
                self._passage(),
            ],
        )
        alpha = self.store.upsert_concept("Alpha")
        beta = self.store.upsert_concept("Beta")
        gamma = self.store.upsert_concept("Gamma")
        # passage-a: Alpha and Beta anchor the identical [0,2) span, and
        # Alpha's [4,6) sits wholly inside Gamma's [3,7).
        self.store.add_concept_mention(alpha, "passage-a", start_codepoint=0, end_codepoint=2)
        self.store.add_concept_mention(beta, "passage-a", start_codepoint=0, end_codepoint=2)
        self.store.add_concept_mention(gamma, "passage-a", start_codepoint=3, end_codepoint=7)
        self.store.add_concept_mention(alpha, "passage-a", start_codepoint=4, end_codepoint=6)
        # passage-b: [0,4) and [2,7) overlap without either containing the other.
        self.store.add_concept_mention(alpha, "passage-b", start_codepoint=0, end_codepoint=4)
        self.store.add_concept_mention(gamma, "passage-b", start_codepoint=2, end_codepoint=7)
        return [alpha, beta, gamma]

    @staticmethod
    def _span_keys(rows: list[dict[str, object]]) -> list[tuple[object, object, object]]:
        return [(row["passage_id"], row["start_codepoint"], row["end_codepoint"]) for row in rows]

    def test_graph_occurrences_enumerate_distinct_source_spans_not_mention_rows(self) -> None:
        """One row per distinct piece of source, attributed to every concept.

        Six mention rows describe four distinct source spans: the two mentions
        of ``[0,2)`` are the same characters, and ``[4,6)`` is already visible
        inside ``[3,7)``.  A reader wants each piece of source once, so the
        maximal span survives and carries the concepts it absorbed.

        With every concept matched directly, the two two-concept spans lead and
        the longer span of each pair leads its passage — book order only breaks
        what the ranking signals leave tied.
        """
        concept_ids = self._graph_span_fixture()
        rows = self.store.list_concept_occurrences(concept_ids, offset=0, limit=20)

        self.assertEqual(
            self._span_keys(rows),
            [("passage-a", 3, 7), ("passage-a", 0, 2), ("passage-b", 2, 7), ("passage-b", 0, 4)],
        )
        # The nested span collapses into its container, which carries both.
        self.assertEqual(rows[0]["canonical_names"], ("Alpha", "Gamma"))
        # The exact duplicate collapses; neither concept is dropped.
        self.assertEqual(rows[1]["canonical_names"], ("Alpha", "Beta"))
        self.assertEqual(rows[1]["concept_ids"], tuple(sorted(concept_ids[:2])))
        # Every surviving span is still a byte-exact slice of its passage.
        for row in rows:
            start, end = row["start_codepoint"], row["end_codepoint"]
            self.assertEqual(
                row["content"][start:end],
                self.store.get_passage(row["passage_id"])["content"][start:end],
            )

    def test_partially_overlapping_graph_spans_are_not_merged(self) -> None:
        """Only containment collapses; a union would cite text nobody anchored."""
        concept_ids = self._graph_span_fixture()
        rows = [
            row
            for row in self.store.list_concept_occurrences(concept_ids, offset=0, limit=20)
            if row["passage_id"] == "passage-b"
        ]

        self.assertEqual(self._span_keys(rows), [("passage-b", 2, 7), ("passage-b", 0, 4)])
        self.assertEqual(rows[0]["canonical_names"], ("Gamma",))
        self.assertEqual(rows[1]["canonical_names"], ("Alpha",))

    def test_graph_occurrence_total_equals_every_page_walked_one_by_one(self) -> None:
        """The count and the pages must apply the same de-duplication predicate.

        Filtering duplicates after ``LIMIT``/``OFFSET`` would give ragged pages
        and a total that disagrees with them, so this walks the pages instead
        of recomputing the expectation.  Ranking is deliberately expressed as
        one total order in ``ORDER BY`` rather than as a per-page rerank, so it
        cannot move a span across a page boundary: the walk is done with a
        relation cost that reorders the result set, and it must still visit
        every span exactly once and end at the count.
        """
        concept_ids = self._graph_span_fixture()
        costs = {concept_ids[0]: 0.0, concept_ids[1]: 3.0, concept_ids[2]: 5.32}
        total = self.store.count_concept_occurrences(concept_ids)

        walked: list[dict[str, object]] = []
        offset = 0
        while True:
            page = self.store.list_concept_occurrences(
                concept_ids, offset=offset, limit=1, concept_costs=costs
            )
            if not page:
                break
            self.assertEqual(len(page), 1)
            walked.extend(page)
            offset += 1
            self.assertLessEqual(offset, 20, "graph pagination did not terminate")

        self.assertEqual(len(walked), total)
        keys = self._span_keys(walked)
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(
            keys,
            self._span_keys(
                self.store.list_concept_occurrences(
                    concept_ids, offset=0, limit=20, concept_costs=costs
                )
            ),
        )
        # The count is a function of the shared predicate alone, so declaring a
        # cost must not add, drop, or duplicate a single span.
        self.assertEqual(
            set(keys), set(self._span_keys(self.store.list_concept_occurrences(concept_ids, offset=0, limit=20)))
        )
        self.assertEqual(self.store.count_concept_occurrences(concept_ids), total)

    def test_a_directly_matched_span_is_paged_before_a_relation_expanded_one(self) -> None:
        """The caller's per-concept cost, not book position, picks the first page.

        ``Gamma`` here stands for a concept reached only by walking out of a
        high-degree hub: its span sits earlier in the book and is longer, and
        it must still sort behind every span a directly matched concept
        anchored.  A span that merely *absorbed* a hub concept keeps its direct
        rank, because that is what its attribution shows the reader.
        """
        concept_ids = self._graph_span_fixture()
        alpha, beta, gamma = concept_ids
        rows = self.store.list_concept_occurrences(
            concept_ids,
            offset=0,
            limit=20,
            concept_costs={alpha: 0.0, beta: 0.0, gamma: 5.32},
        )

        self.assertEqual(
            self._span_keys(rows),
            [("passage-a", 3, 7), ("passage-a", 0, 2), ("passage-b", 0, 4), ("passage-b", 2, 7)],
        )
        # passage-b[2,7) is Gamma alone: longer, earlier in the passage than
        # nothing else, and still last because it cost 5.32 to reach.
        self.assertEqual(rows[-1]["canonical_names"], ("Gamma",))
        self.assertEqual(rows[-1]["rank_relation_cost"], 5.32)
        # passage-a[3,7) is anchored by Gamma but absorbed Alpha's [4,6), so it
        # is attributed to a direct match and ranks as one.
        self.assertEqual(rows[0]["canonical_names"], ("Alpha", "Gamma"))
        self.assertEqual(rows[0]["rank_relation_cost"], 0.0)
        self.assertEqual(rows[0]["rank_concept_count"], 2)


class SQLiteEpubConceptMergeTest(unittest.TestCase):
    """An administrator remedy for a model-suggested duplicate concept.

    The Batch ingest guard refuses an item whose suggestion exactly matches
    two concepts.  These tests cover the only action that can resolve such a
    candidate: folding one concept into the other in a single transaction.
    """

    PASSAGE_ONE = "双轨校准法见于《观测规程》2.4-2.11。"
    PASSAGE_TWO = "又论双轨校准法。"

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SQLiteEpubStore(os.path.join(self.tempdir.name, "epub.db"))
        self.addCleanup(self.store.close)
        book_id = self.store.create_book("单轨校准法", book_id="book-a")
        self.store.create_book_version(
            book_id, epub_bytes=b"merge test epub", version_id="version-a"
        )
        self.store.add_passages(
            "version-a",
            [
                {
                    "passage_id": "p1",
                    "source_href": "chapter-13.xhtml",
                    "spine_index": 0,
                    "ordinal": 0,
                    "content_kind": "paragraph",
                    "content": self.PASSAGE_ONE,
                },
                {
                    "passage_id": "p2",
                    "source_href": "chapter-13.xhtml",
                    "spine_index": 0,
                    "ordinal": 1,
                    "content_kind": "paragraph",
                    "content": self.PASSAGE_TWO,
                },
            ],
        )
        # The real collision: a parable and the scripture citation naming it.
        # The citation is currently canonical and already owns the short form.
        self.target = self.store.upsert_concept(
            "《观测规程》2.4-2.11", aliases=["规程2.4-2.11"], concept_id="citation"
        )
        self.source = self.store.upsert_concept("双轨校准法", concept_id="parable")
        self.store.add_concept_mention(self.target, "p1", start_codepoint=7, end_codepoint=21)
        self.store.add_concept_mention(self.source, "p1", start_codepoint=0, end_codepoint=5)

    def _connection(self):
        return self.store._connection()

    def _count(self, sql: str, *parameters: object) -> int:
        return int(self._connection().execute(sql, parameters).fetchone()[0])

    def _aliases(self, concept_id: str) -> set[str]:
        return {
            str(row["alias"])
            for row in self._connection().execute(
                "SELECT alias FROM concept_aliases WHERE concept_id = ?", (concept_id,)
            )
        }

    def _merge(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "target_concept_id": self.target,
            "source_concept_id": self.source,
            "merged_by": "administrator",
        }
        arguments.update(overrides)
        return self.store.merge_concepts(**arguments)  # type: ignore[arg-type]

    def _relation(self, subject: str, predicate: str, object_: str) -> str:
        """Assert one relation, grounded on a real span of the first passage."""
        return self.store.add_concept_relation(
            "version-a",
            subject,
            predicate,
            object_,
            evidence=[
                {
                    "passage_id": "p1",
                    "start_codepoint": 0,
                    "end_codepoint": 5,
                    "evidence": self.PASSAGE_ONE[0:5],
                }
            ],
        )

    def test_enumerated_referencing_columns_survive_a_chained_merge(self) -> None:
        """A future migration that adds a reference must fail this, not silently orphan rows.

        Enumerating the columns is not enough on its own, and the gap was not
        hypothetical: ``concept_merges.target_concept_id`` was listed here from
        the day it was added, yet no test ever put a row in it and then merged
        the concept it named, so its ``RESTRICT`` silently forbade every second
        merge.  The chain below therefore does two merges rather than one, and
        asserts that the middle concept was genuinely referenced from *every*
        enumerated column before it was merged away.  A new referencing column
        fails the enumeration; a new referencing column that a merge cannot
        move now fails the merge itself.
        """
        connection = self._connection()
        tables = [
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        referencing = sorted(
            (table, str(row["from"]))
            for table in tables
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            if row["table"] == "concepts"
        )
        self.assertEqual(referencing, sorted(CONCEPT_REFERENCING_COLUMNS))
        # The audit row keeps the deleted identifier without a foreign key.
        self.assertIn(
            "source_concept_id",
            {str(row[1]) for row in connection.execute("PRAGMA table_info(concept_merges)")},
        )

        last = self.store.upsert_concept("单轨校准法", concept_id="third")
        other = self.store.upsert_concept("校准法", concept_id="parable-genre")
        self.store.add_concept_mention(last, "p2", start_codepoint=2, end_codepoint=7)
        self.store.add_concept_mention(other, "p1", start_codepoint=3, end_codepoint=5)
        # Distinct predicates, so no relation folds away and every chain member
        # occupies both the subject and the object column.
        self._relation(self.source, "ELABORATES", other)
        self._relation(other, "PRECEDES", self.source)
        self._relation(self.target, "CONTRASTS", other)
        self._relation(other, "HAS_PART", self.target)
        self._relation(last, "PREREQUISITE", other)
        self._relation(other, "CAUSES", last)

        self._merge()

        for table, column in CONCEPT_REFERENCING_COLUMNS:
            self.assertGreater(
                self._count(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", self.target),
                0,
                f"the fixture never referenced the merged concept from {table}.{column}",
            )

        # The second merge is the one the audit row's RESTRICT used to forbid.
        self._merge(target_concept_id=last, source_concept_id=self.target)

        for table, column in CONCEPT_REFERENCING_COLUMNS:
            for gone in (self.source, self.target):
                self.assertEqual(
                    self._count(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", gone),
                    0,
                    f"{table}.{column} still references a merged-away concept",
                )
            self.assertEqual(
                self._count(
                    f"""SELECT COUNT(*) FROM {table} AS t
                          LEFT JOIN concepts AS c ON c.concept_id = t.{column}
                         WHERE c.concept_id IS NULL"""
                ),
                0,
                f"{table}.{column} has a dangling concept reference",
            )
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_a_concept_that_already_absorbed_another_can_itself_be_merged(self) -> None:
        """The live failure: fold A into B, then B into C.  Both must succeed.

        Reproduced on a real store as 潮位观测站 → 潮位站 → 全域潮汐枢纽, where the
        second administrator decision died on the foreign key of the first
        merge's audit row.
        """
        last = self.store.upsert_concept("单轨校准法", concept_id="third")
        self.store.add_concept_mention(last, "p2", start_codepoint=2, end_codepoint=7)

        first = self._merge()
        second = self._merge(target_concept_id=last, source_concept_id=self.target)

        self.assertEqual(first["repointed_merge_audits"], 0)
        self.assertEqual(second["repointed_merge_audits"], 1)
        self.assertEqual(
            [str(row[0]) for row in self._connection().execute("SELECT concept_id FROM concepts")],
            [last],
        )
        self.assertEqual(
            self._aliases(last),
            {"单轨校准法", "《观测规程》2.4-2.11", "规程2.4-2.11", "双轨校准法"},
        )
        # Every mention of all three concepts is now held by the survivor, with
        # its own passage and offsets untouched.
        self.assertEqual(
            {
                tuple(row)
                for row in self._connection().execute(
                    """SELECT concept_id, passage_id, start_codepoint, end_codepoint, evidence
                         FROM concept_mentions"""
                )
            },
            {
                (last, "p1", 0, 5, "双轨校准法"),
                (last, "p1", 7, 21, "《观测规程》2.4-2.11"),
                (last, "p2", 2, 7, "双轨校准法"),
            },
        )
        # Both audit rows survive, now pointing at the concept that holds the
        # merged material, and neither names itself.
        audit = {
            (str(row["target_concept_id"]), str(row["source_concept_id"]), str(row["source_canonical_name"]))
            for row in self._connection().execute("SELECT * FROM concept_merges")
        }
        self.assertEqual(
            audit,
            {
                (last, self.source, "双轨校准法"),
                (last, self.target, "《观测规程》2.4-2.11"),
            },
        )
        self.assertEqual(self._connection().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_a_three_deep_merge_chain_keeps_every_folded_in_concept_on_record(self) -> None:
        """Consolidating in several passes is normal review, not a one-off case."""
        third = self.store.upsert_concept("单轨校准法", concept_id="third")
        fourth = self.store.upsert_concept("全网校准法", concept_id="fourth")
        self.store.add_concept_mention(third, "p2", start_codepoint=2, end_codepoint=7)
        self.store.add_concept_mention(fourth, "p2", start_codepoint=0, end_codepoint=2)

        self._merge()
        self._merge(target_concept_id=third, source_concept_id=self.target)
        final = self._merge(target_concept_id=fourth, source_concept_id=third)

        self.assertEqual(final["repointed_merge_audits"], 2)
        self.assertEqual(
            [str(row[0]) for row in self._connection().execute("SELECT concept_id FROM concepts")],
            [fourth],
        )
        self.assertEqual(
            self._aliases(fourth),
            {"全网校准法", "单轨校准法", "《观测规程》2.4-2.11", "规程2.4-2.11", "双轨校准法"},
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM concept_mentions WHERE concept_id = ?", fourth), 4
        )
        # No history is dropped by the repointing: the audit table still names
        # every concept that was folded in, in order, all pointing at the one
        # that now holds their mentions.
        self.assertEqual(
            [
                (str(row["target_concept_id"]), str(row["source_canonical_name"]))
                for row in self._connection().execute(
                    "SELECT * FROM concept_merges ORDER BY merged_at, rowid"
                )
            ],
            [
                (fourth, "双轨校准法"),
                (fourth, "《观测规程》2.4-2.11"),
                (fourth, "单轨校准法"),
            ],
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM concept_merges WHERE target_concept_id = source_concept_id"),
            0,
            "an audit row must never record a concept as merged into itself",
        )
        self.assertEqual(self._connection().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_merge_moves_every_alias_including_the_source_canonical_spelling(self) -> None:
        self.store.upsert_concept("双轨校准法", aliases=["双轨对照", "  双轨校准法  "])

        result = self._merge()

        self.assertEqual(
            self._aliases(self.target),
            {"《观测规程》2.4-2.11", "规程2.4-2.11", "双轨校准法", "双轨对照"},
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_aliases"), 4)
        self.assertEqual(result["canonical_name"], "《观测规程》2.4-2.11")
        self.assertEqual(result["source_canonical_name"], "双轨校准法")

    def test_merge_moves_mentions_and_deduplicates_them_without_losing_offsets(self) -> None:
        self.store.add_concept_mention(self.target, "p2", start_codepoint=2, end_codepoint=7)
        self.store.add_concept_mention(self.source, "p2", start_codepoint=2, end_codepoint=7)
        self.store.add_concept_mention(self.target, "p2")
        self.store.add_concept_mention(self.source, "p2")
        before = self._count("SELECT COUNT(*) FROM concept_mentions")

        result = self._merge()

        self.assertEqual(before, 6)
        self.assertEqual(result["moved_mentions"], 1)
        # An offset-less duplicate has to deduplicate too: SQLite's UNIQUE
        # constraint treats NULLs as distinct and would happily store both.
        self.assertEqual(result["duplicate_mentions"], 2)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_mentions"), 4)
        self.assertEqual(
            {
                tuple(row)
                for row in self._connection().execute(
                    "SELECT passage_id, start_codepoint, end_codepoint, evidence FROM concept_mentions"
                )
            },
            {
                ("p1", 0, 5, "双轨校准法"),
                ("p1", 7, 21, "《观测规程》2.4-2.11"),
                ("p2", 2, 7, "双轨校准法"),
                ("p2", None, None, None),
            },
            "mentions keep their exact passage, offsets and evidence",
        )

    def test_merge_repoints_relations_on_both_sides_and_folds_a_duplicate(self) -> None:
        other = self.store.upsert_concept("校准法", concept_id="parable-genre")
        self.store.add_concept_mention(other, "p1", start_codepoint=3, end_codepoint=5)
        def evidence(start: int, end: int) -> list[dict[str, object]]:
            return [
                {
                    "passage_id": "p1",
                    "start_codepoint": start,
                    "end_codepoint": end,
                    "evidence": self.PASSAGE_ONE[start:end],
                }
            ]

        subject_side = self.store.add_concept_relation(
            "version-a", self.source, "ELABORATES", other, evidence=evidence(0, 5)
        )
        object_side = self.store.add_concept_relation(
            "version-a", other, "PRECEDES", self.source, evidence=evidence(0, 5)
        )
        survivor = self.store.add_concept_relation(
            "version-a", self.target, "CONTRASTS", other, evidence=evidence(0, 5)
        )
        duplicate = self.store.add_concept_relation(
            "version-a", self.source, "CONTRASTS", other, evidence=evidence(5, 7)
        )

        result = self._merge()

        self.assertEqual(result["repointed_relations"], 2)
        self.assertEqual(result["folded_relations"], 1)
        self.assertEqual(
            dict(
                self._connection()
                .execute(
                    "SELECT subject_concept_id, object_concept_id FROM concept_relations WHERE relation_id = ?",
                    (subject_side,),
                )
                .fetchone()
            ),
            {"subject_concept_id": self.target, "object_concept_id": other},
        )
        self.assertEqual(
            dict(
                self._connection()
                .execute(
                    "SELECT subject_concept_id, object_concept_id FROM concept_relations WHERE relation_id = ?",
                    (object_side,),
                )
                .fetchone()
            ),
            {"subject_concept_id": other, "object_concept_id": self.target},
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM concept_relations WHERE relation_id = ?", duplicate), 0
        )
        # The folded duplicate's grounded evidence survives on the relation
        # that absorbed it; nothing about the spans themselves changes.
        surviving_evidence = {
            (str(row["evidence"]), int(row["start_codepoint"]), int(row["end_codepoint"]))
            for row in self._connection().execute(
                """SELECT e.evidence, e.start_codepoint, e.end_codepoint
                     FROM concept_relation_evidence AS e
                     JOIN concept_relation_assertions AS a ON a.assertion_id = e.assertion_id
                    WHERE a.relation_id = ?""",
                (survivor,),
            )
        }
        self.assertEqual(surviving_evidence, {("双轨校准法", 0, 5), ("见于", 5, 7)})
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM concept_relation_assertions WHERE relation_id = ?", survivor
            ),
            1,
        )

    def test_merge_drops_a_relation_between_the_two_merged_concepts(self) -> None:
        relation_id = self.store.add_concept_relation(
            "version-a",
            self.source,
            "HAS_PART",
            self.target,
            evidence=[
                {
                    "passage_id": "p1",
                    "start_codepoint": 0,
                    "end_codepoint": 5,
                    "evidence": "双轨校准法",
                }
            ],
        )

        result = self._merge()

        # After the merge this relation would assert a link from the surviving
        # concept to itself, which the schema forbids by CHECK.  It is dropped
        # with its assertions and evidence, and the count says so out loud.
        self.assertEqual(result["dropped_self_relations"], 1)
        self.assertEqual(result["repointed_relations"], 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_relations"), 0)
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM concept_relation_assertions WHERE relation_id = ?", relation_id
            ),
            0,
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_relation_evidence"), 0)

    def test_merge_deletes_the_source_and_leaves_no_orphan_in_any_referencing_table(self) -> None:
        other = self.store.upsert_concept("校准法", concept_id="parable-genre")
        self.store.add_concept_mention(other, "p1", start_codepoint=3, end_codepoint=5)
        self.store.add_concept_mention(self.source, "p2", start_codepoint=2, end_codepoint=7)
        self.store.add_concept_relation(
            "version-a",
            self.source,
            "ELABORATES",
            other,
            evidence=[
                {
                    "passage_id": "p1",
                    "start_codepoint": 0,
                    "end_codepoint": 5,
                    "evidence": "双轨校准法",
                }
            ],
        )

        self._merge()

        self.assertIsNone(
            self._connection()
            .execute("SELECT 1 FROM concepts WHERE concept_id = ?", (self.source,))
            .fetchone()
        )
        for table, column in CONCEPT_REFERENCING_COLUMNS:
            self.assertEqual(
                self._count(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", self.source),
                0,
                f"{table}.{column} still references the merged-away concept",
            )
            self.assertEqual(
                self._count(
                    f"""SELECT COUNT(*) FROM {table} AS t
                          LEFT JOIN concepts AS c ON c.concept_id = t.{column}
                         WHERE c.concept_id IS NULL"""
                ),
                0,
                f"{table}.{column} has a dangling concept reference",
            )
        self.assertEqual(self._connection().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_canonical_override_promotes_the_source_spelling_and_demotes_the_previous_one(self) -> None:
        result = self._merge(canonical_name="双轨校准法")

        concept = self._connection().execute(
            "SELECT canonical_name, normalized_name, definition, status FROM concepts WHERE concept_id = ?",
            (self.target,),
        ).fetchone()
        self.assertEqual(concept["canonical_name"], "双轨校准法")
        self.assertEqual(concept["normalized_name"], "双轨校准法")
        self.assertEqual(concept["status"], "PROVISIONAL")
        self.assertEqual(concept["definition"], "")
        self.assertEqual(result["canonical_name"], "双轨校准法")
        self.assertEqual(
            self._aliases(self.target), {"《观测规程》2.4-2.11", "规程2.4-2.11", "双轨校准法"}
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 1)

    def test_merge_preserves_target_metadata_when_no_override_is_given(self) -> None:
        self.store.upsert_concept(
            "《观测规程》2.4-2.11", definition="主控制台解释双轨校准法", status="APPROVED"
        )

        self._merge()

        concept = self._connection().execute(
            "SELECT canonical_name, definition, status FROM concepts WHERE concept_id = ?",
            (self.target,),
        ).fetchone()
        self.assertEqual(concept["canonical_name"], "《观测规程》2.4-2.11")
        self.assertEqual(concept["definition"], "主控制台解释双轨校准法")
        self.assertEqual(concept["status"], "APPROVED")

    def test_merge_refuses_an_identical_or_unknown_concept_without_changing_anything(self) -> None:
        with self.assertRaisesRegex(IntegrityError, "merged into itself"):
            self._merge(source_concept_id=self.target)
        with self.assertRaisesRegex(UnknownConceptError, "unknown concept_id: missing"):
            self._merge(source_concept_id="missing")
        with self.assertRaisesRegex(UnknownConceptError, "unknown concept_id: missing"):
            self._merge(target_concept_id="missing")
        with self.assertRaisesRegex(IntegrityError, "operator identity"):
            self._merge(merged_by="   ")

        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 2)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_merges"), 0)

    def test_merging_an_already_merged_pair_fails_cleanly(self) -> None:
        self._merge()

        with self.assertRaisesRegex(UnknownConceptError, "unknown concept_id"):
            self._merge()

        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_merges"), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_mentions"), 2)

    def test_merge_refuses_a_canonical_override_owned_by_a_third_concept(self) -> None:
        self.store.upsert_concept("单轨校准法", aliases=["双轨对照"], concept_id="third")

        with self.assertRaisesRegex(IntegrityError, "belongs to a different concept"):
            self._merge(canonical_name="单轨校准法")
        with self.assertRaisesRegex(IntegrityError, "alias of a different concept"):
            self._merge(canonical_name="双轨对照")

        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 3)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_merges"), 0)

    def test_merge_writes_an_identifier_only_audit_row(self) -> None:
        self.store.add_concept_mention(self.source, "p2", start_codepoint=2, end_codepoint=7)

        result = self._merge(merged_by="administrator")

        row = self._connection().execute("SELECT * FROM concept_merges").fetchone()
        self.assertEqual(row["concept_merge_id"], result["concept_merge_id"])
        self.assertEqual(row["target_concept_id"], self.target)
        self.assertEqual(row["source_concept_id"], self.source)
        self.assertEqual(row["source_canonical_name"], "双轨校准法")
        self.assertEqual(row["merged_by"], "administrator")
        self.assertTrue(row["merged_at"])
        # A canonical name is a concept label.  No passage text, evidence span,
        # prompt or model output may reach this audit record.
        recorded = " ".join(str(value) for value in tuple(row))
        self.assertNotIn(self.PASSAGE_ONE, recorded)
        self.assertNotIn("见于", recorded)

    def test_concept_listing_pages_the_graph_with_aliases_and_mention_counts(self) -> None:
        self.store.add_concept_mention(self.source, "p2", start_codepoint=2, end_codepoint=7)
        self.store.upsert_concept("单轨校准法", concept_id="third", status="REJECTED")

        self.assertEqual(self.store.count_concepts(), 3)
        self.assertEqual(self.store.count_concepts(status="REJECTED"), 1)
        listed = self.store.list_concepts(offset=0, limit=2)
        self.assertEqual([concept["concept_id"] for concept in listed], ["citation", "third"])
        self.assertEqual(listed[0]["aliases"], ["《观测规程》2.4-2.11", "规程2.4-2.11"])
        self.assertEqual(listed[0]["mention_count"], 1)
        self.assertEqual(listed[1]["mention_count"], 0)
        remainder = self.store.list_concepts(offset=2, limit=2)
        self.assertEqual([concept["concept_id"] for concept in remainder], ["parable"])
        self.assertEqual(remainder[0]["mention_count"], 2)
        with self.assertRaisesRegex(IntegrityError, "pagination values are invalid"):
            self.store.list_concepts(offset=0, limit=0)
        with self.assertRaisesRegex(IntegrityError, "invalid concept status"):
            self.store.count_concepts(status="UNKNOWN")


class SQLiteEpubConceptSplitTest(unittest.TestCase):
    """The correction path for a merge an administrator got wrong.

    ``merge_concepts`` is one-way, and two merges have already had to be undone
    after review.  Restoring a backup and replaying stops working the moment a
    later job postdates the backup, which is why this exists.  The fixture is
    the second of those two mistakes, committed for real: the teaching
    ``双轨校准法`` folded into ``《观测规程》2.4-2.11``, the scripture locator that
    merely names it.  Every test starts from that merged state.

    A split is explicitly a *new* decision, not a rewind: ``concept_merges``
    never recorded which aliases or mentions moved, so the administrator names
    them.
    """

    PASSAGE_ONE = "双轨校准法见于《观测规程》2.4-2.11。"
    PASSAGE_TWO = "又论双轨校准法。"

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SQLiteEpubStore(os.path.join(self.tempdir.name, "epub.db"))
        self.addCleanup(self.store.close)
        book_id = self.store.create_book("单轨校准法", book_id="book-a")
        self.store.create_book_version(
            book_id, epub_bytes=b"split test epub", version_id="version-a"
        )
        self.store.add_passages(
            "version-a",
            [
                {
                    "passage_id": "p1",
                    "source_href": "chapter-13.xhtml",
                    "spine_index": 0,
                    "ordinal": 0,
                    "content_kind": "paragraph",
                    "content": self.PASSAGE_ONE,
                },
                {
                    "passage_id": "p2",
                    "source_href": "chapter-13.xhtml",
                    "spine_index": 0,
                    "ordinal": 1,
                    "content_kind": "paragraph",
                    "content": self.PASSAGE_TWO,
                },
            ],
        )
        self.citation = self.store.upsert_concept(
            "《观测规程》2.4-2.11", aliases=["规程2.4-2.11"], concept_id="citation"
        )
        self.parable = self.store.upsert_concept(
            "双轨校准法", aliases=["双轨对照"], concept_id="parable"
        )
        self.store.add_concept_mention(self.citation, "p1", start_codepoint=7, end_codepoint=21)
        self.store.add_concept_mention(self.parable, "p1", start_codepoint=0, end_codepoint=5)
        self.store.add_concept_mention(self.parable, "p2", start_codepoint=2, end_codepoint=7)
        # Snapshot before the mistake, so a round trip has something to be
        # measured against rather than merely "looking right".
        self.before_merge = self._state(self.parable)
        self.store.merge_concepts(
            target_concept_id=self.citation,
            source_concept_id=self.parable,
            merged_by="administrator",
        )

    def _connection(self):
        return self.store._connection()

    def _count(self, sql: str, *parameters: object) -> int:
        return int(self._connection().execute(sql, parameters).fetchone()[0])

    def _aliases(self, concept_id: str) -> set[str]:
        return {
            str(row["alias"])
            for row in self._connection().execute(
                "SELECT alias FROM concept_aliases WHERE concept_id = ?", (concept_id,)
            )
        }

    def _mentions(self, concept_id: str) -> set[tuple[object, ...]]:
        return {
            tuple(row)
            for row in self._connection().execute(
                """SELECT passage_id, start_codepoint, end_codepoint, evidence, source
                     FROM concept_mentions WHERE concept_id = ?""",
                (concept_id,),
            )
        }

    def _state(self, concept_id: str) -> dict[str, object]:
        """Everything about a concept that a merge moved and a split must restore."""
        row = self._connection().execute(
            "SELECT canonical_name, normalized_name, definition, status FROM concepts WHERE concept_id = ?",
            (concept_id,),
        ).fetchone()
        return {
            "concept": dict(row),
            "aliases": self._aliases(concept_id),
            "mentions": self._mentions(concept_id),
        }

    def _split(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "source_concept_id": self.citation,
            "canonical_name": "双轨校准法",
            "aliases": ["双轨校准法", "双轨对照"],
            "mentions": [
                {"passage_id": "p1", "start_codepoint": 0, "end_codepoint": 5},
                {"passage_id": "p2", "start_codepoint": 2, "end_codepoint": 7},
            ],
            "split_by": "administrator",
        }
        arguments.update(overrides)
        return self.store.split_concept(**arguments)  # type: ignore[arg-type]

    def _passage(self, passage_id: str) -> str:
        return str(
            self._connection()
            .execute("SELECT content FROM passages WHERE passage_id = ?", (passage_id,))
            .fetchone()["content"]
        )

    def _assert_every_mention_reslices(self, concept_id: str) -> None:
        rows = self._connection().execute(
            """SELECT passage_id, start_codepoint, end_codepoint, evidence
                 FROM concept_mentions WHERE concept_id = ?""",
            (concept_id,),
        ).fetchall()
        self.assertTrue(rows, "the concept holds no mention to re-slice")
        for row in rows:
            if row["start_codepoint"] is None:
                continue
            self.assertEqual(
                row["evidence"],
                self._passage(str(row["passage_id"]))[
                    int(row["start_codepoint"]) : int(row["end_codepoint"])
                ],
                "a moved mention no longer re-slices byte-exact from its passage",
            )

    def test_split_moves_the_named_aliases_and_mentions_and_leaves_both_concepts_valid(self) -> None:
        result = self._split()
        new_id = str(result["new_concept_id"])

        self.assertEqual(result["moved_aliases"], 2)
        self.assertEqual(result["moved_mentions"], 2)
        self.assertEqual(result["canonical_name"], "双轨校准法")
        self.assertEqual(result["source_canonical_name"], "《观测规程》2.4-2.11")
        self.assertEqual(self._aliases(new_id), {"双轨校准法", "双轨对照"})
        # The source keeps the rest, including its own canonical spelling: a
        # concept whose name were an alias of another concept is exactly the
        # ambiguity ``upsert_concept`` refuses to create.
        self.assertEqual(self._aliases(self.citation), {"《观测规程》2.4-2.11", "规程2.4-2.11"})
        self.assertEqual(
            self._mentions(new_id),
            {("p1", 0, 5, "双轨校准法", "MODEL"), ("p2", 2, 7, "双轨校准法", "MODEL")},
        )
        self.assertEqual(
            self._mentions(self.citation), {("p1", 7, 21, "《观测规程》2.4-2.11", "MODEL")}
        )
        self._assert_every_mention_reslices(new_id)
        self._assert_every_mention_reslices(self.citation)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 2)
        self.assertEqual(self._connection().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_the_new_canonical_name_may_be_a_moving_alias_but_never_a_taken_spelling(self) -> None:
        third = self.store.upsert_concept(
            "单轨校准法", aliases=["单轨法"], concept_id="third"
        )

        with self.assertRaisesRegex(IntegrityError, "already belongs to an existing concept"):
            self._split(canonical_name="单轨校准法")
        with self.assertRaisesRegex(IntegrityError, "already an alias of an existing concept"):
            self._split(canonical_name="单轨法")
        # The source's own name is owned by the source, so it is taken too.
        with self.assertRaisesRegex(IntegrityError, "already belongs to an existing concept"):
            self._split(canonical_name="《观测规程》2.4-2.11")
        # An alias of the source that is *not* moving is still a taken spelling.
        with self.assertRaisesRegex(IntegrityError, "already an alias of an existing concept"):
            self._split(canonical_name="规程2.4-2.11", aliases=["双轨校准法"])
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_splits"), 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 2)

        # A spelling nobody owns is fine, and so is one of the moving aliases.
        free = self._split(canonical_name="双轨对照校准法")
        self.assertEqual(
            self._aliases(str(free["new_concept_id"])),
            {"双轨校准法", "双轨对照", "双轨对照校准法"},
        )
        self.assertEqual(free["moved_aliases"], 3)
        self.assertEqual(self._aliases(third), {"单轨校准法", "单轨法"})

    def test_split_refuses_to_move_every_mention_because_that_is_a_rename(self) -> None:
        every = [
            {"passage_id": "p1", "start_codepoint": 0, "end_codepoint": 5},
            {"passage_id": "p1", "start_codepoint": 7, "end_codepoint": 21},
            {"passage_id": "p2", "start_codepoint": 2, "end_codepoint": 7},
        ]

        with self.assertRaisesRegex(IntegrityError, "cannot move every mention"):
            self._split(mentions=every)

        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_splits"), 0)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM concept_mentions WHERE concept_id = ?", self.citation),
            3,
        )

    def test_split_refuses_an_unknown_source_a_foreign_alias_or_a_foreign_mention(self) -> None:
        other = self.store.upsert_concept("校准法", concept_id="parable-genre")
        self.store.add_concept_mention(other, "p1", start_codepoint=3, end_codepoint=5)

        with self.assertRaisesRegex(UnknownConceptError, "unknown concept_id: missing"):
            self._split(source_concept_id="missing")
        with self.assertRaisesRegex(IntegrityError, "does not own this alias"):
            self._split(aliases=["双轨校准法", "校准法"])
        with self.assertRaisesRegex(IntegrityError, "does not own this alias"):
            self._split(aliases=["双轨校准法", "从未出现的别名"])
        with self.assertRaisesRegex(IntegrityError, "belongs to a different concept"):
            self._split(
                mentions=[{"passage_id": "p1", "start_codepoint": 3, "end_codepoint": 5}]
            )
        with self.assertRaisesRegex(IntegrityError, "does not exist"):
            self._split(
                mentions=[{"passage_id": "p2", "start_codepoint": 0, "end_codepoint": 1}]
            )
        with self.assertRaisesRegex(IntegrityError, "own canonical spelling"):
            self._split(canonical_name="观测规程第二章", aliases=["《观测规程》2.4-2.11"])
        with self.assertRaisesRegex(IntegrityError, "operator identity"):
            self._split(split_by="   ")

        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_splits"), 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 2)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM concept_mentions WHERE concept_id = ?", self.citation),
            3,
        )
        self.assertEqual(self._aliases(other), {"校准法"})

    def test_relations_stay_on_the_source_and_the_report_names_the_ones_to_review(self) -> None:
        """A relation's correct endpoint after a split is nobody's derivation.

        Moving one automatically would assert something no administrator
        decided, which is the very class of error a split exists to correct.
        """
        other = self.store.upsert_concept("校准法", concept_id="parable-genre")
        self.store.add_concept_mention(other, "p1", start_codepoint=3, end_codepoint=5)

        def evidence(start: int, end: int) -> list[dict[str, object]]:
            return [
                {
                    "passage_id": "p1",
                    "start_codepoint": start,
                    "end_codepoint": end,
                    "evidence": self.PASSAGE_ONE[start:end],
                }
            ]

        about_the_parable = self.store.add_concept_relation(
            "version-a", self.citation, "ELABORATES", other, evidence=evidence(0, 5)
        )
        about_the_citation = self.store.add_concept_relation(
            "version-a", other, "PRECEDES", self.citation, evidence=evidence(7, 21)
        )

        result = self._split()

        self.assertEqual(result["relations_on_source"], 2)
        # Only the one grounded on text that literally names a split-off
        # spelling is shortlisted; nothing is moved either way.
        self.assertEqual(result["relations_naming_split_aliases"], 1)
        endpoints = {
            str(row["relation_id"]): (str(row["subject_concept_id"]), str(row["object_concept_id"]))
            for row in self._connection().execute(
                "SELECT relation_id, subject_concept_id, object_concept_id FROM concept_relations"
            )
        }
        self.assertEqual(
            endpoints,
            {
                about_the_parable: (self.citation, other),
                about_the_citation: (other, self.citation),
            },
            "a split must never repoint a relation",
        )
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM concept_relations WHERE subject_concept_id = ? OR object_concept_id = ?",
                str(result["new_concept_id"]),
                str(result["new_concept_id"]),
            ),
            0,
        )
        self.assertEqual(self._connection().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_split_writes_an_identifier_only_audit_row(self) -> None:
        result = self._split(split_by="administrator")

        row = self._connection().execute("SELECT * FROM concept_splits").fetchone()
        self.assertEqual(row["concept_split_id"], result["concept_split_id"])
        self.assertEqual(row["source_concept_id"], self.citation)
        self.assertEqual(row["new_concept_id"], result["new_concept_id"])
        self.assertEqual(row["new_canonical_name"], "双轨校准法")
        self.assertEqual(row["split_by"], "administrator")
        self.assertTrue(row["split_at"])
        self.assertEqual(result["split_at"], row["split_at"])
        # A canonical name is a concept label.  No passage text, evidence span,
        # prompt or model output may reach this audit record.
        recorded = " ".join(str(value) for value in tuple(row))
        self.assertNotIn(self.PASSAGE_ONE, recorded)
        self.assertNotIn("见于", recorded)
        # The merge audit is untouched: a split is a new decision beside it, not
        # a retraction of what an administrator did earlier.
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_merges"), 1)
        self.assertEqual(self._connection().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_merging_then_splitting_restores_the_concept_that_was_folded_away(self) -> None:
        """The capability's real purpose, measured against the pre-merge state.

        The identifiers are new, and deliberately so -- this is an
        administrator's fresh decision, not a rewind of the merge.  Everything
        the merge actually moved comes back byte for byte.
        """
        result = self._split()
        restored = self._state(str(result["new_concept_id"]))

        self.assertEqual(restored["aliases"], self.before_merge["aliases"])
        self.assertEqual(restored["mentions"], self.before_merge["mentions"])
        self.assertEqual(
            restored["concept"],
            self.before_merge["concept"],
            "the recreated concept must match the one the merge folded away",
        )
        self.assertNotEqual(result["new_concept_id"], self.parable)
        self._assert_every_mention_reslices(str(result["new_concept_id"]))
        # And the concept it was wrongly merged into is back to what it was.
        self.assertEqual(self._aliases(self.citation), {"《观测规程》2.4-2.11", "规程2.4-2.11"})
        self.assertEqual(
            self._mentions(self.citation), {("p1", 7, 21, "《观测规程》2.4-2.11", "MODEL")}
        )
        self.assertEqual(self._connection().execute("PRAGMA foreign_key_check").fetchall(), [])


class PortableAnalysisOverlayTest(unittest.TestCase):
    """T-170a: an analysis must travel without a single character of the book.

    Every store built here holds the *same* book, created independently of any
    overlay, which is the situation the feature exists for: one administrator
    pays for the Batch run, everyone else already owns the EPUB.
    """

    MARKER = "MARKER-DO-NOT-EXPORT-7f3"
    PASSAGES = (f"TCP 是传输控制协议。{MARKER}", "第二段落，说明 TCP 与 IP。")
    EPUB_BYTES = b"one complete deterministic epub archive"

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.stores: list[object] = []
        self.source, self.source_version = self._book("source.db")
        self.tcp = self.source.upsert_concept(
            "TCP",
            aliases=["Transmission Control Protocol"],
            definition="传输层协议",
            status="APPROVED",
        )
        self.ip = self.source.upsert_concept("IP", status="PROVISIONAL")
        first, second = (row["passage_id"] for row in self.source.list_passages(self.source_version))
        # An ADMIN mention proves the artifact carries no authority claim: the
        # receiving store must record its own copy as model output.
        self.source.add_concept_mention(
            self.tcp, first, start_codepoint=0, end_codepoint=3, source="ADMIN"
        )
        self.source.add_concept_mention(self.tcp, second, start_codepoint=8, end_codepoint=11)
        self.source.add_concept_mention(self.ip, second, start_codepoint=14, end_codepoint=16)
        self.source.add_concept_relation(
            self.source_version,
            self.tcp,
            "CONTRASTS",
            self.ip,
            evidence=[
                {
                    "passage_id": second,
                    "start_codepoint": 8,
                    "end_codepoint": 16,
                    "evidence": "TCP 与 IP",
                }
            ],
        )
        self.overlay = self.source.export_concept_overlay(self.source_version)

    def tearDown(self) -> None:
        for store in self.stores:
            store.close()
        self.tempdir.cleanup()

    def _book(self, name: str, *, passages: tuple[str, ...] | None = None) -> tuple[object, str]:
        """Build a store holding this book, independent of any overlay."""
        path = os.path.join(self.tempdir.name, name)
        store = SQLiteEpubStore(path)
        self.stores.append(store)
        book_id = store.create_book("原书")
        version = store.create_book_version(book_id, epub_bytes=self.EPUB_BYTES)
        store.add_passages(
            version.version_id,
            [
                {
                    "source_href": "chapter.xhtml",
                    "spine_index": 0,
                    "ordinal": ordinal,
                    "content_kind": "paragraph",
                    "content": content,
                }
                for ordinal, content in enumerate(passages or self.PASSAGES)
            ],
        )
        store.set_version_status(version.version_id, "READY")
        store.path_for_test = path  # type: ignore[attr-defined]
        return store, version.version_id

    def _graph(self, store: object, version_id: str) -> dict[str, object]:
        """Read the whole applied graph as comparable, order-independent sets."""
        connection = sqlite3.connect(store.path_for_test)  # type: ignore[attr-defined]
        try:
            return {
                "concepts": set(
                    connection.execute(
                        "SELECT canonical_name, normalized_name, definition, status FROM concepts"
                    )
                ),
                "aliases": set(connection.execute("SELECT alias, normalized_alias FROM concept_aliases")),
                "mentions": set(
                    connection.execute(
                        """SELECT c.normalized_name, p.ordinal, m.start_codepoint,
                                  m.end_codepoint, m.evidence
                             FROM concept_mentions AS m
                             JOIN concepts AS c ON c.concept_id = m.concept_id
                             JOIN passages AS p ON p.passage_id = m.passage_id"""
                    )
                ),
                "relations": set(
                    connection.execute(
                        """SELECT s.normalized_name, r.predicate, o.normalized_name, a.status,
                                  p.ordinal, e.start_codepoint, e.end_codepoint, e.evidence
                             FROM concept_relation_evidence AS e
                             JOIN concept_relation_assertions AS a ON a.assertion_id = e.assertion_id
                             JOIN concept_relations AS r ON r.relation_id = a.relation_id
                             JOIN concepts AS s ON s.concept_id = r.subject_concept_id
                             JOIN concepts AS o ON o.concept_id = r.object_concept_id
                             JOIN passages AS p ON p.passage_id = e.passage_id"""
                    )
                ),
            }
        finally:
            connection.close()

    def _assert_spans_are_own_source(self, store: object) -> None:
        """Every stored span must be a byte-exact slice of *this* store's book."""
        connection = sqlite3.connect(store.path_for_test)  # type: ignore[attr-defined]
        try:
            spans = list(
                connection.execute(
                    """SELECT p.content, m.start_codepoint, m.end_codepoint, m.evidence
                         FROM concept_mentions AS m JOIN passages AS p ON p.passage_id = m.passage_id"""
                )
            ) + list(
                connection.execute(
                    """SELECT p.content, e.start_codepoint, e.end_codepoint, e.evidence
                         FROM concept_relation_evidence AS e
                         JOIN passages AS p ON p.passage_id = e.passage_id"""
                )
            )
            self.assertTrue(spans)
            for content, start, end, evidence in spans:
                self.assertEqual(evidence, content[start:end])
        finally:
            connection.close()

    def test_the_artifact_contains_no_passage_text_at_all(self) -> None:
        """The invariant the whole design rests on."""
        serialized = self.overlay.to_json()

        self.assertNotIn(self.MARKER, serialized)
        for passage in self.PASSAGES:
            self.assertNotIn(passage, serialized)
        # A concept label that also occurs in the book still travels: labels
        # and definitions are the analysis product, not source material.
        self.assertIn("传输层协议", serialized)

        # Labels and definitions are short by construction - the prompt profile
        # caps a definition at 30 code points - so no *contiguous* run of source
        # longer than that can ride along inside one.  Measured on the real book
        # the longest definition is 23 code points and exactly one 12-code-point
        # window of 56,634 appears at all, via a label.  This bound is what would
        # fail loudly if a later profile ever let a definition quote a sentence,
        # which is the only way this artifact could start carrying real source.
        longest_permitted_run = 40
        for passage in self.PASSAGES:
            for start in range(0, max(1, len(passage) - longest_permitted_run)):
                window = passage[start:start + longest_permitted_run]
                if len(window) == longest_permitted_run:
                    self.assertNotIn(window, serialized)
        payload = json.loads(serialized)
        self.assertEqual(
            set(payload),
            {
                "overlay_format_version",
                "epub_sha256",
                "parser_version",
                "book_title",
                "passage_fingerprint",
                "concepts",
                "mentions",
                "relations",
            },
        )
        for mention in payload["mentions"]:
            self.assertEqual(
                set(mention),
                {"concept_key", "ordinal", "content_sha256", "start_codepoint", "end_codepoint"},
            )
        for relation in payload["relations"]:
            for span in relation["evidence"]:
                self.assertEqual(
                    set(span), {"ordinal", "content_sha256", "start_codepoint", "end_codepoint"}
                )

    def test_exporting_the_same_graph_twice_yields_identical_bytes(self) -> None:
        self.assertEqual(
            self.source.export_concept_overlay(self.source_version).to_json(),
            self.overlay.to_json(),
        )
        self.assertEqual(
            self.source.export_concept_overlay(self.source_version).digest(), self.overlay.digest()
        )

    def test_overlay_round_trips_into_an_independently_built_store(self) -> None:
        target, version_id = self._book("target.db")

        summary = target.apply_overlay(parse_overlay_json(self.overlay.to_json()), version_id=version_id)

        self.assertEqual(summary["applied_detail"]["concepts_created"], 2)
        self.assertEqual(summary["applied_detail"]["mentions_created"], 3)
        self.assertEqual(summary["applied_detail"]["relations_created"], 1)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(self._graph(target, version_id), self._graph(self.source, self.source_version))
        self._assert_spans_are_own_source(target)
        # Re-exporting the imported graph reproduces the publisher's bytes.
        self.assertEqual(target.export_concept_overlay(version_id).to_json(), self.overlay.to_json())
        # The publisher's ADMIN mention arrives as model output, never as this
        # operator's own decision.
        connection = sqlite3.connect(target.path_for_test)
        try:
            self.assertEqual(
                sorted(connection.execute("SELECT DISTINCT source FROM concept_mentions")), [("MODEL",)]
            )
        finally:
            connection.close()

    def test_applying_the_same_overlay_twice_changes_nothing(self) -> None:
        target, version_id = self._book("target.db")
        overlay = parse_overlay_json(self.overlay.to_json())

        # Without an explicit target the archive hash resolves the version,
        # which is how a reader who never saw the publisher's identifiers
        # reattaches the analysis to their own copy.
        target.apply_overlay(overlay)
        before = self._graph(target, version_id)
        second = target.apply_overlay(overlay, version_id=version_id)

        self.assertEqual(second["applied"], 0)
        self.assertEqual(second["skipped_detail"]["mentions_existing"], 3)
        self.assertEqual(second["skipped_detail"]["relations_existing"], 1)
        self.assertEqual(self._graph(target, version_id), before)

    def test_a_local_decision_outranks_the_published_analysis(self) -> None:
        """Never downgrade APPROVED, never overwrite an ADMIN mention."""
        target, version_id = self._book("target.db")
        local_concept = target.upsert_concept(
            "TCP", definition="本地定义", status="APPROVED", alias_source="ADMIN"
        )
        first = target.list_passages(version_id)[0]["passage_id"]
        target.add_concept_mention(
            local_concept, first, start_codepoint=0, end_codepoint=3, source="ADMIN"
        )
        published = replace(
            self.overlay,
            concepts=tuple(
                replace(concept, status="PROVISIONAL", definition="覆盖层定义")
                if concept.key == "tcp"
                else concept
                for concept in self.overlay.concepts
            ),
        )

        summary = target.apply_overlay(published, version_id=version_id)

        surviving = next(
            concept for concept in target.list_concepts() if concept["canonical_name"] == "TCP"
        )
        self.assertEqual(surviving["status"], "APPROVED")
        self.assertEqual(surviving["definition"], "本地定义")
        # The published spelling still arrives as an alias, so a later model
        # response naming it resolves to this concept.
        self.assertIn("Transmission Control Protocol", surviving["aliases"])
        self.assertEqual(summary["skipped_reasons"]["concept_status_locked"], 1)
        self.assertEqual(summary["skipped_reasons"]["concept_definition_locked"], 1)
        self.assertEqual(summary["skipped_reasons"]["mention_admin_owned"], 1)
        connection = sqlite3.connect(target.path_for_test)
        try:
            self.assertEqual(
                list(
                    connection.execute(
                        """SELECT source FROM concept_mentions
                            WHERE start_codepoint = 0 AND end_codepoint = 3"""
                    )
                ),
                [("ADMIN",)],
            )
        finally:
            connection.close()

    def test_an_overlay_for_a_different_archive_is_refused(self) -> None:
        target, version_id = self._book("target.db")
        foreign = replace(self.overlay, epub_sha256="b" * 64)

        with self.assertRaises(OverlayRejected) as refusal:
            target.apply_overlay(foreign, version_id=version_id)

        self.assertEqual(refusal.exception.reason, "epub_sha256_mismatch")
        self.assertEqual(target.count_concepts(), 0)
        with self.assertRaises(OverlayRejected) as unresolvable:
            target.apply_overlay(foreign)
        self.assertEqual(unresolvable.exception.reason, "epub_sha256_mismatch")

    def test_a_published_spelling_owned_by_another_local_concept_is_refused(self) -> None:
        """A duplicate is an administrator merge decision, not an import's."""
        target, version_id = self._book("target.db")
        # Locally, the published concept's spelling is already an alias of a
        # different concept, so silently attaching the analysis to either one
        # would be a guess.
        target.upsert_concept("网络协议", aliases=["TCP"], status="APPROVED")

        with self.assertRaises(OverlayRejected) as refusal:
            target.apply_overlay(self.overlay, version_id=version_id)

        self.assertEqual(refusal.exception.reason, "alias_conflict")
        self.assertNotIn("TCP", str(refusal.exception))
        self.assertEqual(self._graph(target, version_id)["mentions"], set())

    def test_an_overlay_from_another_parser_format_is_refused(self) -> None:
        """A parser change can shift every offset, so the format must match."""
        target, version_id = self._book("target.db")
        stale = replace(self.overlay, parser_version="2")

        with self.assertRaises(OverlayRejected) as refusal:
            target.apply_overlay(stale, version_id=version_id)

        self.assertEqual(refusal.exception.reason, "parser_version_mismatch")
        self.assertEqual(target.count_concepts(), 0)

    def test_a_store_whose_passage_set_differs_is_refused(self) -> None:
        target, version_id = self._book("target.db", passages=(*self.PASSAGES, "多出来的一段。"))

        with self.assertRaises(OverlayRejected) as refusal:
            target.apply_overlay(self.overlay, version_id=version_id)

        self.assertEqual(refusal.exception.reason, "passage_fingerprint_mismatch")
        self.assertEqual(target.count_concepts(), 0)
        # A tampered digest is refused by the same gate.
        matching, matching_version = self._book("matching.db")
        with self.assertRaises(OverlayRejected) as tampered:
            matching.apply_overlay(
                replace(self.overlay, fingerprint=PassageFingerprint(count=2, digest="c" * 64)),
                version_id=matching_version,
            )
        self.assertEqual(tampered.exception.reason, "passage_fingerprint_mismatch")

    def test_a_single_drifted_passage_hash_rejects_the_whole_artifact(self) -> None:
        target, version_id = self._book("target.db")
        drifted = replace(
            self.overlay,
            mentions=(
                replace(self.overlay.mentions[0], content_sha256="d" * 64),
                *self.overlay.mentions[1:],
            ),
        )

        with self.assertRaises(OverlayRejected) as refusal:
            target.apply_overlay(drifted, version_id=version_id)

        self.assertEqual(refusal.exception.reason, "passage_content_drift")
        # Atomicity: the concepts written earlier in the same transaction are
        # gone too, so no partial graph survives a refused artifact.
        self.assertEqual(target.count_concepts(), 0)
        self.assertEqual(self._graph(target, version_id)["mentions"], set())

    def test_an_out_of_range_span_rejects_the_whole_artifact(self) -> None:
        target, version_id = self._book("target.db")
        overrun = replace(
            self.overlay,
            mentions=(
                replace(self.overlay.mentions[0], end_codepoint=9_999),
                *self.overlay.mentions[1:],
            ),
        )

        with self.assertRaises(OverlayRejected) as refusal:
            target.apply_overlay(overrun, version_id=version_id)

        self.assertEqual(refusal.exception.reason, "offsets_out_of_range")
        self.assertEqual(target.count_concepts(), 0)

    def test_a_location_that_names_no_passage_is_refused(self) -> None:
        target, version_id = self._book("target.db")
        missing = replace(
            self.overlay,
            mentions=(replace(self.overlay.mentions[0], ordinal=99), *self.overlay.mentions[1:]),
        )

        with self.assertRaises(OverlayRejected) as refusal:
            target.apply_overlay(missing, version_id=version_id)

        self.assertEqual(refusal.exception.reason, "passage_missing")
        self.assertEqual(target.count_concepts(), 0)

    def test_a_malformed_artifact_never_reaches_the_store(self) -> None:
        serialized = json.loads(self.overlay.to_json())
        serialized["concepts"][0]["key"] = "not-the-normalized-name"
        with self.assertRaisesRegex(OverlayError, "normalized form"):
            parse_overlay_json(json.dumps(serialized))

        smuggled = json.loads(self.overlay.to_json())
        smuggled["mentions"][0]["evidence"] = self.MARKER
        with self.assertRaisesRegex(OverlayError, "list of objects|unsupported fields|must be"):
            parse_overlay_json(json.dumps(smuggled))

        with self.assertRaisesRegex(OverlayError, "UTF-8 JSON"):
            parse_overlay_json(b"\xff\xfe not json")

    def test_a_mention_naming_an_undeclared_concept_cannot_be_built(self) -> None:
        with self.assertRaisesRegex(OverlayError, "does not declare"):
            OVERLAY_MODULE.build_overlay(
                epub_sha256="a" * 64,
                parser_version="1",
                book_title="原书",
                fingerprint=self.overlay.fingerprint,
                mentions=[OverlayMention("ghost", 0, "e" * 64, 0, 3)],
            )


if __name__ == "__main__":
    unittest.main()
