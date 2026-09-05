import unittest
from unittest.mock import AsyncMock, patch

from open_webui.utils.context_compaction import (
    _find_compaction_boundary,
    _resolve_context_size_details,
    _resolve_token_threshold,
    compact_messages_for_request,
    context_usage_from_model_usage,
    uses_hard_context_truncation,
)


class ContextUsageTest(unittest.TestCase):
    def test_llama_usage_replaces_estimate(self):
        snapshot = {
            'used_tokens': 9000,
            'input_tokens': 9000,
            'output_tokens': 0,
            'limit_tokens': 32768,
            'estimated': True,
            'compacted': True,
        }

        result = context_usage_from_model_usage(
            snapshot,
            {'prompt_n': 6200, 'predicted_n': 400},
        )

        self.assertEqual(result['used_tokens'], 6600)
        self.assertEqual(result['input_tokens'], 6200)
        self.assertEqual(result['output_tokens'], 400)
        self.assertFalse(result['estimated'])
        self.assertTrue(result['compacted'])

    def test_request_context_limit_takes_precedence(self):
        context_size, source, estimated = _resolve_context_size_details(
            {'params': {'num_ctx': 16384}},
            'model-1',
            {
                'model-1': {
                    'info': {
                        'params': {'num_ctx': 32768},
                        'meta': {'context_length': 65536},
                    }
                }
            },
        )

        self.assertEqual(context_size, 16384)
        self.assertEqual(source, 'request')
        self.assertFalse(estimated)

    def test_unknown_context_limit_is_marked_as_estimated(self):
        context_size, source, estimated = _resolve_context_size_details({}, 'missing', {})

        self.assertEqual(context_size, 16384)
        self.assertEqual(source, 'fallback')
        self.assertTrue(estimated)

    def test_percentage_threshold_is_not_capped_by_legacy_global_limit(self):
        threshold = _resolve_token_threshold(
            {'threshold_percent': 75, 'token_threshold': 80000},
            {},
            'model-1',
            {'model-1': {'info': {'params': {'num_ctx': 131072}}}},
        )

        self.assertEqual(threshold, 98304)

    def test_request_token_threshold_can_apply_a_stricter_limit(self):
        threshold = _resolve_token_threshold(
            {'threshold_percent': 75, 'token_threshold': 80000},
            {'params': {'compact_token_threshold': 40000}},
            'model-1',
            {'model-1': {'info': {'params': {'num_ctx': 131072}}}},
        )

        self.assertEqual(threshold, 40000)

    def test_translation_modes_use_hard_truncation_but_learning_does_not(self):
        self.assertTrue(uses_hard_context_truncation({'translation': True}))
        self.assertTrue(uses_hard_context_truncation({'interpretation': True}))
        self.assertTrue(uses_hard_context_truncation({'manuscript_translation': True}))
        self.assertFalse(uses_hard_context_truncation({'learning': True}))

    def test_boundary_starts_at_a_complete_user_turn(self):
        messages = [
            {'role': 'user', 'content': 'first'},
            {'role': 'assistant', 'content': 'first answer'},
            {'role': 'assistant', 'tool_calls': [{'id': 'call-1'}]},
            {'role': 'tool', 'content': 'tool result'},
            {'role': 'user', 'content': 'latest'},
            {'role': 'assistant', 'content': 'latest answer'},
        ]

        self.assertEqual(_find_compaction_boundary(messages), 4)


class ContextCompactionFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_preserves_system_prompt_and_summarizes_only_history(self):
        messages = [
            {'role': 'system', 'content': 'Always preserve this instruction.'},
            {'role': 'user', 'content': 'A' * 120},
            {'role': 'assistant', 'content': 'B' * 120},
            {'role': 'user', 'content': 'C' * 120},
            {'role': 'assistant', 'content': 'D' * 120},
            {'role': 'user', 'content': 'E' * 120},
        ]
        config = {
            'enable': True,
            'token_threshold': 80000,
            'threshold_percent': 75,
            'prompt_template': '',
        }
        generate_summary = AsyncMock(return_value='compact summary')

        with (
            patch(
                'open_webui.utils.context_compaction._load_config',
                AsyncMock(return_value=config),
            ),
            patch(
                'open_webui.utils.context_compaction._generate_summary',
                generate_summary,
            ),
        ):
            result, summary, compacted = await compact_messages_for_request(
                request=object(),
                user=object(),
                messages=messages,
                metadata={},
                model_id='model-1',
                models={'model-1': {'info': {'params': {'num_ctx': 100}}}},
                system_prompt=messages[0]['content'],
            )

        self.assertTrue(compacted)
        self.assertEqual(summary, 'compact summary')
        self.assertEqual(result[0], messages[0])
        summarized_messages = generate_summary.await_args.args[4]
        self.assertTrue(summarized_messages)
        self.assertTrue(all(message['role'] != 'system' for message in summarized_messages))

    async def test_checkpoint_drops_old_history_but_keeps_system_prompt(self):
        messages = [
            {'role': 'system', 'content': 'System instruction'},
            {'role': 'user', 'content': 'old user'},
            {'role': 'assistant', 'content': 'old assistant'},
            {
                'role': 'user',
                'content': 'checkpoint user',
                'contextSummary': 'previous summary',
            },
            {'role': 'assistant', 'content': 'recent assistant'},
            {'role': 'user', 'content': 'current user'},
        ]
        config = {
            'enable': True,
            'token_threshold': 80000,
            'threshold_percent': 75,
            'prompt_template': '',
        }

        with (
            patch(
                'open_webui.utils.context_compaction._load_config',
                AsyncMock(return_value=config),
            ),
            patch(
                'open_webui.utils.context_compaction._generate_summary',
                AsyncMock(),
            ) as generate_summary,
        ):
            result, summary, compacted = await compact_messages_for_request(
                request=object(),
                user=object(),
                messages=messages,
                metadata={},
                model_id='model-1',
                models={'model-1': {'info': {'params': {'num_ctx': 32768}}}},
                system_prompt=messages[0]['content'],
            )

        self.assertFalse(compacted)
        self.assertEqual(summary, 'previous summary')
        self.assertEqual(
            [message['content'] for message in result],
            [
                'System instruction',
                'checkpoint user',
                'recent assistant',
                'current user',
            ],
        )
        generate_summary.assert_not_awaited()

    async def test_hard_truncation_mode_bypasses_summary_without_loading_config(self):
        messages = [
            {'role': 'user', 'content': 'A' * 500},
            {'role': 'assistant', 'content': 'B' * 500},
            {'role': 'user', 'content': 'C' * 500},
            {'role': 'assistant', 'content': 'D' * 500},
        ]

        with patch(
            'open_webui.utils.context_compaction._load_config',
            AsyncMock(),
        ) as load_config:
            result, summary, compacted = await compact_messages_for_request(
                request=object(),
                user=object(),
                messages=messages,
                metadata={'features': {'translation': True}},
                model_id='model-1',
                models={},
            )

        self.assertIs(result, messages)
        self.assertIsNone(summary)
        self.assertFalse(compacted)
        load_config.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
