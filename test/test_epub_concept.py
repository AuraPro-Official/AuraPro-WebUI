import os
import sys
import unittest

# Directly add backend/open_webui path to sys.path to bypass open_webui.__init__ CLI dependencies for standalone tests
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../backend"))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import json
from open_webui.utils.concept_wiki import ConceptWiki
from open_webui.utils.batch_pipeline import BatchPipeline

class TestEPUBConceptPipeline(unittest.TestCase):

    def setUp(self):
        self.wiki = ConceptWiki()

    def test_seed_vocabulary_and_matching(self):
        seed_data = [
            {
                "term": "残差网络",
                "aliases": ["ResNet", "Residual Network"],
                "definition": "通过跳跃连接解决深度网络梯度消失问题的神经网络架构。"
            },
            {
                "term": "拥塞控制",
                "aliases": ["Congestion Control", "TCP 拥塞控制"],
                "definition": "防止过多的数据注入到网络中，使网络中的路由器或链路不致过载。"
            }
        ]

        count = self.wiki.load_seed_vocabulary(seed_data)
        self.assertEqual(count, 2)
        self.assertEqual(len(self.wiki.concepts), 2)

        # Test Tier 1 matching
        text = "在深度学习中，残差网络 (ResNet) 能有效解决退化问题。"
        matches = self.wiki.find_concepts_in_text(text)
        self.assertTrue(len(matches) >= 1)
        self.assertEqual(matches[0]["canonical_name"], "残差网络")

    def test_batch_jsonl_generation(self):
        passages = [
            {
                "passage_id": "BookA_P00001",
                "book_title": "测试图书",
                "toc_path": ["第1章 概述", "1.1 基本定义"],
                "content": "残差网络通过引入跳跃连接 (Shortcut Connection) 避免梯度消失。"
            }
        ]

        output_path = "/tmp/test_batch_output.jsonl"
        res_path = BatchPipeline.create_openai_batch_jsonl(passages, output_path, model="gpt-4o-mini")

        self.assertTrue(os.path.exists(res_path))
        with open(res_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            self.assertEqual(len(lines), 1)
            item = json.loads(lines[0])
            self.assertEqual(item["custom_id"], "BookA_P00001")

        if os.path.exists(output_path):
            os.remove(output_path)


if __name__ == "__main__":
    unittest.main()
