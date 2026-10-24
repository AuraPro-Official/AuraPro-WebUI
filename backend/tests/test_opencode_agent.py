from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
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
_agent_changed_files = opencode_agent._agent_changed_files
_normalize_capabilities = opencode_agent._normalize_capabilities
_normalize_todos = opencode_agent._normalize_todos
_resolve_user_message_id = opencode_agent._resolve_user_message_id
_normalize_session_diffs = opencode_agent._normalize_session_diffs
_select_changed_files = opencode_agent._select_changed_files
_split_model = opencode_agent._split_model
_message_text = opencode_agent._message_text
_normalize_runtime_url = opencode_agent._normalize_runtime_url
_progress_status = opencode_agent._progress_status
_tool_description = opencode_agent._tool_description


class OpenCodeAgentHelpersTest(unittest.TestCase):
    def test_agent_translations_keep_legacy_keys_with_unified_display_name(self):
        locales = Path(__file__).parents[2] / 'src/lib/i18n/locales'
        for locale in ('en-US', 'es-ES', 'zh-CN', 'zh-TW'):
            with self.subTest(locale=locale):
                translations = json.loads((locales / locale / 'translation.json').read_text(encoding='utf-8'))
                for key, value in translations.items():
                    if 'OpenCode' in key or 'PI task' in key:
                        self.assertIn('Agent', value, key)
                        self.assertNotRegex(value, r'\b(?:OpenCode|PI|Pi agent)\b')


class AgentSessionControlsTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_session_preserves_conversation_permissions_and_compaction_preferences(self):
        chat = types.SimpleNamespace(
            title='Task',
            chat={'opencode': {'directory': 'workspace', 'approval_mode': 'step', 'auto_compaction': False}},
        )
        chats = types.SimpleNamespace(
            get_chat_by_id_and_user_id=AsyncMock(return_value=chat), update_chat_by_id=AsyncMock()
        )
        with (
            patch.object(opencode_agent, 'Chats', chats),
            patch.object(opencode_agent, '_request_json', AsyncMock(return_value={'id': 'new-session'})),
        ):
            await opencode_agent._ensure_session(None, None, 'chat', 'user', 'workspace', 'plan', 'model')
        binding = chats.update_chat_by_id.call_args.args[1]['opencode']
        self.assertEqual(binding['approval_mode'], 'step')
        self.assertFalse(binding['auto_compaction'])
        self.assertEqual(binding['session_id'], 'new-session')

    async def test_workspace_reservation_covers_preparation_and_releases_on_failure(self):
        with self.assertRaisesRegex(ValueError, 'failure'):
            async with opencode_agent._workspace_run_guard('workspace-a'):
                with self.assertRaisesRegex(OpenCodeError, 'already running'):
                    async with opencode_agent._workspace_run_guard('workspace-a'):
                        self.fail('A second task must not reserve the same workspace')
                async with opencode_agent._workspace_run_guard('workspace-b'):
                    pass
                raise ValueError('failure')
        async with opencode_agent._workspace_run_guard('workspace-a'):
            pass

    async def test_abort_cannot_access_another_users_active_session(self):
        chats = types.SimpleNamespace(get_chat_by_id_and_user_id=AsyncMock(return_value=None))
        with (
            patch.object(opencode_agent, 'Chats', chats),
            patch.dict(opencode_agent._active_sessions, {'private-chat': ('session-id', 'workspace')}),
            patch.object(opencode_agent, '_load_runtime') as load_runtime,
        ):
            self.assertFalse(await opencode_agent.abort_chat('private-chat', 'other-user'))
            load_runtime.assert_not_called()

    async def test_control_requires_chat_ownership_before_loading_session(self):
        services = types.ModuleType('open_webui.services')
        services.pi_rpc = types.SimpleNamespace(get_session=AsyncMock())
        chats = types.SimpleNamespace(get_chat_by_id_and_user_id=AsyncMock(return_value=None))
        with patch.object(opencode_agent, 'Chats', chats), patch.dict(sys.modules, {'open_webui.services': services}):
            with self.assertRaisesRegex(OpenCodeError, 'does not have an Agent session'):
                await opencode_agent.control_agent_session(
                    None, 'private-chat', types.SimpleNamespace(id='other-user'), 'reply', {}
                )
            services.pi_rpc.get_session.assert_not_awaited()

    async def test_control_dispatches_only_running_instructions_and_persists_compaction_preference(self):
        session = types.SimpleNamespace(
            busy=False,
            process=None,
            saved={},
            command=AsyncMock(),
            record=lambda *_args, **_kwargs: None,
            save=lambda: None,
        )
        services = types.ModuleType('open_webui.services')
        services.pi_rpc = types.SimpleNamespace(get_session=AsyncMock(return_value=session))
        binding = AsyncMock(return_value=(None, {}, 'session', 'workspace'))
        with (
            patch.object(opencode_agent, '_get_chat_binding', binding),
            patch.dict(sys.modules, {'open_webui.services': services}),
        ):
            user = types.SimpleNamespace(id='user')
            with self.assertRaisesRegex(OpenCodeError, 'idle'):
                await opencode_agent.control_agent_session(None, 'chat', user, 'steer', {'message': 'Continue'})
            session.busy = True
            await opencode_agent.control_agent_session(None, 'chat', user, 'follow_up', {'message': ' Verify '})
            session.command.assert_awaited_once_with('follow_up', message='Verify')
            await opencode_agent.control_agent_session(None, 'chat', user, 'auto_compaction', {'enabled': False})
            self.assertFalse(session.saved['auto_compaction'])


class OpenCodeAgentBehaviorTest(unittest.TestCase):
    def test_turn_usage_sums_calls_including_cache_without_history_or_duplicates(self):
        def message(mid, **usage):
            return {'info': {'id': mid, 'role': 'assistant', 'usage': usage}}

        first = message('first', input=100, output=20, cacheRead=30, cacheWrite=10)
        result = opencode_agent._turn_usage(
            [message('old', input=999), first, first, message('second', input=50, output=5), message('live')], {'old'}
        )
        self.assertEqual(
            result,
            {
                'prompt_tokens': 190,
                'completion_tokens': 25,
                'total_tokens': 215,
                'cache_read_tokens': 30,
                'cache_write_tokens': 10,
            },
        )
        self.assertIsNone(opencode_agent._turn_usage([message('empty', input=0, output=0)], set()))
        context = opencode_agent._turn_context_usage([first, message('second', input=50, output=5)], set(), 32768)
        self.assertEqual(context['used_tokens'], 55)
        self.assertEqual(context['limit_tokens'], 32768)

    def test_truncated_turn_is_not_success(self):
        message = {'info': {'id': 'new', 'role': 'assistant', 'stopReason': 'length'}}
        self.assertIn('未完成', opencode_agent._turn_failure([message], set(), []))
        self.assertEqual(opencode_agent._turn_failure([message], {'new'}, []), '')

    def test_failed_tool_requires_successful_retry(self):
        failed = {'tool': 'write', 'state': {'status': 'error', 'input': {'path': 'game.html'}}}
        success = {'tool': 'write', 'state': {'status': 'completed', 'input': {'path': 'game.html'}}}
        self.assertIn('未完成', opencode_agent._turn_failure([], set(), [failed]))
        self.assertEqual(opencode_agent._turn_failure([], set(), [failed, success]), '')

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

    def test_turn_snapshot_and_file_fallback_cover_multi_message_agent_runs(self):
        directory = str(Path.cwd() / 'workspace' / 'project')
        created_file = str(Path(directory) / 'index.html')
        messages = [
            {
                'info': {'id': 'msg_user', 'role': 'user'},
                'parts': [{'type': 'text', 'text': 'Create an index page'}],
            },
            {
                'info': {'id': 'msg_work', 'role': 'assistant', 'parentID': 'msg_user'},
                'parts': [
                    {
                        'id': 'tool_write',
                        'type': 'tool',
                        'tool': 'write',
                        'state': {
                            'status': 'completed',
                            'input': {'filePath': created_file},
                            'metadata': {'filepath': created_file, 'exists': False},
                        },
                    }
                ],
            },
            {
                'info': {'id': 'msg_done', 'role': 'assistant', 'parentID': 'msg_user'},
                'parts': [{'type': 'text', 'text': 'Done'}],
            },
        ]

        text, tools, assistant_id = _assistant_snapshot(messages, set())
        user_id = _resolve_user_message_id(messages, assistant_id)
        changes = _agent_changed_files(messages, user_id, directory)

        self.assertEqual(text, 'Done')
        self.assertEqual(assistant_id, 'msg_done')
        self.assertEqual(user_id, 'msg_user')
        self.assertEqual([tool['id'] for tool in tools], ['tool_write'])
        self.assertEqual(
            changes,
            [
                {
                    'file': 'index.html',
                    'path': 'index.html',
                    'status': 'added',
                    'source': 'agent_actions',
                }
            ],
        )

    def test_changed_files_prefer_session_diff_then_use_layered_fallbacks(self):
        directory = str(Path.cwd() / 'workspace' / 'project')
        edited_file = str(Path(directory) / 'app.py')
        messages = [
            {'info': {'id': 'msg_user', 'role': 'user'}, 'parts': []},
            {
                'info': {'id': 'msg_assistant', 'role': 'assistant', 'parentID': 'msg_user'},
                'parts': [
                    {
                        'type': 'tool',
                        'tool': 'edit',
                        'state': {
                            'status': 'completed',
                            'input': {'filePath': edited_file},
                            'metadata': {'exists': True},
                        },
                    }
                ],
            },
        ]
        session_result, session_source = _select_changed_files(
            [{'file': 'exact.py', 'additions': 2, 'deletions': 1}],
            messages,
            'msg_user',
            [{'path': 'other.py', 'status': 'modified', 'added': 1, 'removed': 0}],
            directory,
        )
        agent_result, agent_source = _select_changed_files(
            [],
            messages,
            'msg_user',
            [{'path': 'other.py', 'status': 'modified', 'added': 1, 'removed': 0}],
            directory,
        )
        workspace_result, workspace_source = _select_changed_files(
            [],
            [],
            None,
            [{'path': 'other.py', 'status': 'modified', 'added': 1, 'removed': 0}],
            directory,
        )

        self.assertEqual(session_source, 'session')
        self.assertEqual([item['file'] for item in session_result], ['exact.py'])
        self.assertEqual(agent_source, 'agent_actions')
        self.assertEqual([item['file'] for item in agent_result], ['app.py'])
        self.assertEqual(workspace_source, 'workspace_status')
        self.assertEqual(workspace_result[0]['additions'], 1)

    def test_session_diff_converts_before_and_after_to_a_bounded_unified_patch(self):
        result = _normalize_session_diffs(
            [
                {
                    'file': 'index.html',
                    'before': '<title>Old</title>\n<p>Before</p>\n',
                    'after': '<title>New</title>\n<p>Before</p>\n',
                }
            ],
            str(Path.cwd()),
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['file'], 'index.html')
        self.assertEqual(result[0]['status'], 'modified')
        self.assertEqual(result[0]['additions'], 1)
        self.assertEqual(result[0]['deletions'], 1)
        self.assertIn('--- a/index.html', result[0]['patch'])
        self.assertIn('+++ b/index.html', result[0]['patch'])
        self.assertIn('-<title>Old</title>', result[0]['patch'])
        self.assertIn('+<title>New</title>', result[0]['patch'])
        self.assertNotIn('before', result[0])
        self.assertNotIn('after', result[0])

    def test_session_diff_infers_added_and_deleted_file_status(self):
        added = _normalize_session_diffs([{'file': 'new.txt', 'before': '', 'after': 'new\n'}], '')
        deleted = _normalize_session_diffs([{'file': 'old.txt', 'before': 'old\n', 'after': ''}], '')

        self.assertEqual(added[0]['status'], 'added')
        self.assertEqual(deleted[0]['status'], 'deleted')

    def test_progress_status_reports_elapsed_time_and_long_idle_periods(self):
        status = _progress_status(
            'tool',
            elapsed_seconds=125.9,
            idle_seconds=61.2,
            detail='OpenCode · bash: npm test',
        )

        self.assertEqual(status['action'], 'opencode_progress')
        self.assertEqual(status['phase'], 'tool')
        self.assertEqual(status['elapsed_seconds'], 125)
        self.assertEqual(status['idle_seconds'], 61)
        self.assertEqual(status['detail'], 'OpenCode · bash: npm test')
        self.assertTrue(status['delayed'])
        self.assertTrue(status['replace'])
        self.assertFalse(status['done'])

        completed = _progress_status('completed', 126, idle_seconds=90, done=True)
        self.assertTrue(completed['done'])
        self.assertFalse(completed['delayed'])

    def test_tool_description_reports_progress_and_completion(self):
        description, done = _tool_description(
            {
                'tool': 'bash',
                'state': {'status': 'running', 'input': {'command': 'npm test'}},
            }
        )
        self.assertEqual(description, 'Agent · bash: npm test')
        self.assertFalse(done)

        description, done = _tool_description(
            {
                'tool': 'edit',
                'state': {'status': 'error', 'error': 'permission denied'},
            }
        )
        self.assertEqual(description, 'Agent · edit (permission denied)')
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
