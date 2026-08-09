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
   `PROVISIONAL` until reviewed.
6. **Output that cannot be grounded against the immutable source fails the whole
   Batch item, with no partial graph mutation. Output that *is* grounded, but
   which a decision on our side has made unusable, has that element skipped and
   counted while the rest of the item ingests.**

   This replaces an earlier rule — "ambiguous or invalid output is a failed
   Batch item" — that drew its line in the wrong place. It read every rejection
   as a defect in the response, and three separate conditions then had to be
   exempted from it one at a time. All three share a shape the original rule did
   not anticipate: the model answered correctly and something *we* decided
   afterwards is what made one element unusable. A rule that has to be exempted
   three times is stating its principle wrongly, so the principle is restated
   here rather than carrying a fourth exemption later.

   Hard failure keeps everything that is genuinely ungrounded: evidence absent
   from the immutable source, evidence whose occurrence cannot be resolved to
   one span, a relation endpoint naming a `local_id` the response never
   declared, a response that is not valid JSON for its schema. None of these
   becomes lenient, and none of them is recoverable by any decision of ours.

   The skip applies where our own state is the obstacle. Three conditions
   qualify today; the wording is deliberately general, since the property that
   matters is the *cause*, not the enumeration.

   a. **A relation whose two endpoints resolve to the same concept.** An
      administrator merged the endpoints after the response was produced: the
      model named two distinct concepts and a later, correct administrative act
      made them one. `merge_concepts` already drops a relation a merge turns
      into a self-loop rather than refusing the merge, so this keeps ingest
      consistent with it.
   b. **An evidence span below the floor its prompt profile enforces**, dropped
      during grounding. Such a span is real source text, correctly quoted; it is
      simply too small to locate anything for a reader, and the floor is our
      threshold, not a property of the response.
   c. **A concept whose name and aliases match more than one existing concept.**
      Ingest cannot link it without asserting a merge no administrator decided,
      and SDD 4.2 forbids a model performing a semantic merge — so the concept
      and its mentions are skipped, and the rest of the item ingests. This is
      the case that most clearly belongs on this side of the line: the response
      is accurate, the passage genuinely contains both spellings, and the
      collision exists only because an administrator adjudicated those concepts
      as distinct. It differs from (a) and (b) in one respect worth stating
      plainly — a human *could* resolve it, by merging. Measured against the
      full-book runs, that remedy is mostly unavailable: of 33 held items, 32
      collided on pairs already adjudicated as distinct (13 on
      `全域潮汐枢纽`｜`潮汐源` alone), which no merge can resolve without
      reversing the adjudication, and a model will keep proposing them on every
      future book because the text genuinely uses both. Exactly one was a
      reviewable merge candidate. So the choice was never "skip versus review by
      hand"; it was skip versus permanently discarding 32 items and every valid
      concept and mention that arrived beside the collision. The trade taken in
      exchange is real and is not hidden: a skipped concept's mentions link to
      neither concept, and that silence is the cost of not failing the item.

   In every case the skip cascades only to what the contract can no longer
   express, and never further: a concept left with no mentions is dropped, a
   relation left with no evidence spans is dropped, and a relation whose
   endpoint was a dropped or skipped concept is dropped. That last one is not an
   unresolved endpoint — the model declared the concept correctly and ingest is
   what removed it. A payload reduced to nothing is still a success contributing
   nothing; an empty result is what the instruction itself asks for when there is
   nothing to report.

   Every skip is counted in the item's durable result, per condition, so the
   count is visible rather than silent. Each count is the only record of what
   the *write* decided, but the three differ in whether the element itself
   survives in the stored response, and the difference is a consequence of when
   it is detected. (b) is detected by the read-only grounding pass, which
   removes the span before anything is stored, so the count is the only record
   the span existed at all. (a) and (c) are detected at write time — a
   `local_id` becomes a concept only through resolution, which is a write — so
   the relation and the concept are both still in the stored response verbatim.
   That is deliberate and load-bearing: the stored response is the payload as
   written, it must serialize byte-identically on replay for ingest to stay
   idempotent, and a write that edited it to reflect its own skips would destroy
   that guarantee.
7. The evidence floor named in 6b, and why it is a number rather than a
   judgement. Failing an item over a sub-floor span discards everything valid
   that arrived beside it: on the full-book section-graph run that cost 13 of 43
   packets, 140 concepts, 140 mentions and 105 relations, against 184 relations
   actually ingested.

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
  with Latin-token boundary handling and **token-aligned CJK phrase matching**.
  Only the query is segmented, by a deterministic local dictionary tokenizer
  that performs no inference and opens no socket; concept terms are never
  segmented, since a phrase such as `枢对锚站的校验` exists as a term but as no
  tokenizer's token. Segmentation therefore supplies a boundary predicate, not
  a pattern set: a CJK match is valid only where both its ends coincide with a
  query token boundary, so a term may span several adjacent tokens while a term
  landing inside one (`义` within `意义`, the one-character alias `约` within
  `锚站`) is rejected. Where two valid matches overlap, one strictly contained
  in another is suppressed and the longer survives; equal spans both survive,
  matching Channel A's containment rule. If the segmenter is unavailable, Tier 1
  falls back to unsegmented CJK matching and the response reports a degraded
  `query-segmenter` component — a broader match over the same immutable source
  is acceptable, silently taking it is not.
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
- The preceding clause scopes to **ranking**, and it is not contradicted by the
  **resolution-stage** rule that follows, which decides a different question:
  not how a reached span is ordered, but which matched concepts are allowed to
  reach anything at all. A concept matched only by a short surface form still
  resolves, still appears in `resolved_concepts`, and still contributes every
  one of its own spans; it simply does not *seed* expansion. The test is about
  the **matched term**, never about the concept behind it: a concept is
  expansion-eligible when its winning matched term is at least two code points
  and is not a one-character model-proposed alias. Term length is measured on
  the winning match, since the matcher already keeps the longest surviving span
  per concept, so a concept whose full name the query spelled out is judged on
  that name. Nothing a seed reaches is capped or truncated — the down-weighting
  rule above still governs everything that is reached.
- Two properties of the *concept* are deliberately excluded from that rule.
  **Mention count is not a signal.** How often a book discusses something is a
  fact about the book, not about what the reader asked for: `枢纽的权重` is matched
  by its full five-code-point name and is a specific topic that a acceptance corpus
  naturally mentions often, so a frequency ceiling refuses exactly the reader who
  asked for it by name. The accepted consequence is that naming a high-degree
  concept in full expands to its subtree — that is the correct answer to having
  asked for it — while its one-character alias remains blocked by the term-length
  rule, so an incidental occurrence inside an unrelated query still drags nothing
  in. **The fraction of the query a term covered is not a signal** either, being
  unstable across phrasings and already handled by longest-match suppression. A
  repository that does not report a concept's alias source or `HAS_PART`
  out-degree is not second-guessed: the condition it cannot answer does not
  apply.
- Channel A additionally expands along **deterministic TOC parent/child edges**,
  which are structurally distinct from model-suggested semantic edges and are
  labelled as such: a hit reached this way carries `structure:TOC_CHILD:<depth>`
  and never `relation:...`, so a reader can tell the book's own hierarchy from a
  claim a model made. These edges are **computed per query and stored nowhere**;
  materialising them into `concept_relations` is not permitted, because a
  relation assertion requires an evidence span validated as an exact slice of a
  real passage and a structural edge has no prose evidence — the only way to
  satisfy that constraint would be to anchor structural edges on heading
  passages, which is the very failure this expansion exists to route around.
  Four rules bound it. A concept **binds** to a TOC node only when *every* one of
  its mentions falls in a passage under that node, so a concept the book
  discusses throughout binds to nothing and can neither seed this expansion nor
  be admitted by it. A seed expands this way only when it is expansion-eligible
  by the resolution rule above, is bound to exactly one node, and has
  **`HAS_PART` out-degree 0** — wherever the model supplied a decomposition that
  decomposition is authoritative and TOC structure stays out of the way, so this
  is a fallback and never a competitor. Only **child** nodes are walked and only
  concepts themselves fully bound inside those children are admitted; siblings
  are not, being an unbounded associative bag rather than a decomposition. A
  seed whose child set exceeds a fixed concept budget is **skipped whole and the
  skip reported as a degraded component**, never truncated, since a silently
  shortened set would leave `graph_total` a smaller number with nothing saying
  what was dropped. The hop is priced on the same scale as a `HAS_PART` hop —
  `parent_cost + 1 + log2(children reached)` — so it enters the one existing
  ranking with no second ordering. Expansion is to **concepts**, never to
  passages: the store's occurrence queries receive only a longer concept tuple,
  so the count and the pages keep their one shared predicate and a page walk
  over a TOC-expanded set still ends exactly at `graph_total`.
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
