# EPUB Concept Wiki — Persistent Task Status

**Purpose:** Durable implementation state for SDD work. A new session must read
this file and `epub_concept_sdd.md` before making changes.

## Update rules

- Each task has one state: `planned`, `in_progress`, `blocked`, `completed`, or
  `superseded`.
- Update this file in the same change as the implementation or verification that
  moves a task forward. Record evidence (test name, command, or review link).
- Do not mark a task `completed` until its listed acceptance evidence passes.
- Add newly discovered work as a numbered task; do not silently fold it into an
  existing task.
- At the completion of each implementation phase, commit only that phase's
  reviewed files. Do not include pre-existing or unrelated working-tree edits.
- After each completed phase's acceptance evidence passes, push that phase's
  focused commit(s) and open one GitHub Pull Request. Keep subsequent phases on
  a new branch based on the accepted phase, so review remains small and
  independently mergeable.

## Confirmed product decisions

| ID | Decision | Status |
|---|---|---|
| D-001 | Source content is stable visible EPUB text, not XHTML markup; preserve text and punctuation exactly. | confirmed |
| D-002 | Store complete paragraphs; responses include full content plus verified continuous excerpts and offsets. | confirmed |
| D-003 | Cloud Batch is an administrator-configured, one-time offline process; online inference remains local/private. | confirmed |
| D-004 | All authenticated users can browse and search; administration is the only write/cost-bearing role. | confirmed |
| D-005 | The feature UI lives in AuraPro-WebUI and is displayed by Desktop's WebView. | confirmed |
| D-006 | Normal HTML flow whitespace collapses per visible-text semantics; `<pre>` whitespace is preserved. | confirmed |
| D-007 | First release targets textual EPUBs. Preserve typed text blocks; images and tables are out of scope and warn. | confirmed |
| D-008 | Keep Batch prompt generic initially. Add only passage-relevant seed-term context if review proves the remote model misses established proper nouns; deterministic seed matching remains authoritative. | deferred optimization |
| D-009 | First operational acceptance uses a Simplified Chinese EPUB. Retrieval windows target 800 Unicode code points with 150 overlap and prefer Chinese sentence boundaries; source passages remain intact. | confirmed |

## Work breakdown

| ID | Task | State | Depends on | Acceptance evidence / notes |
|---|---|---|---|---|
| T-000 | Baseline audit of existing prototype | completed | — | Existing parser, DB, Batch, search, UI, and tests reviewed. Critical concept-occurrence foreign-key ordering failure reproduced. |
| T-010 | Finalize SDD specification and persistent task process | completed | T-000 | SDD decisions D-001 through D-007 are recorded; parser, storage/search, and UI reviews completed. |
| T-015 | Provision a supported Python test environment | completed | T-010 | Repository declares Python >=3.11 and <3.13. The Homebrew 3.12/3.14 XML extensions remain faulty, but a compatible temporary source build of Python 3.12.13 at `/private/var/folders/dt/y5fzlt453wd9ypxfr29rxvcc0000gn/T/python-build.20260801153902.35544/Python-3.12.13/python.exe` successfully imports `xml.parsers.expat` and runs acceptance tests. |
| T-020 | EPUB parser and faithful passage fixtures | completed | T-010, T-015 | Archive limits/duplicate-member rejection, OPF spine, NAV/NCX fragment paths, typed visible-text passages, ordered fallback text, and warnings for excluded tables/images are implemented. Python 3.12 acceptance: `test_epub_parser_sdd.py` — 5 tests passed. |
| T-025 | Provision independent storage migration and local vector extension | in_progress | T-010 | `sqlite-vec==0.1.9` is pinned in `pyproject.toml`/`uv.lock`. A healthy temporary validation runtime uses `pysqlite3` compiled against Homebrew SQLite and passes real `vec0` cosine KNN tests. `SQLiteVecDerivedVectorBackend` persists only derived vectors, isolates profile/dimension indexes, and prevents source rebinding (`test_epub_sqlite_vec_backend.py` — 2 tests passed). Production startup must still enforce the extension health check; SQLite/PG migration runners remain. |
| T-030 | Independent store, migrations, versioning, and source integrity | in_progress | T-010, T-025 | SQLite canonical-store baseline implemented, including immutable passages, verified derived offsets, full-file hash dedupe, FK-safe concepts, and durable Batch job/item records. `/opt/homebrew/bin/python3.14 -m unittest discover -s AuraPro-WebUI/test -p 'test_epub_store.py' -v` passes 5 isolated-store tests; SQLite vector extension and PostgreSQL parity remain. |
| T-040 | Durable Batch job service and glossary-concept ingest | in_progress | T-030 | Provider-neutral SQLite service baseline now persists draft requests before submit, idempotent provider submission/poll/recovery, atomic concept/mention ingest, failed-item successor jobs, and credential-field rejection. `OpenAIBatchProvider` now uploads validated OpenAI JSONL, uses idempotency headers, normalizes lifecycle states, and translates output/error JSONL without persisting credentials. Python 3.12: `test_epub_batch.py` — 10 tests passed. PostgreSQL adapter remains. |
| T-050 | Local-only inference adapters and derived vector index | in_progress | T-030 | EPUB now consumes AuraPro's existing local RAG embedding and reranking functions via fail-closed adapters; embedding bridges from worker thread to AuraPro's application event loop, while the Cross-Encoder receives document-compatible immutable strings. Python 3.12: `test_epub_local_inference.py` — 13 tests passed. llama.cpp Tier-2 resolver dispatch and PostgreSQL/pgvector remain. |
| T-060 | Search pipeline, exact excerpt contract, and pagination | completed | T-020, T-030, T-050 | Dependency-free local-only search orchestration now uses a trie multi-pattern Tier-1 matcher (Latin boundaries/CJK direct phrase), explicit degraded Tier-2 resolver, exhaustive graph count/offset pagination, verified full-passage excerpts, derived-vector recall, cross-encoder reranking, and MMR. Python 3.12: `test_epub_search.py` — 5 tests passed; store read-surface coverage is included in `test_epub_store.py` — 6 tests passed. |
| T-070 | Authenticated REST API and admin authorization | completed | T-040, T-060 | Python 3.12 `test_epub_api.py` — 5 tests passed: authenticated shared reads/search, admin-only mutations, fail-closed missing startup service, and an initialized independent runtime serving the real router. The test isolates only the auth dependency identity, while exercising the real FastAPI router and service. |
| T-080 | AuraPro-WebUI frontend and Desktop WebView integration | completed | T-070 | Typed `/api/v1/epub` client plus `/epub` verified-user browse/search and `/admin/epub` admin workflow are implemented. The shared EPUB library is discoverable from the WebUI sidebar and the admin layout exposes the admin route; no Desktop-specific code was added. Node 22 Vitest client suite — 3 tests passed. Full-project strict checking still reports pre-existing errors, while the new EPUB page is clean. |
| T-090 | End-to-end, migration, resilience, and operational validation | in_progress | T-020, T-040, T-060, T-070, T-080 | Component acceptance suite passes: parser 5, store 6, Batch 10, local inference 8, search 5, API 5, frontend client 3. Local production startup now constructs an independent persistent SQLite service and rejects remote PostgreSQL misconfiguration without falling back to the main database (`test_epub_runtime.py` — 3 tests passed). PostgreSQL/pgvector parity and resilience scenarios remain. |
| T-095 | Provision a healthy native SQLite extension test runtime | completed | T-025 | Homebrew Python 3.14 remains broken (`pyexpat` symbol mismatch), but a temporary Python 3.12 test runtime with `pysqlite3` compiled against Homebrew SQLite successfully loads locked `sqlite-vec==0.1.9` and executes a real `vec0` cosine KNN query. |
| T-100 | Automatically derive Chinese-first retrieval units on EPUB import | completed | T-030, D-009 | Import now persists exact source windows before a version becomes `READY`: <=800 code points yields one whole-passage unit; longer text targets 800 with 150 overlap and prefers `。！？；`, then `.!?;`, before a character fallback. Re-running derivation reuses the exact stored units. Compatible Python 3.12: `test_epub_retrieval_units.py` — 7 tests passed; `test_epub_store.py` — 6 regression tests passed. |

## Phase delivery

| Phase | Commit | Pull request | State / evidence |
|---|---|---|---|
| Parser baseline (T-010/T-020) | `796c763` | pending | Branch `feat/epub-concept-wiki` is ready locally. Push to `origin` was rejected because SSH identity `mikdw` lacks write permission to `AuraPro-Official/AuraPro-WebUI`; create the PR after repository write access is granted. |
| Source store and vector boundary (T-025/T-030 baseline) | `6a6f1d2` | pending | Isolated store tests 5/5 and sqlite-vec boundary tests pass. Push remains pending repository administrator approval. |
| Durable OpenAI Batch workflow (T-040 baseline) | current branch head | pending | Batch lifecycle and mocked OpenAI provider tests 10/10 pass; no real API key or network call. Push remains pending repository administrator approval. |
| Local inference and faithful search (T-050/T-060) | `a5b4071`, `d257704` | pending | Local-only inference/vector boundary tests 8/8; search contract tests 5/5. Push remains pending repository administrator approval. |
| Authenticated API (T-070) | `78854f3`, `526caa6` | pending | FastAPI authorization/service suite 5/5 passes, including initialized-runtime route availability. Push remains pending repository administrator approval. |
| WebUI workflow (T-080) | `abff971`, `363e226` | pending | Vitest client suite 3/3 passes; new EPUB page has no strict-check findings. Push remains pending repository administrator approval. |
| Persistent local vector backend (T-025/T-050) | pending commit | pending | Real sqlite-vec backend KNN and source-identity tests 2/2 pass in the healthy temporary extension runtime. |
| Automatic retrieval-unit derivation (T-100) | pending commit | pending | Chinese-window planning, source fidelity, import ordering, and retry idempotency pass in 7 focused tests. |

## Active-session handoff

- **Current focus:** T-090 — production startup, migration parity, resilience, and operational validation.
- **Current implementation state:** The frontend keeps reads on authenticated `/epub` routes and renders complete passages plus explicit exact excerpts. The administrator route is separate, presents imports, Batch controls, concept review, and derived-index operations, and sends no provider credentials. Backend authorization remains authoritative. Import automatically writes retry-safe, Chinese-first derived retrieval windows without changing canonical passages. Local startup attaches a persistent independent SQLite service, and the tested sqlite-vec backend is available for production adapter wiring; the remote PostgreSQL implementation remains.
- **Next action:** Add a server-owned llama.cpp Tier-2 resolver adapter, then validate a configured local server can import, index, and browse a textual EPUB through the full application path.
