import ast
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from typing import Optional

import langcodes


BACKEND = Path(__file__).resolve().parents[1]
ROUTING_PATH = BACKEND / 'open_webui/utils/glossary_routing.py'
SPEC = importlib.util.spec_from_file_location('smart_detect_test_routing', ROUTING_PATH)
ROUTING = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ROUTING
SPEC.loader.exec_module(ROUTING)


def load_detection(detected_code: str):
    """Load the direction-detection functions with fast_langdetect stubbed to a fixed answer."""
    source = BACKEND / 'open_webui/utils/glossary_translation.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    functions = {
        '_is_chinese_language',
        '_lang_to_code',
        '_find_explicit_command',
        '_char_script',
        '_script_pair',
        '_smart_language_detect',
    }
    constants = {'_SCRIPT_RANGES', '_LANGUAGE_SCRIPTS'}
    nodes = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in functions)
        or (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)
        )
    ]
    sys.modules['fast_langdetect'] = types.SimpleNamespace(detect_language=lambda text: detected_code)
    namespace = {
        're': re,
        'Optional': Optional,
        'langcodes': langcodes,
        '_language_key': ROUTING.language_key,
        'DEFAULT_SETTINGS': {'source_lang': '中文', 'target_lang': 'English'},
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['_smart_language_detect']


class SmartLanguageDetectTest(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop('fast_langdetect', None)

    def test_misdetected_short_chinese_still_translates_out_of_chinese(self):
        # Real fast_langdetect results for these inputs; the old fallback translated them into Chinese.
        for text, code in (
            ('你好呀', 'yue'),
            ('你好~', 'yue'),
            ('嗨', 'en'),
            ('嗯嗯', 'en'),
            ('谢啦', 'wuu'),
            ('你好😀', 'en'),
        ):
            detect = load_detection(code)
            self.assertEqual(detect(text, '中文', '西班牙语'), ('中文', '西班牙语'), text)
            self.assertEqual(detect(text, '西班牙语', '中文'), ('中文', '西班牙语'), text)
            self.assertEqual(detect(text, '中文', '韩语'), ('中文', '韩语'), text)

    def test_pairs_with_different_scripts_follow_the_script(self):
        detect = load_detection('sv')  # e.g. "Bok" -> sv; the old fallback flipped the pair
        self.assertEqual(detect('OK', '英语', '印地语'), ('英语', '印地语'))
        self.assertEqual(detect('नमस्ते', '英语', '印地语'), ('印地语', '英语'))
        self.assertEqual(detect('Да', '俄语', '格鲁吉亚语'), ('俄语', '格鲁吉亚语'))
        self.assertEqual(detect('გამარჯობა', '俄语', '格鲁吉亚语'), ('格鲁吉亚语', '俄语'))
        self.assertEqual(detect('안녕하세요', '中文', '韩语'), ('韩语', '中文'))
        self.assertEqual(detect('今日は天気がいいですね', '中文', '日语'), ('日语', '中文'))

    def test_latin_text_keeps_detector_direction(self):
        detect = load_detection('es')
        self.assertEqual(detect('Hola', '中文', '西班牙语'), ('西班牙语', '中文'))
        self.assertEqual(detect('María visitó 北京 ayer con su familia', '中文', '西班牙语'), ('西班牙语', '中文'))

    def test_shared_scripts_and_explicit_commands_use_the_old_rules(self):
        detect = load_detection('yue')
        self.assertEqual(detect('你好呀', '中文', '粤语'), ('粤语', '中文'))
        self.assertEqual(detect('你好呀', '中文', '日语'), ('日语', '中文'))
        self.assertEqual(detect('请翻译成中文：你好', '中文', '西班牙语'), ('西班牙语', '中文'))
        self.assertEqual(load_detection('tl')('Salamat', '英语', '他加禄语'), ('他加禄语', '英语'))


if __name__ == '__main__':
    unittest.main()
