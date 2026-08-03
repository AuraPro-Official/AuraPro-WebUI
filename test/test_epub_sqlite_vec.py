"""Focused tests for the explicit sqlite-vec extension boundary."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "backend/open_webui/retrieval/epub/sqlite_vec.py"
SPEC = importlib.util.spec_from_file_location("epub_sqlite_vec_for_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
SQLiteVecUnavailable = MODULE.SQLiteVecUnavailable
load_sqlite_vec = MODULE.load_sqlite_vec


class NoExtensionConnection:
    pass


class SQLiteVecBoundaryTest(unittest.TestCase):
    def test_missing_dynamic_extension_support_is_a_hard_failure(self) -> None:
        with self.assertRaisesRegex(SQLiteVecUnavailable, "does not support"):
            load_sqlite_vec(NoExtensionConnection())


if __name__ == "__main__":
    unittest.main()
