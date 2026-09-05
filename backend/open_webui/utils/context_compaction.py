from __future__ import annotations

import json
import logging
from typing import Any

from fastapi.responses import JSONResponse
from open_webui.models.chats import Chats
from open_webui.models.config import Config
from open_webui.utils.misc import get_content_from_message, get_last_user_message, get_message_list
from open_webui.utils.task import (
    get_task_model_id,
    prompt_template,
    prompt_variables_template,
    replace_messages_variable,
    replace_prompt_variable,
)

log = logging.getLogger(__name__)

DEFAULT_CONTEXT_SIZE = 16384
DEFAULT_CONTEXT_COMPACTION_PERCENT = 75
SUMMARY_RECENT_MESSAGE_LIMIT = 4
HARD_TRUNCATION_FEATURES = (
    'translation',
    'manuscript_translation',
    'manuscript_translation_mode',
    'document_translation',
    'document_translation_mode',
    'interpretation',
    'simultaneous',
    'rag_translation',
)

DEFAULT_CONTEXT_COMPACTION_PROMPT = """### Task:
Summarize the conversation history that will be compacted out of the active chat context.

### Instructions:
- Preserve key decisions, user preferences, and constraints.
- Preserve files, artifacts, tool results, and code changes that matter going forward.
- Preserve the current task state, unresolved questions, and next steps.
- Be factual and specific. Do not invent details.
- Keep the summary concise, but complete enough for the assistant to continue without the removed messages.

### Previous Summary:
{{PREVIOUS_SUMMARY}}

### Messages Being Compacted:
{{COMPACTED_MESSAGES}}

### Recent Messages Kept In Context:
{{RECENT_MESSAGES}}"""


async def compact_messages_for_request(
    request,
    user,
    messages: list[dict],
    metadata: dict,
    model_id: str,
    models: dict,
    system_prompt: str = '',
) -> tuple[list[dict], str | None, bool]:
    features = metadata.get('features') if isinstance(metadata.get('features'), dict) else {}
    config = await _load_enabled_config(features)
    if config is None:
        return messages, None, False

    system_messages, conversation_messages = _split_system_messages(messages)
    conversation_messages, previous_summary = _apply_latest_summary_checkpoint(conversation_messages)
    active_messages = [*system_messages, *conversation_messages]
    token_threshold = _resolve_token_threshold(config, metadata, model_id, models)
    if (
        not _exceeds_token_threshold(
            conversation_messages,
            system_prompt,
            previous_summary,
            token_threshold,
        )
        or len(conversation_messages) <= 3
    ):
        return active_messages, previous_summary, False

    boundary = _find_compaction_boundary(conversation_messages)
    compacted_messages = conversation_messages[:boundary]
    recent_messages = conversation_messages[boundary:]
    if not compacted_messages or not recent_messages:
        return active_messages, previous_summary, False

    event_emitter = None
    if metadata.get('chat_id') and metadata.get('message_id'):
        from open_webui.socket.main import get_event_emitter

        event_emitter = await get_event_emitter(metadata)

    if event_emitter:
        await event_emitter(
            {
                'type': 'context_compaction',
                'data': {
                    'action': 'context_compaction',
                    'description': 'Compacting context',
                    'done': False,
                },
            }
        )

    try:
        summary = await _generate_summary(
            request,
            user,
            model_id,
            models,
            compacted_messages,
            recent_messages,
            previous_summary,
            config['prompt_template'],
        )
    except Exception:
        if event_emitter:
            await event_emitter(
                {
                    'type': 'context_compaction',
                    'data': {
                        'action': 'context_compaction',
                        'description': 'Context compaction failed',
                        'done': True,
                        'error': True,
                    },
                }
            )
        raise

    chat_id = metadata.get('chat_id')
    checkpoint_message_id = metadata.get('user_message_id') or metadata.get('message_id')
    if chat_id and checkpoint_message_id and not chat_id.startswith(('local:', 'channel:')):
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id,
            checkpoint_message_id,
            {'contextSummary': summary},
        )

    log.info(
        'Compacted chat context for chat=%s checkpoint=%s response=%s dropped=%d kept=%d summary_chars=%d',
        chat_id,
        checkpoint_message_id,
        metadata.get('message_id'),
        len(compacted_messages),
        len(recent_messages),
        len(summary),
    )

    if event_emitter:
        await event_emitter(
            {
                'type': 'context_compaction',
                'data': {
                    'action': 'context_compaction',
                    'description': 'Context compacted',
                    'done': True,
                },
            }
        )

    return [*system_messages, *recent_messages], summary, True


async def compact_chat_branch(request, user, chat: Any, model_id: str, models: dict) -> dict:
    config = await _load_config()
    if not config['enable']:
        return {'ok': True, 'compacted': False, 'reason': 'disabled'}

    history = (chat.chat or {}).get('history') or {}
    current_id = history.get('currentId')
    if not current_id:
        return {'ok': True, 'compacted': False, 'reason': 'empty'}

    messages_map = await Chats.get_messages_map_by_chat_id(chat.id)
    if not messages_map:
        messages_map = history.get('messages') or {}

    messages, previous_summary = _apply_latest_summary_checkpoint(get_message_list(messages_map, current_id))
    if len(messages) <= 2:
        return {'ok': True, 'compacted': False, 'reason': 'too_short'}

    compacted_messages = messages[:-1]
    recent_messages = messages[-1:]
    summary = await _generate_summary(
        request,
        user,
        model_id,
        models,
        compacted_messages,
        recent_messages,
        previous_summary,
        config['prompt_template'],
    )
    await Chats.upsert_message_to_chat_by_id_and_message_id(chat.id, current_id, {'contextSummary': summary})

    return {
        'ok': True,
        'compacted': True,
        'dropped_messages': len(compacted_messages),
        'kept_messages': len(recent_messages),
        'summary_chars': len(summary),
    }


async def _load_config() -> dict:
    values = await Config.get_many(
        'chat.context_compaction.enable',
        'chat.context_compaction.token_threshold',
        'chat.context_compaction.threshold_percent',
        'chat.context_compaction.prompt_template',
    )
    return {
        'enable': bool(values.get('chat.context_compaction.enable', True)),
        'token_threshold': int(values.get('chat.context_compaction.token_threshold', 80000) or 80000),
        'threshold_percent': int(
            values.get('chat.context_compaction.threshold_percent', DEFAULT_CONTEXT_COMPACTION_PERCENT)
            or DEFAULT_CONTEXT_COMPACTION_PERCENT
        ),
        'prompt_template': values.get('chat.context_compaction.prompt_template', '') or '',
    }


async def _load_enabled_config(features: dict) -> dict | None:
    if uses_hard_context_truncation(features):
        return None

    user_enabled = features.get('context_compaction')
    if user_enabled is False:
        return None

    config = await _load_config()
    enabled = user_enabled if isinstance(user_enabled, bool) else config['enable']
    return config if enabled else None


def _parse_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def uses_hard_context_truncation(features: dict | None) -> bool:
    features = features if isinstance(features, dict) else {}
    return any(features.get(key) for key in HARD_TRUNCATION_FEATURES)


def _resolve_context_size(metadata: dict, model_id: str, models: dict) -> int:
    return _resolve_context_size_details(metadata, model_id, models)[0]


def _resolve_context_size_details(metadata: dict, model_id: str, models: dict) -> tuple[int, str, bool]:
    params = metadata.get('params') if isinstance(metadata.get('params'), dict) else {}
    model = models.get(model_id, {}) if isinstance(models, dict) else {}
    model_info = model.get('info') if isinstance(model, dict) and isinstance(model.get('info'), dict) else {}
    model_params = model_info.get('params') if isinstance(model_info.get('params'), dict) else {}
    model_meta = model_info.get('meta') if isinstance(model_info.get('meta'), dict) else {}
    provider_model = model.get('openai') if isinstance(model, dict) and isinstance(model.get('openai'), dict) else {}
    provider_meta = provider_model.get('meta') if isinstance(provider_model.get('meta'), dict) else {}
    base_meta = model.get('meta') if isinstance(model, dict) and isinstance(model.get('meta'), dict) else {}

    candidates = (
        (params.get('num_ctx'), 'request'),
        (params.get('n_ctx'), 'request'),
        (params.get('ctx_size'), 'request'),
        (params.get('context_length'), 'request'),
        (params.get('context_size'), 'request'),
        (model_params.get('num_ctx'), 'model_params'),
        (model_params.get('n_ctx'), 'model_params'),
        (model_params.get('ctx_size'), 'model_params'),
        (model_params.get('context_length'), 'model_params'),
        (model_params.get('context_size'), 'model_params'),
        (base_meta.get('n_ctx'), 'model_metadata'),
        (base_meta.get('context_length'), 'model_metadata'),
        (base_meta.get('context_size'), 'model_metadata'),
        (model_meta.get('context_length'), 'model_metadata'),
        (model_meta.get('context_size'), 'model_metadata'),
        (model_meta.get('n_ctx'), 'model_metadata'),
        (provider_meta.get('n_ctx'), 'model_metadata'),
        (provider_meta.get('context_length'), 'model_metadata'),
        (provider_meta.get('context_size'), 'model_metadata'),
        (model.get('context_length') if isinstance(model, dict) else None, 'model'),
        (model.get('n_ctx') if isinstance(model, dict) else None, 'model'),
    )
    for value, source in candidates:
        parsed = _parse_positive_int(value)
        if parsed is not None:
            return parsed, source, False
    return DEFAULT_CONTEXT_SIZE, 'fallback', True


def _resolve_token_threshold(config: dict, metadata: dict, model_id: str, models: dict) -> int:
    try:
        threshold_percent = int(config.get('threshold_percent', DEFAULT_CONTEXT_COMPACTION_PERCENT))
    except (TypeError, ValueError):
        threshold_percent = DEFAULT_CONTEXT_COMPACTION_PERCENT
    threshold_percent = min(95, max(10, threshold_percent))

    percentage_threshold = max(
        1,
        round(_resolve_context_size(metadata, model_id, models) * threshold_percent / 100),
    )
    configured_threshold = _parse_positive_int((metadata.get('params') or {}).get('compact_token_threshold'))
    thresholds = [percentage_threshold]
    if configured_threshold is not None:
        thresholds.append(configured_threshold)
    return min(thresholds)


def _split_system_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    system_messages = [message for message in messages if message.get('role') == 'system']
    conversation_messages = [message for message in messages if message.get('role') != 'system']
    return system_messages, conversation_messages


def _apply_latest_summary_checkpoint(messages: list[dict]) -> tuple[list[dict], str | None]:
    summary = None
    summary_idx = None

    for idx, message in enumerate(messages):
        value = message.get('contextSummary') or message.get('context_summary')
        if isinstance(value, str) and value.strip():
            summary = value
            summary_idx = idx

    if summary_idx is None:
        return messages, None
    return messages[summary_idx:], summary


def _exceeds_token_threshold(messages: list[dict], system_prompt: str, summary: str | None, threshold: int) -> bool:
    if threshold <= 0:
        return False

    for idx in range(len(messages) - 1, -1, -1):
        usage = messages[idx].get('usage') or (messages[idx].get('info') or {}).get('usage')
        if isinstance(usage, dict):
            llama_usage = _llamacpp_context_usage(usage)
            if llama_usage is not None:
                total = llama_usage[2]
                return total + _estimate_messages_tokens(messages[idx + 1 :]) > threshold
            if usage.get('input_tokens'):
                total = int(usage.get('input_tokens') or 0) + int(usage.get('output_tokens') or 0)
                return total + _estimate_messages_tokens(messages[idx + 1 :]) > threshold

    estimated = _estimate_tokens(system_prompt) + _estimate_tokens(summary or '') + _estimate_messages_tokens(messages)
    return estimated > threshold


def _find_compaction_boundary(messages: list[dict]) -> int:
    keep_count = max(2, len(messages) * 2 // 5)
    max_split = max(1, len(messages) - 2)
    target = min(max_split, max(1, len(messages) - keep_count))

    # Start retained history at a complete user turn whenever possible.
    candidates = [index for index in range(1, max_split + 1) if messages[index].get('role') == 'user']
    if candidates:
        return min(candidates, key=lambda index: (abs(index - target), index > target))

    split = target
    while split > 1 and messages[split].get('role') == 'tool':
        split -= 1
    if split > 1 and messages[split - 1].get('tool_calls'):
        split -= 1
    return split


async def _generate_summary(
    request,
    user,
    model_id: str,
    models: dict,
    compacted_messages: list[dict],
    recent_messages: list[dict],
    previous_summary: str | None,
    summary_prompt_template: str,
) -> str:
    from open_webui.utils.chat import generate_chat_completion

    task_model_id = get_task_model_id(
        model_id,
        await Config.get('task.model.default'),
        await Config.get('task.model.external'),
        models,
    )
    if task_model_id not in models:
        task_model_id = model_id
    if task_model_id not in models:
        raise ValueError('No available model for context compaction')

    summary_prompt_template = summary_prompt_template.strip() or DEFAULT_CONTEXT_COMPACTION_PROMPT
    summary_recent_messages = recent_messages[-SUMMARY_RECENT_MESSAGE_LIMIT:]
    all_messages = [*compacted_messages, *summary_recent_messages]
    prompt = replace_prompt_variable(summary_prompt_template, get_last_user_message(all_messages) or '')
    prompt = replace_messages_variable(prompt, all_messages)
    prompt = replace_messages_variable(prompt, compacted_messages, 'COMPACTED_MESSAGES')
    prompt = replace_messages_variable(prompt, summary_recent_messages, 'RECENT_MESSAGES')
    prompt = prompt_variables_template(prompt, {'{{PREVIOUS_SUMMARY}}': previous_summary or ''})
    prompt = await prompt_template(prompt, user)

    max_tokens = models[task_model_id].get('info', {}).get('params', {}).get('max_tokens', 1000)
    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        **(
            {'max_tokens': max_tokens}
            if models[task_model_id].get('owned_by') == 'ollama'
            else {'max_completion_tokens': max_tokens}
        ),
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': 'context_compaction',
        },
    }

    response = await generate_chat_completion(request, form_data=payload, user=user)
    summary = _response_text(response).strip()
    if summary:
        return summary

    parts = [previous_summary] if previous_summary else []
    for message in compacted_messages:
        content = get_content_from_message(message)
        if content:
            parts.append(f'- {message.get("role", "unknown")}: {content[:500]}')
    return '\n'.join(parts)[:4000]


def _response_text(response: Any) -> str:
    if isinstance(response, list) and len(response) == 1:
        response = response[0]

    if isinstance(response, JSONResponse):
        try:
            response = json.loads(response.body.decode('utf-8', 'replace'))
        except Exception:
            return ''

    if not isinstance(response, dict):
        return ''

    choices = response.get('choices') or []
    if choices:
        message = choices[0].get('message') or {}
        return message.get('content') or message.get('reasoning_content') or ''

    parts = []
    for item in response.get('output') or []:
        for content in item.get('content') or []:
            if isinstance(content, dict):
                parts.append(content.get('text') or content.get('content') or '')
    return '\n'.join(part for part in parts if part)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += 4
        content = message.get('content')
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    total += _estimate_tokens(item)
                elif item.get('type') in {'image', 'image_url'}:
                    total += 1000
                else:
                    total += _estimate_tokens(item.get('text') or item.get('content') or item)
        else:
            total += _estimate_tokens(content)

        total += _estimate_tokens(message.get('output'))
        total += _estimate_tokens(message.get('tool_calls'))
        total += _estimate_tokens(message.get('files'))
    return total


def _estimate_tokens(value: Any) -> int:
    if value is None:
        return 0

    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except Exception:
            value = str(value)

    if not value:
        return 0

    return max(1, len(value) // 4)


async def build_context_usage_snapshot(
    messages: list[dict],
    metadata: dict,
    model_id: str,
    models: dict,
    *,
    tools: Any = None,
    compacted: bool = False,
) -> dict:
    config = await _load_config()
    features = metadata.get('features') if isinstance(metadata.get('features'), dict) else {}
    context_size, limit_source, limit_estimated = _resolve_context_size_details(metadata, model_id, models)
    threshold_tokens = _resolve_token_threshold(config, metadata, model_id, models)
    hard_truncation = uses_hard_context_truncation(features)
    user_enabled = features.get('context_compaction')
    compaction_enabled = user_enabled if isinstance(user_enabled, bool) else config['enable']

    used_tokens = _estimate_messages_tokens(messages) + _estimate_tokens(tools)
    return {
        'used_tokens': used_tokens,
        'input_tokens': used_tokens,
        'output_tokens': 0,
        'limit_tokens': context_size,
        'limit_source': limit_source,
        'limit_estimated': limit_estimated,
        'threshold_tokens': threshold_tokens,
        'threshold_percent': round(threshold_tokens * 100 / context_size, 1),
        'estimated': True,
        'compacted': compacted,
        'compaction_enabled': bool(compaction_enabled and not hard_truncation),
        'hard_truncation': hard_truncation,
    }


def context_usage_from_model_usage(snapshot: dict | None, usage: dict | None) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    if not isinstance(usage, dict) or not usage:
        return snapshot

    input_tokens = _parse_nonnegative_int(
        usage.get('input_tokens')
        if usage.get('input_tokens') is not None
        else usage.get('prompt_tokens', usage.get('prompt_n'))
    )
    output_tokens = _parse_nonnegative_int(
        usage.get('output_tokens')
        if usage.get('output_tokens') is not None
        else usage.get('completion_tokens', usage.get('predicted_n'))
    )
    total_tokens = _parse_nonnegative_int(usage.get('total_tokens'))

    llama_usage = _llamacpp_context_usage(usage)
    if llama_usage is not None:
        input_tokens, output_tokens, total_tokens = llama_usage

    has_model_count = input_tokens is not None or output_tokens is not None or total_tokens is not None
    if not has_model_count:
        return snapshot

    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    used_tokens = total_tokens if total_tokens is not None else input_tokens + output_tokens

    return {
        **snapshot,
        'used_tokens': used_tokens,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'estimated': False,
    }


def _llamacpp_context_usage(usage: dict) -> tuple[int, int, int] | None:
    # llama.cpp timings separate the reused cache_n prompt prefix from newly
    # evaluated prompt_n tokens. Include both to match the slot's final n_tokens.
    cache_tokens = _parse_nonnegative_int(usage.get('cache_n'))
    prompt_tokens = _parse_nonnegative_int(usage.get('prompt_n'))
    if cache_tokens is None or prompt_tokens is None:
        return None

    output_tokens = _parse_nonnegative_int(usage.get('predicted_n'))
    if output_tokens is None:
        output_tokens = _parse_nonnegative_int(
            usage.get('output_tokens') if usage.get('output_tokens') is not None else usage.get('completion_tokens')
        )
    output_tokens = output_tokens or 0
    input_tokens = cache_tokens + prompt_tokens
    return input_tokens, output_tokens, input_tokens + output_tokens


def _parse_nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
