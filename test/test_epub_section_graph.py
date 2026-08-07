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
    # v2 has two submitted cloud samples (gpt-4.1 and gpt-4o-mini), so it became
    # immutable the moment those requests left the machine, exactly as
    # zh-glossary-v5 did.  v3 is scored against those samples, so an edit to v2
    # now would silently move the baseline it is being compared with.
    "zh-section-graph-v2": "1b89eb4e6117450d3a72eaea332d6034fb33f0275ee8ae21f01569d53e96fde9",
}

# ``min_evidence_codepoints`` postdates the digests above and is pinned beside
# them rather than folded into ``_profile_digest``, following the precedent the
# concept path set for ``asks_for_offsets``: recomputing a historical digest to
# admit one new field would destroy the only thing that digest is for, which is
# proving nobody has touched the bytes since they were sampled.
#
# Pinning matters more here than for the flags, because ingest acts on these
# numbers - a span below its job's floor is rejected outright and costs the
# whole packet.  v1 is 0 because its instruction never named a minimum, and its
# stored requests must keep replaying on the contract they were given; v2 is 10
# because its instruction says 10.
_PUBLISHED_PROFILE_EVIDENCE_FLOORS = {
    "zh-section-graph-v1": 0,
    "zh-section-graph-v2": 10,
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

    def test_v1_and_v2_are_frozen_and_still_selectable_while_v3_is_the_default(self) -> None:
        registered = GRAPH.available_section_graph_profiles()
        self.assertEqual(GRAPH.DEFAULT_SECTION_GRAPH_PROFILE, "zh-section-graph-v3")
        self.assertIn("zh-section-graph-v3", registered)
        for profile_id, digest in _PUBLISHED_PROFILE_DIGESTS.items():
            with self.subTest(profile=profile_id):
                self.assertIn(profile_id, registered)
                self.assertEqual(_profile_digest(GRAPH.get_section_graph_profile(profile_id)), digest)
        for profile_id, floor in _PUBLISHED_PROFILE_EVIDENCE_FLOORS.items():
            with self.subTest(profile=profile_id, flag="min_evidence_codepoints"):
                self.assertEqual(
                    GRAPH.get_section_graph_profile(profile_id).min_evidence_codepoints, floor
                )
        # Every published profile is pinned on both axes, so a new one cannot be
        # added without deciding what it froze.
        self.assertEqual(
            set(_PUBLISHED_PROFILE_DIGESTS), set(_PUBLISHED_PROFILE_EVIDENCE_FLOORS)
        )
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
            self._request("zh-section-graph-v4")

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
        # The minimum evidence clause and the enforced floor must state the same
        # number.  Ingest reads only the field and the model reads only the
        # text, so drift between them would enforce a contract nobody was given.
        self.assertIn("evidence 至少 10 个 Unicode 字符", v2.system_instruction)
        self.assertIn(
            "该 passage 总长不足 10 个 Unicode 字符时，evidence 取该 passage 全文",
            v2.system_instruction,
        )
        self.assertEqual(v2.min_evidence_codepoints, 10)
        # v1 asked for no minimum at all, so it is enforced with none.
        self.assertNotIn("至少 10 个 Unicode 字符", v1.system_instruction)
        self.assertEqual(v1.min_evidence_codepoints, 0)

    def test_v3_is_the_default_and_keeps_v2s_contract_and_decoding_budget(self) -> None:
        v2 = GRAPH.get_section_graph_profile("zh-section-graph-v2")
        v3 = GRAPH.get_section_graph_profile("zh-section-graph-v3")
        self.assertEqual(GRAPH.DEFAULT_SECTION_GRAPH_PROFILE, "zh-section-graph-v3")
        self.assertEqual(v3.profile_id, "zh-section-graph-v3")
        # v3 changes the payload and one clause, never the decoding budget: a
        # difference in max_tokens or temperature would confound the comparison
        # against the two submitted v2 samples.
        self.assertEqual(v3.max_tokens, v2.max_tokens)
        self.assertEqual(v3.temperature, v2.temperature)
        # The section-graph spelling of the concept path's uses_context_anchors:
        # the profile, not "whichever profile is currently the default", decides
        # that the anchored, offset-free schema is what gets sent.
        self.assertFalse(v3.asks_for_offsets)
        # v3 carries v2's minimum evidence clause verbatim, escape hatch
        # included, so it carries v2's enforced floor.
        self.assertEqual(v3.min_evidence_codepoints, v2.min_evidence_codepoints)
        self.assertEqual(v3.min_evidence_codepoints, 10)
        self.assertIn("evidence 至少 10 个 Unicode 字符", v3.system_instruction)
        self.assertIn(
            "该 passage 总长不足 10 个 Unicode 字符时，evidence 取该 passage 全文",
            v3.system_instruction,
        )
        self.assertEqual(v3.output_schema, v2.output_schema)
        request = self._request("zh-section-graph-v3")
        schema = request["response_format"]["json_schema"]["schema"]
        mention = _mention_schema(schema)
        self.assertEqual(
            mention["required"], ["passage_id", "evidence", "context_before", "context_after"]
        )
        self.assertEqual(mention, _relation_evidence_schema(schema))
        self.assertEqual(
            mention["properties"]["context_before"]["maxLength"],
            GRAPH.MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS,
        )
        self.assertEqual(request["max_tokens"], 8_192)
        self.assertEqual(request["seed"], 0)

    def test_the_packet_no_longer_hands_the_model_a_per_passage_toc_path(self) -> None:
        # This, not the clause, is the fix.  v2 already carried a verbatim-copy
        # rule and it did not help, because the model was not paraphrasing: it
        # was copying a section title out of a field the packet gave it and
        # never scoped.  Removing the field removes the temptation, and it costs
        # nothing - TOC provenance is a deterministic server-side write.
        titles = ("神是万物生命的源头（二）", "神为人类预备的日用饮食")
        packet = GRAPH.build_section_graph_packets(
            [
                {"passage_id": "a1", "ordinal": 1, "toc_path": ["卷一", titles[0]], "content": "原文甲"},
                {"passage_id": "a2", "ordinal": 2, "toc_path": ["卷一", titles[1]], "content": "原文乙"},
            ]
        )[0]
        # The packet still knows the full path; only what is sent is narrower.
        self.assertEqual(packet.passages[0]["toc_path"], ["卷一", titles[0]])
        for profile_id in GRAPH.available_section_graph_profiles():
            with self.subTest(profile=profile_id):
                user_message = GRAPH.build_section_graph_completion_request(
                    model="model-snapshot", packet=packet, profile_id=profile_id
                )["messages"][1]["content"]
                body = json.loads(user_message)
                self.assertEqual(
                    [set(passage) for passage in body["passages"]],
                    [{"passage_id", "content"}, {"passage_id", "content"}],
                )
                self.assertEqual(
                    [passage["content"] for passage in body["passages"]], ["原文甲", "原文乙"]
                )
                # Not merely absent from the parsed shape: the leaf titles are
                # nowhere in the bytes the provider receives, so they cannot be
                # quoted back as evidence at all.
                for title in titles:
                    self.assertNotIn(title, user_message)
                # The packet-level path stays.  SDD 4.2.2 point 2 justifies the
                # packet shape by one request carrying enough local context to
                # recognise a section-level concept and its parts together, so
                # removing all context would undercut the packet itself.
                self.assertEqual(body["toc_path"], ["卷一"])
                self.assertEqual(body["packet_id"], packet.packet_id)

    def test_v3_adds_only_the_toc_path_scoping_clause_on_top_of_v2(self) -> None:
        v2 = GRAPH.get_section_graph_profile("zh-section-graph-v2").system_instruction
        v3 = GRAPH.get_section_graph_profile("zh-section-graph-v3").system_instruction
        self.assertNotEqual(v2, v3)
        # v3 exists to isolate one variable against the two submitted v2
        # samples, so it may differ by exactly two edits that both express that
        # variable.  The first is forced: the opening line told the model each
        # passage carries a 目录路径, and after this change it does not, so
        # leaving the sentence would make the prompt describe a payload that is
        # not the one being sent.  The second is the scoping clause itself.
        # Undoing both must reproduce v2 byte for byte; any other drift fails.
        header_v2 = "passage_id、目录路径和原文"
        header_v3 = "passage_id 和原文"
        self.assertIn(header_v2, v2)
        self.assertNotIn(header_v2, v3)
        self.assertIn(header_v3, v3)

        reverted = v3.replace(header_v3, header_v2, 1)
        prefix = 0
        while prefix < min(len(v2), len(reverted)) and v2[prefix] == reverted[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < min(len(v2), len(reverted)) - prefix
            and v2[len(v2) - 1 - suffix] == reverted[len(reverted) - 1 - suffix]
        ):
            suffix += 1
        self.assertEqual(v2[prefix:len(v2) - suffix], "")
        inserted = reverted[prefix:len(reverted) - suffix]
        self.assertEqual(v2, reverted.replace(inserted, "", 1))
        # The clause has to do three things an instruction against paraphrasing
        # could not: name the only field evidence may come from, name toc_path
        # as navigation rather than source, and forbid it outright.
        # The insertion point falls inside the shared word "evidence ", so the
        # minimal inserted block starts one word later; the clause as written is
        # asserted against v3 itself.
        self.assertIn("只能取自 passage_id 所指那条段落的 content 字段", inserted)
        self.assertIn("evidence 只能取自 passage_id 所指那条段落的 content 字段", v3)
        self.assertIn("toc_path 只是本包在目录中的导航", inserted)
        self.assertIn("不是可引用的原文", inserted)
        self.assertIn("不得把 toc_path 或其中的章节标题当作 evidence", inserted)
        # Every v2 lever survives verbatim, so the sample stays one-variable:
        # no offsets, conditional anchors, the minimum evidence span with its
        # short-passage escape hatch, verbatim copy, the caps that make the
        # token budget defensible, the predicate vocabulary, local_id, and the
        # strict JSON shape.
        for clause in (
            "不要输出任何字符位置",
            "逐字复制",
            "不得改写、翻译或统一引号",
            "至少 10 个 Unicode 字符",
            "完整、有意义的短语或分句",
            "不得只给出概念本身",
            "不足 10 个 Unicode 字符时",
            "只出现一次时",
            "必须都是空字符串",
            "重复出现时",
            "最多 48 个 Unicode 字符",
            "紧邻",
            "最多输出 12 个",
            "最多输出 12 条关系",
            "只保留一个最有代表性的出现位置",
            "每条只给出一条最有代表性的 evidence",
            "本响应唯一的 local_id",
            "predicate 只能使用指定枚举",
            "不要把单纯目录层级重复写成语义关系",
            "不要依据外部知识补充事实",
            "只返回严格 JSON 对象，不要 Markdown 或解释",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, v2)
                self.assertIn(clause, v3)
        self.assertNotIn("start_codepoint", v3)
        self.assertNotIn("end_codepoint", v3)

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
