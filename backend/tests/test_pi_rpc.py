from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
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


class ModelServer(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        has_tool = any(message.get('role') == 'tool' for message in body['messages'])
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if has_tool:
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
        self.wfile.write(b'data: [DONE]\n\n')


@unittest.skipUnless(os.getenv('PI_TEST_EXECUTABLE'), 'Set PI_TEST_EXECUTABLE for real PI integration tests')
class RealPiTest(unittest.IsolatedAsyncioTestCase):
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

    async def run_turn(self, confirmed=True, provider='test'):
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
        while True:
            event = await asyncio.wait_for(stream.queue.get(), 20)
            if event['type'] == 'pi.ui' and event['properties']['request'].get('method') == 'confirm':
                seen_confirmation = True
                await rpc.request(
                    'POST',
                    f'/session/{session_id}/ui',
                    directory,
                    {'id': event['properties']['request']['id'], 'confirmed': confirmed},
                    None,
                )
            if event['type'] == 'session.error':
                self.fail(event)
            if event['type'] == 'session.idle':
                break
        stream.close()
        self.assertTrue(seen_confirmation)
        return session_id

    async def test_real_rpc_write_unicode_restore_and_conflict(self):
        file = self.workspace / '中文.txt'
        file.write_text('before\n', encoding='utf-8')
        session_id = await self.run_turn()
        self.assertEqual(file.read_text(encoding='utf-8'), 'after\n')
        messages = await rpc.request('GET', f'/session/{session_id}/message', str(self.workspace), None, None)
        self.assertIn('完成\u2028验证', messages[-1]['parts'][0]['text'])
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
            accepted = await asyncio.wait_for(rpc.request(
                'POST', f'/session/{session_id}/prompt_async', directory,
                {'parts': [{'text': '/ask-test'}]}, None,
            ), 2)
            self.assertTrue(accepted['accepted'])
            confirmed = False
            while True:
                event = await asyncio.wait_for(stream.queue.get(), 10)
                if event['type'] == 'pi.ui' and event['properties']['request'].get('method') == 'confirm':
                    confirmed = True
                    await rpc.request('POST', f'/session/{session_id}/ui', directory,
                                      {'id': event['properties']['request']['id'], 'confirmed': True}, None)
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
        with self.assertRaisesRegex(RuntimeError, 'PI command get_state did not respond'):
            await session.command('get_state', timeout=0.01)

    async def test_extension_select_input_editor_and_cancellation(self):
        extension = self.root / 'dialogs.mjs'
        extension.write_text(
            'import bridge from ' + json.dumps(self.extension.as_posix()) + ';\n'
            'export default async function(pi) { await bridge(pi); '
            'pi.registerCommand("dialogs-test", {handler: async (_args, ctx) => {'
            'const selected = await ctx.ui.select("Choose", ["one", "two"]);'
            'const input = await ctx.ui.input("Input", "placeholder");'
            'const edited = await ctx.ui.editor("Editor", "initial");'
            'const cancelled = await ctx.ui.input("Cancel");'
            'ctx.ui.setEditorText("draft text");'
            'ctx.ui.notify(JSON.stringify({selected,input,edited,cancelled:cancelled===undefined}), "info");'
            '}}); }', encoding='utf-8')
        settings = json.loads((self.agent / 'settings.json').read_text())
        settings['extensions'] = [str(extension)]
        (self.agent / 'settings.json').write_text(json.dumps(settings))
        directory = str(self.workspace)
        created = await rpc.request('POST', '/session', directory, {}, None)
        stream = rpc.EventStream(directory)
        methods, outcome = [], None
        try:
            await rpc.request('POST', f'/session/{created["id"]}/prompt_async', directory,
                              {'parts': [{'text': '/dialogs-test'}]}, None)
            while True:
                event = await asyncio.wait_for(stream.queue.get(), 20)
                if event['type'] == 'session.error': self.fail(event)
                if event['type'] == 'pi.ui':
                    ui = event['properties']['request']
                    method = ui['method']
                    if method in {'select', 'input', 'editor'}:
                        methods.append(method)
                        answer = {'id': ui['id']}
                        if ui.get('title') == 'Cancel': answer['cancelled'] = True
                        else: answer['value'] = {'select':'two','input':'中文输入','editor':'line1\nline2'}[method]
                        await rpc.request('POST', f'/session/{created["id"]}/ui', directory, answer, None)
                    elif method == 'notify' and '"selected"' in ui.get('message', ''):
                        outcome = json.loads(ui['message'])
                if event['type'] == 'session.idle': break
            self.assertEqual(methods, ['select', 'input', 'editor', 'input'])
            self.assertEqual(outcome, {'selected':'two','input':'中文输入','edited':'line1\nline2','cancelled':True})
        finally:
            stream.close()

    async def test_webui_transport_model_requires_no_pi_model_configuration(self):
        (self.agent / 'models.json').unlink()
        data = json.loads(self.descriptor.read_text())
        data['environment']['AURAPRO_WEBUI_MODEL'] = json.dumps({
            'baseUrl': f'http://127.0.0.1:{self.server.server_port}/v1', 'apiKey': 'temporary-webui-token', 'id': 'test',
        })
        self.descriptor.write_text(json.dumps(data))
        await self.run_turn(provider='aurapro-webui')
        self.assertEqual((self.workspace / '中文.txt').read_text(encoding='utf-8'), 'after\n')


if __name__ == '__main__':
    unittest.main()
