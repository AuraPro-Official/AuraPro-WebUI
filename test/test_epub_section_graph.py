"""Unit coverage for deterministic TOC-scoped graph extraction packets."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


def _load_module():
    path = Path(__file__).parents[1] / 'backend/open_webui/retrieval/epub/section_graph.py'
    spec = importlib.util.spec_from_file_location('epub_section_graph_test', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GRAPH = _load_module()


class SectionGraphPacketTest(unittest.TestCase):
    def test_packets_keep_top_level_toc_boundaries_and_have_real_anchors(self) -> None:
        passages = [
            {'passage_id': 'a1', 'ordinal': 1, 'toc_path': ['A'], 'content': '甲' * 4},
            {'passage_id': 'a2', 'ordinal': 2, 'toc_path': ['A', 'A.1'], 'content': '乙' * 4},
            {'passage_id': 'b1', 'ordinal': 3, 'toc_path': ['B'], 'content': '丙' * 4},
        ]
        packets = GRAPH.build_section_graph_packets(passages, max_characters=6)

        self.assertEqual(
            [(packet.toc_path, packet.anchor_passage_id) for packet in packets],
            [(('A',), 'a1'), (('A',), 'a2'), (('B',), 'b1')],
        )
        self.assertEqual([packet.packet_id for packet in packets], ['A:0:a1', 'A:1:a2', 'B:0:b1'])

    def test_request_uses_strict_schema_with_packet_local_relation_endpoints(self) -> None:
        packet = GRAPH.build_section_graph_packets(
            [{'passage_id': 'a1', 'ordinal': 1, 'toc_path': ['A'], 'content': '原文'}]
        )[0]
        request = GRAPH.build_section_graph_completion_request(model='model-snapshot', packet=packet)
        relation = request['response_format']['json_schema']['schema']['properties']['relations']['items']

        self.assertEqual(request['response_format']['type'], 'json_schema')
        self.assertTrue(request['response_format']['json_schema']['strict'])
        self.assertEqual(relation['required'], ['subject_local_id', 'predicate', 'object_local_id', 'evidence'])
        self.assertEqual(request['messages'][1]['role'], 'user')


if __name__ == '__main__':
    unittest.main()
