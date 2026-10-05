import ast
import asyncio
import json
import logging
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Literal, Optional
from unittest.mock import AsyncMock

from pydantic import BaseModel, ValidationError


BACKEND = Path(__file__).resolve().parents[1]


def load_settings_functions(data_dir):
    # Exercise the production file persistence without loading model or database services.
    source = BACKEND / 'open_webui/utils/glossary_translation.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    functions = {
        '_data_dir',
        'make_relative_glossary_path',
        '_safe_glossary_id',
        'normalize_term',
        '_normalize_bool_setting',
        'normalize_settings',
        'active_glossary',
        'read_settings',
        'write_settings',
    }
    personal = {
        'id': 'user',
        'name': 'User',
        'path': 'glossaries/user.json',
        'version': '1',
        'source_lang': 'Chinese',
        'glossary_lang': 'English',
        'target_lang': 'English',
    }
    namespace = {
        'Any': Any,
        'Optional': Optional,
        'Path': Path,
        'asyncio': asyncio,
        'json': json,
        'os': os,
        're': re,
        'tempfile': tempfile,
        'log': logging.getLogger(__name__),
        'DATA_DIR': data_dir,
        'SETTINGS_PATH': data_dir / 'glossary.settings.json',
        'DEFAULT_GLOSSARY_ID': 'user',
        'DEFAULT_GLOSSARY_PATH': 'glossaries/user.json',
        'DEFAULT_GLOSSARY_RELATIVE_PATH': 'glossaries/user.json',
        '_official_glossary_items': lambda: [],
        '_personal_glossary_item': lambda: dict(personal),
        '_official_glossary_config': lambda *args: None,
        '_available_official_glossary_configs': lambda: [],
        'is_official_glossary_path': lambda *args: False,
        'invalidate_cache': AsyncMock(),
    }
    nodes = [
        node
        for node in tree.body
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions)
        or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == 'DEFAULT_SETTINGS'
        )
    ]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


def load_settings_form():
    source = BACKEND / 'open_webui/routers/glossary.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'GlossarySettings')
    namespace = {'BaseModel': BaseModel, 'Any': Any, 'Optional': Optional, 'Literal': Literal}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['GlossarySettings']


class GlossaryRuntimeSettingsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = load_settings_functions(Path(self.directory.name))
        self.form = load_settings_form()

    async def test_missing_setting_defaults_to_q8(self):
        settings = await self.settings['read_settings']()
        self.assertEqual(settings['kv_cache_type'], 'q8_0')

    async def test_api_values_persist_and_unrelated_updates_preserve_precision(self):
        for precision in ('q8_0', 'q4_0', 'f16'):
            with self.subTest(precision=precision):
                form = self.form(kv_cache_type=precision, token_limit=32768)
                await self.settings['write_settings'](form.model_dump(exclude_unset=True))
                await self.settings['write_settings']({'max_terms_injected': 25})
                settings = await self.settings['read_settings']()
                self.assertEqual(settings['kv_cache_type'], precision)
                self.assertEqual(settings['token_limit'], 32768)
                self.assertEqual(settings['max_terms_injected'], 25)

    async def test_desktop_file_update_is_visible_on_next_api_read(self):
        await self.settings['write_settings']({'kv_cache_type': 'q8_0'})
        path = self.settings['SETTINGS_PATH']
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['kv_cache_type'] = 'f16'
        path.write_text(json.dumps(saved), encoding='utf-8')
        self.assertEqual((await self.settings['read_settings']())['kv_cache_type'], 'f16')

    async def test_invalid_file_value_falls_back_to_q8(self):
        self.settings['SETTINGS_PATH'].write_text('{"kv_cache_type": "bad-value"}', encoding='utf-8')
        self.assertEqual((await self.settings['read_settings']())['kv_cache_type'], 'q8_0')

    def test_api_rejects_unsupported_precision(self):
        for value in ('q2', 'Q8', '', 8, {'type': 'q4_0'}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.form(kv_cache_type=value)


if __name__ == '__main__':
    unittest.main()
