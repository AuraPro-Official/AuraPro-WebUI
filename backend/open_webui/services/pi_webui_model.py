"""Private, per-turn transport from PI to WebUI's existing model dispatcher."""
from __future__ import annotations

import json
import secrets
import copy
from aiohttp import web
from starlette.requests import Request


class WebUIModelTransport:
    def __init__(self, request, user, form_data: dict):
        self.request, self.user = request, user
        self.model = str(form_data['model'])
        self.params = dict(form_data.get('params') or {})
        self.token = secrets.token_urlsafe(32)
        self.runner = None
        models = getattr(request.app.state, 'MODELS', {})
        model = models.get(self.model, {})
        model_params = (model.get('info') or {}).get('params') or {}
        self.context_window = max(4096, int(self.params.get('num_ctx') or model_params.get('num_ctx') or 32768))
        self.max_tokens = min(self.context_window, int(self.params.get('max_tokens') or model_params.get('max_tokens') or 8192))

    async def completion(self, incoming):
        if incoming.headers.get('Authorization') != f'Bearer {self.token}':
            raise web.HTTPUnauthorized()
        from open_webui.utils.chat import generate_chat_completion
        try:
            payload = await incoming.json()
            payload['model'] = self.model
            payload['params'] = self.params
            if payload.get('stream'):
                payload['stream_options'] = {**(payload.get('stream_options') or {}), 'include_usage': True}
            scope = {**self.request.scope, 'state': dict(self.request.scope.get('state', {}))}
            if 'metadata' in scope['state']:
                scope['state']['metadata'] = copy.deepcopy(scope['state']['metadata'])
            request = Request(scope, receive=self.request.receive)
            result = await generate_chat_completion(request, payload, self.user)
            if hasattr(result, 'body_iterator'):
                response = web.StreamResponse(status=result.status_code, headers={'Content-Type': 'text/event-stream'})
                await response.prepare(incoming)
                try:
                    async for chunk in result.body_iterator:
                        await response.write(chunk.encode('utf-8') if isinstance(chunk, str) else chunk)
                    await response.write_eof()
                finally:
                    if result.background:
                        await result.background()
                return response
            if hasattr(result, 'body'):
                return web.Response(body=result.body, status=result.status_code, content_type='application/json')
            return web.json_response(result)
        except Exception as error:
            return web.json_response({'error': {'message': str(getattr(error, 'detail', error)) or 'WebUI model request failed.'}}, status=502)

    async def start(self):
        app = web.Application(client_max_size=32 * 1024 * 1024)
        app.router.add_post('/v1/chat/completions', self.completion)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return {'AURAPRO_WEBUI_MODEL': json.dumps({
            'baseUrl': f'http://127.0.0.1:{port}/v1', 'apiKey': self.token,
            'id': self.model,
            'contextWindow': self.context_window, 'maxTokens': self.max_tokens,
        })}

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
