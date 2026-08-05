"""Focused, dependency-free tests for the canonical EPUB SQLite store."""

from __future__ import annotations

import os
import importlib.util
import sys
import tempfile
import unittest


STORE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../backend/open_webui/retrieval/epub/store.py")
)
STORE_SPEC = importlib.util.spec_from_file_location("epub_store_for_test", STORE_PATH)
assert STORE_SPEC is not None and STORE_SPEC.loader is not None
STORE_MODULE = importlib.util.module_from_spec(STORE_SPEC)
sys.modules[STORE_SPEC.name] = STORE_MODULE
STORE_SPEC.loader.exec_module(STORE_MODULE)
IntegrityError = STORE_MODULE.IntegrityError
SQLiteEpubStore = STORE_MODULE.SQLiteEpubStore
UnknownConceptError = STORE_MODULE.UnknownConceptError

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
        # The real collision: a procedure and the document citation naming it.
        # The citation is currently canonical and already owns the short form.
        self.target = self.store.upsert_concept(
            "《观测规程》2.4-2.11", aliases=["规程2.4-2.11"], concept_id="citation"
        )
        self.source = self.store.upsert_concept("双轨校准法", concept_id="procedure")
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
        other = self.store.upsert_concept("校准法", concept_id="procedure-genre")
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
        other = self.store.upsert_concept("校准法", concept_id="procedure-genre")
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
        self.assertEqual([concept["concept_id"] for concept in remainder], ["procedure"])
        self.assertEqual(remainder[0]["mention_count"], 2)
        with self.assertRaisesRegex(IntegrityError, "pagination values are invalid"):
            self.store.list_concepts(offset=0, limit=0)
        with self.assertRaisesRegex(IntegrityError, "invalid concept status"):
            self.store.count_concepts(status="UNKNOWN")


if __name__ == "__main__":
    unittest.main()
