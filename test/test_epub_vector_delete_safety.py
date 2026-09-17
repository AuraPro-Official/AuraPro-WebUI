"""Safety net for removing derived EPUB vectors, and for the state around it.

Nothing in the product deletes a vector yet.  These tests exist because the
primitives a delete needs are the ones nothing was exercising, and three real
hazards lived in that gap undetected:

  * the vec0 virtual table can hold no foreign key, so a metadata row and its
    vector can be orphaned from each other and no SQLite integrity check
    notices;
  * the rowid allocator read its high-water mark out of the very table a
    delete empties, so one delete permanently poisoned the next insert;
  * the vector backend called ``commit()`` on a connection it had borrowed
    from the store, which ended the store's transaction mid-flight and made a
    later rollback silently partial.

What hid all three is stated plainly, because it is the real lesson: no test
anywhere wired a real :class:`SQLiteEpubStore` to a real
:class:`SQLiteVecDerivedVectorBackend`.  ``test_epub_sqlite_vec_backend``
builds its own two-row ``:memory:`` schema, which is the right shape for what
it checks and cannot see any of this.  The harness below is therefore the
point of this module; the assertions are what it made visible.

Fixtures follow the invented tidal-observation corpus used by the other EPUB
tests (see the note at the top of ``test_epub_search``).  The acceptance corpus
is a copyrighted book and none of its text, headings or concept names appear in
this repository.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest


BACKEND = Path(__file__).resolve().parents[1] / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# As in test_epub_retrieval_units: the store, the vector backend and the EPUB
# application service do not need Open WebUI's CLI bootstrap dependencies.
if 'open_webui' not in sys.modules:
    package = types.ModuleType('open_webui')
    package.__path__ = [str(BACKEND / 'open_webui')]
    sys.modules['open_webui'] = package

from open_webui.retrieval.epub.search import EpubSearchService  # noqa: E402
from open_webui.retrieval.epub.sqlite_vec_backend import SQLiteVecDerivedVectorBackend  # noqa: E402
from open_webui.retrieval.epub.sqlite_vec_backend import _table_name  # noqa: E402
from open_webui.retrieval.epub.store import IntegrityError, SQLiteEpubStore  # noqa: E402
from open_webui.retrieval.epub.inference import ModelAvailability  # noqa: E402
from open_webui.retrieval.epub.vector_index import (  # noqa: E402
    DerivedVectorIndexer,
    DerivedVectorRecord,
    InMemoryDerivedVectorBackend,
)
from open_webui.services.epub_concept import EpubConceptService  # noqa: E402


# Same guard, and same reason, as test_epub_sqlite_vec_backend: not every
# CPython build can load SQLite extensions.  CI's can (scripts/epub_test_env.sh
# installs sqlite-vec there), so this protects developer machines only.
EXTENSION_LOADING_SUPPORTED = hasattr(sqlite3.Connection, 'enable_load_extension')
SKIP_REASON = (
    'sqlite3.Connection.enable_load_extension is unavailable; this CPython was built '
    'without loadable SQLite extension support. Provision a supported interpreter with '
    'scripts/epub_test_env.sh.'
)

PROFILE = 'tidal-embed-v1'
OTHER_PROFILE = 'tidal-embed-v2'

# Three invented passages from the tidal-observation corpus.  Their only job
# here is to be immutable source text that a retrieval unit can prove itself a
# faithful window of, so the vector rows under test are real rows and not
# fixtures that skipped the store's own invariants.
PASSAGES = (
    ('p-hub', '全域潮汐枢纽按班次编排全网的观测时序。'),
    ('p-buoy', '浮标阵列在每个班次回传一次水位读数。'),
    ('p-cable', '锚站的缆索在汛期观测中承受最大拉力。'),
)


class FakeEmbeddings:
    """The local-embedding seam, shaped as in test_epub_local_inference."""

    def __init__(self, *, profile: str = PROFILE, vectors: list[list[float]] | None = None):
        self.profile = profile
        self.vectors = vectors if vectors is not None else [[1.0, 0.0]]
        self.calls: list[list[str]] = []

    def availability(self) -> ModelAvailability:
        return ModelAvailability.ready('local-embedding')

    def embed(self, texts):
        self.calls.append(list(texts))
        return self.vectors


class RealStoreHarness(unittest.TestCase):
    """A real ``SQLiteEpubStore`` on a real file, with real source rows.

    A file rather than ``:memory:`` so a test can close the store and reopen
    it, which is the only way to check that durable state (the rowid
    high-water mark) really is durable and not a happy accident of one live
    connection.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = str(Path(self.temporary.name) / 'epub.db')
        self.store = self._open_store()
        self.store.create_book('潮汐观测总志', book_id='book')
        self.store.create_book_version('book', epub_bytes=b'invented tidal archive', version_id='version')
        self.store.add_passages(
            'version',
            [
                {
                    'passage_id': passage_id,
                    'source_href': 'chapter.xhtml',
                    'spine_index': 0,
                    'ordinal': ordinal,
                    'content_kind': 'paragraph',
                    'content': content,
                }
                for ordinal, (passage_id, content) in enumerate(PASSAGES)
            ],
        )

    def _open_store(self) -> SQLiteEpubStore:
        store = SQLiteEpubStore(self.path)
        self.addCleanup(store.close)
        return store

    def _unit(self, passage_id: str, *, profile: str | None = PROFILE, vector_state: str = 'PENDING') -> str:
        content = dict(PASSAGES)[passage_id]
        return self.store.add_retrieval_unit(
            passage_id, 0, len(content), embedding_profile=profile, vector_state=vector_state
        )

    def _record(self, unit_id: str, passage_id: str, vector: tuple[float, ...], *, profile: str = PROFILE):
        content = dict(PASSAGES)[passage_id]
        return DerivedVectorRecord(
            retrieval_unit_id=unit_id,
            passage_id=passage_id,
            start_codepoint=0,
            end_codepoint=len(content),
            content_sha256=sha256(content.encode('utf-8')).hexdigest(),
            embedding_profile=profile,
            vector=vector,
        )

    def _metadata_ids(self, store: SQLiteEpubStore | None = None) -> set[str]:
        connection = (store or self.store)._connection()
        return {row[0] for row in connection.execute('SELECT retrieval_unit_id FROM epub_derived_vectors')}

    def _vec_rowids(self, profile: str = PROFILE, dimension: int = 2) -> list[int]:
        table = _table_name(profile, dimension)
        connection = self.store._connection()
        if connection.execute('SELECT 1 FROM sqlite_master WHERE name = ?', (table,)).fetchone() is None:
            return []
        return sorted(int(row[0]) for row in connection.execute(f'SELECT rowid FROM "{table}"'))


@unittest.skipUnless(EXTENSION_LOADING_SUPPORTED, SKIP_REASON)
class DerivedVectorDeleteTest(RealStoreHarness):
    """Removal keeps the two halves of one derived vector in step."""

    def setUp(self) -> None:
        super().setUp()
        self.backend = SQLiteVecDerivedVectorBackend(self.store)
        self.hub = self._unit('p-hub')
        self.buoy = self._unit('p-buoy')
        self.cable = self._unit('p-cable')
        self.backend.upsert(self._record(self.hub, 'p-hub', (1.0, 0.0)))
        self.backend.upsert(self._record(self.buoy, 'p-buoy', (0.0, 1.0)))

    def test_delete_removes_the_vec0_row_and_its_metadata_row_together(self) -> None:
        self.assertEqual(self._metadata_ids(), {self.hub, self.buoy})
        self.assertEqual(self._vec_rowids(), [1, 2])

        self.assertTrue(self.backend.delete(self.hub))

        # Neither side may be left behind: a metadata row whose vector is gone
        # makes search offer a candidate it cannot score, and a vector whose
        # metadata is gone is invisible to every query this module issues while
        # still occupying its rowid.
        self.assertEqual(self._metadata_ids(), {self.buoy})
        self.assertEqual(self._vec_rowids(), [2])
        self.assertEqual(
            [row.retrieval_unit_id for row in self.backend.search((1.0, 0.0), embedding_profile=PROFILE, limit=5)],
            [self.buoy],
        )

    def test_deleting_a_unit_the_backend_does_not_hold_is_a_no_op(self) -> None:
        """Absence reports False rather than raising, and both backends agree.

        Idempotence is the contract a resumable uninstall needs: the second
        attempt must not fail on rows the interrupted first attempt already
        removed.
        """
        self.assertFalse(self.backend.delete('no-such-retrieval-unit'))
        self.assertTrue(self.backend.delete(self.hub))
        self.assertFalse(self.backend.delete(self.hub))

        in_memory = InMemoryDerivedVectorBackend()
        in_memory.upsert(self._record(self.hub, 'p-hub', (1.0, 0.0)))
        self.assertFalse(in_memory.delete('no-such-retrieval-unit'))
        self.assertTrue(in_memory.delete(self.hub))
        self.assertFalse(in_memory.delete(self.hub))

    def test_delete_many_counts_only_the_units_it_actually_held(self) -> None:
        removed = self.backend.delete_many([self.hub, 'no-such-retrieval-unit', self.buoy, self.hub])

        self.assertEqual(removed, 2)
        self.assertEqual(self._metadata_ids(), set())
        self.assertEqual(self._vec_rowids(), [])
        self.assertEqual(InMemoryDerivedVectorBackend().delete_many([]), 0)

    def test_a_deleted_rowid_is_never_re_issued_to_another_unit(self) -> None:
        """Insert-after-delete must step over the freed rowid, not recycle it.

        The old allocator was ``MAX(vector_rowid) + 1`` over
        ``epub_derived_vectors`` -- the table a delete empties a row from -- so
        the mark fell back down and the next insert handed rowid 2, just
        freed, to a different retrieval unit.  A vec0 rowid is the only thing
        the search JOIN matches metadata on, so re-issuing one re-points every
        surviving reference to it at another passage's vector.  A wrong
        citation, not a missing one.
        """
        self.assertTrue(self.backend.delete(self.buoy))

        self.backend.upsert(self._record(self.cable, 'p-cable', (0.5, 0.5)))

        self.assertEqual(self._metadata_ids(), {self.hub, self.cable})
        self.assertEqual(self._vec_rowids(), [1, 3])
        self.assertEqual(
            {row.retrieval_unit_id for row in self.backend.search((0.5, 0.5), embedding_profile=PROFILE, limit=5)},
            {self.hub, self.cable},
        )

    def test_an_orphaned_vector_cannot_jam_the_allocator_for_good(self) -> None:
        """The permanent-lockout case, and the sharper half of the hazard.

        ``delete_many`` removes both sides together, so this backend does not
        create orphans.  An interrupted delete or a hand-written one can, and
        vec0 holds no foreign key, so nothing in SQLite notices -- the orphan
        is forced here with raw SQL because that is the only way to produce
        it.  ``unit-buoy`` holds the highest rowid, so orphaning *it* is what
        drops the metadata MAX below a rowid the vec0 table still holds: the
        old allocator then re-issued 2, hit the live orphan, and failed
        ``UNIQUE constraint failed`` -- and kept failing, for this profile,
        for good, because nothing ever raised the mark again.  The high-water
        mark is not stored in the table the orphaning emptied, so it steps
        past instead.
        """
        with self.store._write() as connection:
            connection.execute('DELETE FROM epub_derived_vectors WHERE retrieval_unit_id = ?', (self.buoy,))

        self.assertEqual(self._vec_rowids(), [1, 2])
        self.assertEqual(self._metadata_ids(), {self.hub})
        self.assertEqual(self.store._connection().execute('PRAGMA foreign_key_check').fetchall(), [])

        self.backend.upsert(self._record(self.cable, 'p-cable', (0.5, 0.5)))

        self.assertEqual(self._vec_rowids(), [1, 2, 3])
        self.assertEqual(self._metadata_ids(), {self.hub, self.cable})

    def test_the_rowid_high_water_mark_survives_reopening_the_database(self) -> None:
        """A restart must not reset the mark, or the collision simply returns.

        This is why the mark is a durable table row and not a counter on the
        backend instance: the desktop app stops and starts, and every start
        builds a new store and a new backend over the same file.
        """
        self.assertTrue(self.backend.delete(self.buoy))
        self.store.close()

        self.store = self._open_store()
        reopened = SQLiteVecDerivedVectorBackend(self.store)
        reopened.upsert(self._record(self.cable, 'p-cable', (0.5, 0.5)))

        self.assertEqual(self._vec_rowids(), [1, 3])

    def test_a_delete_leaves_no_row_the_metadata_foreign_key_would_have_caught(self) -> None:
        """The metadata side is FK-protected; the vec0 side is what needs us.

        ``epub_derived_vectors.retrieval_unit_id`` really does reference
        ``retrieval_units``, so SQLite refuses to strand it.  The vec0 table
        declares nothing, which is why its row has to be removed in the same
        breath and cannot be left to the database.
        """
        self.backend.delete(self.hub)

        connection = self.store._connection()
        self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        orphans = connection.execute(
            f'''SELECT vectors.rowid FROM "{_table_name(PROFILE, 2)}" AS vectors
                LEFT JOIN epub_derived_vectors AS metadata
                  ON metadata.vector_table = ? AND metadata.vector_rowid = vectors.rowid
                WHERE metadata.retrieval_unit_id IS NULL''',
            (_table_name(PROFILE, 2),),
        ).fetchall()
        self.assertEqual(orphans, [])


@unittest.skipUnless(EXTENSION_LOADING_SUPPORTED, SKIP_REASON)
class VectorBackendTransactionOwnershipTest(RealStoreHarness):
    """The vector backend must never end a transaction it did not open."""

    def setUp(self) -> None:
        super().setUp()
        self.backend = SQLiteVecDerivedVectorBackend(self.store)
        self.hub = self._unit('p-hub')

    def test_a_callers_rollback_undoes_the_vector_backends_work(self) -> None:
        """The regression test for the bare ``commit()``.

        Before the fix this failed: ``_ensure_table`` committed the store's
        open transaction, so by the time the caller raised there was nothing
        left to roll back, and both the metadata row and the vec0 row
        survived a transaction that had explicitly failed.
        """
        with self.assertRaises(RuntimeError):
            with self.store._write() as connection:
                connection.execute(
                    'UPDATE retrieval_units SET vector_state = ? WHERE retrieval_unit_id = ?', ('READY', self.hub)
                )
                self.backend.upsert(self._record(self.hub, 'p-hub', (1.0, 0.0)))
                raise RuntimeError('the caller abandons this unit of work')

        # Everything inside the transaction is gone, the store's own row and
        # the vector backend's alike.  Partial survival is the failure mode:
        # a unit marked READY whose vector was rolled back, or the reverse.
        self.assertEqual(self._metadata_ids(), set())
        self.assertEqual(self._vec_rowids(), [])
        self.assertEqual(self.store.get_retrieval_unit(self.hub)['vector_state'], 'PENDING')

    def test_a_callers_commit_keeps_the_vector_backends_work(self) -> None:
        """The other half: joining a transaction must not mean writing nothing."""
        with self.store._write() as connection:
            connection.execute(
                'UPDATE retrieval_units SET vector_state = ? WHERE retrieval_unit_id = ?', ('READY', self.hub)
            )
            self.backend.upsert(self._record(self.hub, 'p-hub', (1.0, 0.0)))

        self.assertEqual(self._metadata_ids(), {self.hub})
        self.assertEqual(self._vec_rowids(), [1])
        self.assertEqual(self.store.get_retrieval_unit(self.hub)['vector_state'], 'READY')

    def test_the_backend_leaves_the_callers_transaction_open(self) -> None:
        """State the ownership rule directly, not only through its consequence."""
        connection = self.store._connection()
        with self.store._write():
            self.assertTrue(connection.in_transaction)
            self.backend.upsert(self._record(self.hub, 'p-hub', (1.0, 0.0)))
            self.assertTrue(connection.in_transaction)
            self.backend.delete(self.hub)
            self.assertTrue(connection.in_transaction)

    def test_the_backend_still_writes_durably_with_no_caller_transaction(self) -> None:
        """Removing the commits must not make autocommit writes disappear."""
        self.backend.upsert(self._record(self.hub, 'p-hub', (1.0, 0.0)))
        self.assertFalse(self.store._connection().in_transaction)
        self.store.close()

        self.store = self._open_store()
        SQLiteVecDerivedVectorBackend(self.store)
        self.assertEqual(self._metadata_ids(), {self.hub})
        self.assertEqual(self._vec_rowids(), [1])


class VersionIndexProfileFilterTest(RealStoreHarness):
    """A bulk index considers only the configured embedding profile's units.

    The indexer here is the real :class:`DerivedVectorIndexer`, not a double,
    because the behaviour under test starts with its genuine refusal to embed
    a unit belonging to another profile.  A fake that merely simulated the
    refusal would let the test keep passing if that rule ever changed.
    """

    def setUp(self) -> None:
        super().setUp()
        self.old_ready = self._unit('p-hub', profile=OTHER_PROFILE, vector_state='READY')
        self.old_pending = self._unit('p-buoy', profile=OTHER_PROFILE, vector_state='PENDING')
        self.current = self._unit('p-cable', profile=PROFILE, vector_state='PENDING')
        self.embeddings = FakeEmbeddings(profile=PROFILE)
        self.service = EpubConceptService(
            store=self.store,
            retrieval_embedding_profile=PROFILE,
            vector_indexer=DerivedVectorIndexer(
                source=self.store, embeddings=self.embeddings, backend=InMemoryDerivedVectorBackend()
            ),
        )

    def _states(self) -> dict[str, str]:
        return {
            unit_id: self.store.get_retrieval_unit(unit_id)['vector_state']
            for unit_id in (self.old_ready, self.old_pending, self.current)
        }

    def test_another_profiles_units_are_left_alone_and_not_reported_as_failures(self) -> None:
        result = self.service.index_version_retrieval_units('version')

        self.assertEqual(result['total_retrieval_units'], 1)
        self.assertEqual(result['selected_retrieval_units'], 1)
        self.assertEqual(result['skipped_other_profile'], 2)
        self.assertEqual(result['ready'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['error_count'], 0)
        self.assertEqual(result['errors'], [])
        self.assertEqual(
            self._states(),
            {self.old_ready: 'READY', self.old_pending: 'PENDING', self.current: 'READY'},
        )

    def test_rebuild_under_a_changed_profile_leaves_ready_units_ready(self) -> None:
        """The self-locking case: a rebuild used to destroy the good record.

        Before the filter, ``rebuild`` selected every unit in the version, the
        indexer refused the previous profile's, and the refusal was written
        back as ``FAILED`` -- erasing the only evidence that those vectors were
        fine.  The following non-rebuild run then re-selected them, because
        they were no longer ``READY``, and failed them again, for ever.
        """
        first = self.service.index_version_retrieval_units('version', rebuild=True)

        self.assertEqual(first['failed'], 0)
        self.assertEqual(self._states()[self.old_ready], 'READY')

        # The run after the rebuild is the half that proves the lock is gone:
        # it must find nothing left to do rather than the previous profile's
        # units freshly marked FAILED.
        second = self.service.index_version_retrieval_units('version')

        self.assertEqual(second['selected_retrieval_units'], 0)
        self.assertEqual(second['failed'], 0)
        self.assertEqual(
            self._states(),
            {self.old_ready: 'READY', self.old_pending: 'PENDING', self.current: 'READY'},
        )

    def test_a_unit_that_genuinely_fails_to_embed_is_still_recorded_as_failed(self) -> None:
        """The filter must not have bought quiet by swallowing real failures."""
        self.embeddings.vectors = [[1.0, 0.0], [0.0, 1.0]]

        result = self.service.index_version_retrieval_units('version')

        self.assertEqual(result['ready'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['error_count'], 1)
        self.assertEqual(result['errors'][0]['retrieval_unit_id'], self.current)
        self.assertEqual(self._states()[self.current], 'FAILED')


class CurrentVersionPointerTest(RealStoreHarness):
    """``books.current_version_id`` must never outlive the version it names."""

    def _delete_version(self, version_id: str) -> None:
        """Remove a version the way a deletion feature eventually will.

        Raw SQL on purpose: the invariant has to hold for a caller that does
        not route through a helper, which is the whole reason it is a trigger.
        """
        with self.store._write() as connection:
            connection.execute('DELETE FROM retrieval_units WHERE passage_id IN (SELECT passage_id FROM passages)')
            connection.execute('DELETE FROM passages WHERE version_id = ?', (version_id,))
            connection.execute('DELETE FROM epub_blobs WHERE version_id = ?', (version_id,))
            connection.execute('DELETE FROM book_versions WHERE version_id = ?', (version_id,))

    def test_deleting_a_version_clears_the_pointer_that_names_it(self) -> None:
        self.store.set_version_status('version', 'READY')
        self.assertEqual(self.store.get_book('book')['current_version_id'], 'version')

        self._delete_version('version')

        self.assertIsNone(self.store.get_book('book')['current_version_id'])
        self.assertEqual(self.store.dangling_current_version_book_ids(), [])
        # And the catalogue read that joins through the pointer still works.
        self.assertEqual(self.store.list_books()[0]['current_version_status'], None)

    def test_a_dangling_pointer_is_invisible_to_sqlites_own_integrity_check(self) -> None:
        """Why a trigger, and why a bespoke check beside it.

        The column declares no ``REFERENCES`` clause, so ``foreign_key_check``
        has nothing to check and reports a database with a dangling pointer as
        perfectly healthy.  Forced here by writing the bad state directly,
        which is the only way to produce it now that the trigger exists.
        """
        self.store.set_version_status('version', 'READY')
        with self.store._write() as connection:
            connection.execute("UPDATE books SET current_version_id = 'version-that-never-existed'")

        self.assertEqual(self.store._connection().execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.store.dangling_current_version_book_ids(), ['book'])

    def test_set_current_version_repoints_and_clears(self) -> None:
        self.store.create_book_version('book', epub_bytes=b'a second tidal archive', version_id='version-2')
        self.store.set_version_status('version', 'READY')

        self.store.set_current_version('book', 'version-2')
        self.assertEqual(self.store.get_book('book')['current_version_id'], 'version-2')

        self.store.set_current_version('book', None)
        self.assertIsNone(self.store.get_book('book')['current_version_id'])

    def test_set_current_version_refuses_a_version_of_another_book(self) -> None:
        self.store.create_book('另一部观测志', book_id='other-book')
        self.store.create_book_version('other-book', epub_bytes=b'another tidal archive', version_id='other-version')

        with self.assertRaisesRegex(IntegrityError, 'another book version'):
            self.store.set_current_version('book', 'other-version')
        with self.assertRaisesRegex(IntegrityError, 'unknown version_id'):
            self.store.set_current_version('book', 'version-that-never-existed')
        with self.assertRaisesRegex(IntegrityError, 'unknown book_id'):
            self.store.set_current_version('no-such-book', None)

        self.assertIsNone(self.store.get_book('book')['current_version_id'])


class EmptyVectorIndexBackend:
    """A backend that answers, correctly, that it holds nothing.

    This is what the real sqlite-vec backend does when the configured
    profile's vec0 table does not exist, and it is the shape of the bug: a
    successful call returning no rows.
    """

    def search(self, query_vector, *, embedding_profile: str, limit: int):
        return []


class VectorIndexMissingIsReportedTest(RealStoreHarness):
    """An empty index must be said out loud, not served as a normal answer."""

    def _degraded_reasons(self, service: EpubSearchService) -> list[str]:
        response = service.search('全域潮汐枢纽的班次')
        return [
            marker.reason or ''
            for marker in response.degraded
            if marker.component == 'local-vector-search' and not marker.available
        ]

    def test_an_empty_pool_reports_a_degraded_vector_channel(self) -> None:
        service = EpubSearchService(
            source=self.store,
            vector_backend=EmptyVectorIndexBackend(),
            embeddings=FakeEmbeddings(),
        )

        reasons = self._degraded_reasons(service)

        self.assertEqual(len(reasons), 1)
        self.assertIn('no vectors are indexed for the configured embedding profile', reasons[0])
        self.assertIn('graph-only', reasons[0])

    @unittest.skipUnless(EXTENSION_LOADING_SUPPORTED, SKIP_REASON)
    def test_a_profile_mismatch_against_the_real_backend_is_reported(self) -> None:
        """The real thing: vectors indexed, but under a different profile.

        The vec0 tables are named per profile, so the table this query looks
        for genuinely does not exist.  Without a marker the response is
        indistinguishable from a book with no semantic match, which is exactly
        how a mis-set embedding model stayed invisible.
        """
        backend = SQLiteVecDerivedVectorBackend(self.store)
        indexed = self._unit('p-hub', profile=OTHER_PROFILE)
        backend.upsert(self._record(indexed, 'p-hub', (1.0, 0.0), profile=OTHER_PROFILE))

        service = EpubSearchService(
            source=self.store,
            vector_backend=backend,
            embeddings=FakeEmbeddings(profile=PROFILE),
        )

        reasons = self._degraded_reasons(service)

        self.assertEqual(len(reasons), 1)
        self.assertIn('re-index the affected versions under the new profile', reasons[0])
        # The vectors themselves are fine; nothing is wrong except which
        # profile is configured, and searching under theirs still finds them.
        self.assertEqual(
            [row.retrieval_unit_id for row in backend.search((1.0, 0.0), embedding_profile=OTHER_PROFILE, limit=5)],
            [indexed],
        )


if __name__ == '__main__':
    unittest.main()
