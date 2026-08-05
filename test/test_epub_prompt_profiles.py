"""Unit coverage for local-first concept prompt calibration primitives."""

from __future__ import annotations

import hashlib
import importlib.util
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
        mention = remote["response_format"]["json_schema"]["schema"]["properties"]["concepts"]["items"]["properties"]["mentions"]["items"]
        self.assertEqual(
            mention["required"],
            ["start_codepoint", "end_codepoint", "evidence", "context_before", "context_after"],
        )
        self.assertEqual(
            mention["properties"]["context_before"]["maxLength"],
            PROMPTS.MAX_EVIDENCE_CONTEXT_ANCHOR_CODEPOINTS,
        )

    def test_superseded_profiles_are_frozen_and_v5_is_the_registered_default(self) -> None:
        registered = PROMPTS.available_prompt_profiles()
        self.assertEqual(PROMPTS.DEFAULT_CONCEPT_PROMPT_PROFILE, "zh-glossary-v5")
        self.assertIn("zh-glossary-v5", registered)
        for profile_id, digest in _PUBLISHED_PROFILE_DIGESTS.items():
            with self.subTest(profile=profile_id):
                self.assertIn(profile_id, registered)
                self.assertEqual(_profile_digest(PROMPTS.get_prompt_profile(profile_id)), digest)
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
