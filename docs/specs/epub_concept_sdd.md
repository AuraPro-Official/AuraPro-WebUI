# EPUB Concept Wiki — SDD Specification

**Status:** Draft for implementation
**Last updated:** 2026-08-01
**Task tracker:** [`epub_concept_task_status.md`](./epub_concept_task_status.md)

This document is the implementation-level source of truth for the EPUB Concept
Wiki. It records decisions confirmed with the product owner and takes precedence
over earlier planning documents when they conflict. Read this document and the
task tracker before starting or resuming work.

## 1. Product scope

The system imports EPUB books into a shared private-server library, builds a
concept graph offline, and lets every authenticated user retrieve faithful source
paragraphs. EPUB ingestion and Batch work are administrator-only. Reading books,
viewing concepts, and searching are available to every authenticated user.

The primary UI belongs in the AuraPro-WebUI frontend. AuraPro Desktop displays
that same UI in its authenticated WebView; it is not a separate feature client.

## 2. Non-negotiable invariants

1. `passage` is the immutable source unit. One visible EPUB paragraph becomes
   one passage and is never split, synthesized, or transformed after extraction.
2. `content` is visible reading text, not XHTML markup. It preserves the
   extracted characters and punctuation exactly. The stable reading-text rule is:
   XHTML reading order; entity decoding; `<br>` as newline; ignore scripts,
   styles, and metadata; do not execute CSS or JavaScript.
3. A query response always includes the complete source `content`. An optional
   `excerpt` must be a continuous substring of it and carries Unicode-code-point,
   end-exclusive offsets. The service verifies the substring before returning it.
4. Retrieval windows, sentence ranges, and vectors are derived indexes only.
   Each points back to one immutable parent passage and can never replace it as
   the citation or storage unit.
5. Online search must use local/private-network inference only. It must never
   fall back to a cloud LLM, embedding API, or reranker. Cloud Batch use is
   allowed only as an explicit administrator-triggered offline task.
6. A complete EPUB SHA-256 is the duplicate identity. A same-title book is not
   automatically merged; a content change requires an explicit new-version or
   new-book administrator action.

### 2.1 Confirmed text-extraction rules

- Normal HTML flow text uses default visible-text whitespace semantics: runs of
  source indentation, line breaks, and spaces collapse to one U+0020 space.
- `<pre>` preserves its whitespace exactly.
- The first release targets textual EPUBs. Images and tables are out of scope;
  they produce parser warnings rather than synthetic text.
- Besides `<p>`, non-overlapping headings, list items, block quotes, and `pre`
  blocks are retained as typed evidence units (`content_kind`). Text outside a
  typed element is retained in an ordered `fallback` unit rather than dropped;
  malformed XHTML additionally produces a recovery warning.

### 2.2 First-phase language and retrieval-window rule

The first operational EPUB acceptance sample is Simplified Chinese. Derived
retrieval units retain their parent passage and are created only for vector
retrieval: passages up to 800 Unicode code points produce one full-passage
unit; longer passages are split near Chinese sentence boundaries (`。！？；`)
with English punctuation as a fallback, targeting about 800 code points with
about 150 code points of overlap. A fallback character boundary is allowed only
when no sentence boundary is available. The displayed source remains the full,
immutable parent passage in every case.

## 3. Deployment model

| Profile | Canonical store | Derived vector index | Inference |
|---|---|---|---|
| Desktop / local server | Independent SQLite database | `sqlite-vec` in that database | Desktop-managed llama.cpp/Ollama or local model runtime |
| Private remote server | Independent PostgreSQL database | `pgvector` plus `pg_trgm` | Model service on the server/private network |

The EPUB store must not use the main `webui.db` or the generic OpenWebUI RAG
collection as its source of truth. Existing OpenWebUI model adapters may be
reused behind an EPUB-specific policy that permits only local/private endpoints.

### 3.1 Current runtime configuration

On a local server, startup creates and attaches an independent SQLite source
store at `${DATA_DIR}/epub_concept_v1.db` by default. The administrator may
set `EPUB_CONCEPT_DB_PATH` to another persistent file. The optional
`EPUB_CONCEPT_DATABASE_URL` is reserved for the remote-store implementation;
until the PostgreSQL repository is delivered, a PostgreSQL URL is rejected at
startup and EPUB routes remain fail-closed rather than writing to the main
WebUI database. `:memory:` is never valid for the shared library.

Offline OpenAI Batch is enabled only by the server-side
`EPUB_CONCEPT_BATCH_OPENAI_API_KEY` (with optional
`EPUB_CONCEPT_BATCH_OPENAI_ENDPOINT` and
`EPUB_CONCEPT_BATCH_OPENAI_COMPLETION_WINDOW`). It deliberately does not reuse
generic OpenAI/RAG credentials and no browser request can set it.

The online EPUB inference policy is stricter than generic RAG.  The built-in
AuraPro embedding and Cross-Encoder engines are accepted as in-process local
execution.  An AuraPro Ollama embedding configuration is accepted only when
its actual URL is loopback/private, or its private DNS hostname is explicitly
listed in the server-only comma-separated
`EPUB_CONCEPT_TRUSTED_MODEL_HOSTNAMES`; OpenAI, Azure, and external reranker
engines are disabled for EPUB rather than falling back.  Tier-2 concept
resolution is optional only while it is unconfigured: when enabled it requires
`EPUB_CONCEPT_LOCAL_LLM_ENDPOINT` and `EPUB_CONCEPT_LOCAL_LLM_MODEL`, validates
the llama.cpp endpoint with the same private-address policy, and accepts an
explicit private-DNS allowlist through
`EPUB_CONCEPT_LOCAL_LLM_TRUSTED_HOSTNAMES`.  Optional llama.cpp timeout and
output limit settings are `EPUB_CONCEPT_LOCAL_LLM_TIMEOUT_SECONDS` and
`EPUB_CONCEPT_LOCAL_LLM_MAX_TOKENS`.

Startup and the administrator runtime-status endpoint report the independent
vector extension, embedding, reranker, and resolver separately.  The response
contains no model URL, database path, or credentials.  A failed sqlite-vec SQL
health check or model policy validation remains degraded/fail-closed and never
substitutes a cloud service.

### 3.2 End-user local packaging requirement (D-010)

The development and acceptance-test path may run a separately installed
Homebrew `llama-server`, but this is not an end-user prerequisite.  For the
desktop/local-server profile, a user must be able to install only AuraPro
Desktop and AuraPro-WebUI, then use the EPUB feature without manually
installing Homebrew, llama.cpp, Python model tooling, or a separate model
server.

AuraPro Desktop is the owner of the local runtime lifecycle: it installs and
updates its compatible llama.cpp binary, selects an available loopback port,
downloads/selects the approved GGUF model, starts/stops the process, and
reports health.  It must expose the currently managed local endpoint and model
identifier to WebUI through an authenticated Desktop-to-WebUI integration;
WebUI must not require an end user to edit `EPUB_CONCEPT_LOCAL_LLM_*` variables.
The first-run UI must show model download progress, disk requirements, and a
recoverable failure state.  This requirement does not mean models are embedded
in the application installer: initial online model download is acceptable.  A
fully offline first run requires a separately distributed, versioned model
bundle.

For a shared remote WebUI server, user Desktop runtimes cannot execute
server-side EPUB retrieval.  That deployment instead requires an
administrator-owned private model runtime on the server/network.  Validate the
current Homebrew acceptance path first, then validate the Desktop-managed path
with no external user installation.

## 4. Functional requirements

### 4.1 Import and parsing

- Validate EPUB archives against size, entry-count, expansion-ratio, duplicate
  member names, and path traversal limits before parsing.
- Parse OPF spine order and both EPUB2 NCX and EPUB3 NAV trees.
- Preserve hierarchy and fragment anchors, including multiple TOC entries in one
  XHTML document.
- Retain the original EPUB object and full hash for reproducible re-parsing;
  raw-file download is administrator-only.

The parser is a pure, versioned component. It uses safe XML/XHTML parsing (no
external entities or network), honors declared encodings, and returns warnings
or an explicit failure rather than silently dropping malformed content. NAV takes
precedence over NCX; disagreement is retained as an auditable warning.

### 4.2 Offline concept build

- Persist each Batch job and item before submission; do not use an untracked
  temporary JSONL file as the job record.
- Support sample validation, JSONL construction, provider submission, polling,
  output retrieval, idempotent ingest, per-item retry, and restart recovery.
- Provider credentials are administrator server configuration and are never sent
  to or supplied by the client.
- Seed glossary aliases are authoritative. Only deterministic normalized-name or
  alias matches merge automatically. Model-suggested semantic merges are review
  candidates, not irreversible automatic graph mutations.
- **Deferred prompt optimization:** the initial Batch prompt performs generic
  concept extraction and does not include the entire seed glossary. If offline
  result review shows that the remote model fails to recognize established
  proper nouns, add only the seed canonical names/aliases relevant to the
  current passage as controlled candidate context. Keep the existing exact
  normalized-name/alias ingestion checks authoritative; prompt context must
  never itself create a semantic merge or bypass administrator review.

### 4.3 Search

- Tier 1 uses an in-memory multi-pattern matcher (Aho-Corasick or equivalent),
  with Latin-token boundary handling and direct CJK phrase matching.
- Tier 2 uses a local/private small LLM to resolve a concept only when Tier 1
  has no useful match. If unavailable, return an explicit degraded state rather
  than calling a cloud fallback.
- Channel A enumerates all graph occurrences and exposes an exhaustive count and
  pagination. Channel B returns vector candidates from derived retrieval units.
- Candidate windows are locally cross-encoder reranked, then diversified with
  MMR before their parent passages are rendered.

## 5. Result contract

Every result includes `passage_id`, `book_title`, `toc_path`, complete `content`,
`content_sha256`, `matched_concepts`, retrieval provenance, and this optional
object:

```json
{
  "excerpt": {
    "content": "a continuous source substring",
    "start_codepoint": 42,
    "end_codepoint": 86
  }
}
```

`end_codepoint` is exclusive. When no precise extractive span is available, the
excerpt is the full passage (`0` to the code-point length), never a generated
summary.

## 6. Implementation boundaries

- Place the parser under `backend/open_webui/retrieval/parsers/epub/` with
  separate archive, package/OPF, TOC, XHTML, TOC-mapping, and model modules.
  The current loader-path parser may only be a temporary compatibility shim.
- Use versioned repository/service interfaces for the EPUB data domain; business
  services must not invoke `sqlite3` or the generic `VECTOR_DB_CLIENT` directly.
- The WebUI frontend owns the feature page and its typed API client. Desktop
  receives no EPUB-specific IPC, credentials, or write privileges.
- Public read APIs require `get_verified_user`; every import, destructive,
  glossary, indexing, and Batch command requires `get_admin_user`.

## 7. Required acceptance evidence

- Fixture tests prove NCX/NAV and fragment breadcrumb correctness.
- Fidelity tests cover normal-flow whitespace collapsing, nested XHTML elements,
  entities, `<br>`, CJK punctuation, emoji, short paragraphs, typed text blocks,
  and long passages with derived windows.
- Integration tests cover duplicated imports, explicit versioning, foreign-key
  integrity, interrupted/retried Batch jobs, and idempotent output ingest.
- Search tests prove graph exhaustiveness, local-only inference, vector recall,
  reranking/MMR, exact excerpt offsets, and admin/read-only authorization.
