from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import weakref
from typing import Any

from fastapi import HTTPException

from open_webui.models.config import Config
from open_webui.models.memories import Memories
from open_webui.utils.misc import add_or_update_system_message, get_content_from_message

log = logging.getLogger(__name__)

MEMORY_CONTEXT_OPEN = '<memory_context>'
MEMORY_CONTEXT_CLOSE = '</memory_context>'
CHAT_HISTORY_MEMORY_PATH = '_system/chat-history-summary'

_MEMORY_REVIEW_TASKS: set[asyncio.Task] = set()
_MEMORY_REVIEW_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_MEMORY_REVIEW_SEMAPHORE = asyncio.Semaphore(2)
_MEMORY_REVIEW_STATUS: dict[str, dict[str, Any]] = {}
_REVIEWED_MESSAGE_KEYS: dict[str, float] = {}

_EXPLICIT_MEMORY_RE = re.compile(
    r"(?:\bremember\b|\bdon't forget\b|\bdo not forget\b|\bforget\b|\bremove (?:that|this) memory\b|"
    r'\u8bb0\u4f4f|\u8bb0\u4e00\u4e0b|\u522b\u5fd8|\u4e0d\u8981\u5fd8|\u5fd8\u8bb0|'
    r'\u5220(?:\u9664|\u6389).{0,8}\u8bb0\u5fc6|\u4e0d\u518d\u8bb0\u5f97)',
    re.IGNORECASE,
)
_FORGET_MEMORY_RE = re.compile(
    r'(?:\bforget\b|\bremove (?:that|this) memory\b|\u5fd8\u8bb0|'
    r'\u5220(?:\u9664|\u6389).{0,8}\u8bb0\u5fc6|\u4e0d\u518d\u8bb0\u5f97)',
    re.IGNORECASE,
)
_DURABLE_MEMORY_RE = re.compile(
    r'(?:\bmy (?:(?:preferred|primary) )?(?:name|job|role|language|timezone|location|preference|goal|project|pronouns?)\b|'
    r'\bi (?:am|work|live|prefer|like|dislike|always|never|need you to|want you to)\b|'
    r'\u6211(?:\u53eb|\u662f|\u5728.{0,12}(?:\u5de5\u4f5c|\u751f\u6d3b|\u5c45\u4f4f)|'
    r'\u4ece\u4e8b|\u559c\u6b22|\u4e0d\u559c\u6b22|\u4e60\u60ef|\u5e0c\u671b\u4f60|'
    r'\u9700\u8981\u4f60|\u901a\u5e38|\u4e00\u76f4|\u4ece\u4e0d)|'
    r'\u4ee5\u540e(?:\u8bf7|\u4e0d\u8981|\u90fd|\u603b\u662f)|'
    r'\u6211\u7684(?:\u540d\u5b57|\u804c\u4e1a|\u5de5\u4f5c|\u8bed\u8a00|\u65f6\u533a|'
    r'\u6240\u5728\u5730|\u504f\u597d|\u4e60\u60ef|\u76ee\u6807|\u9879\u76ee))',
    re.IGNORECASE,
)
_SENSITIVE_SECRET_RE = re.compile(
    r'(?:\bpassword\b|\bpasscode\b|\bapi[ _-]?key\b|\baccess[ _-]?token\b|\bprivate[ _-]?key\b|'
    r'\u5bc6\u7801|\u53e3\u4ee4|\u9a8c\u8bc1\u7801|\u6388\u6743\u7801|\u8bbf\u95ee\u4ee4\u724c|\u79c1\u94a5)',
    re.IGNORECASE,
)
_TRANSLATION_FEATURES = {
    'translation',
    'rag_translation',
    'manuscript_translation',
    'manuscript_translation_mode',
    'document_translation',
    'document_translation_mode',
    'interpretation',
    'simultaneous',
}


def is_temporary_memory_chat(chat_id: str | None) -> bool:
    return bool(chat_id and chat_id.startswith(('local:', 'channel:')))


def is_chat_history_memory(memory) -> bool:
    meta = memory.meta if isinstance(getattr(memory, 'meta', None), dict) else {}
    return meta.get('created_by') == 'chat_history_review' or memory.path == CHAT_HISTORY_MEMORY_PATH


def _last_user_content(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if message.get('role') != 'user':
            continue
        content = get_content_from_message(message)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ''


def memory_review_candidate(
    messages: list[dict],
    features: dict,
    interval: int,
    *,
    automatic_enabled: bool,
) -> tuple[bool, str]:
    """Use cheap local checks before spending a second model inference."""
    content = _last_user_content(messages)
    if not content:
        return False, 'no_user_content'

    explicit = bool(_EXPLICIT_MEMORY_RE.search(content))
    if _FORGET_MEMORY_RE.search(content):
        return True, 'explicit_request'
    if _SENSITIVE_SECRET_RE.search(content):
        return False, 'sensitive_secret'
    if explicit:
        return True, 'explicit_request'
    if not automatic_enabled:
        return False, 'automatic_review_disabled'
    if any(features.get(name) for name in _TRANSLATION_FEATURES):
        return False, 'translation_mode'
    if _DURABLE_MEMORY_RE.search(content):
        return True, 'durable_signal'

    user_turns = sum(1 for message in messages or [] if message.get('role') == 'user')
    if user_turns and user_turns % max(1, interval) == 0 and len(content) >= 24:
        return True, 'periodic_review'
    return False, 'no_durable_signal'


def get_memory_review_status(user_id: str) -> dict[str, Any]:
    return dict(_MEMORY_REVIEW_STATUS.get(user_id) or {'state': 'idle'})


def _set_memory_review_status(user_id: str, **values) -> None:
    _MEMORY_REVIEW_STATUS[user_id] = {'updated_at': int(time.time()), **values}
    if len(_MEMORY_REVIEW_STATUS) > 1000:
        oldest = min(_MEMORY_REVIEW_STATUS, key=lambda key: _MEMORY_REVIEW_STATUS[key].get('updated_at', 0))
        _MEMORY_REVIEW_STATUS.pop(oldest, None)


def clean_memory_content(content: str | None) -> str:
    value = (content or '').strip()
    if not value:
        raise HTTPException(status_code=400, detail='Memory content cannot be empty')
    return value


def clean_memory_path(path: str | None) -> str | None:
    value = re.sub(r'/+', '/', (path or '').strip().strip('/'))
    if not value:
        return None
    parts = value.split('/')
    if any(part in {'', '.', '..'} for part in parts) or any(ord(char) < 32 for char in value):
        raise HTTPException(status_code=400, detail='Invalid memory path')
    return value


def memory_vector_text(content: str, path: str | None = None) -> str:
    path = clean_memory_path(path)
    return f'{path}\n{content}' if path else content


def memory_label(memory) -> str:
    return f'{memory.path}: {memory.content}' if memory.path else memory.content


def _path_parts(path: str | None) -> list[str]:
    return [part for part in (path or '').split('/') if part]


def _parent_path(path: str | None) -> str | None:
    parts = _path_parts(path)
    return '/'.join(parts[:-1]) if len(parts) > 1 else None


def _path_rank(memory_path: str | None, lookup_path: str | None) -> tuple | None:
    if not lookup_path:
        return None

    memory_path = clean_memory_path(memory_path)
    lookup_path = clean_memory_path(lookup_path)
    if not memory_path or not lookup_path:
        return None

    if memory_path == lookup_path:
        return (0, 0)
    if memory_path.startswith(f'{lookup_path}/'):
        return (1, len(_path_parts(memory_path)) - len(_path_parts(lookup_path)))
    if lookup_path.startswith(f'{memory_path}/'):
        return (2, len(_path_parts(lookup_path)) - len(_path_parts(memory_path)))
    if _parent_path(memory_path) and _parent_path(memory_path) == _parent_path(lookup_path):
        return (3, 0)

    memory_parts = set(_path_parts(memory_path))
    lookup_parts = set(_path_parts(lookup_path))
    shared = len(memory_parts & lookup_parts)
    if shared:
        return (4, -shared)
    if _path_parts(memory_path)[-1:] == _path_parts(lookup_path)[-1:]:
        return (5, 0)

    return None


def _memory_matches_query(memory, query: str) -> bool:
    value = query.strip().lower()
    if not value:
        return True
    return value in (memory.content or '').lower() or value in (memory.path or '').lower()


def search_memory_rows(
    memories: list,
    *,
    query: str | None = None,
    path: str | None = None,
    memory_id: str | None = None,
    memory_type: str = 'all',
    limit: int = 20,
) -> list:
    rows = list(memories or [])
    if memory_id:
        rows = [memory for memory in rows if memory.id == memory_id]
    if memory_type != 'all':
        rows = [memory for memory in rows if memory.type == memory_type]

    query = (query or '').strip()
    lookup_path = clean_memory_path(path)
    if lookup_path:
        basename = _path_parts(lookup_path)[-1] if _path_parts(lookup_path) else lookup_path

        def related(memory) -> bool:
            rank = _path_rank(memory.path, lookup_path)
            if rank is not None:
                return True
            haystack = f'{memory.path or ""}\n{memory.content or ""}'.lower()
            return lookup_path.lower() in haystack or basename.lower() in haystack

        rows = [memory for memory in rows if related(memory)]

    if query:
        rows = [memory for memory in rows if _memory_matches_query(memory, query)]

    def sort_key(memory):
        rank = _path_rank(memory.path, lookup_path) if lookup_path else None
        return rank if rank is not None else (9, 0), -(memory.updated_at or 0)

    return sorted(rows, key=sort_key)[: max(1, min(limit or 20, 100))]


def list_memory_path_groups(
    memories: list,
    *,
    query: str = '',
    memory_type: str = 'all',
    limit: int = 100,
) -> dict:
    rows = [
        memory
        for memory in (memories or [])
        if (memory_type == 'all' or memory.type == memory_type) and _memory_matches_query(memory, query)
    ]
    grouped: dict[tuple[str | None, str], dict] = {}
    for memory in rows:
        key = (memory.path, memory.type)
        group = grouped.setdefault(
            key,
            {
                'path': memory.path,
                'type': memory.type,
                'count': 0,
                'updated_at': 0,
                'children': [],
            },
        )
        group['count'] += 1
        group['updated_at'] = max(group['updated_at'], memory.updated_at or 0)

    paths = [path for path, _ in grouped if path]
    for group in grouped.values():
        path = group['path']
        if not path:
            continue
        prefix = f'{path}/'
        children = []
        for candidate in paths:
            if not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix) :]
            child = f'{prefix}{remainder.split("/", 1)[0]}'
            if child not in children:
                children.append(child)
        group['children'] = children[:20]

    groups = sorted(grouped.values(), key=lambda item: item['updated_at'], reverse=True)
    return {'paths': groups[: max(1, min(limit or 100, 500))], 'count': len(groups)}


def read_memory_path_rows(
    memories: list,
    *,
    path: str,
    memory_type: str = 'all',
    include_children: bool = True,
    limit: int = 50,
) -> dict:
    lookup_path = clean_memory_path(path)
    if not lookup_path:
        raise HTTPException(status_code=400, detail='Memory path is required')

    rows = [memory for memory in (memories or []) if memory_type == 'all' or memory.type == memory_type]
    path_set = {memory.path for memory in rows if memory.path}
    parents = [
        '/'.join(_path_parts(lookup_path)[:idx])
        for idx in range(1, len(_path_parts(lookup_path)))
        if '/'.join(_path_parts(lookup_path)[:idx]) in path_set
    ]
    children = sorted(
        {
            f'{lookup_path}/{memory.path[len(lookup_path) + 1 :].split("/", 1)[0]}'
            for memory in rows
            if memory.path and memory.path.startswith(f'{lookup_path}/')
        }
    )

    def selected(memory) -> bool:
        if memory.path == lookup_path:
            return True
        if memory.path in parents:
            return True
        return bool(include_children and memory.path and memory.path.startswith(f'{lookup_path}/'))

    selected_rows = [memory for memory in rows if selected(memory)]

    def sort_key(memory):
        if memory.path == lookup_path:
            return (0, 0, -(memory.updated_at or 0))
        if memory.path and memory.path.startswith(f'{lookup_path}/'):
            return (1, len(_path_parts(memory.path)), -(memory.updated_at or 0))
        return (2, -len(_path_parts(memory.path)), -(memory.updated_at or 0))

    return {
        'path': lookup_path,
        'parents': parents,
        'children': children[:50],
        'memories': sorted(selected_rows, key=sort_key)[: max(1, min(limit or 50, 100))],
    }


def memory_path_hints(query: str, memories: list, limit: int = 6) -> list[str]:
    lowered = (query or '').lower()
    if not lowered:
        return []

    hints: list[str] = []
    for memory in memories or []:
        path = memory.path
        if not path or path in hints:
            continue
        parts = _path_parts(path)
        last = parts[-1] if parts else path
        if path.lower() in lowered or last.lower() in lowered:
            hints.append(path)
        elif any(len(part) >= 3 and part.lower() in lowered for part in parts):
            hints.append(path)
        if len(hints) >= limit:
            break
    return hints


def validate_memory_operations(form_data) -> list[dict]:
    if not form_data.operations:
        raise HTTPException(status_code=400, detail='No memory operations provided')

    operations = []
    for operation in form_data.operations:
        op = operation.model_dump()
        action = op.get('action')

        if action == 'add':
            op['content'] = clean_memory_content(op.get('content'))
            op['type'] = Memories.normalize_memory_type(op.get('type'))
            op['path'] = clean_memory_path(op.get('path'))
        elif action == 'replace':
            if not op.get('id'):
                raise HTTPException(status_code=400, detail='Memory id is required for replace')
            op['content'] = clean_memory_content(op.get('content'))
            if op.get('type') is not None:
                op['type'] = Memories.normalize_memory_type(op.get('type'))
            op['path'] = clean_memory_path(op.get('path'))
        elif action == 'move':
            if not op.get('id'):
                raise HTTPException(status_code=400, detail='Memory id is required for move')
            op['path'] = clean_memory_path(op.get('path'))
        elif action == 'remove':
            if not op.get('id'):
                raise HTTPException(status_code=400, detail='Memory id is required for remove')
        else:
            raise HTTPException(status_code=400, detail=f'Unsupported memory operation: {action}')

        operations.append(op)

    return operations


def model_allows_memory(model: dict | None) -> bool:
    model = model if isinstance(model, dict) else {}
    model_info = model.get('info') if isinstance(model.get('info'), dict) else {}
    model_meta = model_info.get('meta') if isinstance(model_info.get('meta'), dict) else {}
    model_capabilities = model_meta.get('capabilities') if isinstance(model_meta.get('capabilities'), dict) else {}
    return model_capabilities.get('memory', True)


async def add_memory_context(
    request,
    form_data: dict,
    user,
    model: dict | None = None,
    *,
    features: dict | None = None,
    chat_id: str | None = None,
):
    if not model_allows_memory(model) or is_temporary_memory_chat(chat_id):
        return form_data

    features = features if isinstance(features, dict) else {'memory': True}
    use_saved_memories = bool(features.get('memory'))
    use_chat_history = bool(features.get('chat_history_memory'))
    if not use_saved_memories and not use_chat_history:
        return form_data

    user_messages = []
    for message in reversed(form_data.get('messages', [])):
        if message.get('role') != 'user':
            continue

        content = get_content_from_message(message)
        if isinstance(content, str) and content.strip():
            user_messages.append(content.strip())

        if len(user_messages) >= 7:
            break

    query = '\n\n'.join(reversed(user_messages))[-4000:]
    if not query:
        return form_data

    all_memories = await Memories.get_memories_by_user_id(user.id) or []
    history_memories = [memory for memory in all_memories if is_chat_history_memory(memory)]
    saved_memories = [memory for memory in all_memories if not is_chat_history_memory(memory)]

    results = None
    if use_saved_memories and any(memory.type == 'context' for memory in saved_memories):
        try:
            from open_webui.routers.memories import QueryMemoryForm, query_memory

            results = await query_memory(request, QueryMemoryForm(content=query, k=16), user)
        except Exception as e:
            log.debug('Memory vector query failed: %s', e)

    sections = {'user': [], 'neighborhood': [], 'context': [], 'history': []}
    seen_ids = set()

    if use_saved_memories:

        def pinned_first(memory):
            meta = memory.meta if isinstance(memory.meta, dict) else {}
            return (not bool(meta.get('pinned')), memory.path or '', -(memory.updated_at or 0))

        for memory in sorted(
            [memory for memory in saved_memories if memory.type == 'user'],
            key=pinned_first,
        )[:100]:
            seen_ids.add(memory.id)
            sections['user'].append(memory_label(memory))

        for memory in sorted(
            [
                memory
                for memory in saved_memories
                if memory.type == 'context' and isinstance(memory.meta, dict) and memory.meta.get('pinned')
            ],
            key=pinned_first,
        )[:20]:
            seen_ids.add(memory.id)
            sections['neighborhood'].append(memory_label(memory))

        for hint in memory_path_hints(query, saved_memories):
            for memory in search_memory_rows(
                saved_memories,
                path=hint,
                memory_type='context',
                limit=4,
            ):
                if memory.id in seen_ids:
                    continue
                seen_ids.add(memory.id)
                sections['neighborhood'].append(memory_label(memory))

        allowed_ids = {memory.id for memory in saved_memories}
        if results and hasattr(results, 'documents') and results.documents:
            for doc_idx, doc in enumerate(results.documents[0]):
                if not doc:
                    continue

                metadata = {}
                if results.metadatas and results.metadatas[0] and len(results.metadatas[0]) > doc_idx:
                    metadata = results.metadatas[0][doc_idx] or {}

                memory_id = None
                if results.ids and results.ids[0] and len(results.ids[0]) > doc_idx:
                    memory_id = results.ids[0][doc_idx]
                if not memory_id or memory_id not in allowed_ids or memory_id in seen_ids:
                    continue
                seen_ids.add(memory_id)

                content = str(doc)
                if metadata.get('path') and content.startswith(f'{metadata.get("path")}\n'):
                    content = content[len(metadata.get('path')) + 1 :]
                label = f'{metadata.get("path")}: {content}' if metadata.get('path') else content
                sections[Memories.normalize_memory_type(metadata.get('type'))].append(label)

    if use_chat_history and history_memories:
        summary = max(history_memories, key=lambda memory: memory.updated_at or 0)
        if summary.content.strip():
            sections['history'].append(summary.content.strip())

    parts = []
    if sections['user']:
        parts.append('[User Memory]\n' + '\n'.join(f'- {memory}' for memory in sections['user']))
    if sections['history']:
        parts.append('[Chat History Summary]\n' + '\n'.join(sections['history']))
    if sections['neighborhood']:
        parts.append('[Memory Neighborhood]\n' + '\n'.join(f'- {memory}' for memory in sections['neighborhood']))
    if sections['context']:
        parts.append('[Relevant Context]\n' + '\n'.join(f'- {memory}' for memory in sections['context']))
    if not parts:
        return form_data

    config = await Config.get_many('memories.user_char_limit', 'memories.context_char_limit')
    try:
        user_limit = max(250, int(config.get('memories.user_char_limit') or 2000))
    except Exception:
        user_limit = 2000
    try:
        context_limit = max(250, int(config.get('memories.context_char_limit') or 2000))
    except Exception:
        context_limit = 2000

    messages = form_data['messages']
    if messages and messages[0].get('role') == 'system':
        content = messages[0].get('content', '')
        if isinstance(content, str) and MEMORY_CONTEXT_OPEN in content:
            start = content.find(MEMORY_CONTEXT_OPEN)
            end = content.find(MEMORY_CONTEXT_CLOSE, start)
            if end != -1:
                messages[0]['content'] = (content[:start] + content[end + len(MEMORY_CONTEXT_CLOSE) :]).strip()

    user_parts = [part for part in parts if part.startswith('[User Memory]')]
    context_parts = [part for part in parts if not part.startswith('[User Memory]')]
    rendered = '\n\n'.join(
        [
            '\n\n'.join(user_parts)[:user_limit],
            '\n\n'.join(context_parts)[:context_limit],
        ]
    ).strip()
    if not rendered:
        return form_data

    memory_context = f'{MEMORY_CONTEXT_OPEN}\n{rendered}\n{MEMORY_CONTEXT_CLOSE}'
    form_data['messages'] = add_or_update_system_message(memory_context, messages, append=True)
    return form_data


async def review_memory_after_turn(
    *,
    request,
    user,
    model: dict | None,
    metadata: dict,
    form_data: dict,
    assistant_message: dict,
    messages: list[dict],
) -> None:
    if not model_allows_memory(model):
        _set_memory_review_status(user.id, state='skipped', reason='model_disallows_memory')
        return

    features = metadata.get('features') or {}
    saved_memory_enabled = bool(features.get('memory'))
    chat_history_enabled = bool(features.get('chat_history_memory'))
    if not saved_memory_enabled and not chat_history_enabled:
        return

    chat_id = metadata.get('chat_id') or ''
    if is_temporary_memory_chat(chat_id):
        _set_memory_review_status(user.id, state='skipped', reason='temporary_chat')
        return

    assistant_content = assistant_message.get('content', '')
    if not isinstance(assistant_content, str) or not assistant_content.strip():
        _set_memory_review_status(user.id, state='skipped', reason='empty_assistant_response')
        return

    config = await Config.get_many(
        'memories.background_review.enable',
        'memories.review_interval_turns',
        'memories.review_model',
        'memories.update_notifications.enable',
    )
    automatic_enabled = bool(config.get('memories.background_review.enable'))
    try:
        interval = max(1, int(config.get('memories.review_interval_turns') or 6))
    except Exception:
        interval = 6

    should_review, reason = memory_review_candidate(
        messages,
        features,
        interval,
        automatic_enabled=automatic_enabled,
    )
    if not should_review:
        _set_memory_review_status(user.id, state='skipped', reason=reason)
        return

    message_key = ':'.join(
        [
            user.id,
            chat_id,
            str(metadata.get('message_id') or metadata.get('user_message_id') or len(messages)),
        ]
    )
    now = time.monotonic()
    if message_key in _REVIEWED_MESSAGE_KEYS:
        _set_memory_review_status(user.id, state='skipped', reason='duplicate_completion')
        return
    _REVIEWED_MESSAGE_KEYS[message_key] = now
    if len(_REVIEWED_MESSAGE_KEYS) > 2000:
        cutoff = now - 3600
        for key, timestamp in list(_REVIEWED_MESSAGE_KEYS.items()):
            if timestamp < cutoff:
                _REVIEWED_MESSAGE_KEYS.pop(key, None)

    reviewer_model = str(config.get('memories.review_model') or '').strip() or None
    notify = config.get('memories.update_notifications.enable') is not False
    _set_memory_review_status(user.id, state='queued', reason=reason, chat_id=chat_id)

    task = asyncio.create_task(
        _run_memory_review(
            request=request,
            user=user,
            model=model,
            metadata=metadata,
            form_data=form_data,
            assistant_message=assistant_message,
            messages=messages,
            saved_memory_enabled=saved_memory_enabled,
            chat_history_enabled=chat_history_enabled,
            reviewer_model=reviewer_model,
            notify=notify,
            trigger_reason=reason,
        )
    )
    _MEMORY_REVIEW_TASKS.add(task)

    def finish_review(done_task):
        _MEMORY_REVIEW_TASKS.discard(done_task)
        try:
            done_task.result()
        except Exception:
            log.exception('Memory review task failed')

    task.add_done_callback(finish_review)


async def _run_memory_review(
    *,
    request,
    user,
    model: dict | None,
    metadata: dict,
    form_data: dict,
    assistant_message: dict,
    messages: list[dict],
    saved_memory_enabled: bool,
    chat_history_enabled: bool,
    reviewer_model: str | None,
    notify: bool,
    trigger_reason: str,
) -> None:
    lock = _MEMORY_REVIEW_LOCKS.get(user.id)
    if lock is None:
        lock = asyncio.Lock()
        _MEMORY_REVIEW_LOCKS[user.id] = lock

    try:
        async with _MEMORY_REVIEW_SEMAPHORE:
            async with lock:
                _set_memory_review_status(
                    user.id,
                    state='running',
                    reason=trigger_reason,
                    chat_id=metadata.get('chat_id'),
                )
                result = await _review_memory(
                    request=request,
                    user=user,
                    model=model,
                    metadata=metadata,
                    form_data=form_data,
                    assistant_message=assistant_message,
                    messages=messages,
                    saved_memory_enabled=saved_memory_enabled,
                    chat_history_enabled=chat_history_enabled,
                    reviewer_model=reviewer_model,
                    trigger_reason=trigger_reason,
                )

        _set_memory_review_status(
            user.id,
            state='completed',
            reason=trigger_reason,
            chat_id=metadata.get('chat_id'),
            saved_changes=result['saved_changes'],
            history_updated=result['history_updated'],
            model=result['model'],
        )
        if notify and result['saved_changes']:
            await _emit_memory_notification(
                metadata,
                notification_type='success',
                key='Memory updated automatically',
                fallback='Memory updated automatically',
            )
    except Exception as e:
        log.warning('Automatic memory review failed for user %s: %s', user.id, e, exc_info=True)
        _set_memory_review_status(
            user.id,
            state='error',
            reason=trigger_reason,
            chat_id=metadata.get('chat_id'),
            error=str(e)[:500],
        )
        if trigger_reason == 'explicit_request':
            await _emit_memory_notification(
                metadata,
                notification_type='warning',
                key='Automatic memory review failed',
                fallback='Automatic memory review failed',
            )


async def _emit_memory_notification(
    metadata: dict,
    *,
    notification_type: str,
    key: str,
    fallback: str,
) -> None:
    if not metadata.get('user_id') or not metadata.get('chat_id') or not metadata.get('message_id'):
        return
    if is_temporary_memory_chat(metadata.get('chat_id')):
        return

    try:
        from open_webui.socket.main import get_event_emitter

        event_emitter = await get_event_emitter(metadata, update_db=False)
        await event_emitter(
            {
                'type': 'notification',
                'data': {
                    'type': notification_type,
                    'key': key,
                    'content': fallback,
                },
            }
        )
    except Exception as e:
        log.debug('Unable to emit memory notification: %s', e)


async def _review_memory(
    *,
    request,
    user,
    model: dict | None,
    metadata: dict,
    form_data: dict,
    assistant_message: dict,
    messages: list[dict],
    saved_memory_enabled: bool,
    chat_history_enabled: bool,
    reviewer_model: str | None,
    trigger_reason: str,
) -> dict[str, Any]:
    existing_memories = await Memories.get_memories_by_user_id(user.id) or []
    saved_memories = [memory for memory in existing_memories if not is_chat_history_memory(memory)]
    history_memories = [memory for memory in existing_memories if is_chat_history_memory(memory)]
    history_memory = max(history_memories, key=lambda memory: memory.updated_at or 0) if history_memories else None

    existing_lines = [
        f'- id={memory.id} type={memory.type} path={memory.path or ""} content={memory.content}'
        for memory in sorted(saved_memories, key=lambda item: item.updated_at or 0, reverse=True)[:80]
    ]

    transcript_lines = []
    for message in messages[-14:]:
        role = message.get('role', '')
        content = message.get('content', '')
        if not isinstance(content, str):
            content = get_content_from_message(message)
        content = content.strip()
        if role not in {'user', 'assistant'} or not content:
            continue
        if len(content) > 1400:
            content = f'{content[:900]}\n...(truncated)...\n{content[-300:]}'
        transcript_lines.append(f'{role}: {content}')

    assistant_content = assistant_message.get('content', '')
    if not isinstance(assistant_content, str):
        assistant_content = get_content_from_message(assistant_message)
    assistant_final = assistant_content.strip()
    if assistant_final:
        if len(assistant_final) > 1400:
            assistant_final = f'{assistant_final[:900]}\n...(truncated)...\n{assistant_final[-300:]}'
        final_line = f'assistant: {assistant_final}'
        if not transcript_lines or transcript_lines[-1] != final_line:
            transcript_lines.append(final_line)

    default_model_id = model.get('id') if isinstance(model, dict) else form_data.get('model')
    model_id = reviewer_model or default_model_id
    if not model_id:
        raise ValueError('No model is available for memory review')

    review = await _generate_memory_review(
        request=request,
        user=user,
        model_id=model_id,
        metadata=metadata,
        existing_text='\n'.join(existing_lines) if existing_lines else '(none)',
        existing_ids={memory.id for memory in saved_memories},
        previous_history_summary=history_memory.content if history_memory else '',
        transcript='\n\n'.join(transcript_lines),
        saved_memory_enabled=saved_memory_enabled,
        chat_history_enabled=chat_history_enabled,
        trigger_reason=trigger_reason,
    )

    saved_changes = 0
    operations = review.get('operations') if saved_memory_enabled else []
    if operations:
        from open_webui.routers.memories import UpdateMemoriesForm, update_memories

        results = await update_memories(
            request,
            UpdateMemoriesForm(operations=operations, source='background_review'),
            user,
        )
        saved_changes = sum(1 for result in results if result.get('status') in {'created', 'updated', 'deleted'})

    history_updated = False
    history_summary = review.get('history_summary') if chat_history_enabled else None
    if isinstance(history_summary, str):
        history_summary = history_summary.strip()[:4000]
    if history_summary and history_summary != (history_memory.content.strip() if history_memory else ''):
        from open_webui.routers.memories import UpdateMemoriesForm, update_memories

        history_operation = (
            {
                'action': 'replace',
                'id': history_memory.id,
                'content': history_summary,
                'type': 'context',
                'path': CHAT_HISTORY_MEMORY_PATH,
            }
            if history_memory
            else {
                'action': 'add',
                'content': history_summary,
                'type': 'context',
                'path': CHAT_HISTORY_MEMORY_PATH,
            }
        )
        results = await update_memories(
            request,
            UpdateMemoriesForm(operations=[history_operation], source='chat_history_review'),
            user,
        )
        history_updated = any(result.get('status') in {'created', 'updated'} for result in results)

    return {
        'saved_changes': saved_changes,
        'history_updated': history_updated,
        'model': model_id,
    }


def _balanced_json_objects(value: str) -> list[str]:
    objects = []
    start = None
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(value):
        if start is None:
            if char == '{':
                start = index
                depth = 1
            continue

        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                objects.append(value[start : index + 1])
                start = None
    return objects


def parse_memory_review_response(content: Any) -> dict[str, Any] | None:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        return None

    without_thoughts = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
    candidates = re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', without_thoughts, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(_balanced_json_objects(without_thoughts))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            repaired = re.sub(r',\s*([}\]])', r'\1', candidate)
            try:
                parsed = json.loads(repaired)
            except Exception:
                continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _sanitize_memory_operations(operations: Any, existing_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(operations, list):
        return []

    sanitized = []
    for operation in operations[:8]:
        if not isinstance(operation, dict):
            continue
        action = operation.get('action')
        if action not in {'add', 'replace', 'move', 'remove'}:
            continue

        item: dict[str, Any] = {'action': action}
        if action in {'replace', 'move', 'remove'}:
            memory_id = operation.get('id')
            if not isinstance(memory_id, str) or memory_id not in existing_ids:
                continue
            item['id'] = memory_id

        if action in {'add', 'replace'}:
            content = operation.get('content')
            if not isinstance(content, str) or not content.strip():
                continue
            item['content'] = content.strip()[:2000]
            item['type'] = 'user' if operation.get('type') == 'user' else 'context'

        if action in {'add', 'replace', 'move'}:
            path = operation.get('path')
            if isinstance(path, str) and path.strip():
                try:
                    item['path'] = clean_memory_path(path[:300])
                except HTTPException:
                    item['path'] = None
            else:
                item['path'] = None

        sanitized.append(item)
    return sanitized


async def _generate_memory_review(
    *,
    request,
    user,
    model_id: str,
    metadata: dict,
    existing_text: str,
    existing_ids: set[str],
    previous_history_summary: str,
    transcript: str,
    saved_memory_enabled: bool,
    chat_history_enabled: bool,
    trigger_reason: str,
) -> dict[str, Any]:
    from open_webui.utils.chat import generate_chat_completion

    review_prompt = f"""Review this completed conversation for durable user memory.

Enabled outputs:
- Saved memories: {saved_memory_enabled}
- Chat history summary: {chat_history_enabled}
- Trigger: {trigger_reason}

Saved-memory rules:
- Learn only from user statements, never from assistant claims.
- Save stable preferences, identity details, standing instructions, long-term goals, projects, and relationships.
- Do not save transient tasks, copied source text, translations, assistant output, or unsupported guesses.
- Never save passwords, tokens, credentials, private keys, or authentication codes.
- Do not infer sensitive traits. Health, religion, politics, sexuality, race, financial, or legal details may only be saved when the user explicitly asks to remember them.
- Resolve conflicts by replacing or removing an existing memory. Do not create duplicates.
- Use at most 8 operations. Use exact existing IDs for replace, move, and remove.

Chat-history-summary rules:
- Maintain a concise, useful profile synthesized from prior summary and this conversation.
- Include durable themes that can improve future conversations, but omit secrets and sensitive inferences.
- Do not mention that it is a summary. Preserve useful prior details unless corrected.
- Return null when chat history summary is disabled.

Return only JSON:
{{"operations":[
  {{"action":"add","type":"user|context","path":"optional/path","content":"..."}},
  {{"action":"replace","id":"existing-id","type":"user|context","path":"optional/path","content":"..."}},
  {{"action":"move","id":"existing-id","path":"optional/path"}},
  {{"action":"remove","id":"existing-id"}}
],"history_summary":"updated summary or null"}}

Existing saved memories:
{existing_text}

Previous chat history summary:
{previous_history_summary or '(none)'}

Conversation:
{transcript}
"""

    response = await generate_chat_completion(
        request,
        form_data={
            'model': model_id,
            'messages': [
                {
                    'role': 'system',
                    'content': 'You are a private memory reviewer. Return one valid JSON object and no commentary.',
                },
                {'role': 'user', 'content': review_prompt},
            ],
            'stream': False,
            'metadata': {
                'task': 'memory_review',
                'chat_id': metadata.get('chat_id'),
                'message_id': metadata.get('message_id'),
            },
        },
        user=user,
    )

    if not isinstance(response, dict) or not response.get('choices'):
        raise ValueError('Memory reviewer returned no choices')

    response_message = response.get('choices', [{}])[0].get('message', {})
    contents = [response_message.get('content'), response_message.get('reasoning_content')]
    parsed = None
    for content in contents:
        parsed = parse_memory_review_response(content)
        if parsed is not None:
            break
    if parsed is None:
        raise ValueError('Memory reviewer returned invalid JSON')

    history_summary = parsed.get('history_summary')
    if history_summary is not None and not isinstance(history_summary, str):
        history_summary = None

    return {
        'operations': _sanitize_memory_operations(parsed.get('operations'), existing_ids),
        'history_summary': history_summary,
    }
