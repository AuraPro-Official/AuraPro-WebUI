"""Desktop-owned runtime selection; never accept control endpoints from clients."""

import asyncio
import contextlib
import os
from urllib.parse import urlsplit

import aiohttp
from fastapi import HTTPException

PRO_MODEL_ID = 'aurapro-pro'
PRO_BASE_URL = 'http://127.0.0.1:18882/v1'


def apply_runtime_thinking(payload: dict, runtime: str | None) -> dict:
    if runtime not in ('standard', 'pro'):
        return payload
    raw_template = payload.get('chat_template_kwargs')
    template = dict(raw_template) if isinstance(raw_template, dict) else {}
    raw_effort = payload.get('reasoning_effort')
    effort = str(raw_effort).strip().lower() if raw_effort is not None else ''
    # WebUI's Default/null is opt-out; Custom selects a reasoning level.
    enabled = effort not in ('', 'none', 'off', 'disabled', 'false', 'minimal')
    template['enable_thinking'] = enabled
    payload['reasoning_effort'] = effort if enabled else 'none'
    if enabled:
        template['reasoning_effort'] = 'xhigh' if runtime == 'pro' and effort == 'high' else effort
    else:
        template.pop('reasoning_effort', None)
    payload['chat_template_kwargs'] = template
    return payload


def control_config():
    url = os.environ.get('AURAPRO_INFERENCE_CONTROL_URL', '')
    token = os.environ.get('AURAPRO_INFERENCE_CONTROL_TOKEN', '')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != 'http'
        or parsed.hostname != '127.0.0.1'
        or not port
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or not token
    ):
        return None
    return url, token


def runtime_for_url(url):
    if not control_config():
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        return None
    if parsed.scheme not in ('http', 'https') or parsed.path.rstrip('/') != '/v1':
        return None
    if parsed.username or parsed.query or parsed.fragment:
        return None
    return {18881: 'standard', 18882: 'pro'}.get(port)


async def control_request(path, data=None, timeout=600):
    config = control_config()
    if not config:
        raise HTTPException(503, 'Desktop runtime control is unavailable')
    url, token = config
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.request(
                'GET' if data is None else 'POST',
                url + path,
                json=data,
                headers={'Authorization': f'Bearer {token}'},
                allow_redirects=False,
            ) as response:
                result = await response.json()
                if response.status != 200:
                    raise HTTPException(503, result.get('error', 'Runtime control failed'))
                return result
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise HTTPException(503, 'Desktop runtime control connection failed') from error


async def desktop_models():
    if not control_config():
        return None
    try:
        return await control_request('/models', timeout=3)
    except HTTPException:
        return None


class RuntimeLease:
    def __init__(self, lease_id):
        self.lease_id = lease_id
        self.released = False
        self.heartbeat = asyncio.create_task(self._renew())

    @classmethod
    async def acquire(cls, url):
        runtime = runtime_for_url(url)
        if not runtime:
            return None
        result = await control_request('/acquire', {'runtime': runtime})
        return cls(result['lease'])

    async def _renew(self):
        while True:
            await asyncio.sleep(15)
            try:
                await control_request('/renew', {'lease': self.lease_id}, timeout=5)
            except HTTPException:
                # Transient control errors must not interrupt an otherwise healthy stream.
                await asyncio.sleep(2)

    async def release(self):
        if self.released:
            return
        self.released = True
        self.heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.heartbeat
        with contextlib.suppress(HTTPException):
            await control_request('/release', {'lease': self.lease_id}, timeout=5)

    async def stream(self, iterator):
        try:
            async for chunk in iterator:
                yield chunk
        finally:

            async def finish():
                try:
                    close = getattr(iterator, 'aclose', None)
                    if close:
                        await close()
                finally:
                    await self.release()

            await asyncio.shield(finish())
