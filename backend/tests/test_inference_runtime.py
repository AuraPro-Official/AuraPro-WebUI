import asyncio
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

spec = importlib.util.spec_from_file_location(
    'inference_runtime', Path(__file__).parents[1] / 'open_webui/utils/inference_runtime.py'
)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

ENV = {
    'AURAPRO_INFERENCE_CONTROL_URL': 'http://127.0.0.1:12345',
    'AURAPRO_INFERENCE_CONTROL_TOKEN': 'test-token',
}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_managed_thinking_defaults_off_and_preserves_template_options(self):
        for engine in ('standard', 'pro'):
            for effort in (None, '', 'none', 'off'):
                payload = {'reasoning_effort': effort, 'chat_template_kwargs': {'other': 'kept', 'reasoning_effort': 'high'}}
                result = runtime.apply_runtime_thinking(payload, engine)
                self.assertFalse(result['chat_template_kwargs']['enable_thinking'])
                self.assertEqual(result['reasoning_effort'], 'none')
                self.assertEqual(result['chat_template_kwargs']['other'], 'kept')
                self.assertNotIn('reasoning_effort', result['chat_template_kwargs'])

    def test_managed_thinking_enables_requested_level(self):
        for engine in ('standard', 'pro'):
            for effort in ('low', 'medium', 'high', 'xhigh'):
                result = runtime.apply_runtime_thinking({'reasoning_effort': effort}, engine)
                self.assertTrue(result['chat_template_kwargs']['enable_thinking'])
                expected = 'xhigh' if engine == 'pro' and effort == 'high' else effort
                self.assertEqual(result['chat_template_kwargs']['reasoning_effort'], expected)

    def test_external_provider_thinking_unchanged(self):
        payload = {'reasoning_effort': 'high'}
        self.assertEqual(runtime.apply_runtime_thinking(dict(payload), None), payload)

    def test_thinking_filter_toggle_beats_default_reasoning_effort(self):
        for engine in ('standard', 'pro'):
            for effort in (None, '', 'none', 'off'):
                payload = {
                    'reasoning_effort': effort,
                    'reasoning_budget': 8192,
                    'chat_template_kwargs': {'enable_thinking': True, 'other': 'kept'},
                }
                result = runtime.apply_runtime_thinking(payload, engine)
                self.assertTrue(result['chat_template_kwargs']['enable_thinking'])
                self.assertEqual(result['reasoning_effort'], 'medium')
                self.assertEqual(result['chat_template_kwargs']['reasoning_effort'], 'medium')
                self.assertEqual(result['reasoning_budget'], 8192)
                self.assertEqual(result['chat_template_kwargs']['other'], 'kept')

    def test_explicit_thinking_off_beats_saved_reasoning_level(self):
        for engine in ('standard', 'pro'):
            payload = {
                'reasoning_effort': 'high',
                'reasoning_budget': 0,
                'chat_template_kwargs': {'enable_thinking': False},
            }
            result = runtime.apply_runtime_thinking(payload, engine)
            self.assertFalse(result['chat_template_kwargs']['enable_thinking'])
            self.assertEqual(result['reasoning_effort'], 'none')
            self.assertEqual(result['reasoning_budget'], 0)

    def test_filter_toggle_preserves_requested_reasoning_level(self):
        for engine in ('standard', 'pro'):
            result = runtime.apply_runtime_thinking({
                'reasoning_effort': 'high',
                'chat_template_kwargs': {'enable_thinking': True},
            }, engine)
            self.assertEqual(result['reasoning_effort'], 'high')
            self.assertEqual(result['chat_template_kwargs']['reasoning_effort'],
                             'xhigh' if engine == 'pro' else 'high')

    def test_control_is_opt_in_and_loopback_only(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(runtime.control_config())
        for url in [
            'http://example.com:12345',
            'http://127.0.0.1:abc',
            'http://127.0.0.1:12345#fragment',
            'http://user@127.0.0.1:12345',
        ]:
            with patch.dict(os.environ, {**ENV, 'AURAPRO_INFERENCE_CONTROL_URL': url}):
                self.assertIsNone(runtime.control_config())

    def test_only_managed_ports_are_intercepted(self):
        with patch.dict(os.environ, ENV):
            self.assertEqual(runtime.runtime_for_url('http://127.0.0.1:18881/v1'), 'standard')
            self.assertEqual(runtime.runtime_for_url('http://localhost:18882/v1/'), 'pro')
            for url in [
                'https://example.com:18881/v1',
                'http://127.0.0.1:18883/v1',
                'http://127.0.0.1:18881/custom',
                'http://127.0.0.1:18881/v1?x=1',
            ]:
                self.assertIsNone(runtime.runtime_for_url(url))

    async def test_stream_retains_lease_until_consumed(self):
        async def content():
            yield b'first'
            yield b'last'

        with patch.object(runtime, 'control_request', new_callable=AsyncMock) as request:
            lease = runtime.RuntimeLease('stream')
            iterator = lease.stream(content())
            self.assertEqual(await anext(iterator), b'first')
            request.assert_not_awaited()
            self.assertEqual(await anext(iterator), b'last')
            with self.assertRaises(StopAsyncIteration):
                await anext(iterator)
            request.assert_awaited_once_with('/release', {'lease': 'stream'}, timeout=5)
            await lease.release()
            self.assertEqual(request.await_count, 1)

    async def test_cancelled_stream_releases_lease(self):
        closed = asyncio.Event()

        async def content():
            try:
                yield b'first'
                await asyncio.Event().wait()
            finally:
                closed.set()

        with patch.object(runtime, 'control_request', new_callable=AsyncMock) as request:
            lease = runtime.RuntimeLease('cancelled')
            iterator = lease.stream(content())
            await anext(iterator)
            await iterator.aclose()
            request.assert_awaited_once()
            self.assertTrue(lease.heartbeat.done())
            self.assertTrue(closed.is_set())

    async def test_external_requests_do_not_acquire(self):
        with patch.dict(os.environ, ENV), patch.object(runtime, 'control_request', new_callable=AsyncMock) as request:
            self.assertIsNone(await runtime.RuntimeLease.acquire('https://api.example.com/v1'))
            request.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
