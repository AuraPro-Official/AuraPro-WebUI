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
resolution is optional only while it is unconfigured.  Administrator-managed
or development deployments may configure `EPUB_CONCEPT_LOCAL_LLM_ENDPOINT`
and `EPUB_CONCEPT_LOCAL_LLM_MODEL`; the endpoint is validated with the same
private-address policy and accepts an explicit private-DNS allowlist through
`EPUB_CONCEPT_LOCAL_LLM_TRUSTED_HOSTNAMES`.  In a Desktop-managed local
deployment, WebUI instead receives an absolute
`AURAPRO_DESKTOP_LLM_RUNTIME_FILE` path. Desktop atomically writes the
versioned, credential-free JSON descriptor containing its current loopback
endpoint and selected model identifier. WebUI re-reads it for every resolver
operation and validates the endpoint; a missing or malformed descriptor is
degraded/fail-closed and does not fall back to static settings. Optional
llama.cpp timeout and output limit settings are
`EPUB_CONCEPT_LOCAL_LLM_TIMEOUT_SECONDS` and
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
reports health. It must atomically publish the current endpoint and model
identifier to an absolute local JSON descriptor, pass only that descriptor's
path through `AURAPRO_DESKTOP_LLM_RUNTIME_FILE`, and remove or invalidate the
descriptor when the runtime stops. WebUI must not require an end user to edit
`EPUB_CONCEPT_LOCAL_LLM_*` variables.
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
- A terminal provider state is not enough to infer that every output was read.
  Only after the complete output/error result set has been fetched and
  structurally validated may the service mark an otherwise unreported item as
  `FAILED`. A successor job copies only durable `FAILED` items, never
  `SUCCEEDED` items. If result retrieval/download/parsing fails, preserve every
  unconfirmed item, record only the controlled `RESULTS_PENDING_RETRIEVAL`
  operator state, and require a later administrator poll before retry is
  enabled. Do not persist or display raw provider errors or result data for
  this condition.
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

### 4.2.1 Local-first prompt calibration

Before an administrator submits any passage to a cloud Batch provider, the
administrator can run a deterministic cross-chapter sample through the
Desktop-managed local Qwen runtime. A calibration profile has a stable ID and
defines the system instruction, output contract, decoding limits, and whether
passage-relevant seed terms are included. Local calibration records only
aggregate quality metrics in its administrator-facing report: schema-valid
rate, exact mention-offset/evidence rate, failure count, chapter coverage, and
candidate concept counts. It must not expose passage text through a report or
commit test-book content to the repository.

The local model is used to quickly reject malformed or low-signal prompt
profiles; its scores are not evidence that a cloud model will have equivalent
semantic quality. In particular, a small local model may identify a visible
evidence string while counting Unicode code points unreliably. Local calibration
may deterministically derive the start/end offsets only when that evidence has
exactly one literal occurrence in the immutable passage; missing or ambiguous
evidence remains a hard failure. This never changes source text or permits an
unverified citation.

Cloud Batch concept ingest uses the same immutable-source standard. A legacy
profile response with an invalid numeric offset may be repaired only when its
exact non-empty evidence has one literal occurrence in the item passage. The
current cloud profile additionally requires bounded (at most 48 Unicode code
points each) `context_before` and `context_after` anchors immediately adjacent
to the evidence. For repeated evidence, the service derives an offset only if
those anchors select exactly one source occurrence; absent, malformed, or still
ambiguous anchors are a hard item failure. A correct direct offset is still
verified against the source and its supplied anchors. The service persists the
canonical grounded payload, not an unverified raw provider result; a later poll
may re-ingest a previously failed item if it becomes safely normalizable, but a
different output can never overwrite a durable success.

A cloud calibration Batch must reuse the selected profile and the same
deterministic sample selection, pin a provider model snapshot, and be explicitly
authorized by the administrator before submission. Only after the cloud sample
is reviewed may a full-version Batch be created.

The server enforces this cloud quality gate durably, rather than relying on the
administrator UI: a full `openai-batch` job may be created only when an
administrator has approved an OpenAI **sample** for the same immutable
`version_id`, `job_kind` (`CONCEPT_MENTIONS` or `SECTION_GRAPH`), exact model
`profile_name`/snapshot, and the same `prompt_profile`. A sample is eligible
for review only after its durable job state is `SUCCEEDED`, it contains at
least one item, and every item has reached `SUCCEEDED` after strict atomic
ingest. The approval audit record contains only version ID, job kind, sample
job ID, prompt profile identifier, reviewer identity, decision, and review
time—never source text, prompt, model output, provider response, or
credentials. An approval for one job kind, model profile, or prompt profile
cannot unlock another. Only an administrator may list or approve/reject these
sample reviews.

`prompt_profile` is recorded on the job itself, because the model snapshot
alone says nothing about the extraction instruction that was sent: reviewed
quality belongs to one specific instruction, so promoting a new default prompt
profile must not ride an older profile's approval. The column is nullable only
because jobs predating it cannot gain a value inside a SQL migration. A null
is read as *unknown* and never satisfies the gate from either side—an
unbackfilled approved sample unlocks nothing, and a full run that names no
prompt profile is refused. An administrator-only backfill recovers the value
for such jobs by matching the system instruction in the job's own stored
request against the registered profiles by exact equality; anything that
matches none, or more than one, stays unknown rather than being guessed.

### 4.2.2 Concept-relation graph (required first-release capability)

Passage-level concept mentions alone are an evidence-backed terminology index,
not a sufficient concept graph. The first release must therefore build a
second, relation layer before its full-version offline Batch is accepted.

1. The parser writes the EPUB TOC hierarchy deterministically. A mention is
   already bound to one passage and therefore to one TOC node; this structural
   provenance is never inferred by a model.
2. A bounded TOC-subtree packet can extract concepts, aliases, definitions,
   exact passage mentions, and its intra-packet relations in one strict
   response. The system first resolves/creates the response's grounded
   concepts and mentions, then persists relations only between those resolved
   packet-local endpoints. Thus a relation cannot introduce a free-floating
   endpoint, while one remote request has enough local context to recognize a
   section-level concept and its parts together. All mention evidence/offset
   invariants remain as in section 4.2.
3. Follow-up relation-only packets remain available for later cross-section or
   cross-book analysis. They use existing concepts and exact evidence, but
   cannot invent a concept, a TOC node, or an ungrounded relation endpoint.
4. Initial relation predicates are controlled vocabulary: `HAS_PART`,
   `PRECEDES`, `PREREQUISITE`, `CAUSES`, `CONTRASTS`, and `ELABORATES`.
   Deterministic TOC parent/child edges remain structurally distinct from
   model-suggested semantic edges.
5. A concept-relation identity is global across the shared library, while every
   model or administrator assertion of that relation is scoped to one EPUB
   version and names one or more exact immutable-source evidence spans. This
   permits the same grounded relationship to accumulate support across books
   without turning it into an unproven universal fact. Assertions are
   `PROVISIONAL` until reviewed; ambiguous or invalid output is a failed Batch
   item with no partial graph mutation.
6. One exception to point 5, because it is not a defect in the output. A
   relation whose two endpoints resolve to the *same* concept is skipped and
   counted, and the rest of its packet still ingests. This arises when an
   administrator has merged the endpoints since the response was produced — the
   model named two distinct concepts, and a later, correct administrative act
   made them one. Failing the whole item would discard valid concepts and
   mentions because of an edge the administrator themselves collapsed.
   `merge_concepts` already resolves the identical condition this way, dropping
   a relation that a merge turns into a self-loop rather than refusing the
   merge, so this makes ingest consistent with it. The skip is reported in the
   item's durable result so the count is visible rather than silent. An
   endpoint that names a `local_id` the response never defined remains a hard
   failure: that is genuinely ungrounded output, not a merge artefact.
7. A second exception to point 5, of the same shape. An evidence span shorter
   than the floor its prompt profile enforces is dropped from the payload during
   grounding and counted, and the rest of its item still ingests. Such a span is
   real source text; it is simply too small to locate anything for a reader, and
   failing the item over it discards everything valid that arrived beside it —
   on the full-book section-graph run that cost 13 of 43 packets, 140 concepts,
   140 mentions and 105 relations, against 184 relations actually ingested. The
   drop cascades only to what the contract can no longer express: a concept left
   with no mentions is dropped, a relation left with no evidence spans is
   dropped, and a relation whose endpoint was one of those dropped concepts is
   dropped — the last of these is not an unresolved endpoint, because the model
   declared that concept correctly and ingest is what removed it. A payload
   reduced to nothing is still a success contributing nothing; an empty result
   is what the instruction itself asks for when there is nothing to report. The
   count is reported in the item's durable result, and is the only record the
   dropped spans existed, since the stored response is the payload as written.
   Nothing else becomes lenient: evidence absent from the immutable source, an
   undeclared relation endpoint, and every other rejection in point 5 still fail
   the whole item.

   The enforced floor is deliberately lower than the minimum the same profile's
   instruction requests. The request encourages a substantive, distinctive
   citation; the floor rejects only what is genuinely unusable. Length is a
   proxy for "distinctive and locatable", and the requested 10 code points
   overshoots that proxy in Chinese — `枢对测点的授时` (7) and `全网同步统一时基`
   (8) are complete citations, while the pathology is the bare term (`枢`,
   `潮位观测站`). The two numbers are named apart on the profile records, the
   requested one asserted against the instruction text so a profile cannot
   silently disagree with its own wording, and the enforced one pinned
   separately.

At query time, direct concept mentions and bounded relation traversal form the
graph candidate set. `HAS_PART` expands a resolved parent concept to its child
concepts; TOC provenance orders the resulting passages in book order. The
graph candidate set is combined with local vector candidates, locally
Cross-Encoder reranked, and MMR diversified. A graph-derived result always
returns its complete immutable passage and a verified source excerpt; the
relationship affects retrieval provenance and ranking, never citation text.

### 4.3 Search

- Tier 1 uses an in-memory multi-pattern matcher (Aho-Corasick or equivalent),
  with Latin-token boundary handling and direct CJK phrase matching.
- Tier 2 uses a local/private small LLM to resolve a concept only when Tier 1
  has no useful match. If unavailable, return an explicit degraded state rather
  than calling a cloud fallback.
- Channel A enumerates all distinct graph source spans and exposes an exhaustive
  count and pagination over them. The unit of enumeration is a distinct
  `(passage, start_codepoint, end_codepoint)` span, not a mention row: several
  concepts anchored on the same characters yield one result, and a span wholly
  contained by another span in the same result set is not returned separately —
  the maximal span survives and is attributed to every concept it absorbed.
  Partially overlapping spans that are not in a containment relation are returned
  separately; spans are never widened to their union, since that would render a
  citation no concept anchored. The exhaustive count and the paged list apply the
  identical predicate, so paging through the channel yields exactly `graph_total`
  results. Deduplication is scoped within one passage, so the set of passages the
  channel reaches is unchanged. Channel B returns vector candidates from derived
  retrieval units.
- Channel A is **ranked and then paginated**. Ranking is one stable total order
  over the whole result set, expressed as an `ORDER BY` over the same predicate
  the count uses, so pagination still walks every span exactly once and still
  ends at `graph_total`; a per-page rerank is not permitted, because it would
  make page 2 meaningless. Three deterministic signals apply strictly in order:
  (1) how expensive it was to reach the span's concepts, where a Tier-1 match
  costs nothing and one `HAS_PART` hop costs `1 + log2(children of that parent)`,
  so a span reached only by expanding through a high-degree hub always sorts
  below a directly matched one; (2) how many queried concepts the one span is
  attributed to; (3) span length, so a bare two-character name sorts below a
  substantive citation and an unanchored mention sorts last. Book order is the
  final tie-break. The ranking signals a span is ordered by are derived
  retrieval signals; they never alter what a span cites.
- A high-degree concept is down-weighted, never capped. Expansion coverage,
  `graph_total`, and the set of reachable spans are exactly what they would be
  without ranking: bounding the fan-out would delete source a reader could
  otherwise page to, and would do so invisibly, since only a smaller count would
  show for it. Ranking uses no model at all — Channel A must keep working with
  no local model configured, and the local Cross-Encoder stays in the fused
  channel, which is allowed to fail closed.
- The fused channel reads the top of the ranked Channel A from offset 0, bounded
  by its own limit, never the page the UI is displaying. `graph_limit` sizes a
  display page; it must not act as a recall ceiling on ranked fusion, and paging
  the graph panel must not change the fused answer.
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
