"""Recognize explicit text-translation requests without a model round trip."""

import re
from dataclasses import dataclass
from typing import Any


LANGUAGE_NAMES = {
    'English': ('英语', '英語', '英文', 'english', 'inglés', 'ingles', 'anglais'),
    '简体中文': ('简体中文', '簡體中文', 'simplified chinese'),
    '繁體中文': ('繁体中文', '繁體中文', 'traditional chinese'),
    'Chinese': ('中文', '汉语', '漢語', 'chinese', 'chino', 'chinois'),
    'Spanish': ('西班牙语', '西班牙語', '西语', '西語', 'spanish', 'español', 'espanol', 'espagnol'),
    'French': ('法语', '法語', 'french', 'français', 'francais', 'francés', 'frances'),
    'German': ('德语', '德語', 'german', 'alemán', 'allemand'),
    'Japanese': ('日语', '日語', '日文', 'japanese', 'japonés', 'japonais'),
    'Korean': ('韩语', '韓語', 'korean', 'coreano', 'coréen'),
    'Portuguese': ('葡萄牙语', '葡萄牙語', '葡语', '葡語', 'portuguese', 'português', 'portugués', 'portugais'),
    'Italian': ('意大利语', '義大利語', 'italian', 'italiano', 'italien'),
    'Russian': ('俄语', '俄語', 'russian', 'ruso', 'russe'),
    'Arabic': ('阿拉伯语', '阿拉伯語', 'arabic', 'árabe', 'arabe'),
}
_LANGUAGES = {alias.casefold(): name for name, aliases in LANGUAGE_NAMES.items() for alias in aliases}
_LANGUAGE = '|'.join(re.escape(alias) for alias in sorted(_LANGUAGES, key=len, reverse=True))
_PREFIX = r'(?:(?:请|請|麻烦|麻煩|帮我|幫我)\s*)?'
_REFERENCE = r'(?:(?:下面|以下|这|這)(?:的)?(?:这|這)?(?:段|句)?(?:文字|文本|内容|內容|话|話|句子)\s*)?'
_VERB = r'(?:翻译|翻譯)(?:成|为|為|到)\s*'
_TARGET = rf'(?P<target>{_LANGUAGE})'
_SEPARATOR = r'(?:\s*[:：]\s*|\s*\n\s*)'
_HEADERS = tuple(
    re.compile(pattern + _SEPARATOR + r'(?P<text>.+)', re.IGNORECASE | re.DOTALL)
    for pattern in (
        rf'{_PREFIX}(?:把|将|將)?\s*{_REFERENCE}{_VERB}{_TARGET}',
        rf'{_PREFIX}(?:把|将|將)?\s*{_REFERENCE}译(?:成|为|為)\s*{_TARGET}',
        rf'{_PREFIX}(?:这|這)(?:句话|句話|段话|段話|段文字)用\s*{_TARGET}\s*怎么说',
        rf'(?:please\s+)?translate(?:\s+(?:the\s+)?(?:following|this)(?:\s+(?:text|sentence|passage))?)?\s+(?:into|to)\s+{_TARGET}',
        rf'(?:por favor,?\s*)?(?:traduce|traduzca|traducir)\s+(?:al?|en)\s+{_TARGET}',
        rf'(?:s’il vous plaît,?\s*|s\x27il vous plaît,?\s*)?tradu(?:is|isez|ire)\s+en\s+{_TARGET}',
    )
)
_QUOTED = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for left, right in (('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』'), ('"', '"'), ("'", "'"))
    for pattern in (
        rf'{_PREFIX}(?:把|将|將)?\s*{re.escape(left)}(?P<text>.+){re.escape(right)}\s*{_VERB}{_TARGET}[。.!！?？]?',
        rf'(?:please\s+)?translate\s+{re.escape(left)}(?P<text>.+){re.escape(right)}\s+(?:into|to)\s+{_TARGET}[.!?]?',
    )
)
_TAIL = re.compile(rf'(?P<text>.+)\n\s*{_PREFIX}{_VERB}{_TARGET}[。.!！?？]?', re.IGNORECASE | re.DOTALL)
_INLINE = re.compile(
    rf'{_PREFIX}(?:把|将|將)\s*(?P<text>.+?)\s*{_VERB}{_TARGET}[。.!！?？]?', re.IGNORECASE | re.DOTALL
)
_ENGLISH_INLINE = re.compile(
    rf'(?:please\s+)?translate\s+(?P<text>.+?)\s+(?:into|to)\s+{_TARGET}[.!?]?', re.IGNORECASE | re.DOTALL
)
_SPACE_HEADER = re.compile(rf'{_PREFIX}{_VERB}{_TARGET}[ \t]+(?P<text>.+)', re.IGNORECASE | re.DOTALL)
_CONTINUE = re.compile(r'(?:请|請)?(?:继续翻译|繼續翻譯|翻译|翻譯)(?:一下)?\s*[:：]\s*(?P<text>.+)', re.DOTALL)
_BLOCKING_FEATURES = {
    'translation',
    'rag_translation',
    'learning',
    'learning_mode',
    'manuscript_translation',
    'manuscript_translation_mode',
    'document_translation',
    'document_translation_mode',
    'interpretation',
    'simultaneous',
    'voice',
    'web_search',
    'image_generation',
    'code_interpreter',
}


@dataclass(frozen=True)
class ChatTranslationRequest:
    text: str
    target: str


def _text_content(message: Any) -> str | None:
    if not isinstance(message, dict) or message.get('role') != 'user' or message.get('files'):
        return None
    content = message.get('content')
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        if content[0].get('type') == 'text' and isinstance(content[0].get('text'), str):
            return content[0]['text'].strip()
    return None


def parse_translation_request(text: str) -> ChatTranslationRequest | None:
    for pattern in (*_HEADERS, *_QUOTED, _TAIL, _INLINE, _ENGLISH_INLINE, _SPACE_HEADER):
        match = pattern.fullmatch(text.strip())
        if match:
            body = match['text'].strip()
            if body:
                return ChatTranslationRequest(body, _LANGUAGES[match['target'].casefold()])
    return None


def detect_chat_translation(
    messages: list[dict[str, Any]],
    features: dict[str, Any],
) -> ChatTranslationRequest | None:
    if any(features.get(key) for key in _BLOCKING_FEATURES):
        return None
    agent = features.get('opencode')
    if agent and (not isinstance(agent, dict) or agent.get('enabled')):
        return None
    if not messages or (text := _text_content(messages[-1])) is None:
        return None
    request = parse_translation_request(text)
    if request:
        return request
    # Only an explicit continuation inherits the preceding user task's language.
    continuation = _CONTINUE.fullmatch(text)
    if continuation:
        for previous in reversed(messages[:-1]):
            if not isinstance(previous, dict) or previous.get('role') != 'user':
                continue
            previous_text = _text_content(previous)
            if not previous_text:
                break
            if request := parse_translation_request(previous_text):
                return ChatTranslationRequest(continuation['text'].strip(), request.target)
            if not _CONTINUE.fullmatch(previous_text):
                break
    return None
