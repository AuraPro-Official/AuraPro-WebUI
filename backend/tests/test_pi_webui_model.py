import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from starlette.requests import Request
from starlette.responses import StreamingResponse

spec = importlib.util.spec_from_file_location('pi_webui_model_test', Path(__file__).parents[1] / 'open_webui/services/pi_webui_model.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class WebUITransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_selected_webui_model_and_tools_use_existing_dispatcher(self):
        async def chunks():
            yield b'data: {"choices": []}\n\n'
            yield b'data: [DONE]\n\n'
        dispatcher = AsyncMock(return_value=StreamingResponse(chunks()))
        chat = types.ModuleType('open_webui.utils.chat')
        chat.generate_chat_completion = dispatcher
        user = object()
        app = types.SimpleNamespace(state=types.SimpleNamespace(MODELS={}))
        request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [], 'state': {}, 'app': app})
        transport = module.WebUIModelTransport(request, user, {'model': 'selected-webui-model', 'params': {'temperature': 0.3}})
        env = await transport.start()
        config = json.loads(env['AURAPRO_WEBUI_MODEL'])
        try:
            with patch.dict(sys.modules, {'open_webui.utils.chat': chat}):
                async with aiohttp.ClientSession() as client:
                    url = config['baseUrl'] + '/chat/completions'
                    async with client.post(url, json={}) as rejected:
                        self.assertEqual(rejected.status, 401)
                    tools = [{'type': 'function', 'function': {'name': 'read'}}]
                    async with client.post(url, headers={'Authorization': 'Bearer '+config['apiKey']}, json={'model': 'ignored-pi-model', 'messages': [], 'tools': tools, 'stream': True}) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn('[DONE]', await response.text())
            args = dispatcher.call_args.args
            self.assertIs(args[2], user)
            self.assertEqual(args[1]['model'], 'selected-webui-model')
            self.assertEqual(args[1]['tools'], tools)
            self.assertEqual(args[1]['params']['temperature'], 0.3)
        finally:
            await transport.close()
