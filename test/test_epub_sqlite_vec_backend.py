"""Real sqlite-vec persistence checks for EPUB derived-vector metadata."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from open_webui.retrieval.epub.sqlite_vec import SQLiteVecUnavailable  # noqa: E402
from open_webui.retrieval.epub.sqlite_vec_backend import SQLiteVecDerivedVectorBackend  # noqa: E402
from open_webui.retrieval.epub.vector_index import DerivedVectorRecord, VectorIndexError  # noqa: E402


# Production (open_webui.retrieval.epub.sqlite_vec.load_sqlite_vec) loads the
# sqlite-vec extension through the *stdlib* sqlite3 module, so these tests use
# stdlib sqlite3 too rather than a second, differently-built binding. Not every
# CPython build enables loadable extensions (pyenv omits it unless configured
# with --enable-loadable-sqlite-extensions); on those interpreters this module
# skips instead of erroring. scripts/epub_test_env.sh provisions a runtime that
# supports it.
EXTENSION_LOADING_SUPPORTED = hasattr(sqlite3.Connection, "enable_load_extension")
SKIP_REASON = (
    "sqlite3.Connection.enable_load_extension is unavailable; this CPython was built "
    "without loadable SQLite extension support. Provision a supported interpreter with "
    "scripts/epub_test_env.sh."
)


class InMemorySQLiteStore:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("CREATE TABLE retrieval_units (retrieval_unit_id TEXT PRIMARY KEY)")
        self.connection.executemany(
            "INSERT INTO retrieval_units(retrieval_unit_id) VALUES (?)", [("unit-a",), ("unit-b",)]
        )
        self.connection.commit()

    def _connection(self):
        return self.connection

    @contextmanager
    def _write(self):
        try:
            self.connection.execute("BEGIN")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


class RevocableSQLiteVecConnection:
    """A real connection whose sqlite-vec functions a test can revoke mid-life.

    sqlite-vec is loaded into a live *connection*, not into the database file,
    so a connection can genuinely stop answering ``vec_version()`` while
    ordinary SQL keeps working (a pooled store handing back a recycled
    connection, an extension unload).  Revoking is the only way to reproduce
    that without a second SQLite build, and everything else delegates to the
    real connection so the backend still runs real SQL.
    """

    def __init__(self, delegate: sqlite3.Connection) -> None:
        self._delegate = delegate
        self.sqlite_vec_revoked = False

    def execute(self, sql: str, *parameters):
        if self.sqlite_vec_revoked and "vec_version()" in sql:
            raise sqlite3.OperationalError("no such function: vec_version")
        return self._delegate.execute(sql, *parameters)

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)


class RevocableSQLiteVecStore(InMemorySQLiteStore):
    """An in-memory store that always hands out one revocable connection view."""

    def __init__(self) -> None:
        super().__init__()
        self.connection_view = RevocableSQLiteVecConnection(self.connection)

    def _connection(self):
        return self.connection_view


class StubbedSQLiteVecConnection:
    """A connection double that accepts the extension but can misreport its version.

    ``load_sqlite_vec`` needs only these four members, none of which require
    ``sqlite3.Connection.enable_load_extension``.  That lets the cached-branch
    health check be exercised even on interpreters that cannot load SQLite
    extensions at all, where no real vec0 table can exist.
    """

    def __init__(self) -> None:
        # Construction must see a conforming version so the backend caches this
        # connection; a test then rewrites the row to degrade it afterwards.
        self.version_row: tuple | None = ("v0.1.9",)

    def enable_load_extension(self, enabled: bool) -> None:
        pass

    def load_extension(self, path: str) -> None:
        pass

    def commit(self) -> None:
        pass

    def execute(self, sql: str, *parameters):
        if "vec_version()" not in sql:
            return SimpleNamespace(fetchone=lambda: None, fetchall=list)
        row = self.version_row
        return SimpleNamespace(fetchone=lambda: row)


def _record(unit_id: str, vector: tuple[float, ...], *, profile: str = "local-embed-v1") -> DerivedVectorRecord:
    content = f"derived content for {unit_id}"
    return DerivedVectorRecord(
        retrieval_unit_id=unit_id,
        passage_id=f"passage-{unit_id}",
        start_codepoint=0,
        end_codepoint=len(content),
        content_sha256=sha256(content.encode("utf-8")).hexdigest(),
        embedding_profile=profile,
        vector=vector,
    )


@unittest.skipUnless(EXTENSION_LOADING_SUPPORTED, SKIP_REASON)
class SQLiteVecDerivedVectorBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemorySQLiteStore()
        self.backend = SQLiteVecDerivedVectorBackend(self.store)

    def tearDown(self) -> None:
        self.store.connection.close()

    def test_persists_and_queries_profile_dimension_specific_vectors(self) -> None:
        first = _record("unit-a", (1.0, 0.0))
        second = _record("unit-b", (0.0, 1.0))
        self.backend.upsert(first)
        self.backend.upsert(second)

        rows = self.backend.search((1.0, 0.0), embedding_profile="local-embed-v1", limit=2)
        self.assertEqual([row.retrieval_unit_id for row in rows], ["unit-a", "unit-b"])
        self.assertEqual(rows[0].vector, (1.0, 0.0))
        self.assertEqual(self.backend.search((1.0, 0.0), embedding_profile="other", limit=2), [])

    def test_rejects_rebinding_a_retrieval_unit_to_other_source_identity(self) -> None:
        self.backend.upsert(_record("unit-a", (1.0, 0.0)))
        changed = DerivedVectorRecord(
            retrieval_unit_id="unit-a",
            passage_id="different-passage",
            start_codepoint=0,
            end_codepoint=1,
            content_sha256="0" * 64,
            embedding_profile="local-embed-v1",
            vector=(1.0, 0.0),
        )
        with self.assertRaisesRegex(VectorIndexError, "cannot be rebound"):
            self.backend.upsert(changed)

    def test_healthcheck_proves_the_active_connection_still_has_sqlite_vec(self) -> None:
        health = self.backend.healthcheck()
        self.assertTrue(health.version)

    def test_healthcheck_reports_unavailable_when_the_cached_connection_loses_sqlite_vec(self) -> None:
        """A degraded connection must fail closed with the domain error type.

        Startup success is not durable: the extension lives in the connection.
        ``services.epub_runtime._runtime_status`` converts *only*
        ``SQLiteVecUnavailable`` into the ``degraded`` vector-subsystem payload
        that ``GET /api/v1/epub/admin/runtime-status`` must report; any other
        exception escapes as an opaque 503 and hides which subsystem failed.
        """
        store = RevocableSQLiteVecStore()
        self.addCleanup(store.connection.close)
        backend = SQLiteVecDerivedVectorBackend(store)
        # Loading happened during construction, so this second call is the
        # cached branch that re-proves the connection with real SQL.
        self.assertTrue(backend.healthcheck().version)

        store.connection_view.sqlite_vec_revoked = True

        with self.assertRaises(SQLiteVecUnavailable) as raised:
            backend.healthcheck()
        self.assertIn("SQL health check", str(raised.exception))


class SQLiteVecCachedConnectionHealthTest(unittest.TestCase):
    """Cover the cached-connection health check on any interpreter.

    This case deliberately sits outside the loadable-extension guard above: a
    connection double reaches the same branch without a real vec0 table, so
    the fail-closed contract stays protected even where the extension cannot
    be loaded.
    """

    def test_healthcheck_reports_unavailable_when_vec_version_stops_returning_a_version(self) -> None:
        connection = StubbedSQLiteVecConnection()
        backend = SQLiteVecDerivedVectorBackend(SimpleNamespace(_connection=lambda: connection))
        self.assertTrue(backend.healthcheck().version)

        connection.version_row = (None,)

        with self.assertRaises(SQLiteVecUnavailable) as raised:
            backend.healthcheck()
        self.assertIn("did not return a version", str(raised.exception))

    def test_healthcheck_reports_unavailable_when_vec_version_returns_no_row(self) -> None:
        connection = StubbedSQLiteVecConnection()
        backend = SQLiteVecDerivedVectorBackend(SimpleNamespace(_connection=lambda: connection))

        connection.version_row = None

        with self.assertRaises(SQLiteVecUnavailable):
            backend.healthcheck()


if __name__ == "__main__":
    unittest.main()
