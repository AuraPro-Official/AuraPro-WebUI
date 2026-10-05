from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from open_webui.services.opencode_agent import (
    OpenCodeError,
    abort_chat,
    get_capabilities,
    get_chat_diff,
    get_status,
    get_workspace,
    reset_chat_session,
    revert_chat_message,
    unrevert_chat,
    validate_directory,
)
from open_webui.utils.auth import get_verified_user
from pydantic import BaseModel, Field
from open_webui.services import pi_rpc
from urllib.parse import urlsplit
import json
import os
from pathlib import Path

router = APIRouter()


class DirectoryForm(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)


class MessageActionForm(BaseModel):
    message_id: str = Field(min_length=1, max_length=256)


class PiModelForm(BaseModel):
    provider: str = Field(default='aurapro', pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key: str = Field(default='', max_length=4096)
    api: str = Field(default='openai-completions')
    vision: bool = False
    context_window: int = Field(default=32768, ge=4096, le=2000000)


@router.post('/pi/model')
async def configure_pi_model(form_data: PiModelForm, user=Depends(get_verified_user)):
    _require_admin(user)
    parsed = urlsplit(form_data.base_url)
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise HTTPException(400, 'Enter a valid API base URL.')
    if form_data.api not in {'openai-completions', 'openai-responses', 'anthropic-messages', 'google-generative-ai'}:
        raise HTTPException(400, 'Unsupported PI API protocol.')
    if form_data.api_key.startswith(('!', '$')):
        raise HTTPException(400, 'Enter a literal API key.')
    try:
        runtime = pi_rpc.descriptor()
        # Model changes affect only idle sessions; active tasks retain their credentials.
        if any(session.busy for session in pi_rpc._sessions.values()):
            raise RuntimeError('Stop active PI tasks before changing models.')
        directory = Path(runtime['agentDir'])
        file = directory / 'models.json'
        data = json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}
        data.setdefault('providers', {})[form_data.provider] = {
            'baseUrl': form_data.base_url.rstrip('/'),
            'api': form_data.api,
            'apiKey': form_data.api_key or 'local',
            'models': [
                {
                    'id': form_data.model,
                    'name': form_data.model,
                    'input': ['text', 'image'] if form_data.vision else ['text'],
                    'contextWindow': form_data.context_window,
                    'maxTokens': min(8192, form_data.context_window // 4),
                }
            ],
        }
        temporary = file.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(temporary, 0o600)
        temporary.replace(file)
        await pi_rpc.shutdown()
        return {'configured': True, 'model': f'{form_data.provider}/{form_data.model}'}
    except (RuntimeError, OSError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


@router.post('/pi/extensions/check')
async def check_pi_extensions(form_data: DirectoryForm, user=Depends(get_verified_user)):
    _require_admin(user)
    probe = None
    try:
        directory = pi_rpc.descriptor()
        normalized = str(Path(form_data.directory).resolve(strict=True))
        if not Path(normalized).is_dir():
            raise RuntimeError('Select a valid workspace.')
        import uuid

        probe = pi_rpc.PiSession(directory, str(uuid.uuid4()), normalized, interactive=False)
        await probe.start()
        results = []
        for name in ('browser_tabs', 'find_roots'):
            probe.probe = {}
            await probe.command('prompt', message=f'/aurapro-check {name}', timeout=30)
            results.append({'tool': name, **probe.probe})
        return {
            'tools': probe.diagnostics.get('tools', []),
            'commands': probe.diagnostics.get('commands', []),
            'checks': results,
        }
    except (RuntimeError, OSError, ValueError, TimeoutError) as error:
        raise HTTPException(400, str(error)) from error
    finally:
        if probe:
            await probe.close()


def _require_admin(user) -> None:
    if user.role != 'admin':
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Code Agent is currently limited to administrators.',
        )


@router.get('/status')
async def opencode_status(
    chat_id: str | None = Query(default=None),
    user=Depends(get_verified_user),
):
    _require_admin(user)
    return await get_status(chat_id=chat_id, user_id=user.id)


@router.post('/directory/validate')
async def opencode_validate_directory(
    form_data: DirectoryForm,
    user=Depends(get_verified_user),
):
    _require_admin(user)
    try:
        return await validate_directory(form_data.directory)
    except OpenCodeError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.post('/chats/{chat_id}/abort')
async def opencode_abort_chat(chat_id: str, user=Depends(get_verified_user)):
    _require_admin(user)
    try:
        return {'aborted': await abort_chat(chat_id, user.id)}
    except OpenCodeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.get('/chats/{chat_id}/diff')
async def opencode_chat_diff(
    chat_id: str,
    message_id: str | None = Query(default=None, max_length=256),
    user=Depends(get_verified_user),
):
    _require_admin(user)
    try:
        return {'items': await get_chat_diff(chat_id, user.id, message_id=message_id)}
    except OpenCodeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.post('/capabilities')
async def opencode_capabilities(
    form_data: DirectoryForm,
    user=Depends(get_verified_user),
):
    _require_admin(user)
    try:
        return await get_capabilities(form_data.directory)
    except OpenCodeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.get('/chats/{chat_id}/workspace')
async def opencode_chat_workspace(
    chat_id: str,
    message_id: str | None = Query(default=None, max_length=256),
    user=Depends(get_verified_user),
):
    _require_admin(user)
    try:
        return await get_workspace(chat_id, user.id, message_id=message_id)
    except OpenCodeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.post('/chats/{chat_id}/session/reset')
async def opencode_reset_chat_session(chat_id: str, user=Depends(get_verified_user)):
    _require_admin(user)
    try:
        return {'reset': await reset_chat_session(chat_id, user.id)}
    except OpenCodeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.post('/chats/{chat_id}/revert')
async def opencode_revert_chat_message(
    chat_id: str,
    form_data: MessageActionForm,
    user=Depends(get_verified_user),
):
    _require_admin(user)
    try:
        return {'reverted': await revert_chat_message(chat_id, user.id, form_data.message_id)}
    except OpenCodeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


@router.post('/chats/{chat_id}/unrevert')
async def opencode_unrevert_chat(chat_id: str, user=Depends(get_verified_user)):
    _require_admin(user)
    try:
        return {'restored': await unrevert_chat(chat_id, user.id)}
    except OpenCodeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
