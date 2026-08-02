"""Unit coverage for local-first concept prompt calibration primitives."""

from __future__ import annotations

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
