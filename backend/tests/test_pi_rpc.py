from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'pi_rpc_under_test', Path(__file__).parents[1] / 'open_webui/services/pi_rpc.py'
)
rpc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rpc
spec.loader.exec_module(rpc)


class SnapshotTest(unittest.TestCase):
    def test_binary_round_trip_and_diff(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / '中文.txt').write_text('before\n', encoding='utf-8')
            (directory / 'binary.bin').write_bytes(b'\x00\xff')
            before, skipped = rpc.snapshot(temporary)
            (directory / '中文.txt').write_text('after\n', encoding='utf-8')
            (directory / 'new.txt').write_text('new', encoding='utf-8')
            after, _ = rpc.snapshot(temporary)
            changes = rpc.changes(before, after, skipped)
            self.assertEqual({item['file'] for item in changes}, {'中文.txt', 'new.txt'})
            self.assertEqual(base64.b64decode(before['binary.bin']), b'\x00\xff')
            self.assertIn('+after', next(item for item in changes if item['file'] == '中文.txt')['patch'])

    def test_large_files_and_dependencies_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'node_modules').mkdir()
            (directory / 'node_modules/package.js').write_text('ignored')
            (directory / 'large.bin').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
            files, skipped = rpc.snapshot(temporary)
            self.assertEqual(files, {})
            self.assertIn('large.bin', skipped)

    def test_revert_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError):
                rpc._safe_path(temporary, '../outside.txt')

    def test_diff_without_final_newline_keeps_changes_on_separate_lines(self):
        before = {'file.txt': base64.b64encode(b'before').decode()}
        after = {'file.txt': base64.b64encode(b'after').decode()}
        result = rpc.changes(before, after, [])
        self.assertIn('-before\n', result[0]['patch'])
        self.assertIn('+after\n', result[0]['patch'])
        self.assertEqual((result[0]['additions'], result[0]['deletions']), (1, 1))

    def test_partial_snapshot_does_not_report_unscanned_file_as_deleted(self):
        before = {'unscanned.txt': base64.b64encode(b'keep').decode()}
        self.assertEqual(rpc.changes(before, {}, ['[snapshot scan limit reached]']), [])


class SessionActivityTest(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_message_keeps_stable_id_and_is_not_duplicated(self):
        self.session.live_id = 'pi_123_assistant'
        self.session.live_text = 'Checking files'
        self.session.command = AsyncMock(
            return_value={
                'messages': [
                    {'role': 'user', 'timestamp': 122, 'content': 'Check'},
                ]
            }
        )
        streaming = await self.session.messages()
        self.assertEqual(streaming[-1]['info']['id'], 'pi_123_assistant')
        self.session.command.return_value['messages'].append(
            {
                'role': 'assistant',
                'timestamp': 123,
                'content': [{'type': 'text', 'text': 'Checking files'}],
            }
        )
        completed = await self.session.messages()
        self.assertEqual(len(completed), 2)
        self.assertEqual(streaming[-1]['info']['id'], completed[-1]['info']['id'])

    async def test_streaming_parts_keep_text_and_tool_order_without_private_reasoning(self):
        self.session.live_id = 'pi_123_assistant'
        self.session.live_text = 'Checking the file'
        self.session.live_message = {
            'content': [
                {'type': 'thinking', 'thinking': 'Private reasoning'},
                {'type': 'text', 'text': 'Checking the file'},
                {'type': 'toolCall', 'id': 'read-1', 'name': 'read', 'arguments': {'path': 'example.txt'}},
            ]
        }
        self.session.command = AsyncMock(return_value={'messages': []})
        parts = (await self.session.messages())[-1]['parts']
        self.assertEqual([part['type'] for part in parts], ['text', 'tool'])
        self.assertEqual(parts[0]['text'], 'Checking the file')
        self.assertEqual(parts[1]['state']['input'], {'path': 'example.txt'})

    async def test_message_events_only_clear_live_text_for_assistant_end(self):
        events = [
            {'type': 'message_start', 'message': {'role': 'assistant', 'timestamp': 123}},
            {'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': 'Checking'}},
            {'type': 'message_end', 'message': {'role': 'toolResult'}},
        ]
        reader = asyncio.StreamReader()
        for event in events:
            reader.feed_data(json.dumps(event).encode() + b'\n')
        reader.feed_eof()
        self.session.process = types.SimpleNamespace(stdout=reader, returncode=0)
        await self.session.read()
        self.assertEqual(self.session.live_id, 'pi_123_assistant')
        self.assertEqual(self.session.live_text, 'Checking')

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.session = rpc.PiSession({'agentDir': self.temporary.name}, 'test-session', self.temporary.name)
        self.session.send = AsyncMock()

    async def asyncTearDown(self):
        await self.session.close()
        self.temporary.cleanup()

    async def test_journal_is_bounded_incremental_and_persisted(self):
        for index in range(510):
            self.session.record('tool', id='tool-id', text=str(index))
        activity = await self.session.activity(507)
        self.assertEqual([item['id'] for item in activity['items']], [508, 509, 510])
        self.assertEqual(len(self.session.saved['activity']), 500)
        self.assertTrue((await self.session.activity(1))['truncated'])
        self.session.save()
        saved = json.loads(self.session.file.read_text())
        restored = rpc.PiSession(self.session.data, self.session.id, self.session.directory, saved)
        self.assertEqual((await restored.activity())['cursor'], 510)
        self.assertEqual((await restored.activity())['items'][-1]['text'], '509')

    async def test_interaction_reply_is_validated_and_consumed_once(self):
        self.session.ui['choice'] = {'id': 'choice', 'method': 'select', 'options': ['A', 'B']}
        with self.assertRaisesRegex(RuntimeError, 'Choose one'):
            await self.session.answer({'id': 'choice', 'value': 'unlisted'})
        self.assertIn('choice', self.session.ui)
        await self.session.answer({'id': 'choice', 'value': 'B', 'type': 'prompt'})
        self.session.send.assert_awaited_once_with({'type': 'extension_ui_response', 'id': 'choice', 'value': 'B'})
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            await self.session.answer({'id': 'choice', 'value': 'A'})

    async def test_confirmation_requires_boolean_and_close_cancels_dialogs(self):
        self.session.ui['approve'] = {'id': 'approve', 'method': 'confirm'}
        with self.assertRaisesRegex(RuntimeError, 'accepted or declined'):
            await self.session.answer({'id': 'approve', 'confirmed': 'true'})
        await self.session.dismiss_dialogs()
        self.session.send.assert_awaited_once_with(
            {'type': 'extension_ui_response', 'id': 'approve', 'cancelled': True}
        )
        self.assertEqual(self.session.ui, {})

    async def test_custom_selection_forwards_text_and_is_consumed_once(self):
        self.session.ui['choice'] = {'id': 'choice', 'method': 'select', 'options': ['Python', 'Other']}
        await self.session.answer({'id': 'choice', 'value': '  Rust\nwith tests  ', 'custom': True})
        self.session.send.assert_awaited_once_with(
            {'type': 'extension_ui_response', 'id': 'choice', 'value': 'Rust\nwith tests'}
        )
        self.assertNotIn('choice', self.session.ui)
        self.assertEqual(self.session.saved['activity'][-1]['text'], 'Rust\nwith tests')
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            await self.session.answer({'id': 'choice', 'value': 'Rust', 'custom': True})

    async def test_custom_selection_requires_explicit_flag_and_nonblank_bounded_text(self):
        self.session.ui['choice'] = {'id': 'choice', 'method': 'select', 'options': ['Python']}
        for payload in [
            {'value': 'Rust'},
            {'value': 'Rust', 'custom': 'true'},
            {'value': ' \n\t ', 'custom': True},
            {'value': 'x' * 20001, 'custom': True},
            {'value': None, 'custom': True},
        ]:
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                await self.session.answer({'id': 'choice', **payload})
            self.assertIn('choice', self.session.ui)
        self.session.send.assert_not_awaited()
        await self.session.answer({'id': 'choice', 'value': 'Go', 'custom': True})
        self.session.send.assert_awaited_once_with({'type': 'extension_ui_response', 'id': 'choice', 'value': 'Go'})

    async def test_custom_selection_is_retained_on_send_failure_and_can_be_cancelled(self):
        self.session.ui['choice'] = {'id': 'choice', 'method': 'select', 'options': ['Other']}
        self.session.send.side_effect = OSError('disconnected')
        with self.assertRaises(OSError):
            await self.session.answer({'id': 'choice', 'value': 'Rust', 'custom': True})
        self.assertIn('choice', self.session.ui)
        self.session.send.side_effect = None
        await self.session.answer({'id': 'choice', 'cancelled': True})
        self.session.send.assert_awaited_with({'type': 'extension_ui_response', 'id': 'choice', 'cancelled': True})

    async def test_failed_task_keeps_file_changes_and_settles_once(self):
        file = Path(self.temporary.name) / 'result.txt'
        file.write_text('before')
        self.session.before, self.session.skipped = rpc.snapshot(self.temporary.name)
        file.write_text('after')
        self.session.busy = True
        self.session.pending_model_error = 'Model connection failed'
        self.session.messages = AsyncMock(return_value=[{'info': {'id': 'user-1', 'role': 'user'}}])
        await asyncio.gather(self.session.finish(), self.session.finish())
        self.assertFalse(self.session.busy)
        self.assertEqual(len(self.session.saved['turns']), 1)
        turn = self.session.saved['turns'][0]
        self.assertEqual(turn['message_id'], 'user-1')
        self.assertIn('+after', next(item for item in turn['diffs'] if item['file'] == 'result.txt')['patch'])
        self.assertEqual(len([item for item in self.session.saved['activity'] if item['kind'] == 'error']), 1)

    async def test_abort_cancels_manual_compaction_without_starting_another_process(self):
        from unittest.mock import patch

        session_id = 'b2ba9087-bf48-4be7-b733-44fd3ceaa6b1'
        self.session.id = session_id
        self.session.directory = str(Path(self.session.directory).resolve())
        self.session.compacting = True
        self.session.start = AsyncMock()
        self.session.maintenance_task = asyncio.create_task(asyncio.sleep(60))
        with (
            patch.object(rpc, 'descriptor', return_value=self.session.data),
            patch.dict(rpc._sessions, {session_id: self.session}),
        ):
            self.assertTrue(await rpc.request('POST', f'/session/{session_id}/abort', self.session.directory, {}, None))
        self.assertTrue(self.session.maintenance_task.cancelled())
        self.session.start.assert_not_called()

    async def test_rpc_events_preserve_partial_tool_output_and_compaction(self):
        events = [
            {
                'type': 'tool_execution_start',
                'toolCallId': 'call',
                'toolName': 'bash',
                'args': {'command': 'echo hello'},
            },
            {
                'type': 'tool_execution_update',
                'toolCallId': 'call',
                'toolName': 'bash',
                'partialResult': {'content': [{'type': 'text', 'text': 'hello'}]},
            },
            {'type': 'compaction_start', 'reason': 'threshold'},
            {'type': 'compaction_end', 'result': {'tokensBefore': 30000, 'estimatedTokensAfter': 5000}},
        ]
        reader = asyncio.StreamReader()
        for event in events:
            reader.feed_data(json.dumps(event).encode() + b'\n')
        reader.feed_eof()
        self.session.process = types.SimpleNamespace(stdout=reader, returncode=0)
        await self.session.read()
        self.assertEqual(self.session.live_tools['call']['output'], 'hello')
        self.assertFalse(self.session.compacting)
        self.assertEqual(self.session.saved['activity'][-1]['tokens_after'], 5000)


class ModelServer(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        has_tool = any(message.get('role') == 'tool' for message in body['messages'])
        recovering = getattr(self.server, 'test_overflow', False)
        summary = recovering and '# Conversation' in json.dumps(body, ensure_ascii=False)
        if recovering and has_tool and not summary and not getattr(self.server, 'overflow_sent', False):
            self.server.overflow_sent = True
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': {'message': 'maximum context length exceeded'}}).encode())
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if summary:
            self.server.summary_seen = True
            events = [
                ({'role': 'assistant', 'content': 'Task: edit 中文.txt. Written after. Continue verification.'}, None),
                ({}, 'stop'),
            ]
        elif has_tool or (recovering and getattr(self.server, 'summary_seen', False)):
            events = [({'role': 'assistant', 'content': '完成\u2028验证'}, None), ({}, 'stop')]
        else:
            events = [
                (
                    {
                        'role': 'assistant',
                        'tool_calls': [
                            {
                                'index': 0,
                                'id': 'call_test',
                                'type': 'function',
                                'function': {
                                    'name': 'write',
                                    'arguments': json.dumps({'path': '中文.txt', 'content': 'after\n'}),
                                },
                            }
                        ],
                    },
                    None,
                ),
                ({}, 'tool_calls'),
            ]
        if getattr(self.server, 'test_planning', False):
            results = [message for message in body['messages'] if message.get('role') == 'tool']
            if len(results) >= 2:
                events = [({'role': 'assistant', 'content': 'Selected CSV; plan complete.'}, None), ({}, 'stop')]
            else:
                tool = 'ask_user' if results else 'update_plan'
                arguments = (
                    {'question': 'Choose format', 'options': ['JSON', 'CSV']}
                    if results
                    else {
                        'steps': [
                            {'content': 'Choose a format', 'status': 'in_progress'},
                            {'content': 'Verify', 'status': 'pending'},
                        ]
                    }
                )
                events = [
                    (
                        {
                            'role': 'assistant',
                            'tool_calls': [
                                {
                                    'index': 0,
                                    'id': f'call_{tool}',
                                    'type': 'function',
                                    'function': {'name': tool, 'arguments': json.dumps(arguments)},
                                }
                            ],
                        },
                        None,
                    ),
                    ({}, 'tool_calls'),
                ]
        if getattr(self.server, 'test_transcript', False):
            results = [message for message in body['messages'] if message.get('role') == 'tool']
            if not results:
                events.insert(0, ({'role': 'assistant', 'content': 'Checking the file before the update.'}, None))
            elif len(results) == 1:
                events = [
                    ({'role': 'assistant', 'content': 'File updated. Verifying the contents.'}, None),
                    (
                        {
                            'tool_calls': [
                                {
                                    'index': 0,
                                    'id': 'call_read',
                                    'type': 'function',
                                    'function': {
                                        'name': 'read',
                                        'arguments': json.dumps({'path': '中文.txt'}),
                                    },
                                }
                            ]
                        },
                        None,
                    ),
                    ({}, 'tool_calls'),
                ]
        for delta, finish in events:
            record = {
                'id': 'chatcmpl-test',
                'object': 'chat.completion.chunk',
                'created': 0,
                'model': 'test',
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}],
            }
            self.wfile.write(('data: ' + json.dumps(record) + '\n\n').encode())
            self.wfile.flush()
        self.wfile.write(
            (
                'data: '
                + json.dumps(
                    {'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}
                )
                + '\n\n'
            ).encode()
        )
        self.wfile.write(b'data: [DONE]\n\n')


@unittest.skipUnless(os.getenv('PI_TEST_EXECUTABLE'), 'Set PI_TEST_EXECUTABLE for real PI integration tests')
class RealPiTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_rpc_retains_interleaved_commentary_and_tools(self):
        self.server.test_transcript = True
        session_id = await self.run_turn()
        messages = await rpc.request('GET', f'/session/{session_id}/message', str(self.workspace), None, None)
        assistants = [message for message in messages if message['info']['role'] == 'assistant']
        self.assertEqual(len(assistants), 3)
        self.assertEqual([part['type'] for part in assistants[0]['parts']], ['text', 'tool'])
        self.assertEqual([part['type'] for part in assistants[1]['parts']], ['text', 'tool'])
        self.assertEqual(assistants[0]['parts'][0]['text'], 'Checking the file before the update.')
        self.assertEqual(assistants[1]['parts'][0]['text'], 'File updated. Verifying the contents.')
        self.assertEqual(assistants[1]['parts'][1]['state']['status'], 'completed')
        self.assertIn('after', str(assistants[1]['parts'][1]['state']['output']))
        self.assertEqual(assistants[2]['parts'][0]['text'], '完成\u2028验证')

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.agent = self.root / 'agent'
        self.agent.mkdir()
        self.extension = Path(os.environ['PI_TEST_EXTENSION'])
        (self.agent / 'settings.json').write_text(
            json.dumps(
                {
                    'extensions': [str(self.extension)],
                    'defaultTools': ['read', 'write', 'edit', 'powershell', 'grep', 'find', 'ls'],
                }
            )
        )
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ModelServer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        (self.agent / 'models.json').write_text(
            json.dumps(
                {
                    'providers': {
                        'test': {
                            'baseUrl': f'http://127.0.0.1:{self.server.server_port}/v1',
                            'api': 'openai-completions',
                            'apiKey': 'test',
                            'models': [{'id': 'test', 'contextWindow': 32768, 'maxTokens': 4096}],
                        }
                    }
                }
            )
        )
        self.descriptor = self.root / 'runtime.json'
        self.descriptor.write_text(
            json.dumps(
                {
                    'engine': 'pi',
                    'version': 2,
                    'executable': os.environ['PI_TEST_EXECUTABLE'],
                    'arguments': json.loads(os.getenv('PI_TEST_ARGUMENTS', '[]')),
                    'agentDir': str(self.agent),
                    'extension': str(self.extension),
                    'piVersion': '1.0.3',
                    'cwd': str(self.workspace),
                    'environment': {'PI_OFFLINE': '1'},
                }
            )
        )
        self.old_runtime = os.environ.get('AURAPRO_PI_RUNTIME_FILE')
        os.environ['AURAPRO_PI_RUNTIME_FILE'] = str(self.descriptor)

    async def asyncTearDown(self):
        await rpc.shutdown()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        if self.old_runtime is None:
            os.environ.pop('AURAPRO_PI_RUNTIME_FILE', None)
        else:
            os.environ['AURAPRO_PI_RUNTIME_FILE'] = self.old_runtime
        self.temp.cleanup()

    async def run_turn(self, confirmed=True, provider='test', expect_confirmation=True):
        directory = str(self.workspace)
        created = await rpc.request('POST', '/session', directory, {}, None)
        session_id = created['id']
        stream = rpc.EventStream(directory)
        await rpc.request(
            'POST',
            f'/session/{session_id}/prompt_async',
            directory,
            {'model': {'providerID': provider, 'modelID': 'test'}, 'parts': [{'text': 'Edit the file'}]},
            None,
        )
        seen_confirmation = False
        self.confirmation_titles = []
        self.compaction_events = []
        while True:
            event = await asyncio.wait_for(stream.queue.get(), 20)
            if event['type'] == 'pi.compaction':
                self.compaction_events.append(event['properties'])
            if event['type'] == 'pi.ui' and event['properties']['request'].get('method') == 'confirm':
                seen_confirmation = True
                self.confirmation_titles.append(event['properties']['request'].get('title'))
                await rpc.request(
                    'POST',
                    f'/session/{session_id}/ui',
                    directory,
                    {'id': event['properties']['request']['id'], 'confirmed': confirmed},
                    None,
                )
            if event['type'] == 'session.error':
                self.fail((event, self.compaction_events))
            if event['type'] == 'session.idle':
                break
        stream.close()
        self.assertEqual(seen_confirmation, expect_confirmation)
        return session_id

    async def test_real_rpc_write_unicode_restore_and_conflict(self):
        file = self.workspace / '中文.txt'
        file.write_text('before\n', encoding='utf-8')
        session_id = await self.run_turn()
        self.assertEqual(file.read_text(encoding='utf-8'), 'after\n')
        messages = await rpc.request('GET', f'/session/{session_id}/message', str(self.workspace), None, None)
        self.assertIn('完成\u2028验证', messages[-1]['parts'][0]['text'])
        assistants = [item for item in messages if item['info']['role'] == 'assistant']
        self.assertEqual(sum(item['info']['usage']['input'] for item in assistants), 200)
        self.assertEqual(sum(item['info']['usage']['output'] for item in assistants), 40)
        diffs = await rpc.request('GET', f'/session/{session_id}/diff', str(self.workspace), None, None)
        self.assertEqual(diffs[0]['file'], '中文.txt')
        turn = rpc._sessions[session_id].saved['turns'][-1]
        await rpc.request(
            'POST', f'/session/{session_id}/revert', str(self.workspace), {'messageID': turn['message_id']}, None
        )
        self.assertEqual(file.read_text(encoding='utf-8'), 'before\n')
        await rpc.request('POST', f'/session/{session_id}/unrevert', str(self.workspace), {}, None)
        file.write_text('user edit', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'changed after'):
            await rpc.request(
                'POST', f'/session/{session_id}/revert', str(self.workspace), {'messageID': turn['message_id']}, None
            )
        await rpc.shutdown()
        resumed = await rpc.get_session(session_id, str(self.workspace))
        self.assertEqual((await resumed.messages())[-1]['parts'][0]['text'], '完成\u2028验证')

    async def test_declined_write_does_not_modify_file(self):
        await self.run_turn(confirmed=False)
        self.assertFalse((self.workspace / '中文.txt').exists())

    async def test_missing_extension_fails_readiness_check(self):
        created = await rpc.request('POST', '/session', str(self.workspace), {}, None)
        session = rpc._sessions[created['id']]
        await session.command('prompt', message='/aurapro-check find_roots')
        self.assertFalse(session.probe['available'])

    async def test_extension_preflight_confirmation_does_not_block_submission(self):
        extension = self.root / 'confirmation.mjs'
        extension.write_text(
            'import bridge from ' + json.dumps(self.extension.as_posix()) + ';\n'
            'export default async function(pi) { await bridge(pi); '
            'pi.registerCommand("ask-test", {handler: async (_args, ctx) => {'
            'await ctx.ui.confirm("Startup confirmation", "Continue?"); }}); }',
            encoding='utf-8',
        )
        settings_file = self.agent / 'settings.json'
        settings = json.loads(settings_file.read_text())
        settings['extensions'] = [str(extension)]
        settings_file.write_text(json.dumps(settings))
        directory = str(self.workspace)
        created = await rpc.request('POST', '/session', directory, {}, None)
        session_id = created['id']
        stream = rpc.EventStream(directory)
        try:
            accepted = await asyncio.wait_for(
                rpc.request(
                    'POST',
                    f'/session/{session_id}/prompt_async',
                    directory,
                    {'parts': [{'text': '/ask-test'}]},
                    None,
                ),
                2,
            )
            self.assertTrue(accepted['accepted'])
            confirmed = False
            while True:
                event = await asyncio.wait_for(stream.queue.get(), 10)
                if event['type'] == 'pi.ui' and event['properties']['request'].get('method') == 'confirm':
                    confirmed = True
                    await rpc.request(
                        'POST',
                        f'/session/{session_id}/ui',
                        directory,
                        {'id': event['properties']['request']['id'], 'confirmed': True},
                        None,
                    )
                if event['type'] == 'session.error':
                    self.fail(event)
                if event['type'] == 'session.idle':
                    break
            self.assertTrue(confirmed)
        finally:
            stream.close()

    async def test_rpc_timeout_has_visible_error_message(self):
        from unittest.mock import AsyncMock

        session = rpc.PiSession(rpc.descriptor(), 'timeout-test', str(self.workspace))
        session.send = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'Agent command get_state did not respond'):
            await session.command('get_state', timeout=0.01)

    async def test_extension_select_input_editor_and_cancellation(self):
        extension = self.root / 'dialogs.mjs'
        extension.write_text(
            'import bridge from ' + json.dumps(self.extension.as_posix()) + ';\n'
            'export default async function(pi) { await bridge(pi); '
            'pi.registerCommand("dialogs-test", {handler: async (_args, ctx) => {'
            'const selected = await ctx.ui.select("Choose", ["one", "two"]);'
            'const custom = await ctx.ui.select("Custom", ["one", "Other"]);'
            'const input = await ctx.ui.input("Input", "placeholder");'
            'const edited = await ctx.ui.editor("Editor", "initial");'
            'const cancelled = await ctx.ui.input("Cancel");'
            'ctx.ui.setEditorText("draft text");'
            'ctx.ui.notify(JSON.stringify({selected,custom,input,edited,cancelled:cancelled===undefined}), "info");'
            '}}); }',
            encoding='utf-8',
        )
        settings = json.loads((self.agent / 'settings.json').read_text())
        settings['extensions'] = [str(extension)]
        (self.agent / 'settings.json').write_text(json.dumps(settings))
        directory = str(self.workspace)
        created = await rpc.request('POST', '/session', directory, {}, None)
        stream = rpc.EventStream(directory)
        methods, outcome = [], None
        try:
            await rpc.request(
                'POST',
                f'/session/{created["id"]}/prompt_async',
                directory,
                {'parts': [{'text': '/dialogs-test'}]},
                None,
            )
            while True:
                event = await asyncio.wait_for(stream.queue.get(), 20)
                if event['type'] == 'session.error':
                    self.fail(event)
                if event['type'] == 'pi.ui':
                    ui = event['properties']['request']
                    method = ui['method']
                    if method in {'select', 'input', 'editor'}:
                        methods.append(method)
                        answer = {'id': ui['id']}
                        if ui.get('title') == 'Cancel':
                            answer['cancelled'] = True
                        elif ui.get('title') == 'Custom':
                            answer.update(value='Rust with Unicode: 自定义', custom=True)
                        else:
                            answer['value'] = {'select': 'two', 'input': '中文输入', 'editor': 'line1\nline2'}[method]
                        await rpc.request('POST', f'/session/{created["id"]}/ui', directory, answer, None)
                    elif method == 'notify' and '"selected"' in ui.get('message', ''):
                        outcome = json.loads(ui['message'])
                if event['type'] == 'session.idle':
                    break
            self.assertEqual(methods, ['select', 'select', 'input', 'editor', 'input'])
            self.assertEqual(
                outcome,
                {
                    'selected': 'two',
                    'custom': 'Rust with Unicode: 自定义',
                    'input': '中文输入',
                    'edited': 'line1\nline2',
                    'cancelled': True,
                },
            )
        finally:
            stream.close()

    async def test_webui_transport_model_requires_no_pi_model_configuration(self):
        (self.agent / 'models.json').unlink()
        data = json.loads(self.descriptor.read_text())
        data['environment']['AURAPRO_WEBUI_MODEL'] = json.dumps(
            {
                'baseUrl': f'http://127.0.0.1:{self.server.server_port}/v1',
                'apiKey': 'temporary-webui-token',
                'id': 'test',
            }
        )
        self.descriptor.write_text(json.dumps(data))
        await self.run_turn(provider='aurapro-webui')
        self.assertEqual((self.workspace / '中文.txt').read_text(encoding='utf-8'), 'after\n')

    async def test_auto_approval_only_confirms_first_operation(self):
        descriptor = json.loads(self.descriptor.read_text())
        descriptor['environment']['AURAPRO_PI_APPROVAL_MODE'] = 'task'
        self.descriptor.write_text(json.dumps(descriptor))
        await self.run_turn()
        self.assertEqual(len(self.confirmation_titles), 1)
        self.assertNotIn('当前任务授权', self.confirmation_titles[0])

    async def test_full_control_writes_without_confirmations(self):
        descriptor = json.loads(self.descriptor.read_text())
        descriptor['environment']['AURAPRO_PI_APPROVAL_MODE'] = 'full'
        self.descriptor.write_text(json.dumps(descriptor))
        await self.run_turn(expect_confirmation=False)
        self.assertEqual(self.confirmation_titles, [])
        self.assertEqual((self.workspace / '中文.txt').read_text(encoding='utf-8'), 'after\n')

    async def test_context_overflow_compacts_and_continues_real_pi(self):
        settings = json.loads((self.agent / 'settings.json').read_text())
        settings['compaction'] = {'enabled': True, 'reserveTokens': 2048, 'keepRecentTokens': 0}
        (self.agent / 'settings.json').write_text(json.dumps(settings))
        self.server.test_overflow = True
        await self.run_turn()
        self.assertTrue(self.server.overflow_sent)
        self.assertTrue(self.server.summary_seen)
        self.assertTrue(any(item['type'] == 'compaction_start' for item in self.compaction_events))
        self.assertTrue(any(item['type'] == 'compaction_end' and item.get('result') for item in self.compaction_events))
        self.assertEqual((self.workspace / '中文.txt').read_text(encoding='utf-8'), 'after\n')

    async def test_model_can_publish_plan_and_wait_for_user_choice_in_plan_mode(self):
        self.server.test_planning = True
        directory = str(self.workspace)
        created = await rpc.request('POST', '/session', directory, {}, None)
        session_id = created['id']
        stream = rpc.EventStream(directory)
        try:
            await rpc.request(
                'POST',
                f'/session/{session_id}/prompt_async',
                directory,
                {
                    'agent': 'plan',
                    'model': {'providerID': 'test', 'modelID': 'test'},
                    'parts': [{'text': 'Plan an export and ask which format to use'}],
                },
                None,
            )
            selected = False
            while True:
                event = await asyncio.wait_for(stream.queue.get(), 20)
                if event['type'] == 'session.error':
                    self.fail(event)
                if event['type'] == 'pi.ui' and event['properties']['request'].get('method') == 'select':
                    session = await rpc.get_session(session_id, directory, start=False)
                    state = await session.activity()
                    self.assertEqual(state['todos'][0]['status'], 'in_progress')
                    self.assertEqual(state['requests'][0]['options'], ['JSON', 'CSV'])
                    await session.answer({'id': state['requests'][0]['id'], 'value': 'CSV'})
                    selected = True
                if event['type'] == 'session.idle':
                    break
            self.assertTrue(
                selected, (await rpc._sessions[session_id].messages(), await rpc._sessions[session_id].activity())
            )
            session = rpc._sessions[session_id]
            state = await session.activity()
            self.assertEqual(state['requests'], [])
            self.assertTrue(any(item['kind'] == 'answer' and item['text'] == 'CSV' for item in state['items']))
            self.assertIn('Selected CSV', (await session.messages())[-1]['parts'][0]['text'])
            self.assertEqual(list(self.workspace.iterdir()), [])
        finally:
            stream.close()


if __name__ == '__main__':
    unittest.main()
