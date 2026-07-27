from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any


class LazyModel:
    """Load a heavyweight model once, on the first attribute access."""

    def __init__(self, factory: Callable[[], Any], name: str, logger: logging.Logger):
        self._factory = factory
        self._name = name
        self._logger = logger
        self._instance: Any = None
        self._lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._instance is not None

    def get(self) -> Any:
        if self._instance is not None:
            return self._instance

        with self._lock:
            if self._instance is None:
                started_at = time.perf_counter()
                self._logger.info('Loading %s on first use', self._name)
                instance = self._factory()
                if instance is None:
                    raise RuntimeError(f'{self._name} failed to load')
                self._instance = instance
                self._logger.info(
                    'Loaded %s in %.2fs',
                    self._name,
                    time.perf_counter() - started_at,
                )

        return self._instance

    def unload(self) -> None:
        with self._lock:
            self._instance = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.get(), name)

    def __repr__(self) -> str:
        state = 'loaded' if self.is_loaded else 'not loaded'
        return f'<LazyModel {self._name!r} ({state})>'
