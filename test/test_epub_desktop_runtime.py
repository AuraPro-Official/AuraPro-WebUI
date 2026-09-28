"""Tests for Desktop's dynamic local llama.cpp handoff."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from open_webui.retrieval.epub.desktop_runtime import (  # noqa: E402
    DesktopManagedLlamaCppConceptResolver,
)
from open_webui.retrieval.epub.calibration import LocalConceptCalibrationRunner  # noqa: E402
from open_webui.retrieval.epub.inference import (  # noqa: E402
    LocalEndpointRejected,
    LocalInferenceUnavailable,
)


class FakeLlamaCppTransport:
    """A llama-server whose ``GET /v1/models`` says what is actually resident.

    Every model Desktop puts in its preset stays servable; ``resident`` is the
    subset that is in memory, which is the only subset a request may name.
    """

    def __init__(self, *, resident: tuple[str, ...] = ('first-model', 'second-model')) -> None:
        self.urls: list[str] = []
        self.payloads: list[dict[str, object]] = []
        self._resident = resident

    def get_json(self, url: str) -> dict[str, object]:
        self.urls.append(url)
        return {
            'object': 'list',
            'data': [
                {'id': identifier, 'object': 'model', 'status': {'value': 'loaded'}} for identifier in self._resident
            ]
            + [{'id': 'a-preset-entry-nobody-loaded', 'object': 'model', 'status': {'value': 'unloaded'}}],
        }

    def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        self.urls.append(url)
        self.payloads.append(payload)
        return {'choices': [{'message': {'content': '{"concept":"候选"}'}}]}


class FakeCalibrationTransport(FakeLlamaCppTransport):
    def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        self.urls.append(url)
        self.payloads.append(payload)
        content = payload['messages'][1]['content']  # type: ignore[index]
        return {
            'choices': [
                {
                    'message': {
                        'content': '{"concepts":[{"name":"词","aliases":[],"definition":"段中术语。","mentions":[{"start_codepoint":0,"end_codepoint":1,"evidence":"词"}]}]}'
                        if content == '词条'
                        else '{"concepts":[]}'
                    }
                }
            ]
        }


class DesktopRuntimeResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name, 'desktop-llama-runtime.json')
        self.transport = FakeLlamaCppTransport()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_descriptor(self, endpoint: str, model: str) -> None:
        self.path.write_text(
            json.dumps({'version': 1, 'llama_cpp': {'endpoint': endpoint, 'model': model}}),
            encoding='utf-8',
        )

    def test_reloads_desktop_endpoint_and_model_hint_from_each_descriptor_snapshot(self) -> None:
        """Nothing from a descriptor is cached: every operation re-reads it.

        The descriptor's ``model`` reaches the request only as a tie-breaker.
        Both names below are resident in this fake runtime, so the snapshot
        genuinely decides which one is borrowed -- and swapping the snapshot
        swaps both the endpoint that is called and the model that is named.
        """
        self._write_descriptor('http://127.0.0.1:18881', 'first-model')
        resolver = DesktopManagedLlamaCppConceptResolver(
            descriptor_path=self.path,
            transport=self.transport,
        )
        self.assertTrue(resolver.availability().available)
        self.assertIn('http://127.0.0.1:18881/v1/models', self.transport.urls)

        self._write_descriptor('http://127.0.0.1:18882', 'second-model')
        self.assertEqual(resolver.resolve('问题', ['候选']), '候选')
        self.assertIn('http://127.0.0.1:18882/v1/models', self.transport.urls)
        self.assertIn('http://127.0.0.1:18882/v1/chat/completions', self.transport.urls)
        self.assertEqual(self.transport.payloads[-1]['model'], 'second-model')

    def test_the_descriptor_model_is_a_hint_that_cannot_evict_what_desktop_loaded(self) -> None:
        """Desktop's descriptor cannot say what is resident, so it does not try.

        Desktop writes the descriptor as soon as llama.cpp reports healthy, and
        its generated preset sets ``load-on-startup = false`` -- so the name it
        writes is a candidate chosen before anything was in memory.  Requesting
        it under ``--models-max 1`` would evict whatever the user is chatting
        with.  The resident model is borrowed instead.
        """
        transport = FakeLlamaCppTransport(resident=('the-model-the-user-loaded',))
        self._write_descriptor('http://127.0.0.1:18881', 'desktop-hardcoded-candidate')
        resolver = DesktopManagedLlamaCppConceptResolver(descriptor_path=self.path, transport=transport)

        self.assertTrue(resolver.availability().available)
        self.assertEqual(resolver.resolve('问题', ['候选']), '候选')
        self.assertEqual([payload['model'] for payload in transport.payloads], ['the-model-the-user-loaded'])

    def test_an_empty_desktop_runtime_is_degraded_rather_than_asked_to_load(self) -> None:
        transport = FakeLlamaCppTransport(resident=())
        self._write_descriptor('http://127.0.0.1:18881', 'desktop-hardcoded-candidate')
        resolver = DesktopManagedLlamaCppConceptResolver(descriptor_path=self.path, transport=transport)

        availability = resolver.availability()
        self.assertFalse(availability.available)
        self.assertIn('no model is loaded', availability.reason or '')
        with self.assertRaisesRegex(LocalInferenceUnavailable, 'no model is loaded'):
            resolver.resolve('问题', ['候选'])
        self.assertEqual(transport.payloads, [])

    def test_missing_or_public_descriptor_fails_closed(self) -> None:
        resolver = DesktopManagedLlamaCppConceptResolver(
            descriptor_path=self.path,
            transport=self.transport,
        )
        missing = resolver.availability()
        self.assertFalse(missing.available)
        self.assertEqual(missing.reason, 'Desktop local runtime is not running')

        self._write_descriptor('https://public.example/v1', 'desktop-model')
        public = resolver.availability()
        self.assertFalse(public.available)
        self.assertIn('neither local/private', public.reason or '')
        with self.assertRaises(LocalEndpointRejected):
            resolver.resolve('问题', ['候选'])

    def test_local_calibration_returns_content_free_schema_and_offset_metrics(self) -> None:
        """The report names the model that answered, not a model family.

        It used to declare ``mode: LOCAL_QWEN`` beside the descriptor's model
        name.  Both became false once the model stopped being one AuraPro ships
        and became whatever the deployer happens to have loaded, so the only
        honest thing an administrator can be shown is the resolved model id.
        """
        transport = FakeCalibrationTransport(resident=('the-model-the-user-loaded',))
        self._write_descriptor('http://127.0.0.1:18881', 'desktop-hardcoded-candidate')
        runner = LocalConceptCalibrationRunner(descriptor_path=self.path, transport=transport)
        report = runner.run(
            passages=[
                {'passage_id': 'p1', 'ordinal': 1, 'toc_path': ['第一章'], 'content': '词条'},
                {'passage_id': 'p2', 'ordinal': 2, 'toc_path': ['第二章'], 'content': '普通句子'},
            ],
            prompt_profile='zh-glossary-v1',
            sample_limit=2,
        )
        self.assertNotIn('mode', report)
        self.assertEqual(report['model'], 'the-model-the-user-loaded')
        self.assertEqual([payload['model'] for payload in transport.payloads], ['the-model-the-user-loaded'] * 2)
        # One inventory read for the whole sample, not one per request.
        self.assertEqual(transport.urls.count('http://127.0.0.1:18881/v1/models'), 1)
        self.assertEqual((report['sample_count'], report['chapter_count']), (2, 2))
        self.assertEqual((report['valid_items'], report['invalid_items']), (2, 0))
        self.assertNotIn('词条', repr(report))

    def test_local_calibration_is_unavailable_while_llama_cpp_holds_no_model(self) -> None:
        transport = FakeCalibrationTransport(resident=())
        self._write_descriptor('http://127.0.0.1:18881', 'desktop-hardcoded-candidate')
        runner = LocalConceptCalibrationRunner(descriptor_path=self.path, transport=transport)

        availability = runner.availability()
        self.assertFalse(availability.available)
        self.assertIn('no model is loaded', availability.reason or '')
        with self.assertRaisesRegex(LocalInferenceUnavailable, 'no model is loaded'):
            runner.run(
                passages=[{'passage_id': 'p1', 'ordinal': 1, 'toc_path': ['第一章'], 'content': '词条'}],
                prompt_profile='zh-glossary-v1',
                sample_limit=1,
            )
        self.assertEqual(transport.payloads, [])


if __name__ == '__main__':
    unittest.main()
