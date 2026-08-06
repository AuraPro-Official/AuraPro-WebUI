"""Unit coverage for local-first concept prompt calibration primitives."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest


def _load_module():
    path = Path(__file__).parents[1] / "backend/open_webui/retrieval/epub/prompt_profiles.py"
    spec = importlib.util.spec_from_file_location("epub_prompt_profiles_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROMPTS = _load_module()

# A durable cloud sample approval keys on the profile ID, so a published
# profile can never be edited in place: doing so would leave an approval
# pointing at an instruction that was never actually sampled.  These digests
# pin the exact instruction text and decoding limits of every superseded
# profile.  If one of them fails, the fix is a new profile version, never a
# new digest.
_PUBLISHED_PROFILE_DIGESTS = {
    "zh-glossary-v1": "b045a53e3b760ba5fcf0591207ee8c0b68d35f4461a2f713ac6419fe0bc7c80b",
    "zh-glossary-v2": "4ba4ac59c3f6d31f38c23e3d4a739f10a593566df1b12c8a117889b0d1a1b3cc",
    "zh-glossary-v3": "5358971cb3ffa79e769b6f9e0f7dc84d701bf3486782d95ff4d0748035d2bf99",
    "zh-glossary-v4": "62c306f19dedf939dd93d313b2957eb8e3e3a4b1fe7dabd531705440f2ec7fcc",
    # v5 has live submitted cloud samples (419dd120, e6e027d1), so it became
    # immutable the moment those requests left the machine; editing it now would
    # invalidate results already paid for and still in flight.
    "zh-glossary-v5": "16f32d361d70fc596a930cea4b0899846fb1e930ca9e80d7732930c2ad2f0630",
    # v6 has the one approved sample in the system (005a7a20, 20/20) and the
    # 1,224-item full-book job that approval unlocked.  Editing it would
    # invalidate both at once - the approval would point at an instruction that
    # was never sampled, and the in-flight job's stored requests would no longer
    # correspond to any registered profile.  Same reason as v5 and
    # zh-section-graph-v2, so it is pinned the moment v7 supersedes it.
    "zh-glossary-v6": "311514e90f29448505645484bcbf944c8535188640cfed0f1ea32383a7d1ba1d",
}

# ``asks_for_offsets`` postdates the digests above and is pinned beside them
# rather than folded into the hash: recomputing every historical digest to add
# one field would destroy the only thing they are for, which is proving that
# nobody has touched the bytes since they were sampled.  Together the two maps
# pin the whole strict contract, because the schema a profile sends is a pure
# function of these two flags (see ``_strict_schema_for``).
_PUBLISHED_PROFILE_OFFSET_CONTRACTS = {
    "zh-glossary-v1": True,
    "zh-glossary-v2": True,
    "zh-glossary-v3": True,
    "zh-glossary-v4": True,
    "zh-glossary-v5": True,
    "zh-glossary-v6": True,
}


def _profile_digest(profile) -> str:
    return hashlib.sha256(
        "\n".join(
            (
                profile.profile_id,
                profile.system_instruction,
                str(profile.max_tokens),
                repr(profile.temperature),
                str(profile.uses_context_anchors),
            )
        ).encode("utf-8")
    ).hexdigest()


def _mention_schema(request) -> dict:
    schema = request["response_format"]["json_schema"]["schema"]
    return schema["properties"]["concepts"]["items"]["properties"]["mentions"]["items"]


class PromptProfileTest(unittest.TestCase):
    def test_same_profile_has_local_json_and_remote_strict_schema_contracts(self) -> None:
        local = PROMPTS.build_concept_completion_request(
            model="local-model",
            profile_id="zh-glossary-v1",
            passage="甲乙",
            remote_structured_output=False,
        )
        remote = PROMPTS.build_concept_completion_request(
            model="remote-model-snapshot",
            profile_id="zh-glossary-v1",
            passage="甲乙",
            remote_structured_output=True,
        )
        self.assertEqual(local["response_format"], {"type": "json_object"})
        self.assertEqual(remote["response_format"]["type"], "json_schema")
        self.assertTrue(remote["response_format"]["json_schema"]["strict"])
        self.assertEqual(local["messages"][0]["content"], remote["messages"][0]["content"])
        self.assertEqual(local["seed"], 0)
        self.assertEqual(remote["seed"], 0)

    def test_default_cloud_profile_requires_bounded_context_anchors(self) -> None:
        remote = PROMPTS.build_concept_completion_request(
            model="remote-model-snapshot",
            profile_id=PROMPTS.DEFAULT_CONCEPT_PROMPT_PROFILE,
            passage="甲甲",
            remote_structured_output=True,
        )
        mention = _mention_schema(remote)
        # The anchors are the whole locating mechanism now: they are the only
        # thing the model supplies that can choose between repeated literals.
        self.assertEqual(mention["required"], ["evidence", "context_before", "context_after"])
        self.assertEqual(
            mention["properties"]["context_before"]["maxLength"],
            PROMPTS.MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS,
        )

    def test_superseded_profiles_are_frozen_and_v7_is_the_registered_default(self) -> None:
        registered = PROMPTS.available_prompt_profiles()
        self.assertEqual(PROMPTS.DEFAULT_CONCEPT_PROMPT_PROFILE, "zh-glossary-v7")
        self.assertIn("zh-glossary-v7", registered)
        for profile_id, digest in _PUBLISHED_PROFILE_DIGESTS.items():
            with self.subTest(profile=profile_id):
                self.assertIn(profile_id, registered)
                self.assertEqual(_profile_digest(PROMPTS.get_prompt_profile(profile_id)), digest)
        for profile_id, asks in _PUBLISHED_PROFILE_OFFSET_CONTRACTS.items():
            with self.subTest(profile=profile_id, flag="asks_for_offsets"):
                self.assertEqual(PROMPTS.get_prompt_profile(profile_id).asks_for_offsets, asks)
        # Every published profile is pinned on both axes, so a new one cannot be
        # added without deciding what it froze.
        self.assertEqual(
            set(_PUBLISHED_PROFILE_DIGESTS), set(_PUBLISHED_PROFILE_OFFSET_CONTRACTS)
        )
        self.assertEqual(
            set(registered) - set(_PUBLISHED_PROFILE_DIGESTS),
            {PROMPTS.DEFAULT_CONCEPT_PROMPT_PROFILE},
        )
        # A superseded anchored profile keeps the anchored strict schema even
        # though it is no longer the default, so an existing cloud sample stays
        # replayable byte for byte.
        v4 = PROMPTS.build_concept_completion_request(
            model="remote-model-snapshot",
            profile_id="zh-glossary-v4",
            passage="甲甲",
            remote_structured_output=True,
        )
        self.assertEqual(v4["max_tokens"], 512)
        self.assertIn(
            "context_before",
            v4["response_format"]["json_schema"]["schema"]["properties"]["concepts"]["items"][
                "properties"
            ]["mentions"]["items"]["required"],
        )

    def test_v5_raises_the_truncation_budget_and_makes_anchors_conditional(self) -> None:
        v4 = PROMPTS.get_prompt_profile("zh-glossary-v4")
        v5 = PROMPTS.get_prompt_profile("zh-glossary-v5")
        # Six of the eighteen v4 sample failures were truncated responses on
        # long passages; the budget has to cover the worst case the contract
        # allows, not the median one.
        self.assertGreater(v5.max_tokens, v4.max_tokens)
        self.assertGreaterEqual(v5.max_tokens, 1_536)
        # Anchors now cost output only where they can actually decide something.
        self.assertIn("只出现一次时", v5.system_instruction)
        self.assertIn("必须都是空字符串", v5.system_instruction)
        self.assertIn("重复出现时", v5.system_instruction)
        self.assertIn("最多 48 个 Unicode 字符", v5.system_instruction)
        self.assertIn("紧邻", v5.system_instruction)
        # Everything that already worked is retained.
        self.assertIn("逐字复制", v5.system_instruction)
        self.assertIn("最多抽取 6 个", v5.system_instruction)
        self.assertIn("第一个字符必须是 {", v5.system_instruction)
        self.assertIn("Markdown 代码块", v5.system_instruction)
        self.assertEqual(v5.temperature, 0.0)

    def test_v5_output_contract_still_passes_strict_payload_validation(self) -> None:
        passage = "甲乙丙。甲乙丙是一个名称。"
        request = PROMPTS.build_concept_completion_request(
            model="remote-model-snapshot",
            profile_id="zh-glossary-v5",
            passage=passage,
            remote_structured_output=True,
        )
        self.assertEqual(request["max_tokens"], PROMPTS.get_prompt_profile("zh-glossary-v5").max_tokens)
        self.assertEqual(
            request["response_format"]["json_schema"]["schema"], PROMPTS.CONCEPT_OUTPUT_SCHEMA
        )
        unique = PROMPTS.validate_concept_payload(
            {
                "concepts": [
                    {
                        "name": "名称",
                        "aliases": [],
                        "definition": "段中出现的名称。",
                        "mentions": [
                            {
                                "start_codepoint": 10,
                                "end_codepoint": 12,
                                "evidence": "名称",
                                "context_before": "",
                                "context_after": "",
                            }
                        ],
                    }
                ]
            },
            passage=passage,
        )
        repeated = PROMPTS.validate_concept_payload(
            {
                "concepts": [
                    {
                        "name": "甲乙丙",
                        "aliases": [],
                        "definition": "段中重复出现的名称。",
                        "mentions": [
                            {
                                "start_codepoint": 4,
                                "end_codepoint": 7,
                                "evidence": "甲乙丙",
                                "context_before": "。",
                                "context_after": "是",
                            }
                        ],
                    }
                ]
            },
            passage=passage,
        )
        self.assertTrue(unique.valid, unique.reason)
        self.assertTrue(repeated.valid, repeated.reason)
        self.assertEqual((unique.concept_count, unique.mention_count), (1, 1))

    def test_v6_keeps_v5_decoding_limits_and_its_anchored_offset_schema(self) -> None:
        v5 = PROMPTS.get_prompt_profile("zh-glossary-v5")
        v6 = PROMPTS.get_prompt_profile("zh-glossary-v6")
        self.assertEqual(v6.profile_id, "zh-glossary-v6")
        # v6 changes the instruction, never the decoding budget: a difference in
        # max_tokens or temperature would confound the comparison against the two
        # in-flight v5 samples.
        self.assertEqual(v6.max_tokens, v5.max_tokens)
        self.assertEqual(v6.temperature, v5.temperature)
        # The flags, not the default, decide which strict schema is sent.  v6
        # has an approved sample and a full-book job whose stored requests carry
        # this schema, so it must keep being built byte for byte after v7
        # supersedes it.
        self.assertTrue(v6.uses_context_anchors)
        self.assertTrue(v6.asks_for_offsets)
        request = PROMPTS.build_concept_completion_request(
            model="remote-model-snapshot",
            profile_id="zh-glossary-v6",
            passage="甲甲",
            remote_structured_output=True,
        )
        self.assertEqual(
            request["response_format"]["json_schema"]["schema"], PROMPTS.CONCEPT_OUTPUT_SCHEMA
        )
        self.assertEqual(request["max_tokens"], 2_048)
        self.assertEqual(
            _mention_schema(request)["required"],
            ["start_codepoint", "end_codepoint", "evidence", "context_before", "context_after"],
        )

    def test_v6_adds_only_a_minimum_evidence_span_on_top_of_v5(self) -> None:
        v5 = PROMPTS.get_prompt_profile("zh-glossary-v5").system_instruction
        v6 = PROMPTS.get_prompt_profile("zh-glossary-v6").system_instruction
        self.assertNotEqual(v5, v6)
        # v6 exists to isolate one variable against the live v5 samples, so it
        # must differ by exactly two edits that both express that variable: the
        # minimum-span clause, and the shape example's offsets made consistent
        # with it.  Leaving the example at end_codepoint 1 while demanding ten
        # code points would be a self-contradictory prompt -- and a one-character
        # example is a plausible contributor to the 1-3 code-point evidence that
        # motivated this profile, so it cannot stay.  Undoing both edits must
        # reproduce v5 byte for byte; any other drift fails here.
        example_v5 = "\"start_codepoint\":0,\"end_codepoint\":1,"
        example_v6 = "\"start_codepoint\":0,\"end_codepoint\":10,"
        self.assertIn(example_v5, v5)
        self.assertIn(example_v6, v6)
        self.assertNotIn(example_v6, v5)

        reverted = v6.replace(example_v6, example_v5)
        prefix = 0
        while prefix < min(len(v5), len(reverted)) and v5[prefix] == reverted[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < min(len(v5), len(reverted)) - prefix
            and v5[len(v5) - 1 - suffix] == reverted[len(reverted) - 1 - suffix]
        ):
            suffix += 1
        self.assertEqual(v5[prefix:len(v5) - suffix], "")
        inserted = reverted[prefix:len(reverted) - suffix]
        self.assertEqual(v5, reverted.replace(inserted, "", 1))
        self.assertIn("至少 10 个 Unicode 字符", inserted)
        # A length floor alone would be satisfiable by padding, so the clause has
        # to demand a real phrase, and it has to stay possible on a passage that
        # is itself shorter than the floor (eight of the twenty sampled passages
        # are 9-10 code points long).
        self.assertIn("完整、有意义的短语或分句", inserted)
        self.assertIn("不得只给出概念本身", inserted)
        self.assertIn("本段总长不足 10 个 Unicode 字符时，evidence 取本段全文", inserted)
        # Every v5 lever survives verbatim: conditional anchors, verbatim copy,
        # the object shape, and the output-format rules.
        for clause in (
            "逐字复制",
            "不得改写、翻译或统一引号",
            "只出现一次时",
            "必须都是空字符串",
            "重复出现时",
            "最多 48 个 Unicode 字符",
            "紧邻",
            "最多抽取 6 个",
            "第一个字符必须是 {",
            "Markdown 代码块",
            "\"context_before\":\"\",\"context_after\":\"\"",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, v5)
                self.assertIn(clause, v6)

    def test_v6_longer_evidence_spans_still_pass_strict_payload_validation(self) -> None:
        passage = (
            "丁戊己在导论中被定义为一种制度安排。"
            "第一节指出甲乙丙是本章讨论的核心名称。"
            "第二节重申甲乙丙是本章讨论的核心名称。"
        )
        unique_evidence = "丁戊己在导论中被定义为一种制度安排"
        repeated_evidence = "甲乙丙是本章讨论的核心名称"
        self.assertGreaterEqual(len(unique_evidence), 10)
        self.assertGreaterEqual(len(repeated_evidence), 10)
        self.assertEqual(passage.count(unique_evidence), 1)
        self.assertEqual(passage.count(repeated_evidence), 2)
        unique_start = passage.index(unique_evidence)
        repeated_start = passage.index(repeated_evidence)
        unique = PROMPTS.validate_concept_payload(
            {
                "concepts": [
                    {
                        "name": "丁戊己",
                        "aliases": [],
                        "definition": "导论中定义的一种制度安排。",
                        "mentions": [
                            {
                                "start_codepoint": unique_start,
                                "end_codepoint": unique_start + len(unique_evidence),
                                "evidence": unique_evidence,
                                "context_before": "",
                                "context_after": "",
                            }
                        ],
                    }
                ]
            },
            passage=passage,
        )
        repeated = PROMPTS.validate_concept_payload(
            {
                "concepts": [
                    {
                        "name": "甲乙丙",
                        "aliases": [],
                        "definition": "本章讨论的核心名称。",
                        "mentions": [
                            {
                                "start_codepoint": repeated_start,
                                "end_codepoint": repeated_start + len(repeated_evidence),
                                "evidence": repeated_evidence,
                                "context_before": "第一节指出",
                                "context_after": "。",
                            }
                        ],
                    }
                ]
            },
            passage=passage,
        )
        self.assertTrue(unique.valid, unique.reason)
        self.assertTrue(repeated.valid, repeated.reason)
        self.assertEqual((unique.concept_count, unique.mention_count), (1, 1))
        self.assertEqual((repeated.concept_count, repeated.mention_count), (1, 1))
        # The longer span is exactly what makes the citation resolvable: the bare
        # concept term repeats, the phrase carrying it is uniquely anchored.
        self.assertEqual(passage.count("第一节指出" + repeated_evidence), 1)

    def test_v7_is_the_default_and_asks_the_model_for_no_offsets_at_all(self) -> None:
        v6 = PROMPTS.get_prompt_profile("zh-glossary-v6")
        v7 = PROMPTS.get_prompt_profile("zh-glossary-v7")
        self.assertEqual(PROMPTS.DEFAULT_CONCEPT_PROMPT_PROFILE, "zh-glossary-v7")
        self.assertEqual(v7.profile_id, "zh-glossary-v7")
        self.assertTrue(v7.uses_context_anchors)
        self.assertFalse(v7.asks_for_offsets)
        # The budget is a ceiling on generation, not a spend, and v7 exists to
        # isolate one variable against the approved v6 sample: keeping it equal
        # is what makes that comparison readable, and lowering it would only
        # move the worst case nearer to a truncated, unparseable response.
        self.assertEqual(v7.max_tokens, v6.max_tokens)
        self.assertEqual(v7.temperature, v6.temperature)
        request = PROMPTS.build_concept_completion_request(
            model="remote-model-snapshot",
            profile_id=PROMPTS.DEFAULT_CONCEPT_PROMPT_PROFILE,
            passage="甲甲",
            remote_structured_output=True,
        )
        schema = request["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema, PROMPTS.CONCEPT_OUTPUT_SCHEMA_WITHOUT_OFFSETS)
        mention = _mention_schema(request)
        self.assertEqual(mention["required"], ["evidence", "context_before", "context_after"])
        self.assertEqual(set(mention["properties"]), {"evidence", "context_before", "context_after"})
        self.assertFalse(mention["additionalProperties"])
        # Nothing anywhere in the contract - schema or instruction - still names
        # an offset, so a model has nothing to be wrong about.
        self.assertNotIn("codepoint", json.dumps(schema))
        self.assertNotIn("codepoint", v7.system_instruction)
        self.assertNotIn("passage[", v7.system_instruction)

    def test_v7_removes_the_offsets_from_v6_and_changes_nothing_else(self) -> None:
        v6 = PROMPTS.get_prompt_profile("zh-glossary-v6").system_instruction
        v7 = PROMPTS.get_prompt_profile("zh-glossary-v7").system_instruction
        self.assertNotEqual(v6, v7)
        # One variable, expressed as exactly two edits: the field-list clause
        # loses the two integers, and the shape example loses them with it.  An
        # example still showing "start_codepoint":0,"end_codepoint":10 beside a
        # contract that forbids offsets would be self-contradictory - the same
        # defect that was corrected when v6 was authored - so the two cannot be
        # separated.  Applying both to v6 must reproduce v7 byte for byte.
        offsets_clause_v6 = (
            "每个 mention 必须有且只能有 start_codepoint、end_codepoint、evidence、"
            "context_before、context_after 五个字段；start_codepoint 从 0 开始，"
            "end_codepoint 为排他位置，"
            "evidence 必须与 passage[start_codepoint:end_codepoint] 完全一致，包括标点和空格。"
        )
        offsets_clause_v7 = (
            "每个 mention 必须有且只能有 evidence、context_before、context_after 三个字段；"
            "不要输出任何字符位置，字符位置由程序依据 evidence 在本段中重新推导。"
            "evidence 必须与本段中的一段原文完全一致，包括标点和空格。"
        )
        example_v6 = "\"mentions\":[{\"start_codepoint\":0,\"end_codepoint\":10,\"evidence\":\"…\","
        example_v7 = "\"mentions\":[{\"evidence\":\"…\","
        self.assertIn(offsets_clause_v6, v6)
        self.assertIn(example_v6, v6)
        self.assertEqual(
            v6.replace(offsets_clause_v6, offsets_clause_v7).replace(example_v6, example_v7), v7
        )
        # The replacement re-states the source-fidelity half of what it removed
        # and hands the position back to the server, exactly as
        # zh-section-graph-v2 does.
        self.assertIn("不要输出任何字符位置", v7)
        self.assertIn("由程序依据 evidence 在本段中重新推导", v7)
        self.assertIn("包括标点和空格", v7)
        # Every v6 lever survives verbatim, so v7 is one variable and not a
        # rewrite: the minimum evidence span with its short-passage escape
        # hatch, the conditional anchors, verbatim copy, the concept cap and the
        # output-format rules.
        for clause in (
            "evidence 至少 10 个 Unicode 字符",
            "完整、有意义的短语或分句",
            "不得只给出概念本身",
            "本段总长不足 10 个 Unicode 字符时，evidence 取本段全文",
            "只出现一次时",
            "必须都是空字符串",
            "重复出现时",
            "最多 48 个 Unicode 字符",
            "紧邻",
            "逐字复制",
            "不得改写、翻译或统一引号",
            "最多抽取 6 个",
            "\"context_before\":\"\",\"context_after\":\"\"",
            "第一个字符必须是 {",
            "Markdown 代码块",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, v6)
                self.assertIn(clause, v7)

    def test_v7_payloads_without_offsets_still_pass_strict_payload_validation(self) -> None:
        # The offsets are gone from the contract, so validation has to locate
        # the citation the same way ingest does - from the literal, and from the
        # anchor when the literal repeats.
        passage = (
            "丁戊己在导论中被定义为一种制度安排。"
            "第一节指出甲乙丙是本章讨论的核心名称。"
            "第二节重申甲乙丙是本章讨论的核心名称。"
        )
        unique_evidence = "丁戊己在导论中被定义为一种制度安排"
        repeated_evidence = "甲乙丙是本章讨论的核心名称"
        self.assertEqual(passage.count(repeated_evidence), 2)

        def payload(evidence: str, before: str, after: str) -> dict:
            return {
                "concepts": [
                    {
                        "name": "甲乙丙",
                        "aliases": [],
                        "definition": "本章讨论的核心名称。",
                        "mentions": [
                            {
                                "evidence": evidence,
                                "context_before": before,
                                "context_after": after,
                            }
                        ],
                    }
                ]
            }

        unique = PROMPTS.validate_concept_payload(
            payload(unique_evidence, "", ""), passage=passage
        )
        anchored = PROMPTS.validate_concept_payload(
            payload(repeated_evidence, "第一节指出", "。"), passage=passage
        )
        unanchored = PROMPTS.validate_concept_payload(
            payload(repeated_evidence, "", ""), passage=passage
        )
        absent = PROMPTS.validate_concept_payload(payload("从未出现的原文", "", ""), passage=passage)
        self.assertTrue(unique.valid, unique.reason)
        self.assertTrue(anchored.valid, anchored.reason)
        self.assertEqual((anchored.concept_count, anchored.mention_count), (1, 1))
        # Repeated evidence with both anchors empty selects nothing, and a
        # literal that is not in the passage is not a citation at all.
        self.assertFalse(unanchored.valid)
        self.assertFalse(absent.valid)

    def test_stratified_selection_covers_chapters_instead_of_taking_first_rows(self) -> None:
        passages = [
            {"passage_id": "a1", "ordinal": 1, "toc_path": ["A"]},
            {"passage_id": "a2", "ordinal": 2, "toc_path": ["A"]},
            {"passage_id": "b1", "ordinal": 3, "toc_path": ["B"]},
            {"passage_id": "b2", "ordinal": 4, "toc_path": ["B"]},
            {"passage_id": "c1", "ordinal": 5, "toc_path": ["C"]},
            {"passage_id": "c2", "ordinal": 6, "toc_path": ["C"]},
        ]
        selected = PROMPTS.select_stratified_passages(passages, limit=3)
        self.assertEqual({tuple(value["toc_path"]) for value in selected}, {("A",), ("B",), ("C",)})
        self.assertEqual([value["passage_id"] for value in selected], ["a2", "b2", "c2"])

    def test_validation_requires_exact_unicode_codepoint_evidence(self) -> None:
        passage = "甲。乙"
        valid = PROMPTS.validate_concept_payload(
            {
                "concepts": [
                    {
                        "name": "乙",
                        "aliases": [],
                        "definition": "段中出现的名称。",
                        "mentions": [{"start_codepoint": 2, "end_codepoint": 3, "evidence": "乙"}],
                    }
                ]
            },
            passage=passage,
        )
        invalid = PROMPTS.validate_concept_payload(
            {
                "concepts": [
                    {
                        "name": "乙",
                        "aliases": [],
                        "definition": "段中出现的名称。",
                        "mentions": [{"start_codepoint": 1, "end_codepoint": 2, "evidence": "乙"}],
                    }
                ]
            },
            passage=passage,
        )
        self.assertTrue(valid.valid)
        self.assertEqual((valid.concept_count, valid.mention_count), (1, 1))
        self.assertFalse(invalid.valid)
        self.assertIn("offset", invalid.reason or "")

    def test_local_offset_normalization_accepts_only_unique_evidence(self) -> None:
        payload = {
            "concepts": [
                {
                    "name": "乙",
                    "aliases": [],
                    "definition": "段中出现的名称。",
                    "mentions": [{"start_codepoint": 0, "end_codepoint": 1, "evidence": "乙"}],
                },
                {
                    "name": "甲",
                    "aliases": [],
                    "definition": "段中出现的名称。",
                    "mentions": [{"start_codepoint": 0, "end_codepoint": 1, "evidence": "甲"}],
                },
            ]
        }
        normalized = PROMPTS.normalize_local_payload_offsets(payload, passage="甲乙甲")
        mentions = normalized["concepts"][0]["mentions"]
        self.assertEqual(mentions[0]["start_codepoint"], 1)
        self.assertEqual(mentions[0]["end_codepoint"], 2)
        ambiguous = normalized["concepts"][1]["mentions"]
        self.assertEqual(ambiguous[0]["start_codepoint"], 0)


if __name__ == "__main__":
    unittest.main()
