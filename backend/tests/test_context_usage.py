import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from open_webui.utils.context_compaction import (
    _exceeds_token_threshold,
    _find_compaction_boundary,
    _generate_summary,
    _resolve_context_size_details,
    _resolve_token_threshold,
    build_context_usage_snapshot,
    compact_messages_for_request,
    context_usage_from_model_usage,
    uses_hard_context_truncation,
)
from open_webui.utils.middleware import load_messages_from_db, strip_compaction_fields


class ContextUsageTest(unittest.TestCase):
    def test_missing_model_usage_does_not_expose_estimate(self):
        snapshot = {
            'limit_tokens': 32768,
        }

        self.assertIsNone(context_usage_from_model_usage(snapshot, None))
        self.assertIsNone(context_usage_from_model_usage(snapshot, {}))

    def test_llama_usage_populates_exact_snapshot(self):
        snapshot = {
            'limit_tokens': 32768,
            'compacted': True,
        }

        result = context_usage_from_model_usage(
            snapshot,
            {'prompt_n': 6200, 'predicted_n': 400},
        )

        self.assertEqual(result['used_tokens'], 6600)
        self.assertEqual(result['input_tokens'], 6200)
        self.assertEqual(result['output_tokens'], 400)
        self.assertTrue(result['compacted'])

    def test_llamacpp_usage_includes_reused_prompt_cache(self):
        snapshot = {
            'limit_tokens': 20224,
        }

        result = context_usage_from_model_usage(
            snapshot,
            {
                'cache_n': 17905,
                'prompt_n': 1316,
                'predicted_n': 168,
                # normalize_usage previously derived these incomplete values.
                'input_tokens': 1316,
                'output_tokens': 168,
                'total_tokens': 1484,
            },
        )

        self.assertEqual(result['input_tokens'], 19221)
        self.assertEqual(result['output_tokens'], 168)
        self.assertEqual(result['used_tokens'], 19389)

    def test_request_context_limit_takes_precedence(self):
        context_size, source = _resolve_context_size_details(
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

    def test_llamacpp_model_metadata_context_limit_is_used(self):
        context_size, source = _resolve_context_size_details(
            {},
            'model-1',
            {'model-1': {'meta': {'n_ctx': 20224}}},
        )

        self.assertEqual(context_size, 20224)
        self.assertEqual(source, 'model_metadata')

    def test_cached_llamacpp_context_triggers_compaction(self):
        messages = [
            {
                'role': 'assistant',
                'usage': {
                    'cache_n': 12596,
                    'prompt_n': 1675,
                    'predicted_n': 1235,
                    'input_tokens': 1675,
                    'output_tokens': 1235,
                },
            },
            {'role': 'user', 'content': 'continue'},
        ]

        self.assertTrue(_exceeds_token_threshold(messages, 15168))

    def test_unknown_context_limit_is_not_guessed(self):
        context_size, source = _resolve_context_size_details({}, 'missing', {})

        self.assertIsNone(context_size)
        self.assertEqual(source, 'unknown')
        self.assertIsNone(
            _resolve_token_threshold(
                {'threshold_percent': 75},
                {},
                'missing',
                {},
            )
        )

    def test_messages_without_exact_usage_do_not_trigger_compaction(self):
        messages = [
            {'role': 'user', 'content': 'A' * 100000},
            {'role': 'assistant', 'content': 'B' * 100000},
        ]

        self.assertFalse(_exceeds_token_threshold(messages, 100))

    def test_latest_exact_usage_is_reused_after_model_switch(self):
        messages = [
            {
                'role': 'assistant',
                'model': 'model-1',
                'usage': {
                    'input_tokens': 16000,
                    'output_tokens': 1000,
                    'total_tokens': 17000,
                },
            }
        ]

        self.assertTrue(_exceeds_token_threshold(messages, 15168))

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
    async def test_pre_request_snapshot_contains_no_estimated_token_values(self):
        config = {
            'enable': True,
            'token_threshold': 80000,
            'threshold_percent': 75,
            'prompt_template': '',
        }

        with patch(
            'open_webui.utils.context_compaction._load_config',
            AsyncMock(return_value=config),
        ):
            snapshot = await build_context_usage_snapshot(
                {},
                'model-1',
                {'model-1': {'meta': {'n_ctx': 20224}}},
            )

        self.assertEqual(snapshot['limit_tokens'], 20224)
        self.assertEqual(snapshot['threshold_tokens'], 15168)
        self.assertNotIn('used_tokens', snapshot)
        self.assertNotIn('input_tokens', snapshot)
        self.assertNotIn('output_tokens', snapshot)
        self.assertNotIn('estimated', snapshot)
        self.assertNotIn('limit_estimated', snapshot)

    async def test_summary_request_only_includes_recent_context_tail(self):
        recent_messages = [{'role': 'user', 'content': f'recent-message-{index}'} for index in range(6)]
        generate_completion = AsyncMock(return_value={'choices': [{'message': {'content': 'compact summary'}}]})

        with (
            patch(
                'open_webui.models.config.Config.get',
                AsyncMock(return_value=''),
            ),
            patch(
                'open_webui.utils.chat.generate_chat_completion',
                generate_completion,
            ),
        ):
            summary = await _generate_summary(
                request=SimpleNamespace(state=SimpleNamespace(metadata={})),
                user=object(),
                model_id='model-1',
                models={'model-1': {'info': {'params': {'max_tokens': 100}}}},
                compacted_messages=[{'role': 'user', 'content': 'old-message'}],
                recent_messages=recent_messages,
                previous_summary=None,
                summary_prompt_template='',
            )

        prompt = generate_completion.await_args.kwargs['form_data']['messages'][0]['content']
        self.assertEqual(summary, 'compact summary')
        self.assertNotIn('recent-message-0', prompt)
        self.assertNotIn('recent-message-1', prompt)
        for index in range(2, 6):
            self.assertIn(f'recent-message-{index}', prompt)

    async def test_compaction_preserves_system_prompt_and_summarizes_only_history(self):
        messages = [
            {'role': 'system', 'content': 'Always preserve this instruction.'},
            {'role': 'user', 'content': 'A' * 120},
            {'role': 'assistant', 'content': 'B' * 120},
            {'role': 'user', 'content': 'C' * 120},
            {
                'role': 'assistant',
                'content': 'D' * 120,
                'model': 'model-1',
                'usage': {
                    'input_tokens': 70,
                    'output_tokens': 20,
                    'total_tokens': 90,
                },
            },
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

    async def test_compaction_persists_checkpoint_without_deleting_full_history(self):
        messages = [
            {'role': 'system', 'content': 'System instruction'},
            {'role': 'user', 'content': 'old user'},
            {'role': 'assistant', 'content': 'old assistant'},
            {'role': 'user', 'content': 'recent user'},
            {
                'role': 'assistant',
                'content': 'recent assistant',
                'usage': {'total_tokens': 90},
            },
            {'role': 'user', 'content': 'current user'},
        ]
        original_messages = deepcopy(messages)
        config = {
            'enable': True,
            'token_threshold': 80000,
            'threshold_percent': 75,
            'prompt_template': '',
        }
        persist_checkpoint = AsyncMock(return_value=True)

        with (
            patch(
                'open_webui.utils.context_compaction._load_config',
                AsyncMock(return_value=config),
            ),
            patch(
                'open_webui.utils.context_compaction._generate_summary',
                AsyncMock(return_value='persisted summary'),
            ),
            patch(
                'open_webui.utils.context_compaction.Chats.upsert_message_to_chat_by_id_and_message_id',
                persist_checkpoint,
            ),
        ):
            request_messages, summary, compacted = await compact_messages_for_request(
                request=object(),
                user=object(),
                messages=messages,
                metadata={
                    'chat_id': 'chat-1',
                    'user_message_id': 'current-user',
                },
                model_id='new-model',
                models={'new-model': {'info': {'params': {'num_ctx': 100}}}},
            )

        self.assertTrue(compacted)
        self.assertEqual(summary, 'persisted summary')
        self.assertEqual(messages, original_messages)
        self.assertLess(len(request_messages), len(messages))
        persist_checkpoint.assert_awaited_once_with(
            'chat-1',
            'current-user',
            {'contextSummary': 'persisted summary'},
        )

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


class ContextCompactionMessageFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_database_messages_retain_exact_usage_for_compaction(self):
        usage = {
            'cache_n': 19745,
            'prompt_n': 447,
            'predicted_n': 31,
        }
        messages = [
            {
                'role': 'assistant',
                'content': 'answer',
                'model': 'high_Q4',
                'usage': usage,
            },
            {'role': 'user', 'content': 'continue'},
        ]

        get_messages_map = AsyncMock(return_value={'message': {}})
        get_message_list_mock = unittest.mock.Mock(return_value=messages)

        with (
            patch(
                'open_webui.utils.middleware.Chats.get_messages_map_by_chat_id',
                get_messages_map,
            ),
            patch(
                'open_webui.utils.middleware.get_message_list',
                get_message_list_mock,
            ),
        ):
            result = await load_messages_from_db('chat-1', 'user-2')

        get_messages_map.assert_awaited_once_with('chat-1')
        get_message_list_mock.assert_called_once_with({'message': {}}, 'user-2')
        self.assertNotIn('model', result[0])
        self.assertEqual(result[0]['usage'], usage)

    async def test_database_history_is_scoped_to_selected_chat_and_branch(self):
        first_map = {'first': {}}
        second_map = {'second': {}}
        get_messages_map = AsyncMock(side_effect=[first_map, second_map])
        get_message_list_mock = unittest.mock.Mock(
            side_effect=[
                [{'role': 'user', 'content': 'first conversation'}],
                [{'role': 'user', 'content': 'second conversation'}],
            ]
        )

        with (
            patch(
                'open_webui.utils.middleware.Chats.get_messages_map_by_chat_id',
                get_messages_map,
            ),
            patch(
                'open_webui.utils.middleware.get_message_list',
                get_message_list_mock,
            ),
        ):
            first = await load_messages_from_db('chat-1', 'branch-1')
            second = await load_messages_from_db('chat-2', 'branch-2')

        self.assertEqual(first[0]['content'], 'first conversation')
        self.assertEqual(second[0]['content'], 'second conversation')
        self.assertEqual(
            get_messages_map.await_args_list,
            [unittest.mock.call('chat-1'), unittest.mock.call('chat-2')],
        )
        self.assertEqual(
            get_message_list_mock.call_args_list,
            [
                unittest.mock.call(first_map, 'branch-1'),
                unittest.mock.call(second_map, 'branch-2'),
            ],
        )

    async def test_persisted_checkpoint_is_restored_for_a_later_model(self):
        persisted_messages = [
            {'role': 'user', 'content': 'old user'},
            {'role': 'assistant', 'content': 'old assistant'},
            {
                'role': 'user',
                'content': 'checkpoint user',
                'contextSummary': 'saved yesterday',
            },
            {
                'role': 'assistant',
                'content': 'recent assistant',
                'usage': {'total_tokens': 20},
            },
            {'role': 'user', 'content': 'continue today'},
        ]
        config = {
            'enable': True,
            'token_threshold': 80000,
            'threshold_percent': 75,
            'prompt_template': '',
        }

        with (
            patch(
                'open_webui.utils.middleware.Chats.get_messages_map_by_chat_id',
                AsyncMock(return_value={'message': {}}),
            ),
            patch(
                'open_webui.utils.middleware.get_message_list',
                return_value=persisted_messages,
            ),
        ):
            loaded_messages = await load_messages_from_db('chat-1', 'today-user')

        with patch(
            'open_webui.utils.context_compaction._load_config',
            AsyncMock(return_value=config),
        ):
            request_messages, summary, compacted = await compact_messages_for_request(
                request=object(),
                user=object(),
                messages=loaded_messages,
                metadata={},
                model_id='different-model',
                models={'different-model': {'info': {'params': {'num_ctx': 32768}}}},
            )

        self.assertFalse(compacted)
        self.assertEqual(summary, 'saved yesterday')
        self.assertEqual(
            [message['content'] for message in request_messages],
            ['checkpoint user', 'recent assistant', 'continue today'],
        )

    async def test_internal_usage_fields_are_removed_before_model_request(self):
        messages = [
            {
                'role': 'assistant',
                'content': 'answer',
                'model': 'high_Q4',
                'usage': {'total_tokens': 17000},
                'info': {'usage': {'total_tokens': 17000}},
                'contextSummary': 'summary',
            }
        ]

        result = strip_compaction_fields(messages)

        self.assertEqual(result, [{'role': 'assistant', 'content': 'answer'}])


if __name__ == '__main__':
    unittest.main()
