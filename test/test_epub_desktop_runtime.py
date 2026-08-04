"""Tests for Desktop's dynamic local llama.cpp handoff."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from open_webui.retrieval.epub.desktop_runtime import (  # noqa: E402
    DesktopManagedLlamaCppConceptResolver,
)
from open_webui.retrieval.epub.inference import LocalEndpointRejected  # noqa: E402


class FakeLlamaCppTransport:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.payloads: list[dict[str, object]] = []

    def get_json(self, url: str) -> dict[str, str]:
        self.urls.append(url)
        return {"status": "ok"}

    def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        self.urls.append(url)
        self.payloads.append(payload)
        return {"choices": [{"message": {"content": '{"concept":"候选"}'}}]}


class DesktopRuntimeResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name, "desktop-llama-runtime.json")
        self.transport = FakeLlamaCppTransport()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_descriptor(self, endpoint: str, model: str) -> None:
        self.path.write_text(
            json.dumps({"version": 1, "llama_cpp": {"endpoint": endpoint, "model": model}}),
            encoding="utf-8",
        )

    def test_reloads_desktop_endpoint_and_model_from_each_descriptor_snapshot(self) -> None:
        self._write_descriptor("http://127.0.0.1:18881", "first-model")
        resolver = DesktopManagedLlamaCppConceptResolver(
            descriptor_path=self.path,
            transport=self.transport,
        )
        self.assertTrue(resolver.availability().available)
        self.assertIn("http://127.0.0.1:18881/health", self.transport.urls)

        self._write_descriptor("http://127.0.0.1:18882", "second-model")
        self.assertEqual(resolver.resolve("问题", ["候选"]), "候选")
        self.assertIn("http://127.0.0.1:18882/v1/chat/completions", self.transport.urls)
        self.assertEqual(self.transport.payloads[-1]["model"], "second-model")

    def test_missing_or_public_descriptor_fails_closed(self) -> None:
        resolver = DesktopManagedLlamaCppConceptResolver(
            descriptor_path=self.path,
            transport=self.transport,
        )
        missing = resolver.availability()
        self.assertFalse(missing.available)
        self.assertEqual(missing.reason, "Desktop local runtime is not running")

        self._write_descriptor("https://public.example/v1", "desktop-model")
        public = resolver.availability()
        self.assertFalse(public.available)
        self.assertIn("neither local/private", public.reason or "")
        with self.assertRaises(LocalEndpointRejected):
            resolver.resolve("问题", ["候选"])


if __name__ == "__main__":
    unittest.main()
