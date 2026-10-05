import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'open_webui' / 'utils' / 'glossary_selection.py'
SPEC = importlib.util.spec_from_file_location('glossary_selection', MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
apply_conversation_glossary = MODULE.apply_conversation_glossary


class ConversationGlossarySelectionTest(unittest.TestCase):
    def setUp(self):
        self.settings = {
            'glossary_mode': 'smart',
            'active_glossary_id': 'zh-es',
            'smart_source_lang': 'Chinese',
            'smart_target_lang': 'Spanish',
            'source_lang': 'Chinese',
            'glossary_lang': 'Spanish',
            'target_lang': 'Spanish',
            'glossary_path': 'glossary_es.json',
            'glossaries': [
                {
                    'id': 'zh-es',
                    'name': 'Chinese-Spanish',
                    'path': 'glossary_es.json',
                    'version': '1.0.1',
                    'source_lang': 'Chinese',
                    'glossary_lang': 'Spanish',
                    'target_lang': 'Spanish',
                },
                {
                    'id': 'en-fr',
                    'name': 'English-French',
                    'path': 'glossary_fr_en.json',
                    'version': '2.0.0',
                    'source_lang': 'English',
                    'glossary_lang': 'French',
                    'target_lang': 'French',
                },
            ],
        }

    def test_smart_selection_overrides_only_language_pair(self):
        result = apply_conversation_glossary(
            self.settings,
            {
                'mode': 'smart',
                'source_lang': 'Spanish',
                'target_lang': 'French',
            },
        )

        self.assertEqual(result['glossary_mode'], 'smart')
        self.assertEqual(result['smart_source_lang'], 'Spanish')
        self.assertEqual(result['smart_target_lang'], 'French')
        self.assertEqual(result['active_glossary_id'], 'zh-es')

    def test_fixed_selection_uses_only_known_glossary_metadata(self):
        result = apply_conversation_glossary(
            self.settings,
            {'mode': 'fixed', 'glossary_id': 'en-fr', 'path': '../unsafe.json'},
        )

        self.assertEqual(result['glossary_mode'], 'fixed')
        self.assertEqual(result['active_glossary_id'], 'en-fr')
        self.assertEqual(result['glossary_path'], 'glossary_fr_en.json')
        self.assertEqual(result['source_lang'], 'English')
        self.assertEqual(result['target_lang'], 'French')

    def test_unknown_fixed_glossary_falls_back_to_global_settings(self):
        result = apply_conversation_glossary(self.settings, {'mode': 'fixed', 'glossary_id': 'missing'})

        self.assertEqual(result, self.settings)

    def test_selection_does_not_mutate_global_settings(self):
        result = apply_conversation_glossary(
            self.settings,
            {'mode': 'smart', 'source_lang': 'English', 'target_lang': 'French'},
        )

        result['glossaries'][0]['name'] = 'Changed'
        self.assertEqual(self.settings['glossaries'][0]['name'], 'Chinese-Spanish')


if __name__ == '__main__':
    unittest.main()
