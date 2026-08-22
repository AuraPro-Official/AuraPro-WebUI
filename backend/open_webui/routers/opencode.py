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

router = APIRouter()


class DirectoryForm(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)


class MessageActionForm(BaseModel):
    message_id: str = Field(min_length=1, max_length=256)


def _require_admin(user) -> None:
    if user.role != 'admin':
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Code mode is currently limited to administrators.',
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
