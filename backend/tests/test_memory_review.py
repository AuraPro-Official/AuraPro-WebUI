from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if 'open_webui' not in sys.modules:
    package = types.ModuleType('open_webui')
    package.__path__ = [str(BACKEND / 'open_webui')]
    sys.modules['open_webui'] = package


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


fastapi = types.ModuleType('fastapi')
fastapi.HTTPException = HTTPException
sys.modules['fastapi'] = fastapi

config_module = types.ModuleType('open_webui.models.config')
config_module.Config = object
sys.modules['open_webui.models.config'] = config_module


class DummyMemories:
    @staticmethod
    def normalize_memory_type(memory_type=None):
        return 'user' if memory_type == 'user' else 'context'


memories_module = types.ModuleType('open_webui.models.memories')
memories_module.Memories = DummyMemories
sys.modules['open_webui.models.memories'] = memories_module

misc_module = types.ModuleType('open_webui.utils.misc')
misc_module.add_or_update_system_message = lambda content, messages, append=False: messages
misc_module.get_content_from_message = lambda message: message.get('content', '')
sys.modules['open_webui.utils.misc'] = misc_module

from open_webui.utils.memory import (  # noqa: E402
    _sanitize_memory_operations,
    memory_review_candidate,
    parse_memory_review_response,
)


class TestMemoryReviewCandidate(unittest.TestCase):
    def test_explicit_request_triggers_on_first_turn(self):
        messages = [{'role': 'user', 'content': '\u8bb0\u4f4f\u6211\u559c\u6b22\u7b80\u6d01\u7684\u56de\u7b54'}]
        self.assertEqual(
            memory_review_candidate(messages, {'memory': True}, 6, automatic_enabled=False),
            (True, 'explicit_request'),
        )

    def test_durable_signal_triggers_without_waiting_for_interval(self):
        messages = [{'role': 'user', 'content': 'My preferred language is Spanish.'}]
        self.assertEqual(
            memory_review_candidate(messages, {'memory': True}, 6, automatic_enabled=True),
            (True, 'durable_signal'),
        )

    def test_translation_mode_skips_automatic_review(self):
        messages = [{'role': 'user', 'content': 'My preferred language is Spanish.'}]
        self.assertEqual(
            memory_review_candidate(
                messages,
                {'memory': True, 'translation': True},
                6,
                automatic_enabled=True,
            ),
            (False, 'translation_mode'),
        )

    def test_secret_is_never_sent_to_reviewer(self):
        messages = [{'role': 'user', 'content': 'Remember my API key is secret-value.'}]
        self.assertEqual(
            memory_review_candidate(messages, {'memory': True}, 6, automatic_enabled=True),
            (False, 'sensitive_secret'),
        )

    def test_forget_request_can_remove_an_old_sensitive_memory(self):
        messages = [{'role': 'user', 'content': 'Forget the password memory.'}]
        self.assertEqual(
            memory_review_candidate(messages, {'memory': True}, 6, automatic_enabled=True),
            (True, 'explicit_request'),
        )

    def test_periodic_review_is_a_low_frequency_fallback(self):
        messages = [
            {'role': 'user', 'content': f'This is ordinary message number {index} with enough detail.'}
            for index in range(1, 7)
        ]
        self.assertEqual(
            memory_review_candidate(messages, {'memory': True}, 6, automatic_enabled=True),
            (True, 'periodic_review'),
        )


class TestMemoryReviewParsing(unittest.TestCase):
    def test_parses_fenced_json_after_reasoning(self):
        parsed = parse_memory_review_response(
            '<think>private reasoning</think>\n```json\n'
            '{"operations": [], "history_summary": "Prefers concise answers"}\n```'
        )
        self.assertEqual(parsed['history_summary'], 'Prefers concise answers')

    def test_repairs_trailing_commas(self):
        parsed = parse_memory_review_response(
            '{"operations":[{"action":"add","content":"Uses macOS","type":"user",}],}'
        )
        self.assertEqual(parsed['operations'][0]['content'], 'Uses macOS')

    def test_sanitizer_rejects_unknown_ids_and_limits_output(self):
        operations = [
            {'action': 'remove', 'id': 'unknown'},
            {'action': 'replace', 'id': 'known', 'content': 'Updated', 'type': 'user'},
        ] + [{'action': 'add', 'content': f'Memory {index}', 'type': 'context'} for index in range(10)]
        sanitized = _sanitize_memory_operations(operations, {'known'})
        self.assertLessEqual(len(sanitized), 8)
        self.assertNotIn('unknown', {item.get('id') for item in sanitized})
        self.assertEqual(sanitized[0]['id'], 'known')


if __name__ == '__main__':
    unittest.main()
