"""Unit coverage for deterministic TOC-scoped graph extraction packets."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest


def _load_module():
    path = Path(__file__).parents[1] / "backend/open_webui/retrieval/epub/section_graph.py"
    spec = importlib.util.spec_from_file_location("epub_section_graph_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GRAPH = _load_module()

# Same convention as the concept profiles: a registered profile is immutable,
# because a durable Batch job stores the profile it was built from and editing
# one in place would re-point a stored request at an instruction that was never
# submitted.  These digests pin the instruction, the decoding limits and the
# output schema of every superseded profile.  If one fails, the fix is a new
# profile version, never a new digest.
_PUBLISHED_PROFILE_DIGESTS = {
    "zh-section-graph-v1": "11fc0cd52dc2820a0b88bd102bfd9122ca31d5d19b653b3d6830d4fd11e260b4",
}


def _profile_digest(profile) -> str:
    return hashlib.sha256(
        "\n".join(
            (
                profile.profile_id,
                profile.system_instruction,
                str(profile.max_tokens),
                repr(profile.temperature),
                str(profile.asks_for_offsets),
                json.dumps(profile.output_schema, sort_keys=True, ensure_ascii=False),
            )
        ).encode("utf-8")
    ).hexdigest()


def _mention_schema(schema: dict) -> dict:
    return schema["properties"]["concepts"]["items"]["properties"]["mentions"]["items"]


def _relation_evidence_schema(schema: dict) -> dict:
    return schema["properties"]["relations"]["items"]["properties"]["evidence"]["items"]


class SectionGraphPacketTest(unittest.TestCase):
    def test_packets_keep_top_level_toc_boundaries_and_have_real_anchors(self) -> None:
        passages = [
            {"passage_id": "a1", "ordinal": 1, "toc_path": ["A"], "content": "甲" * 4},
            {"passage_id": "a2", "ordinal": 2, "toc_path": ["A", "A.1"], "content": "乙" * 4},
            {"passage_id": "b1", "ordinal": 3, "toc_path": ["B"], "content": "丙" * 4},
        ]
        packets = GRAPH.build_section_graph_packets(passages, max_characters=6)

        self.assertEqual([(packet.toc_path, packet.anchor_passage_id) for packet in packets], [(('A',), 'a1'), (('A',), 'a2'), (('B',), 'b1')])
        self.assertEqual([packet.packet_id for packet in packets], ["A:0:a1", "A:1:a2", "B:0:b1"])

    def test_request_uses_strict_schema_with_packet_local_relation_endpoints(self) -> None:
        packet = GRAPH.build_section_graph_packets(
            [{"passage_id": "a1", "ordinal": 1, "toc_path": ["A"], "content": "原文"}]
        )[0]
        request = GRAPH.build_section_graph_completion_request(model="model-snapshot", packet=packet)
        relation = request["response_format"]["json_schema"]["schema"]["properties"]["relations"]["items"]

        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        self.assertEqual(relation["required"], ["subject_local_id", "predicate", "object_local_id", "evidence"])
        self.assertEqual(request["messages"][1]["role"], "user")


class SectionGraphProfileTest(unittest.TestCase):
    def _packet(self):
        return GRAPH.build_section_graph_packets(
            [{"passage_id": "a1", "ordinal": 1, "toc_path": ["A"], "content": "原文"}]
        )[0]

    def _request(self, profile_id: str) -> dict:
        return GRAPH.build_section_graph_completion_request(
            model="model-snapshot", packet=self._packet(), profile_id=profile_id
        )

    def test_v1_is_frozen_and_still_selectable_while_v2_is_the_default(self) -> None:
        registered = GRAPH.available_section_graph_profiles()
        self.assertEqual(GRAPH.DEFAULT_SECTION_GRAPH_PROFILE, "zh-section-graph-v2")
        self.assertIn("zh-section-graph-v2", registered)
        for profile_id, digest in _PUBLISHED_PROFILE_DIGESTS.items():
            with self.subTest(profile=profile_id):
                self.assertIn(profile_id, registered)
                self.assertEqual(_profile_digest(GRAPH.get_section_graph_profile(profile_id)), digest)
        # A stored v1 request must still replay byte for byte: the offsets it
        # asks for are now repaired at ingest, but the request itself is not
        # rewritten under an approval that was made against it.
        v1 = self._request("zh-section-graph-v1")
        self.assertEqual(v1["max_tokens"], 1_800)
        self.assertEqual(
            _mention_schema(v1["response_format"]["json_schema"]["schema"])["required"],
            ["passage_id", "start_codepoint", "end_codepoint", "evidence"],
        )
        with self.assertRaisesRegex(GRAPH.SectionGraphError, "unknown EPUB section graph profile"):
            self._request("zh-section-graph-v3")

    def test_v2_asks_for_no_offsets_and_uses_one_span_shape_everywhere(self) -> None:
        schema = self._request("zh-section-graph-v2")["response_format"]["json_schema"]["schema"]
        mention = _mention_schema(schema)
        # The measured failure this profile exists to remove: a model names the
        # right text almost always and the right offsets about one time in
        # thirty-seven, and a packet needs every one of them to be right.
        self.assertEqual(
            mention["required"], ["passage_id", "evidence", "context_before", "context_after"]
        )
        self.assertNotIn("start_codepoint", mention["properties"])
        self.assertNotIn("end_codepoint", mention["properties"])
        self.assertEqual(
            mention["properties"]["context_before"]["maxLength"],
            GRAPH.MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS,
        )
        self.assertEqual(
            mention["properties"]["context_after"]["maxLength"],
            GRAPH.MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS,
        )
        # A relation evidence span is grounded by the same resolver as a
        # mention, so a shape difference between them could only be an accident.
        self.assertEqual(mention, _relation_evidence_schema(schema))
        # Everything that is not the offsets is unchanged from v1.
        v1_schema = self._request("zh-section-graph-v1")["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["required"], v1_schema["required"])
        self.assertEqual(
            schema["properties"]["concepts"]["items"]["required"],
            v1_schema["properties"]["concepts"]["items"]["required"],
        )
        self.assertEqual(
            schema["properties"]["relations"]["items"]["properties"]["predicate"]["enum"],
            list(GRAPH.RELATION_PREDICATES),
        )
        self.assertEqual(
            schema["properties"]["relations"]["items"]["properties"]["evidence"]["minItems"], 1
        )

    def test_v2_carries_over_the_concept_levers_that_worked(self) -> None:
        instruction = GRAPH.get_section_graph_profile("zh-section-graph-v2").system_instruction
        v1 = GRAPH.get_section_graph_profile("zh-section-graph-v1").system_instruction
        # Verbatim copy, naming the normalizations to avoid: the dominant
        # EVIDENCE_ABSENT cause is a model tidying quotes and punctuation.
        self.assertIn("逐字复制", instruction)
        self.assertIn("不得改写、翻译或统一引号", instruction)
        # A minimum span with a short-passage escape hatch.  A longer span is
        # likelier to be unique, which is exactly what server-side derivation
        # needs, and the hatch keeps it possible on a heading.
        self.assertIn("至少 10 个 Unicode 字符", instruction)
        self.assertIn("完整、有意义的短语或分句", instruction)
        self.assertIn("不得只给出概念本身", instruction)
        self.assertIn("不足 10 个 Unicode 字符时", instruction)
        # Conditional anchors: they cost output only where they decide something.
        self.assertIn("只出现一次时", instruction)
        self.assertIn("必须都是空字符串", instruction)
        self.assertIn("重复出现时", instruction)
        self.assertIn("最多 48 个 Unicode 字符", instruction)
        self.assertIn("紧邻", instruction)
        # Offsets are the server's job now, and the instruction has to say so
        # rather than leave the model to guess why the field is gone.
        self.assertIn("不要输出任何字符位置", instruction)
        self.assertNotIn("start_codepoint", instruction)
        self.assertNotIn("end_codepoint", instruction)
        self.assertIn("start_codepoint", v1)
        # v1's own rules that are not about offsets survive unchanged.
        for clause in (
            "本响应唯一的 local_id",
            "predicate 只能使用指定枚举",
            "不要把单纯目录层级重复写成语义关系",
            "不要依据外部知识补充事实",
            "只返回严格 JSON 对象，不要 Markdown 或解释",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, v1)
                self.assertIn(clause, instruction)

    def test_v2_budget_covers_a_real_packet_instead_of_v1s_guess(self) -> None:
        v1 = GRAPH.get_section_graph_profile("zh-section-graph-v1")
        v2 = GRAPH.get_section_graph_profile("zh-section-graph-v2")
        self.assertEqual(v2.max_tokens, 8_192)
        self.assertFalse(v2.asks_for_offsets)
        self.assertTrue(v1.asks_for_offsets)
        self.assertEqual(v2.temperature, 0.0)
        self.assertEqual(self._request("zh-section-graph-v2")["seed"], 0)

        # A real full-book run is 43 packets averaging 9_465 characters, the
        # largest 11_996.  v1's 1_800 tokens is about five concepts of this
        # contract on a packet that size, and a truncated response is not JSON
        # at all.  The caps in the instruction are what make the worst case
        # finite: without them no budget is defensible, which is how 1_800
        # survived.  12 concepts at ~324 tokens plus 12 relations at ~228 is
        # ~6_650, and 8_192 clears that.
        self.assertGreater(v2.max_tokens, v1.max_tokens)
        self.assertIn("最多输出 12 个", v2.system_instruction)
        self.assertIn("最多输出 12 条关系", v2.system_instruction)
        self.assertIn("只保留一个最有代表性的出现位置", v2.system_instruction)
        self.assertIn("每条只给出一条最有代表性的 evidence", v2.system_instruction)
        self.assertGreaterEqual(v2.max_tokens, 12 * 324 + 12 * 228)

    def test_the_largest_real_packet_still_builds_one_request(self) -> None:
        # The packet builder and the budget have to agree about the same
        # boundary: SECTION_GRAPH_MAX_CHARACTERS is what a request can hold.
        passages = [
            {
                "passage_id": f"p{index}",
                "ordinal": index,
                "toc_path": ["A"],
                "content": "甲" * 100,
            }
            for index in range(int(GRAPH.SECTION_GRAPH_MAX_CHARACTERS / 100))
        ]
        packets = GRAPH.build_section_graph_packets(passages)
        self.assertEqual(len(packets), 1)
        request = GRAPH.build_section_graph_completion_request(
            model="model-snapshot", packet=packets[0]
        )
        body = json.loads(request["messages"][1]["content"])
        self.assertEqual(len(body["passages"]), len(passages))
        self.assertEqual(
            sum(len(passage["content"]) for passage in body["passages"]),
            GRAPH.SECTION_GRAPH_MAX_CHARACTERS,
        )
        self.assertEqual(request["max_tokens"], 8_192)


if __name__ == "__main__":
    unittest.main()
