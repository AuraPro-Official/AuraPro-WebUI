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
# always names the surviving concept; the other four carry graph rows that a
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
        has to sort by ``spine_index``/``ordinal`` rather than by insertion.
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
        """
        concept_ids = self._graph_span_fixture()
        rows = self.store.list_concept_occurrences(concept_ids, offset=0, limit=20)

        self.assertEqual(
            self._span_keys(rows),
            [("passage-a", 0, 2), ("passage-a", 3, 7), ("passage-b", 0, 4), ("passage-b", 2, 7)],
        )
        # The exact duplicate collapses; neither concept is dropped.
        self.assertEqual(rows[0]["canonical_names"], ("Alpha", "Beta"))
        self.assertEqual(rows[0]["concept_ids"], tuple(sorted(concept_ids[:2])))
        # The nested span collapses into its container, which carries both.
        self.assertEqual(rows[1]["canonical_names"], ("Alpha", "Gamma"))
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

        self.assertEqual(self._span_keys(rows), [("passage-b", 0, 4), ("passage-b", 2, 7)])
        self.assertEqual(rows[0]["canonical_names"], ("Alpha",))
        self.assertEqual(rows[1]["canonical_names"], ("Gamma",))

    def test_graph_occurrence_total_equals_every_page_walked_one_by_one(self) -> None:
        """The count and the pages must apply the same de-duplication predicate.

        Filtering duplicates after ``LIMIT``/``OFFSET`` would give ragged pages
        and a total that disagrees with them, so this walks the pages instead
        of recomputing the expectation.
        """
        concept_ids = self._graph_span_fixture()
        total = self.store.count_concept_occurrences(concept_ids)

        walked: list[dict[str, object]] = []
        offset = 0
        while True:
            page = self.store.list_concept_occurrences(concept_ids, offset=offset, limit=1)
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
            keys, self._span_keys(self.store.list_concept_occurrences(concept_ids, offset=0, limit=20))
        )


class SQLiteEpubConceptMergeTest(unittest.TestCase):
    """An administrator remedy for a model-suggested duplicate concept.

    The Batch ingest guard refuses an item whose suggestion exactly matches
    two concepts.  These tests cover the only action that can resolve such a
    candidate: folding one concept into the other in a single transaction.
    """

    PASSAGE_ONE = "稗子的比喻见于《马太福音》13:36-43。"
    PASSAGE_TWO = "又论稗子的比喻。"

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SQLiteEpubStore(os.path.join(self.tempdir.name, "epub.db"))
        self.addCleanup(self.store.close)
        book_id = self.store.create_book("撒种的比喻", book_id="book-a")
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
            "《马太福音》13:36-43", aliases=["太13:36-43"], concept_id="citation"
        )
        self.source = self.store.upsert_concept("稗子的比喻", concept_id="parable")
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

    def test_enumerated_referencing_columns_match_the_live_schema(self) -> None:
        """A future migration that adds a reference must fail this, not silently orphan rows."""
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

    def test_merge_moves_every_alias_including_the_source_canonical_spelling(self) -> None:
        self.store.upsert_concept("稗子的比喻", aliases=["麦子和稗子", "  稗子的比喻  "])

        result = self._merge()

        self.assertEqual(
            self._aliases(self.target),
            {"《马太福音》13:36-43", "太13:36-43", "稗子的比喻", "麦子和稗子"},
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_aliases"), 4)
        self.assertEqual(result["canonical_name"], "《马太福音》13:36-43")
        self.assertEqual(result["source_canonical_name"], "稗子的比喻")

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
                ("p1", 0, 5, "稗子的比喻"),
                ("p1", 7, 21, "《马太福音》13:36-43"),
                ("p2", 2, 7, "稗子的比喻"),
                ("p2", None, None, None),
            },
            "mentions keep their exact passage, offsets and evidence",
        )

    def test_merge_repoints_relations_on_both_sides_and_folds_a_duplicate(self) -> None:
        other = self.store.upsert_concept("比喻", concept_id="parable-genre")
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
        self.assertEqual(surviving_evidence, {("稗子的比喻", 0, 5), ("见于", 5, 7)})
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
                    "evidence": "稗子的比喻",
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
        other = self.store.upsert_concept("比喻", concept_id="parable-genre")
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
                    "evidence": "稗子的比喻",
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
        result = self._merge(canonical_name="稗子的比喻")

        concept = self._connection().execute(
            "SELECT canonical_name, normalized_name, definition, status FROM concepts WHERE concept_id = ?",
            (self.target,),
        ).fetchone()
        self.assertEqual(concept["canonical_name"], "稗子的比喻")
        self.assertEqual(concept["normalized_name"], "稗子的比喻")
        self.assertEqual(concept["status"], "PROVISIONAL")
        self.assertEqual(concept["definition"], "")
        self.assertEqual(result["canonical_name"], "稗子的比喻")
        self.assertEqual(
            self._aliases(self.target), {"《马太福音》13:36-43", "太13:36-43", "稗子的比喻"}
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 1)

    def test_merge_preserves_target_metadata_when_no_override_is_given(self) -> None:
        self.store.upsert_concept(
            "《马太福音》13:36-43", definition="主耶稣解释稗子的比喻", status="APPROVED"
        )

        self._merge()

        concept = self._connection().execute(
            "SELECT canonical_name, definition, status FROM concepts WHERE concept_id = ?",
            (self.target,),
        ).fetchone()
        self.assertEqual(concept["canonical_name"], "《马太福音》13:36-43")
        self.assertEqual(concept["definition"], "主耶稣解释稗子的比喻")
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
        self.store.upsert_concept("撒种的比喻", aliases=["麦子和稗子"], concept_id="third")

        with self.assertRaisesRegex(IntegrityError, "belongs to a different concept"):
            self._merge(canonical_name="撒种的比喻")
        with self.assertRaisesRegex(IntegrityError, "alias of a different concept"):
            self._merge(canonical_name="麦子和稗子")

        self.assertEqual(self._count("SELECT COUNT(*) FROM concepts"), 3)
        self.assertEqual(self._count("SELECT COUNT(*) FROM concept_merges"), 0)

    def test_merge_writes_an_identifier_only_audit_row(self) -> None:
        self.store.add_concept_mention(self.source, "p2", start_codepoint=2, end_codepoint=7)

        result = self._merge(merged_by="administrator")

        row = self._connection().execute("SELECT * FROM concept_merges").fetchone()
        self.assertEqual(row["concept_merge_id"], result["concept_merge_id"])
        self.assertEqual(row["target_concept_id"], self.target)
        self.assertEqual(row["source_concept_id"], self.source)
        self.assertEqual(row["source_canonical_name"], "稗子的比喻")
        self.assertEqual(row["merged_by"], "administrator")
        self.assertTrue(row["merged_at"])
        # A canonical name is a concept label.  No passage text, evidence span,
        # prompt or model output may reach this audit record.
        recorded = " ".join(str(value) for value in tuple(row))
        self.assertNotIn(self.PASSAGE_ONE, recorded)
        self.assertNotIn("见于", recorded)

    def test_concept_listing_pages_the_graph_with_aliases_and_mention_counts(self) -> None:
        self.store.add_concept_mention(self.source, "p2", start_codepoint=2, end_codepoint=7)
        self.store.upsert_concept("撒种的比喻", concept_id="third", status="REJECTED")

        self.assertEqual(self.store.count_concepts(), 3)
        self.assertEqual(self.store.count_concepts(status="REJECTED"), 1)
        listed = self.store.list_concepts(offset=0, limit=2)
        self.assertEqual([concept["concept_id"] for concept in listed], ["citation", "third"])
        self.assertEqual(listed[0]["aliases"], ["《马太福音》13:36-43", "太13:36-43"])
        self.assertEqual(listed[0]["mention_count"], 1)
        self.assertEqual(listed[1]["mention_count"], 0)
        remainder = self.store.list_concepts(offset=2, limit=2)
        self.assertEqual([concept["concept_id"] for concept in remainder], ["parable"])
        self.assertEqual(remainder[0]["mention_count"], 2)
        with self.assertRaisesRegex(IntegrityError, "pagination values are invalid"):
            self.store.list_concepts(offset=0, limit=0)
        with self.assertRaisesRegex(IntegrityError, "invalid concept status"):
            self.store.count_concepts(status="UNKNOWN")


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
