"""Acceptance tests for faithful local-only EPUB concept search."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
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
SEARCH = _load("search")

ConceptTerm = SEARCH.ConceptTerm
ConceptTermMatcher = SEARCH.ConceptTermMatcher
EpubSearchService = SEARCH.EpubSearchService
ModelAvailability = INFERENCE.ModelAvailability
DerivedVectorRecord = VECTOR_INDEX.DerivedVectorRecord


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


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
        ]
        self.occurrences = [
            {**self.passages["p1"], "canonical_name": "TCP", "start_codepoint": 0, "end_codepoint": 3},
            {**self.passages["p2"], "canonical_name": "TCP", "start_codepoint": 7, "end_codepoint": 10},
            {**self.passages["p2"], "canonical_name": "TCP", "start_codepoint": None, "end_codepoint": None},
        ]
        self.units = {
            "u1": self._unit("u1", "p1", 0, 8),
            "u2": self._unit("u2", "p2", 0, 9),
            "u3": self._unit("u3", "p3", 0, 8),
        }

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
        return self.terms

    def count_concept_occurrences(self, concept_ids):
        return len(self.occurrences) if "tcp" in concept_ids else 0

    def list_concept_occurrences(self, concept_ids, *, offset, limit):
        if "tcp" not in concept_ids:
            return []
        return self.occurrences[offset : offset + limit]

    def get_search_passage(self, passage_id):
        return self.passages.get(passage_id)

    def get_retrieval_unit(self, retrieval_unit_id):
        return self.units.get(retrieval_unit_id)

    def matched_concept_names(self, passage_id, concept_ids):
        return ["TCP"] if passage_id in {"p1", "p2"} and "tcp" in concept_ids else []


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
    def test_tier_one_trie_uses_latin_boundaries_and_direct_cjk_matching(self) -> None:
        matcher = ConceptTermMatcher(
            [
                ConceptTerm("tcp", "TCP", "TCP"),
                ConceptTerm("search", "检索", "检索"),
            ]
        )
        self.assertEqual([term.concept_id for term in matcher.match("用 TCP 做检索")], ["tcp", "search"])
        self.assertEqual(matcher.match("TCPIP"), ())
        self.assertEqual([term.concept_id for term in matcher.match("中文检索词")], ["search"])

    def test_graph_channel_is_exhaustive_paginated_and_never_returns_only_excerpt(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
