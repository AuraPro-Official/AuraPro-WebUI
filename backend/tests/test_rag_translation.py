import ast
import logging
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock


SOURCE = Path(__file__).resolve().parents[1] / 'open_webui/utils/glossary_translation.py'


def load_translation_paths():
    # Exercise production branches without loading model services or the application database.
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    names = {
        '_build_bilingual_block',
        'build_rag_translation_prompt',
        'apply_rag_translation_mode',
        'apply_manuscript_translation_mode',
        '_latest_user_text_ref',
        '_set_message_text',
        '_message_has_audio',
    }
    nodes = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    reader = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'BilingualKnowledgeReader'
    )
    reader.body = [
        node for node in reader.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'find_matches'
    ]
    nodes.append(reader)

    @asynccontextmanager
    async def database():
        yield object()

    namespace = {
        'Any': Any,
        'Optional': Optional,
        'log': logging.getLogger(__name__),
        'normalize_settings': lambda settings: settings,
        'read_settings': AsyncMock(return_value={'source': 'Chinese', 'target': 'English'}),
        'read_entries': AsyncMock(return_value=({}, None)),
        '_language_key': lambda language: language.casefold(),
        '_glossary_language_pair': lambda settings: (settings['source'], settings['target']),
        '_smart_language_detect': Mock(return_value=('Chinese', 'English')),
        '_build_translation_text_prompt': Mock(
            side_effect=lambda text, target, block: f'Translate into {target}: {text}'
        ),
        'build_manuscript_translation_prompt': Mock(return_value='manuscript prompt'),
        '_truncate_messages': lambda messages, settings: messages,
        'bilingual_matcher': SimpleNamespace(find_matches=AsyncMock(return_value=([], [], False))),
        'get_async_db': database,
        'Knowledges': SimpleNamespace(get_knowledge_bases=AsyncMock(return_value=[])),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


class RagTranslationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.paths = load_translation_paths()
        self.settings = {'source': 'Chinese', 'target': 'English'}

    async def test_empty_lookup_still_builds_translation_prompt(self):
        prompt, sources = await self.paths['build_rag_translation_prompt'](None, '正文', self.settings, None)
        self.assertEqual(prompt, 'Translate into English: 正文')
        self.assertEqual(sources, [])

    async def test_unconfigured_language_pair_skips_lookup(self):
        result = await self.paths['_build_bilingual_block'](None, 'bonjour', self.settings, 'French', 'English', None)
        self.assertEqual(result, ('', [], False))
        self.paths['bilingual_matcher'].find_matches.assert_not_awaited()

    async def test_empty_sentence_match_is_not_reported_as_a_reference(self):
        self.paths['bilingual_matcher'].find_matches.return_value = ([], [], True)
        self.assertEqual(
            await self.paths['_build_bilingual_block'](None, '正文', self.settings, 'Chinese', 'English', None),
            ('', [], False),
        )

    async def test_word_and_sentence_matches_keep_references_in_both_directions(self):
        self.paths['_strict_target_language_rule'] = lambda source, target: 'target language rule'
        self.paths['_translation_name_rules_for_target'] = lambda target: 'name rule'
        for matched in (False, True):
            for source, target in (('Chinese', 'English'), ('English', 'Chinese')):
                with self.subTest(matched=matched, source=source):
                    references = [{'source': {'id': 'bilingual-knowledge'}}]
                    self.paths['bilingual_matcher'].find_matches.return_value = (
                        [('正文', 'body')],
                        references,
                        matched,
                    )
                    self.paths['_smart_language_detect'].return_value = (source, target)
                    block, sources, is_matched = await self.paths['_build_bilingual_block'](
                        None, '正文', self.settings, source, target, None
                    )
                    self.assertIn('正文 -> body', block)
                    self.assertEqual((sources, is_matched), (references, matched))
                    prompt, sources = await self.paths['build_rag_translation_prompt'](
                        None, '正文', self.settings, None
                    )
                    self.assertTrue(prompt)
                    self.assertEqual(sources, references)

    async def test_rag_request_with_no_references_keeps_attachment(self):
        attachment = {'id': 'document-id', 'type': 'file'}
        form = {
            'files': [attachment],
            'messages': [{'role': 'user', 'content': 'Please translate this entire document into English.'}],
        }
        result, sources = await self.paths['apply_rag_translation_mode'](None, form, None, self.settings)
        self.assertEqual(sources, [])
        self.assertEqual(result['files'], [attachment])
        self.assertIn('Translate into English:', result['messages'][-1]['content'])

    async def test_rag_request_without_text_has_empty_sources(self):
        for messages in ([], [{'role': 'user', 'content': '   '}], [{'role': 'user', 'content': []}]):
            with self.subTest(messages=messages):
                form = {'messages': messages, 'files': [{'id': 'document-id'}]}
                result, sources = await self.paths['apply_rag_translation_mode'](None, form, None, self.settings)
                self.assertEqual((result, sources), (form, []))
        self.paths['bilingual_matcher'].find_matches.assert_not_awaited()

    async def test_manuscript_mode_does_not_require_bilingual_lookup(self):
        form = {'messages': [{'role': 'user', 'content': '正文'}], 'files': [{'id': 'document-id'}]}
        result = await self.paths['apply_manuscript_translation_mode'](form, self.settings)
        self.assertEqual(result['messages'][-1]['content'], 'manuscript prompt')
        self.assertEqual(result['files'], [{'id': 'document-id'}])
        self.paths['bilingual_matcher'].find_matches.assert_not_awaited()

    async def test_missing_bilingual_library_returns_three_values(self):
        reader = self.paths['BilingualKnowledgeReader']()
        hits, sources, matched = await reader.find_matches(None, '正文', 'Chinese', 'English', None)
        self.assertEqual((hits, sources, matched), ([], [], False))

    async def test_database_error_preserves_return_contract_and_logs_failure(self):
        self.paths['Knowledges'].get_knowledge_bases.side_effect = OSError('database unavailable')
        reader = self.paths['BilingualKnowledgeReader']()
        with self.assertLogs(__name__, level='WARNING'):
            result = await reader.find_matches(None, '正文', 'Chinese', 'English', None)
        self.assertEqual(result, ([], [], False))

    async def test_lookup_failure_falls_back_through_the_complete_rag_translation_path(self):
        self.paths['Knowledges'].get_knowledge_bases.side_effect = OSError('database unavailable')
        self.paths['bilingual_matcher'] = self.paths['BilingualKnowledgeReader']()
        form = {'messages': [{'role': 'user', 'content': '正文'}], 'files': [{'id': 'document-id'}]}
        with self.assertLogs(__name__, level='WARNING'):
            result, sources = await self.paths['apply_rag_translation_mode'](None, form, None, self.settings)
        self.assertEqual(result['messages'][-1]['content'], 'Translate into English: 正文')
        self.assertEqual(result['files'], [{'id': 'document-id'}])
        self.assertEqual(sources, [])


if __name__ == '__main__':
    unittest.main()
