import ast
import unittest
from pathlib import Path
from typing import Any, Optional


BACKEND = Path(__file__).resolve().parents[1]
SOURCE = BACKEND / 'open_webui/utils/glossary_translation.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))


def load_cap():
    functions = {'_thinking_requested', '_cap_translation_output'}
    constants = {
        'TRANSLATION_OUTPUT_BASE_TOKENS',
        'TRANSLATION_OUTPUT_TOKENS_PER_CHAR',
        '_OUTPUT_CAP_PROVIDERS',
        '_THINKING_OFF_EFFORTS',
    }
    nodes = [
        node
        for node in TREE.body
        if (isinstance(node, ast.FunctionDef) and node.name in functions)
        or (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)
        )
    ]
    namespace = {'Any': Any, 'Optional': Optional}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace['_cap_translation_output']


LOCAL = {'id': 'lowest_V2', 'provider': 'llama.cpp', 'connection_type': 'local'}


class TranslationOutputCapTest(unittest.TestCase):
    def setUp(self):
        self.cap = load_cap()

    def capped(self, form_data, text='微软访问非人非如果如果', model=LOCAL):
        self.cap(form_data, text, model)
        return form_data.get('max_tokens')

    def test_local_models_get_a_cap_scaled_to_the_source(self):
        self.assertEqual(self.capped({}), 512 + 6 * 11)
        self.assertEqual(self.capped({}, 'x' * 1000), 512 + 6000)
        self.assertEqual(self.capped({}, model={'provider': 'strata'}), 512 + 6 * 11)

    def test_remote_or_unknown_models_are_left_alone(self):
        self.assertIsNone(self.capped({}, model={'provider': 'openai', 'connection_type': 'external'}))
        self.assertIsNone(self.capped({}, model={'id': 'custom'}))
        self.assertIsNone(self.capped({}, model=None))

    def test_existing_limits_and_thinking_are_respected(self):
        self.assertEqual(self.capped({'max_tokens': 99}), 99)
        self.assertIsNone(self.capped({'max_completion_tokens': 99}))
        self.assertIsNone(self.capped({'options': {'num_predict': 99}}))
        self.assertIsNone(self.capped({'reasoning_effort': 'medium'}))
        self.assertIsNone(self.capped({'chat_template_kwargs': {'enable_thinking': True}}))
        self.assertEqual(self.capped({'reasoning_effort': 'none'}), 578)
        self.assertEqual(
            self.capped({'reasoning_effort': 'high', 'chat_template_kwargs': {'enable_thinking': False}}), 578
        )

    def test_empty_source_is_not_capped(self):
        self.assertIsNone(self.capped({}, '   '))

    def test_only_translation_and_interpretation_modes_apply_the_cap(self):
        calls = {
            node.name: any(
                isinstance(call, ast.Call) and getattr(call.func, 'id', None) == '_cap_translation_output'
                for call in ast.walk(node)
            )
            for node in TREE.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        self.assertTrue(calls['apply_translation_mode'])
        self.assertTrue(calls['apply_interpretation_mode'])
        self.assertFalse(calls['apply_learning_mode'])
        self.assertFalse(calls['apply_manuscript_translation_mode'])

        middleware = (BACKEND / 'open_webui/utils/middleware.py').read_text(encoding='utf-8')
        self.assertIn(
            'apply_translation_mode(form_data=form_data, settings=glossary_settings, model=model)', middleware
        )
        self.assertIn('apply_interpretation_mode(form_data, glossary_settings, model=model)', middleware)


if __name__ == '__main__':
    unittest.main()
