#!/usr/bin/env python3
"""Interactive EPUB concept-search harness.

This is a *thin driver* over the production
:class:`open_webui.retrieval.epub.search.EpubSearchService`.  It contains no
ranking, matching, expansion, or excerpt logic of its own: it constructs the
same store, the same sqlite-vec backend, and the same local model adapters that
``open_webui.services.epub_runtime.initialize_epub_concept_service`` builds, then
prints what ``EpubSearchService.search`` returns.  If this harness ever appears
to score differently from the web app, the harness is wrong -- do not "fix" it
by adding ranking code here.

The only production code this file re-implements is the two three-line closures
that AuraPro's own startup uses to adapt the loaded models to the adapter
protocols:

  * ``open_webui.retrieval.utils.get_embedding_function``   (engine == '')
  * ``open_webui.retrieval.utils.get_reranking_function``   (engine != 'external')

They are copied rather than imported because importing
``open_webui.retrieval.utils`` drags in the whole WebUI configuration/database
stack (it requires ``WEBUI_SECRET_KEY`` and opens ``webui.db``), which a
read-only query harness has no business doing.  Model *loading* mirrors
``open_webui.routers.retrieval.get_ef`` / ``get_rf``.

--------------------------------------------------------------------------
WHICH INTERPRETER
--------------------------------------------------------------------------
Channel A (graph) needs no models at all and runs on the EPUB test venv:

    ./scripts/epub_test_env.sh          # provisions it, once
    ~/.cache/aurapro/epub-test-venv/bin/python scripts/epub_query.py "汛期观测"

Channels B (vector) and fused additionally need torch + sentence-transformers.
Those are NOT in the test venv, but they ARE in the Python that AuraPro Desktop
provisions, together with the BGE models this store was indexed with:

    PYTHONPATH=backend \
      "$HOME/Library/Application Support/aurapro/python/bin/python3" \
      scripts/epub_query.py "汛期观测"

Run with no query argument for a REPL; models load once, up front.

The store defaults to the local E2E path recorded in
``docs/specs/epub_concept_task_status.md``; override it with ``--db`` or with
``EPUB_CONCEPT_DB_PATH`` to point at a backup snapshot or another machine's copy.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / 'backend'))

from open_webui.retrieval.epub.inference import (  # noqa: E402
    AuraProEmbeddingAdapter,
    AuraProRerankDocument,
    AuraProRerankerAdapter,
    ModelAvailability,
)
from open_webui.retrieval.epub.search import (  # noqa: E402
    EpubSearchService,
    SearchError,
    SearchHit,
    SearchResponse,
)
from open_webui.retrieval.epub.sqlite_vec import SQLiteVecUnavailable  # noqa: E402
from open_webui.retrieval.epub.sqlite_vec_backend import (  # noqa: E402
    SQLiteVecDerivedVectorBackend,
)
from open_webui.retrieval.epub.store import SQLiteEpubStore  # noqa: E402

DEFAULT_DB = '/private/tmp/aurapro-epub-e2e/epub_concept_v1.db'
DEFAULT_RERANKER = 'BAAI/bge-reranker-base'


# ───────────────────────────── terminal helpers ──────────────────────────────

BOLD, DIM, RESET = '\033[1m', '\033[2m', '\033[0m'
RED, GREEN, YELLOW, BLUE, CYAN, MAGENTA = (
    '\033[31m',
    '\033[32m',
    '\033[33m',
    '\033[34m',
    '\033[36m',
    '\033[35m',
)


def _no_color() -> bool:
    return bool(os.environ.get('NO_COLOR')) or not sys.stdout.isatty()


def c(text: str, *codes: str) -> str:
    """Colorize unless colour is disabled."""
    if _no_color():
        return text
    return ''.join(codes) + text + RESET


def _char_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1


def display_width(text: str) -> int:
    return sum(_char_width(character) for character in text)


def truncate(text: str, width: int) -> str:
    """Truncate to a terminal *display* width, counting CJK as two columns."""
    text = text.replace('\n', '⏎').replace('\r', '')
    if display_width(text) <= width:
        return text
    out, used = [], 0
    for character in text:
        step = _char_width(character)
        if used + step > width - 1:
            break
        out.append(character)
        used += step
    return ''.join(out) + '…'


def wrap(text: str, width: int, indent: str) -> list[str]:
    """Hard-wrap by display width. CJK has no spaces, so wrap on columns."""
    text = text.replace('\r', '')
    lines: list[str] = []
    for paragraph in text.split('\n'):
        if not paragraph:
            lines.append(indent)
            continue
        current, used = [], 0
        for character in paragraph:
            step = _char_width(character)
            if used + step > width:
                lines.append(indent + ''.join(current))
                current, used = [], 0
            current.append(character)
            used += step
        if current:
            lines.append(indent + ''.join(current))
    return lines


def rule(label: str, width: int, colour: str = CYAN) -> str:
    head = f'── {label} '
    pad = max(0, width - display_width(head))
    return c(head + '─' * pad, colour)


# ───────────────────────── production model plumbing ─────────────────────────


class _BackgroundLoop:
    """A real asyncio loop on its own thread.

    ``AuraProEmbeddingAdapter`` exists precisely to bridge EPUB's synchronous
    worker-thread world to AuraPro's application event loop, and it refuses to
    run on the loop's own thread.  Reproducing that shape here means the harness
    exercises the same adapter code path the server does.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name='epub-query-loop', daemon=True)
        self._ready = threading.Event()
        self._thread.start()
        self._ready.wait(timeout=10)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


def _resolve_device(requested: str) -> str:
    """Pick the same device AuraPro's RAG stack picks, unless told otherwise."""
    if requested != 'auto':
        return requested
    try:
        import torch

        if torch.cuda.is_available():
            return 'cuda'
        if getattr(torch.backends, 'mps', None) is not None and torch.backends.mps.is_available():
            return 'mps'
    except Exception:
        pass
    return 'cpu'


def _load_models(
    *,
    embedding_model: str,
    reranker_model: str,
    device: str,
    batch_size: int,
    notes: list[str],
) -> tuple[Callable[..., Any] | None, Callable[..., Any] | None, _BackgroundLoop | None]:
    """Load the local models exactly as ``get_ef`` / ``get_rf`` do.

    Returns ``(embedding_function, reranking_function, loop)`` where the two
    callables already match the shapes ``AuraProEmbeddingAdapter`` and
    ``AuraProRerankerAdapter`` expect.
    """
    try:
        import sentence_transformers  # noqa: F401
        from sentence_transformers import CrossEncoder, SentenceTransformer
    except Exception as error:  # pragma: no cover - environment dependent
        notes.append(
            'sentence-transformers/torch is not importable in this interpreter '
            f'({type(error).__name__}); Channel B and fused cannot run here. '
            "Re-run with AuraPro Desktop's Python (see the module docstring)."
        )
        return None, None, None

    device = _resolve_device(device)
    started = time.time()
    print(c(f'  loading embedding model {embedding_model} on {device} …', DIM), flush=True)
    encoder = SentenceTransformer(embedding_model, device=device)
    print(c(f'  loading cross-encoder {reranker_model} on {device} …', DIM), flush=True)
    cross_encoder = CrossEncoder(reranker_model, device=device)
    print(c(f'  models ready in {time.time() - started:.1f}s', DIM), flush=True)

    # Mirrors open_webui.retrieval.utils.get_embedding_function, engine ''.
    async def embedding_function(query: Any, prefix: str | None = None, user: Any = None):
        return await asyncio.to_thread(lambda: encoder.encode(query, batch_size=batch_size).tolist())

    # Mirrors open_webui.retrieval.utils.get_reranking_function, non-external.
    def reranking_function(query: str, documents: Sequence[AuraProRerankDocument], user: Any = None):
        return cross_encoder.predict(
            [(query, document.page_content) for document in documents],
            batch_size=batch_size,
        )

    return embedding_function, reranking_function, _BackgroundLoop()


def _stored_embedding_profile(store: SQLiteEpubStore) -> str | None:
    """Read the embedding profile the derived vectors were actually built with.

    A query embedded by a different model would silently miss the vec0 table,
    so the harness defaults to whatever is in the store rather than a guess.
    """
    try:
        rows = (
            store._connection()
            .execute(  # noqa: SLF001 - read-only diagnostics
                'SELECT embedding_profile, COUNT(*) AS n FROM epub_derived_vectors '
                'GROUP BY embedding_profile ORDER BY n DESC'
            )
            .fetchall()
        )
    except Exception:
        return None
    return str(rows[0]['embedding_profile']) if rows else None


# ─────────────────────────────── harness core ────────────────────────────────


class Harness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.notes: list[str] = []
        self.startup: list[tuple[str, bool, str]] = []

        db_path = Path(args.db).expanduser()
        if not db_path.exists():
            raise SystemExit(f'EPUB store not found: {db_path}')
        print(c(f'  opening store {db_path}', DIM), flush=True)
        self.store = SQLiteEpubStore(db_path)
        self.startup.append(('store (sqlite)', True, str(db_path)))

        profile = args.embedding_model or _stored_embedding_profile(self.store)
        self.profile = profile

        embeddings = reranker = None
        self.loop: _BackgroundLoop | None = None
        if args.no_models:
            self.notes.append('--no-models was passed: only Channel A (graph) will run.')
        elif not profile:
            self.notes.append(
                'no embedding profile found in epub_derived_vectors and none given '
                'via --embedding-model; Channel B and fused cannot run.'
            )
        else:
            embedding_function, reranking_function, loop = _load_models(
                embedding_model=profile,
                reranker_model=args.reranker_model,
                device=args.device,
                batch_size=args.batch_size,
                notes=self.notes,
            )
            self.loop = loop
            if embedding_function is not None and loop is not None:
                embeddings = AuraProEmbeddingAdapter(
                    embedding_function=embedding_function,
                    event_loop=loop.loop,
                    profile=profile,
                    # The harness proves locality itself: the models above are
                    # in-process sentence-transformers, never a remote service.
                    local_permitted=True,
                    timeout_seconds=args.timeout,
                )
                reranker = AuraProRerankerAdapter(
                    reranking_function=reranking_function,
                    profile=args.reranker_model,
                    local_permitted=True,
                )

        vector_backend = None
        if embeddings is not None:
            try:
                vector_backend = SQLiteVecDerivedVectorBackend(self.store)
                self.startup.append(('sqlite-vec backend', True, f'sqlite-vec {vector_backend.health.version}'))
            except SQLiteVecUnavailable as error:
                self.startup.append(('sqlite-vec backend', False, str(error)[:120]))
                self.notes.append(f'sqlite-vec is unavailable: {error}')
        else:
            self.startup.append(('sqlite-vec backend', False, 'no embedding model configured'))

        for label, model in (('embedding', embeddings), ('reranker', reranker)):
            if model is None:
                self.startup.append((f'{label} adapter', False, 'not configured'))
            else:
                availability = model.availability()
                self.startup.append(
                    (
                        f'{label} adapter',
                        availability.available,
                        f'{model.profile} — {availability.reason or "ready"}',
                    )
                )

        resolver, resolver_availability = self._tier_two_resolver()
        self.startup.append(
            (
                'Tier-2 concept resolver',
                resolver_availability.available,
                resolver_availability.reason or 'ready',
            )
        )

        self.service = EpubSearchService(
            source=self.store,
            vector_backend=vector_backend,
            embeddings=embeddings,
            reranker=reranker,
            concept_resolver=resolver,
        )

    def _tier_two_resolver(self) -> tuple[Any, ModelAvailability]:
        """Build the Tier-2 resolver through the production runtime wiring."""
        from open_webui.services import epub_runtime

        resolver, availability = epub_runtime._llama_cpp_concept_resolver(os.environ)  # noqa: SLF001
        if resolver is None:
            return None, availability
        # A configured resolver is not a reachable one; probe it now so startup
        # reports the truth instead of "ready".
        return resolver, resolver.availability()

    def close(self) -> None:
        if self.loop is not None:
            self.loop.close()
        self.store.close()

    # ── printing ────────────────────────────────────────────────────────────

    def print_startup(self) -> None:
        width = self.args.width
        print()
        print(rule('RUNTIME', width, BLUE))
        for label, ok, detail in self.startup:
            mark = c(' UP ', GREEN, BOLD) if ok else c('DOWN', RED, BOLD)
            print(f' [{mark}] {label:<24} {c(truncate(detail, width - 34), DIM)}')
        live = [
            ('A  graph', True),
            (
                'B  vector',
                any(l == 'embedding adapter' and ok for l, ok, _ in self.startup)
                and any(l == 'sqlite-vec backend' and ok for l, ok, _ in self.startup),
            ),
            (
                '   fused',
                any(l == 'reranker adapter' and ok for l, ok, _ in self.startup)
                and any(l == 'embedding adapter' and ok for l, ok, _ in self.startup),
            ),
        ]
        summary = '  '.join(c(f'[{name}: {"LIVE" if ok else "OFF"}]', GREEN if ok else RED, BOLD) for name, ok in live)
        print(f' channels: {summary}')
        for note in self.notes:
            print(c(f' ! {note}', YELLOW))
        print()

    def run(self, query: str) -> None:
        args = self.args
        width = args.width
        started = time.time()
        try:
            response = self.service.search(
                query,
                graph_offset=args.graph_offset,
                graph_limit=args.graph_limit,
                vector_limit=args.vector_limit,
                vector_candidate_limit=args.vector_candidate_limit,
            )
        except SearchError as error:
            print(c(f'search rejected: {error}', RED, BOLD))
            return
        elapsed = time.time() - started

        print()
        print(c('═' * width, BOLD))
        print(c(f' QUERY  {query}', BOLD))
        print(c('═' * width, BOLD))
        resolved = '、'.join(response.resolved_concepts) or c('(none)', RED)
        print(
            f' {"resolved_concepts":<20} {c(resolved, MAGENTA, BOLD)} {c(f"[{len(response.resolved_concepts)}]", DIM)}'
        )
        print(
            f' {"graph_total":<20} {c(str(response.graph_total), BOLD)}'
            f'   {c(f"(offset {response.graph_offset}, limit {args.graph_limit})", DIM)}'
        )
        print(f' {"elapsed":<20} {c(f"{elapsed:.2f}s", DIM)}')

        self._print_degraded(response, width)
        self._print_graph(response, width)
        self._print_vector(response, width)
        self._print_fused(response, width)
        self._print_comparison(response, width)

    def _print_degraded(self, response: SearchResponse, width: int) -> None:
        if not response.degraded:
            return
        print()
        print(c('!' * width, RED, BOLD))
        print(
            c(f'!! {len(response.degraded)} DEGRADED CHANNEL(S) — results below are INCOMPLETE, not empty', RED, BOLD)
        )
        for availability in response.degraded:
            print(c(f'!!   {availability.component}: {availability.reason or "unavailable"}', RED))
        print(c('!' * width, RED, BOLD))

    @staticmethod
    def _relation_depth(hit: SearchHit) -> int:
        for token in hit.provenance:
            if token.startswith('relation:HAS_PART:'):
                try:
                    return int(token.rsplit(':', 1)[1])
                except ValueError:
                    return -1
        return 0

    def _depth_badge(self, hit: SearchHit) -> str:
        depth = self._relation_depth(hit)
        if depth == 0:
            return c(' depth 0  DIRECT ', GREEN, BOLD)
        colour = YELLOW if depth == 1 else RED
        return c(f' depth {depth}  HAS_PART ', colour, BOLD)

    def _print_hit_body(self, hit: SearchHit, width: int) -> None:
        toc = c(' › '.join(hit.toc_path) or '(no TOC path)', CYAN)
        print(f'      toc      {toc}')
        concepts = '、'.join(hit.matched_concepts) or c('(none attributed)', DIM)
        print(f'      concepts {c(concepts, MAGENTA)}')
        print(
            f'      passage  {c(hit.passage_id, DIM)}'
            f'  {c(f"[{hit.excerpt.start_codepoint}:{hit.excerpt.end_codepoint}]", DIM)}'
            f'  {c("prov=" + ",".join(hit.provenance), DIM)}'
        )
        if self.args.full:
            print(c('      ── excerpt ──', DIM))
            for line in wrap(hit.excerpt.content, width - 8, '      '):
                print(line)
            print(c('      ── full passage ──', DIM))
            for line in wrap(hit.content, width - 8, '      '):
                print(c(line, DIM))
        else:
            print(f'      text     {truncate(hit.excerpt.content, width - 15)}')

    def _print_graph(self, response: SearchResponse, width: int) -> None:
        print()
        shown = len(response.graph_results)
        print(
            rule(
                f'CHANNEL A · GRAPH   {shown} shown of {response.graph_total}'
                '   (concept match + HAS_PART expansion, depth ≤ 2)',
                width,
                CYAN,
            )
        )
        if not response.graph_results:
            reason = (
                'no Tier-1 concept matched the query'
                if not response.resolved_concepts
                else 'matched concepts have no occurrences on this page'
            )
            print(c(f'   (empty — {reason})', DIM))
            return
        depths = [self._relation_depth(hit) for hit in response.graph_results]
        histogram = '  '.join(f'depth {d}: {depths.count(d)}' for d in sorted(set(depths)))
        print(c(f'   relation-depth mix on this page → {histogram}', BOLD))
        for index, hit in enumerate(response.graph_results, start=1):
            print()
            print(f'  {c(f"A{index}", BOLD)} [{self._depth_badge(hit)}]')
            self._print_hit_body(hit, width)

    def _print_vector(self, response: SearchResponse, width: int) -> None:
        print()
        print(
            rule(
                f'CHANNEL B · VECTOR   {len(response.vector_results)} hits   (sqlite-vec KNN → cross-encoder → MMR)',
                width,
                BLUE,
            )
        )
        if not response.vector_results:
            marker = (
                c('UNAVAILABLE — see degraded block above', RED, BOLD)
                if any(
                    'vector' in a.component or 'embedding' in a.component or 'reranker' in a.component
                    for a in response.degraded
                )
                else c('genuinely 0 results (models are up)', DIM)
            )
            print(f'   {marker}')
            return
        for index, hit in enumerate(response.vector_results, start=1):
            print()
            score = f'{hit.score:.4f}' if hit.score is not None else 'n/a'
            print(f'  {c(f"B{index}", BOLD)} score {c(score, YELLOW, BOLD)}')
            self._print_hit_body(hit, width)

    def _print_fused(self, response: SearchResponse, width: int) -> None:
        print()
        print(
            rule(
                f'FUSED   {len(response.fused_results)} hits'
                '   (graph + vector candidates, one cross-encoder + MMR pass)',
                width,
                MAGENTA,
            )
        )
        if not response.fused_results:
            marker = (
                c('UNAVAILABLE — see degraded block above', RED, BOLD)
                if response.degraded
                else c('genuinely 0 results (models are up)', DIM)
            )
            print(f'   {marker}')
            return
        for index, hit in enumerate(response.fused_results, start=1):
            print()
            score = f'{hit.score:.4f}' if hit.score is not None else 'n/a'
            badge = self._depth_badge(hit) if 'graph' in hit.provenance else c(' vector-only ', BLUE)
            print(f'  {c(f"F{index}", BOLD)} score {c(score, YELLOW, BOLD)}  [{badge}]')
            self._print_hit_body(hit, width)

    def _print_comparison(self, response: SearchResponse, width: int) -> None:
        channels: dict[str, set[str]] = {}
        labels: dict[str, str] = {}
        for tag, hits in (('A', response.graph_results), ('B', response.vector_results), ('F', response.fused_results)):
            for hit in hits:
                channels.setdefault(hit.passage_id, set()).add(tag)
                labels.setdefault(hit.passage_id, ' › '.join(hit.toc_path[-2:]) or hit.passage_id)
        if not channels:
            return
        print()
        print(rule('CROSS-CHANNEL OVERLAP', width, BOLD))
        print(c(f'   {"A":^3}{"B":^3}{"F":^3}  passage / toc tail', DIM))
        for passage_id, tags in channels.items():
            marks = ''.join(
                c(f'{"✓" if tag in tags else "·":^3}', GREEN if tag in tags else DIM) for tag in ('A', 'B', 'F')
            )
            print(f'   {marks}  {truncate(labels[passage_id], width - 16)}')


# ─────────────────────────────────── REPL ────────────────────────────────────

REPL_HELP = """
  <text>            run a query
  :limit N          graph page size      (currently {graph_limit})
  :offset N         graph page offset    (currently {graph_offset})
  :vlimit N         vector/fused results (currently {vector_limit})
  :cands N          vector KNN candidates(currently {vector_candidate_limit})
  :full             toggle full passages (currently {full})
  :width N          output width         (currently {width})
  :status           re-print runtime health
  :help             this text
  :q / :quit        exit
"""


def repl(harness: Harness) -> None:
    args = harness.args
    loaded = 'models are loaded' if harness.loop is not None else 'graph-only mode'
    print(c(f' REPL ready — {loaded}, queries are now cheap. :help for commands, :q to quit.', BOLD))
    while True:
        try:
            line = input(c('\nepub> ', GREEN, BOLD)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line.startswith(':'):
            parts = line[1:].split()
            command = parts[0].lower() if parts else ''
            value = parts[1] if len(parts) > 1 else None
            try:
                if command in ('q', 'quit', 'exit'):
                    return
                if command == 'help':
                    print(
                        REPL_HELP.format(
                            full=args.full,
                            **{
                                k: getattr(args, k)
                                for k in (
                                    'graph_limit',
                                    'graph_offset',
                                    'vector_limit',
                                    'vector_candidate_limit',
                                    'width',
                                )
                            },
                        )
                    )
                elif command == 'status':
                    harness.print_startup()
                elif command == 'full':
                    args.full = not args.full
                    print(c(f'  full passages: {args.full}', DIM))
                elif command in ('limit', 'offset', 'vlimit', 'cands', 'width'):
                    if value is None:
                        raise ValueError('this command needs a number')
                    attribute = {
                        'limit': 'graph_limit',
                        'offset': 'graph_offset',
                        'vlimit': 'vector_limit',
                        'cands': 'vector_candidate_limit',
                        'width': 'width',
                    }[command]
                    setattr(args, attribute, int(value))
                    print(c(f'  {attribute} = {getattr(args, attribute)}', DIM))
                else:
                    print(c(f'  unknown command :{command} (try :help)', YELLOW))
            except ValueError as error:
                print(c(f'  {error}', RED))
            continue
        harness.run(line)


# ─────────────────────────────────── main ────────────────────────────────────


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='epub_query.py',
        description='Compare the three EPUB retrieval channels side by side.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('query', nargs='*', help='query text; omit for a REPL')
    parser.add_argument(
        '--db',
        default=os.environ.get('EPUB_CONCEPT_DB_PATH', DEFAULT_DB),
        help=f'EPUB concept store path (default: {DEFAULT_DB})',
    )
    parser.add_argument('--graph-offset', type=int, default=0)
    parser.add_argument('--graph-limit', type=int, default=5)
    parser.add_argument('--vector-limit', type=int, default=5)
    parser.add_argument('--vector-candidate-limit', type=int, default=50)
    parser.add_argument('--full', action='store_true', help='print the full parent passage, not a truncated excerpt')
    parser.add_argument('--width', type=int, default=100, help='output width in columns')
    parser.add_argument(
        '--embedding-model',
        default=None,
        help='override the embedding profile (default: whatever epub_derived_vectors was built with)',
    )
    parser.add_argument('--reranker-model', default=DEFAULT_RERANKER)
    parser.add_argument(
        '--device',
        default=os.environ.get('EPUB_QUERY_DEVICE', 'auto'),
        help='torch device for both models (auto, cpu, mps, cuda)',
    )
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--timeout', type=float, default=120.0, help='embedding bridge timeout in seconds')
    parser.add_argument('--no-models', action='store_true', help='skip model loading; run Channel A only (fast)')
    return parser.parse_args(list(argv))


def main(argv: Sequence[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    # Everything this harness needs is already in the local caches; never let a
    # query stall on a model download.
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

    args = parse_args(argv)
    harness = Harness(args)
    try:
        harness.print_startup()
        if args.query:
            harness.run(' '.join(args.query))
        else:
            repl(harness)
    finally:
        harness.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
