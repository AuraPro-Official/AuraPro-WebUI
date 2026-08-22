from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _load_module():
    aiohttp = types.ModuleType('aiohttp')
    aiohttp.BasicAuth = object
    sys.modules.setdefault('aiohttp', aiohttp)

    open_webui = types.ModuleType('open_webui')
    open_webui.__path__ = []
    models = types.ModuleType('open_webui.models')
    chats = types.ModuleType('open_webui.models.chats')
    chats.Chats = object
    socket = types.ModuleType('open_webui.socket')
    socket_main = types.ModuleType('open_webui.socket.main')
    socket_main.get_event_call = lambda *_args, **_kwargs: None
    socket_main.get_event_emitter = lambda *_args, **_kwargs: None
    sys.modules.setdefault('open_webui', open_webui)
    sys.modules.setdefault('open_webui.models', models)
    sys.modules.setdefault('open_webui.models.chats', chats)
    sys.modules.setdefault('open_webui.socket', socket)
    sys.modules.setdefault('open_webui.socket.main', socket_main)

    module_path = Path(__file__).parents[1] / 'open_webui' / 'services' / 'opencode_agent.py'
    spec = importlib.util.spec_from_file_location('opencode_agent_under_test', module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Unable to load OpenCode agent module for tests.')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


opencode_agent = _load_module()
OpenCodeError = opencode_agent.OpenCodeError
_assistant_snapshot = opencode_agent._assistant_snapshot
_normalize_capabilities = opencode_agent._normalize_capabilities
_normalize_todos = opencode_agent._normalize_todos
_split_model = opencode_agent._split_model
_message_text = opencode_agent._message_text
_normalize_runtime_url = opencode_agent._normalize_runtime_url
_tool_description = opencode_agent._tool_description


class OpenCodeAgentHelpersTest(unittest.TestCase):
    def test_runtime_url_accepts_only_loopback_http_services(self):
        self.assertEqual(_normalize_runtime_url('http://127.0.0.1:4096/'), 'http://127.0.0.1:4096')
        self.assertEqual(_normalize_runtime_url('https://localhost:4096'), 'https://localhost:4096')
        self.assertEqual(_normalize_runtime_url('http://[::1]:4096'), 'http://[::1]:4096')

        rejected = (
            'https://example.com',
            'http://127.0.0.1:4096/api',
            'http://user:pass@127.0.0.1:4096',
            'file:///tmp/opencode',
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(OpenCodeError):
                _normalize_runtime_url(value)

    def test_message_text_supports_openai_content_parts(self):
        self.assertEqual(_message_text('hello'), 'hello')
        self.assertEqual(
            _message_text(
                [
                    {'type': 'text', 'text': 'first'},
                    {'type': 'image_url', 'image_url': {'url': 'ignored'}},
                    {'type': 'input_text', 'content': 'second'},
                ]
            ),
            'first\nsecond',
        )

    def test_assistant_snapshot_ignores_existing_messages(self):
        messages = [
            {
                'info': {'id': 'old', 'role': 'assistant'},
                'parts': [{'type': 'text', 'text': 'old answer'}],
            },
            {
                'info': {'id': 'new', 'role': 'assistant'},
                'parts': [
                    {'type': 'text', 'text': 'new '},
                    {'type': 'tool', 'tool': 'edit', 'state': {'status': 'running'}},
                    {'type': 'text', 'text': 'answer'},
                ],
            },
        ]

        text, tools, message_id = _assistant_snapshot(messages, {'old'})

        self.assertEqual(text, 'new answer')
        self.assertEqual(message_id, 'new')
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['tool'], 'edit')

    def test_tool_description_reports_progress_and_completion(self):
        description, done = _tool_description(
            {
                'tool': 'bash',
                'state': {'status': 'running', 'input': {'command': 'npm test'}},
            }
        )
        self.assertEqual(description, 'OpenCode · bash: npm test')
        self.assertFalse(done)

        description, done = _tool_description(
            {
                'tool': 'edit',
                'state': {'status': 'error', 'error': 'permission denied'},
            }
        )
        self.assertEqual(description, 'OpenCode · edit (permission denied)')
        self.assertTrue(done)

    def test_model_reference_requires_safe_provider_and_model_ids(self):
        self.assertEqual(_split_model('openai/gpt-5.1'), ('openai', 'gpt-5.1'))
        self.assertIsNone(_split_model('gpt-5.1'))
        self.assertIsNone(_split_model('openai/model with spaces'))
        self.assertIsNone(_split_model('../openai/model'))

    def test_capabilities_include_only_connected_models_and_primary_agents(self):
        result = _normalize_capabilities(
            {
                'all': [
                    {
                        'id': 'openai',
                        'name': 'OpenAI',
                        'models': {'gpt-5': {'id': 'gpt-5', 'name': 'GPT-5'}},
                    },
                    {
                        'id': 'offline',
                        'name': 'Offline',
                        'models': {'ignored': {'id': 'ignored'}},
                    },
                ],
                'connected': ['openai'],
                'default': {'openai': 'gpt-5'},
            },
            [
                {'name': 'build', 'mode': 'primary'},
                {'name': 'hidden', 'mode': 'primary', 'hidden': True},
                {'name': 'research', 'mode': 'subagent'},
            ],
        )

        self.assertEqual(result['default_model'], 'openai/gpt-5')
        self.assertEqual([model['id'] for model in result['models']], ['openai/gpt-5'])
        self.assertTrue(result['models'][0]['default'])
        self.assertEqual([agent['id'] for agent in result['agents']], ['build'])

    def test_capabilities_fall_back_to_standard_agents(self):
        result = _normalize_capabilities({}, None)
        self.assertEqual([agent['id'] for agent in result['agents']], ['build', 'plan'])

    def test_todos_are_normalized_for_the_workspace_panel(self):
        self.assertEqual(
            _normalize_todos(
                [
                    {'id': '1', 'content': 'Inspect files', 'status': 'completed'},
                    {'title': 'Run tests', 'priority': 'high'},
                    'ignored',
                ]
            ),
            [
                {
                    'id': '1',
                    'content': 'Inspect files',
                    'status': 'completed',
                    'priority': '',
                },
                {
                    'id': '',
                    'content': 'Run tests',
                    'status': 'pending',
                    'priority': 'high',
                },
            ],
        )


if __name__ == '__main__':
    unittest.main()
