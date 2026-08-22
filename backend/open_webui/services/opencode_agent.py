from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from open_webui.models.chats import Chats
from open_webui.socket.main import get_event_call, get_event_emitter

log = logging.getLogger(__name__)

_MAX_TURN_SECONDS = max(60, int(os.getenv('AURAPRO_OPENCODE_TURN_TIMEOUT', '1800')))
_POLL_INTERVAL_SECONDS = 0.4
_ALLOWED_HOSTS = {'127.0.0.1', 'localhost', '::1'}
_chat_locks: dict[str, asyncio.Lock] = {}
_active_sessions: dict[str, tuple[str, str]] = {}


class OpenCodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenCodeRuntime:
    url: str
    username: str
    password: str
    version: str | None = None


def _normalize_runtime_url(value: str) -> str:
    parsed = urlsplit(value.strip().rstrip('/'))
    if (
        parsed.scheme not in {'http', 'https'}
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {'', '/'}
    ):
        raise OpenCodeError('OpenCode must use a local loopback service address.')
    return value.strip().rstrip('/')


def _load_runtime() -> OpenCodeRuntime:
    runtime_file = os.getenv('AURAPRO_OPENCODE_RUNTIME_FILE', '').strip()
    if runtime_file:
        try:
            values = json.loads(Path(runtime_file).read_text(encoding='utf-8'))
        except FileNotFoundError as error:
            raise OpenCodeError('OpenCode is not running. Start it from Desktop settings.') from error
        except (OSError, json.JSONDecodeError) as error:
            raise OpenCodeError('The OpenCode runtime descriptor is unavailable or invalid.') from error
        return OpenCodeRuntime(
            url=_normalize_runtime_url(str(values.get('url') or '')),
            username=str(values.get('username') or ''),
            password=str(values.get('password') or ''),
            version=str(values.get('openCodeVersion') or '') or None,
        )

    url = os.getenv('AURAPRO_OPENCODE_URL', '').strip()
    if not url:
        raise OpenCodeError('OpenCode is not configured for this WebUI server.')
    return OpenCodeRuntime(
        url=_normalize_runtime_url(url),
        username=os.getenv('AURAPRO_OPENCODE_USERNAME', 'aurapro'),
        password=os.getenv('AURAPRO_OPENCODE_PASSWORD', ''),
        version=None,
    )


def _normalize_directory(value: str) -> str:
    if not value or len(value) > 4096 or '\x00' in value:
        raise OpenCodeError('Select a valid project directory before using Code mode.')
    try:
        directory = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise OpenCodeError('The selected project directory does not exist or cannot be accessed.') from error
    if not directory.is_dir():
        raise OpenCodeError('The selected project path is not a directory.')
    return str(directory)


def _auth(runtime: OpenCodeRuntime) -> aiohttp.BasicAuth | None:
    if not runtime.username and not runtime.password:
        return None
    return aiohttp.BasicAuth(runtime.username, runtime.password)


def _error_detail(text: str, status: int) -> str:
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            detail = value.get('error') or value.get('detail') or value.get('message')
            if isinstance(detail, dict):
                detail = detail.get('message') or detail.get('detail')
            if detail:
                return str(detail)
    except json.JSONDecodeError:
        pass
    return text.strip()[:500] or f'OpenCode returned HTTP {status}.'


async def _request_json(
    session: aiohttp.ClientSession,
    runtime: OpenCodeRuntime,
    method: str,
    path: str,
    *,
    directory: str | None = None,
    payload: dict | None = None,
    query: dict[str, str] | None = None,
    timeout: float = 15,
) -> Any:
    params = dict(query or {})
    if directory:
        params['directory'] = directory
    async with session.request(
        method,
        f'{runtime.url}{path}',
        params=params or None,
        json=payload,
        auth=_auth(runtime),
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        text = await response.text()
        if response.status >= 400:
            raise OpenCodeError(_error_detail(text, response.status))
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise OpenCodeError('OpenCode returned an invalid response.') from error


async def _request_json_optional(
    session: aiohttp.ClientSession,
    runtime: OpenCodeRuntime,
    method: str,
    path: str,
    fallback: Any,
    *,
    directory: str | None = None,
    payload: dict | None = None,
    query: dict[str, str] | None = None,
    timeout: float = 15,
) -> Any:
    try:
        return await _request_json(
            session,
            runtime,
            method,
            path,
            directory=directory,
            payload=payload,
            query=query,
            timeout=timeout,
        )
    except Exception as error:
        log.debug('Optional OpenCode endpoint failed (%s): %s', path, error)
        return fallback


async def get_status(chat_id: str | None = None, user_id: str | None = None) -> dict:
    try:
        runtime = _load_runtime()
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            health = await _request_json(session, runtime, 'GET', '/global/health', timeout=3)
            path_info = await _request_json(session, runtime, 'GET', '/path', timeout=3)
        result = {
            'available': bool(health and health.get('healthy')),
            'version': (health or {}).get('version') or runtime.version,
            'default_directory': (path_info or {}).get('directory') or (path_info or {}).get('root'),
        }
        if chat_id and user_id:
            chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
            binding = (chat.chat or {}).get('opencode') if chat else None
            if isinstance(binding, dict):
                result['session'] = {
                    'id': binding.get('session_id'),
                    'directory': binding.get('directory'),
                    'agent': binding.get('agent', 'build'),
                    'model': binding.get('model', ''),
                }
        return result
    except Exception as error:
        return {'available': False, 'error': str(error)}


async def validate_directory(value: str) -> dict:
    directory = _normalize_directory(value)
    runtime = _load_runtime()
    async with aiohttp.ClientSession(trust_env=False) as session:
        path_info = await _request_json(session, runtime, 'GET', '/path', directory=directory)
    return {'valid': True, 'directory': directory, 'path': path_info}


async def get_capabilities(value: str) -> dict:
    directory = _normalize_directory(value)
    runtime = _load_runtime()
    async with aiohttp.ClientSession(trust_env=False) as session:
        providers, agents, vcs = await asyncio.gather(
            _request_json(session, runtime, 'GET', '/provider', directory=directory),
            _request_json(session, runtime, 'GET', '/agent', directory=directory),
            _request_json_optional(session, runtime, 'GET', '/vcs', {}, directory=directory),
        )
    normalized = _normalize_capabilities(providers, agents)
    return {
        'directory': directory,
        'models': normalized['models'],
        'agents': normalized['agents'],
        'default_model': normalized['default_model'],
        'vcs': _normalize_vcs(vcs),
    }


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(
            str(item.get('text') or item.get('content') or '')
            for item in value
            if isinstance(item, dict) and item.get('type') in {'text', 'input_text'}
        ).strip()
    return str(value or '')


def _event_payload(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    payload = value.get('payload')
    return payload if isinstance(payload, dict) else value


async def _read_sse(response: aiohttp.ClientResponse, queue: asyncio.Queue) -> None:
    try:
        while not response.content.at_eof():
            raw_line = await response.content.readline()
            if not raw_line:
                break
            line = raw_line.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            try:
                await queue.put(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                log.debug('Ignoring invalid OpenCode SSE data')
    except asyncio.CancelledError:
        raise
    except Exception as error:
        await queue.put({'type': 'aurapro.sse.error', 'properties': {'message': str(error)}})


def _session_id_from_event(event: dict) -> str | None:
    properties = event.get('properties') or {}
    return properties.get('sessionID') or properties.get('session_id')


def _assistant_snapshot(messages: Any, baseline_ids: set[str]) -> tuple[str, list[dict], str | None]:
    if not isinstance(messages, list):
        return '', [], None
    candidates = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        info = item.get('info') or {}
        message_id = str(info.get('id') or '')
        if info.get('role') == 'assistant' and message_id not in baseline_ids:
            candidates.append(item)
    if not candidates:
        return '', [], None
    current = candidates[-1]
    parts = current.get('parts') if isinstance(current.get('parts'), list) else []
    text = ''.join(
        str(part.get('text') or '') for part in parts if isinstance(part, dict) and part.get('type') == 'text'
    )
    tools = [part for part in parts if isinstance(part, dict) and part.get('type') == 'tool']
    return text, tools, str((current.get('info') or {}).get('id') or '') or None


def _tool_description(part: dict) -> tuple[str, bool]:
    state = part.get('state') or {}
    tool = str(part.get('tool') or 'tool')
    status = str(state.get('status') or 'running')
    values = state.get('input') if isinstance(state.get('input'), dict) else {}
    detail = (
        values.get('command')
        or values.get('filePath')
        or values.get('path')
        or values.get('pattern')
        or state.get('title')
        or ''
    )
    detail = ' '.join(str(detail).split())[:180]
    description = f'OpenCode · {tool}' + (f': {detail}' if detail else '')
    if status == 'error':
        description += f' ({str(state.get("error") or "failed")[:160]})'
    return description, status in {'completed', 'error'}


_IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z0-9_.:/-]{1,256}$')


def _safe_identifier(value: Any, fallback: str = '') -> str:
    result = str(value or '').strip()
    return result if _IDENTIFIER_PATTERN.fullmatch(result) else fallback


def _split_model(value: Any) -> tuple[str, str] | None:
    model = _safe_identifier(value)
    parts = model.split('/')
    if len(parts) < 2 or any(part in {'', '.', '..'} for part in parts):
        return None
    return parts[0], '/'.join(parts[1:])


def _normalize_agents(agent_data: Any) -> list[dict]:
    agents = []
    for value in agent_data if isinstance(agent_data, list) else []:
        if not isinstance(value, dict) or value.get('hidden') is True:
            continue
        agent_id = _safe_identifier(value.get('name') or value.get('id'))
        mode = str(value.get('mode') or 'all')
        if not agent_id or mode not in {'primary', 'all'}:
            continue
        agents.append(
            {
                'id': agent_id,
                'name': str(value.get('name') or agent_id),
                'description': str(value.get('description') or ''),
                'mode': mode,
            }
        )
    return agents or [
        {'id': 'build', 'name': 'build', 'description': '', 'mode': 'primary'},
        {'id': 'plan', 'name': 'plan', 'description': '', 'mode': 'primary'},
    ]


def _normalize_capabilities(provider_data: Any, agent_data: Any) -> dict:
    provider_payload = provider_data if isinstance(provider_data, dict) else {}
    providers = provider_payload.get('all') if isinstance(provider_payload.get('all'), list) else []
    connected_value = provider_payload.get('connected')
    connected = {str(value) for value in connected_value} if isinstance(connected_value, list) else None
    defaults = provider_payload.get('default') if isinstance(provider_payload.get('default'), dict) else {}
    default_values = {_safe_identifier(value) for value in defaults.values()}

    models = []
    default_model = ''
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = _safe_identifier(provider.get('id'))
        if not provider_id or (connected is not None and provider_id not in connected):
            continue
        provider_name = str(provider.get('name') or provider_id)
        provider_models = provider.get('models') if isinstance(provider.get('models'), dict) else {}
        for model_key, model_value in provider_models.items():
            model = model_value if isinstance(model_value, dict) else {}
            model_id = _safe_identifier(model.get('id') or model_key)
            if not model_id:
                continue
            full_id = f'{provider_id}/{model_id}'
            is_default = defaults.get(provider_id) == model_id or full_id in default_values
            models.append(
                {
                    'id': full_id,
                    'provider_id': provider_id,
                    'provider_name': provider_name,
                    'model_id': model_id,
                    'name': str(model.get('name') or model_id),
                    'default': is_default,
                }
            )
            if is_default and not default_model:
                default_model = full_id

    return {
        'models': models,
        'agents': _normalize_agents(agent_data),
        'default_model': default_model,
    }


def _normalize_vcs(value: Any) -> dict:
    data = value if isinstance(value, dict) else {}
    return {
        'branch': str(data.get('branch') or ''),
        'root': str(data.get('root') or data.get('worktree') or ''),
    }


def _normalize_todos(value: Any) -> list[dict]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                'id': str(item.get('id') or ''),
                'content': str(item.get('content') or item.get('text') or item.get('title') or ''),
                'status': str(item.get('status') or 'pending'),
                'priority': str(item.get('priority') or ''),
            }
        )
    return result


async def _reply_permission(
    session: aiohttp.ClientSession,
    runtime: OpenCodeRuntime,
    directory: str,
    session_id: str,
    permission_id: str,
    approved: bool,
) -> None:
    response = 'once' if approved else 'reject'
    try:
        await _request_json(
            session,
            runtime,
            'POST',
            f'/session/{session_id}/permissions/{permission_id}',
            directory=directory,
            payload={'response': response},
        )
    except OpenCodeError:
        await _request_json(
            session,
            runtime,
            'POST',
            f'/permission/{permission_id}/reply',
            directory=directory,
            payload={'reply': response},
        )


async def _ensure_session(
    session: aiohttp.ClientSession,
    runtime: OpenCodeRuntime,
    chat_id: str,
    user_id: str,
    directory: str,
    agent: str,
    model: str,
) -> str:
    lock = _chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
        if not chat:
            raise OpenCodeError('The chat no longer exists or is not owned by this user.')
        binding = (chat.chat or {}).get('opencode')
        if isinstance(binding, dict) and binding.get('directory') == directory:
            session_id = str(binding.get('session_id') or '')
            if session_id:
                try:
                    await _request_json(
                        session,
                        runtime,
                        'GET',
                        f'/session/{session_id}',
                        directory=directory,
                        timeout=5,
                    )
                    return session_id
                except OpenCodeError:
                    pass

        created = await _request_json(
            session,
            runtime,
            'POST',
            '/session',
            directory=directory,
            payload={'title': chat.title or 'AuraPro Code'},
        )
        session_id = str((created or {}).get('id') or '')
        if not session_id:
            raise OpenCodeError('OpenCode did not return a session ID.')
        updated = {
            **(chat.chat or {}),
            'opencode': {
                'enabled': True,
                'session_id': session_id,
                'directory': directory,
                'agent': agent,
                'model': model,
                'updated_at': int(time.time()),
            },
        }
        await Chats.update_chat_by_id(chat_id, updated)
        return session_id


async def _persist_message(chat_id: str, message_id: str, content: str, **extra: Any) -> None:
    await Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        message_id,
        {'content': content, **extra},
    )


async def run_agent_chat(  # noqa: C901
    request, form_data: dict, user: Any, metadata: dict
) -> dict:
    if user.role != 'admin':
        raise OpenCodeError('Code mode is currently limited to administrators.')

    feature = (metadata.get('features') or {}).get('opencode')
    if not isinstance(feature, dict) or not feature.get('enabled'):
        raise OpenCodeError('Code mode configuration is missing.')

    chat_id = str(metadata.get('chat_id') or '')
    message_id = str(metadata.get('message_id') or '')
    if not chat_id or chat_id.startswith(('local:', 'channel:')) or not message_id:
        raise OpenCodeError('Code mode requires a saved private conversation.')

    directory = _normalize_directory(str(feature.get('directory') or ''))
    agent = _safe_identifier(feature.get('agent'), 'build')
    model_value = _safe_identifier(feature.get('model'))
    model_ref = _split_model(model_value)
    prompt = _message_text((metadata.get('user_message') or {}).get('content'))
    if not prompt:
        raise OpenCodeError('Code mode requires a text prompt.')

    runtime = _load_runtime()
    event_emitter = await get_event_emitter(metadata, update_db=False)
    event_caller = await get_event_call(metadata)
    if not event_emitter:
        raise OpenCodeError('The WebUI event channel is unavailable.')

    await event_emitter(
        {'type': 'status', 'data': {'action': 'opencode', 'description': 'Connecting to OpenCode', 'done': False}}
    )

    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        health = await _request_json(session, runtime, 'GET', '/global/health', timeout=5)
        if not health or not health.get('healthy'):
            raise OpenCodeError('OpenCode is not ready.')

        session_id = await _ensure_session(session, runtime, chat_id, user.id, directory, agent, model_value)
        _active_sessions[chat_id] = (session_id, directory)
        baseline = await _request_json(session, runtime, 'GET', f'/session/{session_id}/message', directory=directory)
        baseline_ids = {
            str((item.get('info') or {}).get('id'))
            for item in (baseline or [])
            if isinstance(item, dict) and (item.get('info') or {}).get('id')
        }

        event_response = await session.get(
            f'{runtime.url}/event',
            params={'directory': directory},
            auth=_auth(runtime),
            headers={'Accept': 'text/event-stream'},
        )
        if event_response.status >= 400:
            text = await event_response.text()
            raise OpenCodeError(_error_detail(text, event_response.status))

        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        event_task = asyncio.create_task(_read_sse(event_response, queue))
        latest_text = ''
        latest_open_code_message_id: str | None = None
        seen_activity = False
        idle = False
        last_poll = 0.0
        last_persist = 0.0
        tool_states: dict[str, tuple[str, bool]] = {}
        started_at = time.monotonic()

        try:
            await asyncio.sleep(0)
            await _request_json(
                session,
                runtime,
                'POST',
                f'/session/{session_id}/prompt_async',
                directory=directory,
                payload={
                    'agent': agent,
                    **({'model': {'providerID': model_ref[0], 'modelID': model_ref[1]}} if model_ref else {}),
                    'parts': [{'type': 'text', 'text': prompt}],
                },
                timeout=30,
            )
            await event_emitter(
                {
                    'type': 'status',
                    'data': {
                        'action': 'opencode',
                        'description': f'OpenCode · {"Planning" if agent == "plan" else "Working"}',
                        'done': False,
                    },
                }
            )

            while True:
                if time.monotonic() - started_at > _MAX_TURN_SECONDS:
                    raise OpenCodeError('OpenCode exceeded the maximum task time.')

                try:
                    raw_event = await asyncio.wait_for(queue.get(), timeout=_POLL_INTERVAL_SECONDS)
                    event = _event_payload(raw_event)
                    event_type = str(event.get('type') or '')
                    properties = event.get('properties') if isinstance(event.get('properties'), dict) else {}
                    event_session_id = _session_id_from_event(event)

                    if event_type == 'aurapro.sse.error':
                        log.warning('OpenCode SSE stream ended: %s', properties.get('message'))
                    elif event_session_id == session_id:
                        if event_type in {
                            'message.part.delta',
                            'message.part.updated',
                            'message.updated',
                            'session.diff',
                        }:
                            seen_activity = True
                        elif event_type == 'session.status':
                            status_value = properties.get('status') or {}
                            status_type = status_value.get('type') if isinstance(status_value, dict) else status_value
                            if status_type == 'busy':
                                seen_activity = True
                                idle = False
                            elif status_type == 'idle' and seen_activity:
                                idle = True
                        elif event_type == 'session.idle' and seen_activity:
                            idle = True
                        elif event_type == 'session.error':
                            error_value = properties.get('error') or properties.get('message') or 'OpenCode failed.'
                            if isinstance(error_value, dict):
                                error_value = error_value.get('message') or error_value.get('name') or str(error_value)
                            raise OpenCodeError(str(error_value))
                        elif event_type in {'permission.asked', 'permission.updated'}:
                            permission_id = str(properties.get('id') or properties.get('requestID') or '')
                            permission = str(properties.get('permission') or 'tool action')
                            patterns = properties.get('patterns') or []
                            detail = ', '.join(str(value) for value in patterns[:4])
                            message = f'OpenCode requests permission for {permission}.'
                            if detail:
                                message += f'\n\n{detail}'
                            approved = False
                            if event_caller:
                                result = await event_caller(
                                    {
                                        'type': 'confirmation',
                                        'data': {'title': 'OpenCode permission required', 'message': message},
                                    }
                                )
                                approved = result is True
                            if permission_id:
                                await _reply_permission(
                                    session,
                                    runtime,
                                    directory,
                                    session_id,
                                    permission_id,
                                    approved,
                                )
                            await event_emitter(
                                {
                                    'type': 'status',
                                    'data': {
                                        'action': 'opencode_permission',
                                        'description': f'OpenCode · {permission}',
                                        'done': True,
                                    },
                                }
                            )
                except TimeoutError:
                    pass

                now = time.monotonic()
                if now - last_poll >= _POLL_INTERVAL_SECONDS:
                    messages = await _request_json(
                        session,
                        runtime,
                        'GET',
                        f'/session/{session_id}/message',
                        directory=directory,
                        timeout=10,
                    )
                    text, tools, open_code_message_id = _assistant_snapshot(messages, baseline_ids)
                    if open_code_message_id:
                        seen_activity = True
                        latest_open_code_message_id = open_code_message_id
                    if text != latest_text:
                        latest_text = text
                        await event_emitter({'type': 'replace', 'data': {'content': latest_text}})
                    for part_index, part in enumerate(tools):
                        state_value = part.get('state') if isinstance(part.get('state'), dict) else {}
                        part_id = str(
                            part.get('id')
                            or part.get('callID')
                            or state_value.get('id')
                            or f'{part.get("tool") or "tool"}:{part_index}'
                        )
                        state = _tool_description(part)
                        if tool_states.get(part_id) != state:
                            tool_states[part_id] = state
                            await event_emitter(
                                {
                                    'type': 'status',
                                    'data': {
                                        'action': f'opencode_{part.get("tool") or "tool"}',
                                        'description': state[0],
                                        'done': state[1],
                                    },
                                }
                            )
                    if latest_text and now - last_persist >= 1.0:
                        await _persist_message(chat_id, message_id, latest_text, done=False)
                        last_persist = now

                    status_map = await _request_json(
                        session, runtime, 'GET', '/session/status', directory=directory, timeout=5
                    )
                    current_status = (status_map or {}).get(session_id) or {}
                    status_type = current_status.get('type') if isinstance(current_status, dict) else current_status
                    if status_type == 'busy':
                        seen_activity = True
                        idle = False
                    elif status_type == 'idle' and seen_activity:
                        idle = True
                    last_poll = now

                if idle and seen_activity and time.monotonic() - started_at > 0.8:
                    break

            diff_query = {'messageID': latest_open_code_message_id} if latest_open_code_message_id else None
            diffs, todos, vcs = await asyncio.gather(
                _request_json_optional(
                    session,
                    runtime,
                    'GET',
                    f'/session/{session_id}/diff',
                    [],
                    directory=directory,
                    query=diff_query,
                    timeout=15,
                ),
                _request_json_optional(session, runtime, 'GET', f'/session/{session_id}/todo', [], directory=directory),
                _request_json_optional(session, runtime, 'GET', '/vcs', {}, directory=directory),
            )
            if not latest_text:
                latest_text = 'OpenCode completed the task.'
            diff_count = len(diffs) if isinstance(diffs, list) else 0
            if diff_count:
                await event_emitter(
                    {
                        'type': 'status',
                        'data': {
                            'action': 'opencode_diff',
                            'description': f'OpenCode · {diff_count} file(s) changed',
                            'done': True,
                        },
                    }
                )
            await _persist_message(
                chat_id,
                message_id,
                latest_text,
                done=True,
                opencode={
                    'session_id': session_id,
                    'message_id': latest_open_code_message_id,
                    'directory': directory,
                    'agent': agent,
                    'diff_count': diff_count,
                    'model': model_value,
                    'todos': _normalize_todos(todos),
                    'vcs': _normalize_vcs(vcs),
                },
            )
            await event_emitter(
                {
                    'type': 'chat:completion',
                    'data': {
                        'content': latest_text,
                        'done': True,
                        'opencode': {
                            'session_id': session_id,
                            'message_id': latest_open_code_message_id,
                            'directory': directory,
                            'agent': agent,
                            'diff_count': diff_count,
                            'model': model_value,
                            'todos': _normalize_todos(todos),
                            'vcs': _normalize_vcs(vcs),
                        },
                    },
                }
            )
            return {'status': True, 'session_id': session_id, 'content': latest_text}
        except asyncio.CancelledError:
            try:
                await _request_json(
                    session,
                    runtime,
                    'POST',
                    f'/session/{session_id}/abort',
                    directory=directory,
                    timeout=5,
                )
            except Exception:
                pass
            raise
        finally:
            event_task.cancel()
            event_response.close()
            try:
                await event_task
            except (asyncio.CancelledError, Exception):
                pass
            _active_sessions.pop(chat_id, None)


async def abort_chat(chat_id: str, user_id: str) -> bool:
    chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
    binding = (chat.chat or {}).get('opencode') if chat else None
    active = _active_sessions.get(chat_id)
    session_id = active[0] if active else str((binding or {}).get('session_id') or '')
    directory = active[1] if active else str((binding or {}).get('directory') or '')
    if not session_id or not directory:
        return False
    runtime = _load_runtime()
    async with aiohttp.ClientSession(trust_env=False) as session:
        result = await _request_json(
            session,
            runtime,
            'POST',
            f'/session/{session_id}/abort',
            directory=_normalize_directory(directory),
            timeout=5,
        )
    return bool(result)


async def _get_chat_binding(chat_id: str, user_id: str) -> tuple[Any, dict, str, str]:
    chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
    binding = (chat.chat or {}).get('opencode') if chat else None
    if not isinstance(binding, dict):
        raise OpenCodeError('This conversation does not have an OpenCode session.')
    session_id = str(binding.get('session_id') or '')
    if not session_id:
        raise OpenCodeError('This conversation does not have an active OpenCode session.')
    directory = _normalize_directory(str(binding.get('directory') or ''))
    return chat, binding, session_id, directory


async def get_chat_diff(chat_id: str, user_id: str, message_id: str | None = None) -> list[dict]:
    _, _, session_id, directory = await _get_chat_binding(chat_id, user_id)
    runtime = _load_runtime()
    async with aiohttp.ClientSession(trust_env=False) as session:
        value = await _request_json(
            session,
            runtime,
            'GET',
            f'/session/{session_id}/diff',
            directory=directory,
            query={'messageID': _safe_identifier(message_id)} if _safe_identifier(message_id) else None,
            timeout=15,
        )
    return value if isinstance(value, list) else []


async def get_workspace(chat_id: str, user_id: str, message_id: str | None = None) -> dict:
    _, binding, session_id, directory = await _get_chat_binding(chat_id, user_id)
    runtime = _load_runtime()

    async def optional(path: str, fallback: Any, *, query: dict[str, str] | None = None) -> Any:
        try:
            return await _request_json(
                session,
                runtime,
                'GET',
                path,
                directory=directory,
                query=query,
                timeout=10,
            )
        except Exception as error:
            log.debug('Optional OpenCode workspace endpoint failed (%s): %s', path, error)
            return fallback

    diff_message_id = _safe_identifier(message_id)
    async with aiohttp.ClientSession(trust_env=False) as session:
        status_map, todos, vcs, files, diffs = await asyncio.gather(
            optional('/session/status', {}),
            optional(f'/session/{session_id}/todo', []),
            optional('/vcs', {}),
            optional('/file/status', []),
            optional(
                f'/session/{session_id}/diff',
                [],
                query={'messageID': diff_message_id} if diff_message_id else None,
            ),
        )
    current_status = status_map.get(session_id) if isinstance(status_map, dict) else None
    return {
        'session_id': session_id,
        'directory': directory,
        'agent': str(binding.get('agent') or 'build'),
        'model': str(binding.get('model') or ''),
        'status': current_status,
        'todos': _normalize_todos(todos),
        'vcs': _normalize_vcs(vcs),
        'files': files if isinstance(files, list) else [],
        'diffs': diffs if isinstance(diffs, list) else [],
    }


async def reset_chat_session(chat_id: str, user_id: str) -> bool:
    chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
    binding = (chat.chat or {}).get('opencode') if chat else None
    if not chat or not isinstance(binding, dict):
        return False
    session_id = str(binding.get('session_id') or '')
    directory_value = str(binding.get('directory') or '')
    if session_id and directory_value:
        runtime = _load_runtime()
        async with aiohttp.ClientSession(trust_env=False) as session:
            try:
                await _request_json(
                    session,
                    runtime,
                    'DELETE',
                    f'/session/{session_id}',
                    directory=_normalize_directory(directory_value),
                    timeout=10,
                )
            except OpenCodeError as error:
                log.info('OpenCode session was already unavailable during reset: %s', error)

    next_binding = {key: value for key, value in binding.items() if key not in {'session_id', 'updated_at'}}
    next_chat = {**(chat.chat or {}), 'opencode': next_binding}
    await Chats.update_chat_by_id(chat_id, next_chat)
    _active_sessions.pop(chat_id, None)
    return True


async def revert_chat_message(chat_id: str, user_id: str, message_id: str) -> bool:
    _, _, session_id, directory = await _get_chat_binding(chat_id, user_id)
    safe_message_id = _safe_identifier(message_id)
    if not safe_message_id:
        raise OpenCodeError('A valid OpenCode message ID is required.')
    runtime = _load_runtime()
    async with aiohttp.ClientSession(trust_env=False) as session:
        result = await _request_json(
            session,
            runtime,
            'POST',
            f'/session/{session_id}/revert',
            directory=directory,
            payload={'messageID': safe_message_id},
            timeout=15,
        )
    return bool(result)


async def unrevert_chat(chat_id: str, user_id: str) -> bool:
    _, _, session_id, directory = await _get_chat_binding(chat_id, user_id)
    runtime = _load_runtime()
    async with aiohttp.ClientSession(trust_env=False) as session:
        result = await _request_json(
            session,
            runtime,
            'POST',
            f'/session/{session_id}/unrevert',
            directory=directory,
            timeout=15,
        )
    return bool(result)
