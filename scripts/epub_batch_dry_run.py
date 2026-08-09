#!/usr/bin/env python3
"""Measure what is actually inside a section-graph job's failed Batch packets.

This is a *thin driver* over the production
:meth:`open_webui.retrieval.epub.batch.BatchJobService.dry_run_failed_packets`.
It contains no grounding, resolution or classification logic of its own: the
spans are located by the same ``_resolve_evidence_span`` that cloud ingest uses,
through the same read-only ``_ground_section_graph_payload`` pass, over the same
immutable passages.  If this harness ever appears to classify a span differently
from ingest, the harness is wrong -- do not "fix" it by adding grounding code
here.  It exists to answer one question the durable store cannot:

  A FAILED item stores no ``response_json`` -- ``ingest_success`` is that
  column's only writer -- so the packet it was rejected for exists nowhere
  locally.  Nobody can say from the store how many of a failed packet's ~25
  spans were ungrounded, or how many valid concepts, mentions and relations sat
  beside them.  Every recovery figure so far has been extrapolated from the
  succeeded packets' average.  This replaces the extrapolation with a count.

The count it produced -- 183 evidence spans across job 31efbf3b's ten failed
packets, of which 17 were ungrounded, discarding 78 concepts, 78 mentions and
51 relations -- is what SDD 4.2.2 point 6d was decided from.  That rule then
absorbed this harness's central behaviour: dropping a claim-level rejection and
carrying on is now what *ingest* does.  So this remains useful as a preview of
what a re-ingest will write, and as the only place a packet's losses are broken
down by reason slug, which an item row deliberately does not keep.

Read ``spans_skipped_by_ingest`` beside ``spans_failed``.  A probe also
classifies the rejections ingest still refuses to drop -- an unavailable
passage, an anchor defect -- so where the two numbers differ, the grounded
counts on that row are what the packet *contains*, not what a re-ingest would
recover.

--------------------------------------------------------------------------
WHAT IT COSTS, AND WHAT IT CHANGES
--------------------------------------------------------------------------
Nothing, and nothing.

  * ``batches.retrieve`` plus an output-file download are the only remote calls.
    Both are free.  No job is submitted and no new remote work is created.  If
    this script ever appears about to call ``submit``, that is a bug: it would
    cost money and is not what this is for.
  * It writes nothing.  ``dry_run_failed_packets`` never records a provider
    state, never ingests, never records or reconciles a failure.  That mattered
    when the rule it informed was still open; it matters now because a preview
    of a write must never be able to perform one.

For the second guarantee to be checkable rather than merely asserted, the
harness never opens the nominated store at all.  It takes a ``VACUUM INTO``
snapshot first -- with a plain read-only ``sqlite3`` connection, not application
code -- and every subsequent line runs against the copy.  ``VACUUM INTO`` rather
than ``cp`` because a plain copy silently drops the WAL, and other processes may
be writing to the live store while this runs.

--------------------------------------------------------------------------
RUNNING IT
--------------------------------------------------------------------------
    ./scripts/epub_test_env.sh          # provisions the venv, once
    source ~/.zshrc >/dev/null 2>&1     # exports the Batch credential
    ~/.cache/aurapro/epub-test-venv/bin/python scripts/epub_batch_dry_run.py \
        31efbf3b-1e09-493a-9da2-ddff7e9f498c

The credential is read from ``EPUB_CONCEPT_BATCH_OPENAI_API_KEY``, exactly as
``open_webui.services.epub_runtime._batch_providers`` reads it, and is passed
straight to the SDK adapter.  It is never printed, logged, written to the
snapshot, or included in an error message -- this file only ever reports whether
the variable was *set*.

The store defaults to the local E2E path recorded in
``docs/specs/epub_concept_task_status.md``; override with ``--db`` or
``EPUB_CONCEPT_DB_PATH``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / 'backend'))

from open_webui.retrieval.epub.batch import (  # noqa: E402
    BatchJobService,
    OpenAIBatchProvider,
    SQLiteBatchRepository,
)
from open_webui.retrieval.epub.store import SQLiteEpubStore  # noqa: E402
from open_webui.services.epub_concept import evidence_floors  # noqa: E402

DEFAULT_DB = '/private/tmp/aurapro-epub-e2e/epub_concept_v1.db'
API_KEY_VARIABLE = 'EPUB_CONCEPT_BATCH_OPENAI_API_KEY'

# The two claim-level classes (SDD 4.2.2 point 6d), aggregated apart because
# they are different findings.  EVIDENCE_ABSENT means the model quoted text that
# is not in the passage it named; EVIDENCE_AMBIGUOUS means the text *is* there,
# verbatim, more than once, and only the occurrence could not be resolved.
REPORTED_CLASSES = ('EVIDENCE_AMBIGUOUS', 'EVIDENCE_ABSENT')

BOLD, DIM, RESET = '\033[1m', '\033[2m', '\033[0m'
RED, GREEN, YELLOW, CYAN = '\033[31m', '\033[32m', '\033[33m', '\033[36m'


def c(text: str, *codes: str) -> str:
    if os.environ.get('NO_COLOR') or not sys.stdout.isatty():
        return text
    return ''.join(codes) + text + RESET


def snapshot(source: Path, destination: Path) -> None:
    """Take a consistent copy of a live store, WAL included.

    ``VACUUM INTO`` runs inside a read transaction, so it captures committed WAL
    content that a file copy would leave behind, and it cannot mutate the source.
    The connection is opened read-only and with plain ``sqlite3`` on purpose:
    the store class migrates on construction, which is a write, and the whole
    guarantee of this harness is that no application code ever touches the
    original.
    """
    if destination.exists():
        raise SystemExit(f'refusing to overwrite an existing snapshot: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    try:
        connection.execute('VACUUM INTO ?', (str(destination),))
    finally:
        connection.close()


def graph_fingerprint(path: Path) -> dict[str, Any]:
    """Row counts plus a content checksum for everything a write would disturb.

    Taken before and after the measurement and compared.  Counting rows alone
    would miss an in-place UPDATE -- a status flipped, a ``response_json``
    rewritten -- which is precisely the failure mode worth ruling out, so each
    table also contributes a checksum over its own bytes.
    """
    tables = (
        'batch_jobs',
        'batch_items',
        'concepts',
        'concept_aliases',
        'concept_mentions',
        'concept_relations',
        'concept_relation_assertions',
        'concept_relation_evidence',
    )
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        fingerprint: dict[str, Any] = {}
        for table in tables:
            rows = connection.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
            fingerprint[table] = {
                'rows': len(rows),
                # A digest, not the rows: this value is printed, and the tables
                # it covers hold model output and source passages.  SHA-256
                # rather than ``hash`` so the same store yields the same value
                # in a later process and two runs stay comparable.
                'checksum': hashlib.sha256(repr(rows).encode('utf-8')).hexdigest()[:16],
            }
        return fingerprint
    finally:
        connection.close()


def measure(snapshot_path: Path, job_id: str, api_key: str) -> dict[str, Any]:
    """Run the production dry run against the snapshot and return its report."""
    store = SQLiteEpubStore(snapshot_path)
    try:
        service = BatchJobService(SQLiteBatchRepository(store, evidence_floors=evidence_floors()))
        provider = OpenAIBatchProvider(api_key=api_key)
        return service.dry_run_failed_packets(job_id, provider)
    finally:
        store.close()


def _packet_class(packet: dict[str, Any]) -> str:
    """Group a packet by the failure class its durable item actually recorded."""
    reason = packet.get('stored_reason')
    return str(reason) if reason else 'UNDIAGNOSED'


COLUMNS = (
    ('spans', 'evidence_spans'),
    ('absent', None),
    ('ambig', None),
    # Of the failed spans, the ones ingest would itself drop.  Where this is
    # lower than absent+ambig the packet still fails whole, so the grounded
    # columns to its right are what the packet holds, not what a re-ingest gets.
    ('skipped', 'spans_skipped_by_ingest'),
    ('floor', 'spans_below_floor'),
    ('concepts', 'concepts_grounded'),
    ('mentions', 'mentions_grounded'),
    ('relations', 'relations_grounded'),
    ('lost:end', 'relations_lost_to_dropped_endpoint'),
    ('lost:ev', 'relations_lost_without_evidence'),
)


def _cell(packet: dict[str, Any], key: str | None, header: str) -> int:
    if key is not None:
        return int(packet.get(key, 0))
    by_reason = packet.get('spans_failed_by_reason') or {}
    slug = 'EVIDENCE_ABSENT' if header == 'absent' else 'EVIDENCE_AMBIGUOUS'
    return int(by_reason.get(slug, 0))


def print_report(report: dict[str, Any]) -> None:
    packets = list(report.get('packets', []))
    print()
    print(c(f'job {report["job_id"]}  state={report["state"]}  failed items={report["failed_item_count"]}', BOLD))
    if report.get('results_pending_retrieval'):
        print(
            c(
                '  provider output could not be retrieved; nothing was measured and nothing was recorded. Re-run.',
                RED,
                BOLD,
            )
        )
        return

    header = f'  {"packet":<14}{"class":<20}' + ''.join(f'{name:>10}' for name, _ in COLUMNS)
    print()
    print(c(header, DIM))
    print(c('  ' + '-' * (len(header) - 2), DIM))
    for packet in sorted(packets, key=lambda item: (_packet_class(item), item['custom_id'])):
        # A packet identifier is a durable Batch custom_id, not content: it is
        # "<version>:section-graph:<n>" and names no passage text.
        label = str(packet['custom_id']).rsplit(':', 1)[-1]
        if not packet.get('grounded'):
            print(
                f'  {label:<14}{_packet_class(packet):<20}'
                + c(f'  not measurable: {packet.get("unmeasurable_reason")}', RED)
            )
            continue
        cells = ''.join(f'{_cell(packet, key, name):>10}' for name, key in COLUMNS)
        print(f'  {label:<14}{_packet_class(packet):<20}{cells}')

    print()
    print(c('  aggregates by the failure class the item durably recorded', BOLD))
    print()
    for failure_class in (
        *REPORTED_CLASSES,
        *sorted({_packet_class(packet) for packet in packets} - set(REPORTED_CLASSES)),
    ):
        group = [packet for packet in packets if _packet_class(packet) == failure_class]
        if not group:
            continue
        measured = [packet for packet in group if packet.get('grounded')]
        totals = {name: sum(_cell(packet, key, name) for packet in measured) for name, key in COLUMNS}
        colour = GREEN if failure_class == 'EVIDENCE_AMBIGUOUS' else YELLOW
        print(c(f'  {failure_class}  ({len(group)} packets, {len(measured)} measurable)', colour, BOLD))
        print(f'    evidence spans          {totals["spans"]}')
        print(f'    spans EVIDENCE_ABSENT   {totals["absent"]}')
        print(f'    spans EVIDENCE_AMBIG.   {totals["ambig"]}')
        print(f'    spans ingest would skip {totals["skipped"]}')
        print(f'    spans below the floor   {totals["floor"]}')
        print(c(f'    concepts recoverable    {totals["concepts"]}', BOLD))
        print(c(f'    mentions recoverable    {totals["mentions"]}', BOLD))
        print(c(f'    relations recoverable   {totals["relations"]}', BOLD))
        print(f'    relations lost, endpoint {totals["lost:end"]}')
        print(f'    relations lost, no evid. {totals["lost:ev"]}')
        print()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='epub_batch_dry_run.py',
        description="Classify a section-graph job's failed packets. Free, and writes nothing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('job_id', help='the SECTION_GRAPH batch_job_id to measure')
    parser.add_argument(
        '--db',
        default=os.environ.get('EPUB_CONCEPT_DB_PATH', DEFAULT_DB),
        help=f'live EPUB concept store, never opened directly (default: {DEFAULT_DB})',
    )
    parser.add_argument(
        '--snapshot', default=None, help='where to keep the VACUUM INTO copy (default: a temporary file)'
    )
    parser.add_argument('--json', action='store_true', help='emit the raw content-free report instead of the table')
    return parser.parse_args(list(argv))


def main(argv: Sequence[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    args = parse_args(argv)

    api_key = os.environ.get(API_KEY_VARIABLE, '').strip()
    if not api_key:
        # The variable's presence, never its value.
        raise SystemExit(
            f'{API_KEY_VARIABLE} is not set; the durable provider output cannot be '
            "re-fetched without it. Prefix the command with 'source ~/.zshrc'."
        )

    source = Path(args.db).expanduser()
    if not source.exists():
        raise SystemExit(f'EPUB store not found: {source}')

    with tempfile.TemporaryDirectory(prefix='epub-dry-run-') as workspace:
        snapshot_path = Path(args.snapshot).expanduser() if args.snapshot else Path(workspace) / 'snapshot.db'
        print(c(f'  VACUUM INTO {snapshot_path}', DIM), flush=True)
        snapshot(source, snapshot_path)
        before = graph_fingerprint(snapshot_path)

        report = measure(snapshot_path, args.job_id, api_key)

        after = graph_fingerprint(snapshot_path)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_report(report)

    # The claim this harness makes about itself, checked rather than asserted.
    # ``SQLiteEpubStore`` migrates on construction, so a genuinely untouched
    # file is not the guarantee available here; an unchanged Batch ledger and an
    # unchanged graph is, and it is the guarantee that matters.
    #
    # One expected exception, and only one: a snapshot taken from a store older
    # than the current SCHEMA_VERSION is migrated on open, and an ALTER TABLE
    # that adds a column changes that table's ``SELECT *`` shape and therefore
    # its checksum with no row having been written.  ``batch_items`` alone,
    # with its row count unchanged, on a store that was behind - that is the
    # migration.  Any other table, or any changed row count, is a real write.
    changed = [table for table in before if before[table] != after[table]]
    print(
        c(
            f'  write check: {len(before)} tables, {"UNCHANGED" if not changed else "CHANGED " + ", ".join(changed)}',
            GREEN if not changed else RED,
            BOLD,
        )
    )
    print(c(f'  batch_items rows {after["batch_items"]["rows"]} checksum {after["batch_items"]["checksum"]}', DIM))
    return 1 if changed else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
