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
    get_agent_activity,
    control_agent_session,
)
from open_webui.utils.auth import get_verified_user
from pydantic import BaseModel, Field
from open_webui.services import pi_rpc
from urllib.parse import urlsplit
import json
import os
from pathlib import Path
from typing import Literal
from fastapi import Request

router = APIRouter()


class DirectoryForm(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)


class MessageActionForm(BaseModel):
    message_id: str = Field(min_length=1, max_length=256)


class AgentControlForm(BaseModel):
    action: Literal['reply', 'steer', 'follow_up', 'clear_queue', 'auto_compaction', 'compact']
    id: str | None = Field(default=None, max_length=256)
    message: str = Field(default='', max_length=20000)
    value: str | None = Field(default=None, max_length=20000)
    confirmed: bool | None = None
    cancelled: bool = False
    enabled: bool | None = None


@router.get('/chats/{chat_id}/activity')
async def agent_activity(chat_id: str, after: int = Query(default=0, ge=0), user=Depends(get_verified_user)):
    _require_admin(user)
    try:
        return await get_agent_activity(chat_id, user.id, after)
    except OpenCodeError as error:
        raise HTTPException(400, str(error)) from error


@router.post('/chats/{chat_id}/control')
async def agent_control(request: Request, chat_id: str, form_data: AgentControlForm, user=Depends(get_verified_user)):
    _require_admin(user)
    try:
        return await control_agent_session(
            request, chat_id, user, form_data.action, form_data.model_dump(exclude_none=True)
        )
    except OpenCodeError as error:
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
