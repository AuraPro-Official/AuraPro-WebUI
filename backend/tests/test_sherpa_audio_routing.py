import ast
import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class FormData(dict):
    def add_field(self, key, value, **kwargs):
        self[key] = value


class SherpaAudioRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'open_webui/routers/audio.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        function = next(
            node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == '_transcribe_sherpa'
        )
        self.response = SimpleNamespace(
            status=200,
            raise_for_status=Mock(),
            json=AsyncMock(return_value={'text': '\u0928\u092e\u0938\u094d\u0924\u0947'}),
        )
        self.post = AsyncMock(return_value=self.response)
        output = AsyncMock()
        namespace = {
            'Config': SimpleNamespace(get=AsyncMock(return_value=None)),
            'WHISPER_LANGUAGE': 'zh',
            'ENABLE_FORWARD_USER_INFO_HEADERS': False,
            'AIOHTTP_CLIENT_SESSION_SSL': True,
            'aiohttp': SimpleNamespace(FormData=FormData),
            'aiofiles': SimpleNamespace(open=Mock(return_value=output)),
            'get_session': AsyncMock(return_value=SimpleNamespace(post=self.post)),
            'open': Mock(side_effect=lambda *args: io.BytesIO(b'audio')),
            'os': os,
            'json': json,
            'HTTPException': HTTPException,
            'log': Mock(),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        self.transcribe = namespace['_transcribe_sherpa']

    async def test_candidates_override_global_default_not_request_language(self):
        metadata = {'language_candidates': 'zh,ne', 'stream_id': 'session', 'chunk_index': 0, 'reset_stream': True}
        await self.transcribe(None, 'sample.wav', 'sample.wav', metadata, '.', 'sample')
        data = self.post.call_args.kwargs['data']
        self.assertEqual(data['language_candidates'], 'zh,ne')
        self.assertNotIn('language', data)
        self.assertEqual((data['stream_id'], data['chunk_index'], data['reset_stream']), ('session', '0', 'true'))

        await self.transcribe(None, 'sample.wav', 'sample.wav', {**metadata, 'language': 'ne'}, '.', 'sample')
        self.assertEqual(self.post.call_args.kwargs['data']['language'], 'ne')

    async def test_returns_sherpa_language_error_to_webui(self):
        self.response.status = 422
        self.response.raise_for_status.side_effect = RuntimeError('422')
        self.response.json.return_value = {'detail': 'Unable to compare ASR language candidates: zh, ne'}
        with self.assertRaises(HTTPException) as raised:
            await self.transcribe(None, 'sample.wav', 'sample.wav', {'language_candidates': 'zh,ne'}, '.', 'sample')
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn('zh, ne', raised.exception.detail)
