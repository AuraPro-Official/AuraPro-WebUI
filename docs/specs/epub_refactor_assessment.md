# EPUB Concept Wiki — Refactoring Assessment

**Status:** Findings and recommendation. No code written.
**Last updated:** 2026-09-09
**Related:** [`epub_concept_sdd.md`](./epub_concept_sdd.md),
[`epub_library_distribution.md`](./epub_library_distribution.md)

## 0. Why this exists

Colleagues proposed rebuilding the EPUB Concept Wiki on top of existing WebUI
features rather than as a parallel stack, on three premises:

1. EPUB retrieval could be built on Workspace → Knowledge and Extensions →
   索引模式, extending RAG rather than starting over.
2. EPUB downloads its own open-source models; it should instead call the
   llama.cpp instance Desktop already manages, since those models are shared.
3. Desktop should download the book and overlay to a path, and the WebUI should
   consume that path.

Two hard constraints were set: **do not break existing functionality**, and **do
not push the published wheel past PyPI's 100 MB limit** (it was believed to be at
80+ MB already).

The instruction was to investigate and disagree where warranted, with evidence.
**Two of the three premises turned out to be factually wrong**, and the size
constraint turned out to be real but unrelated to EPUB. What follows is what the
code says.

## 1. Premise checks

### 1.1 索引模式 is not an index mode

`"RAG Translation Mode": "索引模式"` — `src/lib/i18n/locales/zh-CN/translation.json:2239`;
`"Integrations": "扩展功能"` — `:1186`.

索引模式 is the zh-CN label for **RAG Translation Mode**, one of five mutually
exclusive translation extension modes (`src/lib/utils/extension-modes.ts:11-18`).
`apply_rag_translation_mode` (`backend/open_webui/utils/glossary_translation.py:2970`)
retrieves over knowledge bases tagged `meta.knowledge_type == 'bilingual'` and
rewrites the user's message into a translation prompt. It is a bilingual
translation-memory feature.

`grep -n -i 'epub\|concept' backend/open_webui/utils/middleware.py` returns **zero
matches**. EPUB is reachable only through `/api/v1/epub` and two standalone Svelte
pages; it is not in the chat pipeline at all.

**So "build EPUB on 索引模式" is not a live option.** But the colleague's instinct
points somewhere real: the **bilingual knowledge base** is the best precedent in
the repo, because it already inserts pre-built units via
`save_docs_to_vector_db(..., split=False)` (`routers/retrieval.py:2917-2928`),
bypassing the generic chunker entirely — which is exactly what an EPUB vector
channel on Knowledge would have to do.

### 1.2 EPUB downloads no models and loads no models

`sentence_transformers|SentenceTransformer|snapshot_download|huggingface_hub|CrossEncoder|from_pretrained`
across `retrieval/epub/`, `services/epub_runtime.py`, `services/epub_concept.py`:
**zero matches.**

The adapters wrap the RAG stack's own singletons:

- `AuraProEmbeddingAdapter.from_app_state` → `app_state.EMBEDDING_FUNCTION` (`retrieval/epub/inference.py:485`)
- `AuraProRerankerAdapter.from_app_state` → `app_state.RERANKING_FUNCTION` (`:598`)
- wired only at `services/epub_runtime.py:305,316`

Those are the same objects generic RAG uses (`main.py:681,683`). One embedding
model, one cross-encoder, one process, shared.

**Tier-2 concept resolution already calls Desktop's llama.cpp directly** — `GET
/health` (`inference.py:345`) and `POST /v1/chat/completions` (`:389`) against the
endpoint from Desktop's runtime descriptor. Zero model copies in the Python
process. **Premise 2's desired architecture is the one already implemented.**

Two grains of truth behind the concern:

- Desktop downloads one model _for_ EPUB — a ~2.1 GB Qwen2.5-3B GGUF
  (`AuraPro-Desktop/src/main/utils/llamacpp.ts:41-47`) — but into the **shared**
  model directory, registered in the **shared** preset, served by the **shared**
  llama-server. An extra model, not an extra runtime.
- The three HTTP inference adapters in `inference.py:205-265` are exported and
  **never wired**. They are dead code, and they are the strongest visual evidence
  for a belief that is false. Deleting them is free.

### 1.3 Desktop → path → WebUI: the download half is copyable, the apply half is not

Official glossaries are downloaded by Desktop's main process and written into the
shared data directory; the backend reads them by path
(`utils/glossary_translation.py:510`). The download envelope is good and worth
copying: HTTPS-only, `redirect: 'error'`, per-file SHA-256 and exact byte size,
staged install with rollback, cross-origin guard.

But EPUB book data is **not files on disk** — it is a BLOB inside
`<DATA_DIR>/epub_concept_v1.db` (`retrieval/epub/store.py:175-181`), and import is
multipart-only (`routers/epub.py:198-213`). **No path-based ingestion exists.**

Recommended shape: Desktop downloads to a staging path using the glossary
envelope, then **POSTs the bytes** to the existing local admin endpoints, then
clears staging. Desktop already makes authenticated WebUI calls
(`AuraPro-Desktop/src/main/index.ts:2798,2878`). Adding a server-side path-read
endpoint would create a new file-read surface reachable by any admin, for no gain.

## 2. The size constraint — measured, and EPUB is not the risk

`.github/workflows/release-pypi.yml` builds and publishes a **wheel only**, so the
wheel is the sole artifact under the limit. Built from a clean `git archive HEAD`
with a fresh `npm run build`:

|              |                                                                                 |
| ------------ | ------------------------------------------------------------------------------- |
| Wheel        | **92,141,153 B = 87.87 MiB** compressed (153.0 MiB uncompressed, 5,119 members) |
| PyPI limit   | 100 MiB = 104,857,600 B                                                         |
| **Headroom** | **12,716,447 B = 12.13 MiB — 87.9% of the limit already used**                  |

**EPUB's attributable share: 201,700 B compressed = 0.192 MiB = 0.22% of the
wheel.** Cross-checked by building the wheel with and without the EPUB frontend
routes: the entire EPUB frontend adds **19,862 bytes**.

`test/` and `scripts/` are **not** packaged — only `backend/open_webui` is. The
303-test suite and the query harness cost nothing.

**Only `sqlite-vec` was added as a dependency by the EPUB work.** `ebooklib`,
`jieba`, `wordfreq`, `stanza`, `simalign` and the rest of that family all entered
at the initial release — they are the pre-existing translation stack. `jieba` in
particular contributes **0 bytes to the wheel** (declared dependency, not
vendored) though it is ~36.5 MiB of install size, and it predates EPUB.

**Where the weight actually is** (compressed):

| #   | area               | size          | share |
| --- | ------------------ | ------------- | ----- |
| 1   | `frontend/pyodide` | **52.48 MiB** | 60.3% |
| 2   | `frontend/_app`    | 9.97 MiB      | 11.5% |
| 3   | `frontend/assets`  | 9.69 MiB      | 11.1% |
| 4   | `static/fonts`     | 7.52 MiB      | 8.6%  |
| 5   | `frontend/wasm`    | 4.85 MiB      | 5.6%  |
| —   | all backend `.py`  | 1.03 MiB      | 1.2%  |

Top 5 = 97.2%, all frontend/static. Cheapest relief if headroom is ever needed:
dropping scipy + matplotlib + scikit-learn + sympy from `scripts/prepare-pyodide.js`
saves **28.74 MiB**; one CJK font 6.01 MiB; four hero JPEGs 4.94 MiB.

**Chroma vs sqlite-vec does not touch the file limit.** Neither is bundled;
`chromadb` is 22.24 MiB downloaded / 52.81 MiB unpacked, `sqlite-vec` 0.16 MiB.
Dropping chroma buys roughly 28 MiB of _install_ size and **exactly zero wheel
bytes**. Adding or removing a vector backend cannot breach the limit.

**Conclusion: the size constraint is real but has nothing to do with EPUB.** The
wheel is at 87.9% because of Pyodide, and upstream Pyodide version drift moves
that number with no change on our side.

## 3. Can EPUB sit on the generic RAG stack?

### 3.1 The invariants that decide it

`docs/specs/epub_concept_sdd.md` §2, checked against the generic Knowledge/RAG path:

| Invariant                                                                                  | Generic stack today                                                                                         | Verdict                                                                       |
| ------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Passage is immutable, never split or transformed                                           | No passage entity exists; the chunk **is** the split. Enforced in EPUB by a DB trigger (`store.py:218-224`) | **Incompatible**                                                              |
| Content preserves extracted characters exactly                                             | `ftfy.fix_text()` on every loaded document (`retrieval/loaders/main.py:260`)                                | Incompatible on the loader path; bypassable                                   |
| Response returns complete passage; excerpt is a verified substring with code-point offsets | Response is chunk texts + metadata; no parent unit, no offsets, no verification                             | **Not expressible**                                                           |
| Windows/vectors are derived indexes only, never the citation unit                          | The chunk **is** the citation unit, and the citation id is the filename (`routers/retrieval.py:2065`)       | **Structurally inverted**                                                     |
| Local/private inference only                                                               | Generic RAG supports cloud engines                                                                          | Already solved by policy (`epub_runtime.py:131-196`), not by separate storage |
| Mentions/evidence anchored to `(passage, start, end)`, re-verified                         | A chunk's identity is a `uuid4` destroyed by reindex (`routers/retrieval.py:1917`) — nothing to anchor to   | **Incompatible**                                                              |

Chunk metadata does carry `start_index`, but it indexes into whatever the markdown
splitter handed the character splitter, and nothing records where that section
began in the file. **Given a retrieved chunk you cannot compute its character
range in the source.**

### 3.2 Channel by channel

- **Vector channel — could move**, following the bilingual precedent, at the cost
  of: embedding-profile isolation (EPUB keeps one `vec0` table per
  profile+dimension, `sqlite_vec_backend.py:196-200`; the generic stack writes
  `embedding_config` and never reads it), the FK and verification chain, and
  exposure to `POST /knowledges/reindex`, which deletes and rebuilds every
  collection from `files` (`routers/knowledge.py:367-393`).
- **Graph channel — cannot move.** It is a relational query, not a vector one:
  `COUNT(*)` over a predicate plus `ORDER BY … LIMIT ? OFFSET ?` over the _same_
  predicate so paging ends exactly at `graph_total` (`store.py:1620-1690`). The
  generic vector interface offers `query(filter, limit)` with no offset and no
  count (`retrieval/vector/main.py:76-108`). Concepts, aliases, relations,
  assertions, evidence, merges, splits and TOC nodes have no analogue anywhere in
  the generic model.

**A full rebuild** would rewrite ~16.9k lines of backend and re-satisfy 303 tests
that encode invariants the generic stack does not have — to arrive at a weaker
product. **Not recommended.**

## 4. What is genuinely duplicated

| Overlap                                                                                                                                           | Verdict                                                                                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Two embedded vector engines — chromadb (default) **and** sqlite-vec, both installed                                                               | **Duplication.** The clearest infrastructure waste                                                                            |
| Three EPUB parsers — `retrieval/parsers/epub/` (faithful, offset-preserving), `utils/bilingual/bilingual_epub_parse.py`, `UnstructuredEPubLoader` | **Partly duplication.** The faithful parser is a strict superset of the bilingual one                                         |
| ~60 lines of unwired HTTP inference adapters                                                                                                      | **Dead code**                                                                                                                 |
| Embedding / reranking call paths                                                                                                                  | **Not duplication — already shared**                                                                                          |
| Two chunkers                                                                                                                                      | **Specialisation.** 1000 chars after a markdown pre-split vs 800 code points at CJK sentence boundaries with offsets retained |
| Two search services                                                                                                                               | **Specialisation.** Exhaustive count + offset pagination has no generic equivalent                                            |

## 5. Problems found that matter more than the refactor

1. **A default install runs EPUB search graph-only.** `RAG_RERANKING_MODEL`
   defaults to `''` (`config.py:984`), the Desktop-bundled `data/webui.db` has an
   empty `rag` config, and Desktop's env block never sets the model. So the
   reranker adapter is never built (`epub_runtime.py:315-323`) and
   `_vector_candidates` bails at `search.py:1303`. **The vector and fused channels
   are off for every colleague**; the tracked `.env` is what makes the dev machine
   look correct.
2. **Changing the embedding model is a one-way door.** There is no delete endpoint
   for a book or version anywhere in `routers/epub.py`; `rebuild=true` does not
   re-stamp units (`services/epub_concept.py:841-901`), so under a new profile
   every unit fails. Recovery today means deleting the database file. The
   uninstall-then-reinstall story in `epub_library_distribution.md` D-9 is
   **unimplemented**.
3. **Stale closure on runtime model change.** `from_app_state` captures the RAG
   closure, profile and locality verdict by value (`inference.py:485,598`) and is
   wired only during lifespan (`main.py:399,756`). After an admin changes the
   embedding model at runtime, EPUB keeps the old closure and the old
   `local_permitted` verdict until restart.
4. **EPUB is absent from the chat pipeline.** This is the feature colleagues are
   actually asking for, and it does **not** require moving to Knowledge bases: an
   `apply_*_mode` sibling calling `EpubSearchService.search` and emitting hits
   through the existing `sources` plumbing (`utils/middleware.py:3126-3141`) would
   reuse the chat integration while keeping the full-passage + verified-excerpt
   contract that generic sources cannot offer.
5. Minor: the tracked `.env` names `EPUB_CONCEPT_LOCAL_LLM_ENDPOINT` port 8081 —
   that is the WebUI's own port; llama.cpp defaults to 18881
   (`llamacpp.ts:1616`). Harmless when Desktop's descriptor is present, wrong
   otherwise. (`OPENAI_API_KEY` in that file is empty — checked.)

## 6. Recommendation

**Do not rebuild.** Keep the parallel store; it is what the invariants require.
Act on the following instead, in value-per-risk order:

1. **Fix the default-install reranker gap** (§5.1) — smallest change, largest
   user-visible gain. Today the feature ships with half of itself off.
2. **Implement uninstall / re-index under a new profile** (§5.2) — removes a
   one-way door and is a prerequisite for the distribution design.
3. **Delete the three unwired inference adapters** (§1.2) — zero risk, and it
   removes the evidence for a false belief.
4. **Add the chat-pipeline integration** (§5.4) — the feature actually being asked
   for, without a rebuild.
5. **Collapse the bilingual EPUB parser into the faithful one** (§4) — the largest
   genuinely redundant code block.
6. **Promote sqlite-vec to a generic vector backend and drop chromadb from
   Desktop** (§4) — ~28 MiB of install size, zero wheel bytes, and the win is on
   the generic side.

Access control on book reading was considered and **explicitly declined by the
owner**: all users are internal, the books are already available to them, and the
analysis rather than the text is what is being added. Note this is a different
question from the public GitHub repository, where source-book content was redacted
and must stay redacted.

## 7. Open

- **Deployment shape.** If colleagues do not need offline use, one shared machine
  running Desktop + WebUI, with everyone else on a browser, removes almost all of
  `epub_library_distribution.md`: no catalog, no manifest, no password, no
  per-machine model download, no D-9 uninstall/reinstall. The owner imports once
  and everyone sees it. This is a much larger simplification than any
  configuration change, and it hinges on a single question: **do colleagues need
  the feature offline?**

## 8. Handover — state as of 2026-09-09

Design is finished and recorded. What follows is operational state a new session
needs and that the specs above do not carry.

### Read these three, in this order

1. `epub_refactor_assessment.md` (this file) — why **not** to rebuild, and the
   four problems that matter more.
2. `epub_library_distribution.md` — the distribution design. **13 decisions,
   zero open questions.** D-1..D-13 are settled by the owner; do not reopen them
   without new evidence.
3. `epub_concept_task_status.md` — the long-running tracker for the concept work
   itself (T-000..T-215).

### Open pull requests

| PR          | Branch                            | State                                                                                                                                      |
| ----------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| WebUI #32   | `docs/epub-library-distribution`  | Distribution design. Unmerged                                                                                                              |
| WebUI #33   | `docs/epub-refactor-assessment`   | This document. Unmerged                                                                                                                    |
| Desktop #17 | `fix/startup-seed-data-directory` | Fixes the data-directory wipe. **Merge before shipping the library**, or a future `requiredDataVersion` bump destroys every installed book |

Desktop also has six untouched Dependabot PRs. **That repo has no CI at all**, so
they would merge on the diff alone.

WebUI `main` is `16e2470`. CI now runs both Python suites (303 EPUB + 58 backend)
with a minimum-count assertion, so a green check can no longer mean "ran nothing".

### Do these first, in this order

1. **Fix the default-install reranker gap** (§5.1). Today a stock Desktop install
   runs EPUB search **graph-only** — no reranking model is configured outside the
   git-tracked `.env`, so the vector and fused channels are off for every
   deployer. Small change, largest user-visible gain, and until it lands any
   library work ships a half-working feature.
2. **Drop the dedicated Tier-2 model download** (D-13). Delete
   `EPUB_CONCEPT_MODEL_REPOSITORY` / `ensureEpubConceptModel` in
   `AuraPro-Desktop/src/main/utils/llamacpp.ts` and discover the loaded model
   instead of naming one. Saves ~2.1 GB per deployer.
3. **Implement uninstall / re-index under a new profile** (§5.2). Currently a
   one-way door, and a prerequisite for D-9.
4. Then phase 1 of the distribution plan.

Items 1 and 2 are small, independent and immediately verifiable — a good first PR.

### Things that will mislead you if you do not know them

- **索引模式 is not an index mode.** It is the zh-CN label for RAG Translation
  Mode, a bilingual translation feature. EPUB is not in the chat pipeline at all.
- **EPUB loads no models of its own.** The adapters wrap the RAG stack's
  singletons; Tier-2 already calls Desktop's llama.cpp. Three unwired HTTP
  adapters in `inference.py:205-265` are dead code and are the main reason people
  believe otherwise.
- **The wheel is at 87.9% of PyPI's limit, and EPUB is 0.22% of it.** 60% is
  Pyodide. Do not attribute size pressure to this feature.
- **After the history rewrites, `git merge-base --is-ancestor` gives misleading
  answers** about whether old work landed. Judge by content. A branch stranded on
  pre-rewrite history should be cherry-picked, never rebased.
- The acceptance graph database at `/private/tmp/aurapro-epub-e2e/` **no longer
  exists** — `/tmp` was purged. The source EPUB survives outside the repo. Nothing
  in this project should live under `/tmp` again.

### Standing rule

The EPUB corpus is copyrighted and both repositories are public. **Never write
book concept names, chapter titles, quoted passages or real test queries into any
file, commit message or PR body.** Tests use an invented tidal-observation corpus;
the real mapping lives in the gitignored `docs/specs/local/`.
