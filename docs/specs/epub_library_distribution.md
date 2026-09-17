# EPUB Wiki Library Distribution — Design Proposal

**Status:** Proposal, under discussion. No code written for the distribution
mechanism itself; D-13 has implementations in review (§6.2).
**Last updated:** 2026-09-17
**Related:** [`epub_concept_sdd.md`](./epub_concept_sdd.md) (T-170a portable overlay),
[`epub_concept_task_status.md`](./epub_concept_task_status.md)

## 1. Goal

Ship the EPUB Concept Wiki to colleagues. An operator publishes a book and its
analysis to a personal server; each colleague's AuraPro Desktop downloads what
it needs and the feature just works, with as close to zero configuration as
possible.

Two problems are in scope: **distribution** (getting book + analysis onto a
colleague's machine) and **simplification** (colleagues report too many settings).

## 2. Decisions taken

| #    | Decision                                                                                                                    | Rationale                                                                                                                                                                                                                                                                                                                         |
| ---- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D-1  | The server hosts **both the EPUB and the overlay**                                                                          | Best experience; also makes `epub_sha256` match by construction, so the overlay can never fail to attach because a colleague's copy differs by a byte. The copyright exposure of redistributing the work was raised and accepted by the owner; mitigate with password-protected access limited to internal colleagues.            |
| D-2  | **Each colleague installs their own Desktop** (no shared server)                                                            | Removes shared-library isolation entirely. One store per machine, and the local user is that machine's administrator.                                                                                                                                                                                                             |
| D-3  | **Manual publish, direct-link fetch, password-protected**                                                                   | Operator uploads EPUB + overlay by hand. The client checks for updates and downloads over a direct link behind a password. Reuse the Desktop official-glossary mechanism rather than inventing one.                                                                                                                               |
| D-4  | **Models download on first launch**, not bundled                                                                            | Keeps the installer small. Paired with progressive enhancement (§5) so first use is not a 1.3 GB wait.                                                                                                                                                                                                                            |
| D-5  | **Published books use mirror semantics**; locally built graphs keep today's additive semantics                              | See §4.                                                                                                                                                                                                                                                                                                                           |
| D-6  | The library password is **distributed out of band** and entered once by the reader                                          | Not baked into the build: a build-embedded secret cannot be rotated without shipping a new installer, and it leaks to anyone who unpacks the app. The operator sends it through a separate channel; the client stores it after first entry, exactly as the official-glossary flow already does.                                   |
| D-7  | **Readers may not curate a published book.** Enforced by the service refusing curation on a published version               | Makes the one unacceptable outcome — silently discarding a colleague's work — structurally unreachable instead of merely detected.                                                                                                                                                                                                |
| D-8  | **Exactly one publisher.** The owner alone runs the Batch extraction and uploads; every colleague is permanently a consumer | Settles the persona split in §6 as a permanent property of the deployment, not a default some installs might invert.                                                                                                                                                                                                              |
| D-9  | **Update by uninstall-then-reinstall of the analysis layer only** — not a computed diff, and not a full re-import           | One code path that cannot drift from a fresh install, which is the failure mode a diff invites. Scoped to the analysis because the EPUB bytes are identical by D-1, so re-parsing and re-embedding would cost minutes to reproduce what is already there.                                                                         |
| D-10 | **A single monotonic `overlay_version` is enough**; the catalog does not express supersession                               | Under D-9 an update is a whole replacement, not a replay of increments, so a client jumping v1→v5 lands in exactly the state as one that took every step. Supersession would only matter if the semantics ever became incremental.                                                                                                |
| D-11 | **The library exists because book data must stay out of the public repositories** — not merely for convenience              | `AuraPro-Desktop` and `AuraPro-WebUI` are public. Books and overlays are therefore served from a private password-protected server and can never be bundled, whatever else changes. This is the permanent justification for the whole mechanism.                                                                                  |
| D-12 | **Three roles, not two: one publisher, several deployers, many consumers**                                                  | Several people run Desktop + WebUI on their own machines and install books there; others reach one of those instances through a browser. D-8 still holds for _publishing_ — the owner alone generates overlays and uploads — but installing is done by each deployer, so the library client must exist on every deployer machine. |
| D-13 | **Tier-2 uses whatever model llama.cpp is already serving.** No EPUB-specific model download                                | Desktop currently fetches a dedicated ~2.1 GB GGUF for concept resolution (`EPUB_CONCEPT_MODEL_REPOSITORY`, `ensureEpubConceptModel`). It should instead use the model the deployer chose for their hardware, shared with every non-EPUB use. Saves 2.1 GB per deployer and makes capability scale with the machine.              |

## 3. Distribution architecture

Copy the shape of `AuraPro-Desktop/src/main/utils/official-glossaries.ts`, which
already solves this exact problem for dictionaries:

- `manifest.json` — the catalog; `version.json` — cheap update probe
- HTTP Basic auth, username fixed in the build, password supplied once
- SHA-256 verification per file
- Size and count ceilings (`MAX_MANIFEST_BYTES`, `MAX_FILE_BYTES`,
  `MAX_PACKAGE_BYTES`, `MAX_FILE_COUNT`) — an untrusted server must not be able
  to fill the disk
- URL safety validation before any fetch
- An **installed manifest** persisted locally, diffed on update

Server layout (static hosting; no server-side code to maintain, therefore no
server-side bugs):

```
/manifest.json                 catalog of available books
/version.json                  {version} for a cheap update check
/<epub_sha256>/book.epub
/<epub_sha256>/overlay.json
```

Per-book catalog entry must carry, at minimum:

| Field                                | Why                                                                                                                                                              |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `title`, `epub_sha256`, `epub_bytes` | identity and download sizing                                                                                                                                     |
| `overlay_sha256`, `overlay_bytes`    | integrity and update detection                                                                                                                                   |
| `overlay_version`                    | monotonic publication counter, drives §4                                                                                                                         |
| `parser_format_version`              | **checked before download** — a mismatch makes the overlay unattachable, and the reader should learn that before spending the bytes, not after gate 2 rejects it |
| `concept_count`, `relation_count`    | lets the UI show what will be gained                                                                                                                             |

Client install flow: fetch EPUB → verify sha256 → run the existing import
pipeline (parse, passages, retrieval units) → fetch overlay → `apply_overlay`.

### 3.1 Permission model — confirmed

**The local user is an administrator, so the Library needs no new permission
boundary.** `POST /admin/import` and `POST /admin/overlays` are reusable as-is;
the change is a new **UI entry point**, nothing more. The admin page already
calls both (`src/routes/(app)/admin/epub/+page.svelte`, via `importEpub` and
`applyEpubOverlay` with `token()` from `localStorage`).

Two independent paths both yield `role = 'admin'`:

- **Path A, what a stock install actually does:** Desktop ships a git-tracked
  `data/webui.db` holding one pre-seeded admin account, copied into the data
  directory on install. `ui.enable_signup = False` ships with it.
- **Path B, guaranteed by source:** on an empty database the first signup is
  forced to `admin` after insert (`routers/auths.py`), overriding
  `DEFAULT_USER_ROLE`.

Desktop passes no auth-related environment at all — `WEBUI_AUTH`,
`DEFAULT_USER_ROLE` and `ENABLE_SIGNUP` appear nowhere in that repo — so backend
defaults apply (`WEBUI_AUTH = True`).

**Anchor the design on Path B, not Path A.** Path A is a fact about committed
bytes; if that database is regenerated or dropped from `extraResources`, installs
fall through to Path B. Only Path B is guaranteed by code.

**Do not offer `WEBUI_AUTH=False` as a simplification.** It is a footgun here: the
auto-provisioned account does not exist while the shipped one does, so login
breaks outright rather than being bypassed.

### 3.2 Desktop integration constraints

Each of these shapes the implementation.

1. **A data-loss risk — fixed upstream, no longer a constraint.**
   `migrateDataIfNeeded` used to **delete the entire data directory** while
   `dataVersion < REQUIRED_DATA_VERSION` and re-copy the bundled one, preserving
   only glossary files by name. Imported EPUBs and applied overlays live in
   `webui.db` under `DATA_DIR`, so a future version bump would have destroyed
   every book a colleague installed, along with chats, `uploads/`, `vector_db/`
   and the `.key` secret. **`AuraPro-Desktop` PR #17 merged on 2026-09-13** and
   replaced the wipe with additive seeding
   (`src/main/utils/data-seed.ts`): a bundled top-level entry is copied only when
   the target does not already have it, and nothing is removed. The Library
   therefore needs no preserve-list and no path outside the data directory. One
   obligation survives: a seed change that cannot be applied in place has to name
   the file in that step's `replace` list, which overwrites it after moving the
   existing copy into a timestamped backup — so `webui.db` must never appear
   there.
2. **Glossaries never touch the backend.** The Electron main process downloads
   them and writes them straight into the shared data directory; the backend
   reads those paths. So the Library can reuse the whole _download_ half —
   manifest schema, version compare, Basic auth, sha256 and size verification,
   temp staging, atomic replace, rollback, cross-origin guard — but the _apply_
   half has **no precedent in this codebase**. It must be an authenticated POST
   to the two endpoints above, so the overlay lands inside a database
   transaction.
3. **Prefer a WebUI-frontend entry point over an Electron-main one.** A frontend
   Library page inherits the session and backend-readiness handling for free. A
   main-process implementation must handle a backend that is not up yet (startup
   polls reachability for up to 600 s), obtain a token that only exists after the
   WebView has loaded and the user has signed in, and discover a port that is
   scanned upward from a base rather than fixed.

**How the session actually exists (resolved).** Desktop presents no account
concept to the user, and there is no auto-login in the main process. WebViews run
in **persistent partitions** (`persist:connection-*`), so a session survives
restarts: someone signs in once during setup and the login screen is never seen
again. Operationally the single shipped account is an administrator, which is why
colleagues experience "Desktop has no accounts" while the backend still enforces
`WEBUI_AUTH = True`.

For the Library this means the token is simply there, in that partition's
`localStorage`, exactly where the existing admin EPUB page reads it from. **No
new authentication work is required.**

Two edges to handle rather than assume away:

- If a partition is ever cleared, or on a machine set up without that first
  sign-in, there is no session and no recoverable password. The Library must show
  a plain "not signed in" state, not an opaque failure — it cannot create a
  session and should not pretend it might.
- First-run sign-in is an operations step, outside this design.

## 4. Overlay update semantics — the core design problem

### 4.1 What today's `apply_overlay` does

Strictly additive. Its result counters are only `*_created` / `*_updated`; there
is no removal path anywhere. Aliases are "unioned, never removed". Applying the
same artifact twice is a no-op.

Conflict policy (verbatim intent): a concept's status is adopted only while the
local one is still `PROVISIONAL`; a local `APPROVED` is never downgraded and a
local `REJECTED` never resurrected; local canonical spelling and non-empty
definition win, with the overlay's spelling demoted to an alias; every row is
written with source `MODEL` so published output can never masquerade as the
local operator's `ADMIN` decision.

### 4.2 What that costs, by change type

| Publisher's change                                | Reaches an already-installed reader?                                                      |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| New concept / mention / relation                  | Yes                                                                                       |
| Definition filled where local was empty           | Yes                                                                                       |
| Status → `REJECTED` while reader is `PROVISIONAL` | Yes                                                                                       |
| Concept **renamed**                               | Partially — the new name arrives only as an alias; the old canonical name persists        |
| Concept **merged**                                | **No** — a merge deletes the source concept, and additive apply cannot delete             |
| Concept **split**                                 | **No** — the new concept arrives, but mentions wrongly attached to the old one stay there |
| Bad extraction **deleted**                        | **No**                                                                                    |

**Every one of the administrator curation actions performed during the
2026-08-08/09 sessions falls in the "No" rows** — the corrected merge set, the
two administrator merges, and the merge that was applied and then undone via
`split_concept`. That curation is precisely what turns raw model output into a
usable graph, and it is exactly what additive apply cannot transmit.

### 4.3 Why the conflict policy does not fit this scenario

The policy was designed for _another server that is also building a graph_ — two
parties with genuine curation authority meeting. Under D-2 colleagues are pure
**consumers**: they never merge, split, approve or reject. There are no local
decisions to protect, so the protection buys nothing while its cost — undeliverable
curation — is paid in full.

### 4.4 Proposal: split the semantics by asset origin

| Origin                            | Semantics                                                                                                                                                                                       |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Installed from the library**    | Marked _published_. The graph is a mirror of the publisher's. An update **replaces** that version's overlay-derived rows, then re-applies. Merges, splits, renames and deletions all propagate. |
| **Built locally** (own Batch run) | Unchanged. Today's additive + local-decision-wins policy applies exactly as now.                                                                                                                |

This needs no new provenance column: a _published_ asset has, by construction, a
single source on the reader's machine. The publication boundary is the book
version itself, not individual rows.

Update detection is the glossary mechanism unchanged: store the installed
manifest, compare `overlay_version` / `overlay_sha256`, act on difference.

**Curation of a published book is forbidden outright (D-7).** Earlier drafts
offered a choice between forbidding it and detecting local `ADMIN` rows before a
replace. The owner has settled it: readers do not curate. Forbidding is both
simpler and safer, because it makes the dangerous outcome _unreachable_ rather
than _detected_ — with no local decisions able to exist on a published asset,
replace cannot discard anything, and the guard needs no recovery path.

**Enforce it in the service layer, not by hiding UI.** Under D-2 the local user
is (pending confirmation) that machine's administrator, so the curation endpoints
— merge, split, concept review, relation-assertion review — remain reachable
whatever the UI shows. The rule must be a refusal on a version marked
_published_, not an absent button.

**A consequence worth noticing:** with curation impossible on published assets,
`apply_overlay`'s conflict policy becomes inert on the mirror path — there is
never a local `APPROVED`, `REJECTED`, spelling or definition to defend. Its five
verification gates remain essential and unchanged; only the conflict resolution
is moot. That is what makes replace-then-reapply safe by construction rather
than by discipline.

**Settled (D-9): uninstall then reinstall, scoped to the analysis layer.** One
code path, no diff to keep in step with a fresh install. That decision stands.
What follows replaces the _mechanism_ this section published earlier, which
could not execute as written.

#### The correction, stated plainly

An earlier revision of this section listed two schema facts and said both "were
verified rather than assumed". Both facts are true. The claim was still wrong,
because it was a claim about the _set_: verifying two facts is not the same as
verifying that two facts are all of them, and the set was incomplete.
`concepts(concept_id)` is referenced by four foreign keys. That revision named
one of them, and the two it missed are the two that abort the operation.

Reproduced against the real schema — the migration DDL executed verbatim with
`PRAGMA foreign_keys = ON`:

- On any store where an administrator has ever merged concepts, step 4 of the
  published order failed with `FOREIGN KEY constraint failed`. Steps 1–3 had by
  then already deleted the mentions. The audit table vetoed the cleanup it exists
  to outlive.
- With no merge in the store the order ran to completion and deleted an
  administrator-curated `APPROVED` concept, with a hand-written definition,
  belonging to a **different book** — because the orphan test was global where it
  had to be version-scoped. `PRAGMA foreign_key_check` returned clean afterwards,
  so nothing in the schema would have reported the loss.

Both failures are one mistake seen twice: the section reasoned about the tables
it had in mind rather than about everything that points at `concepts`. A reader
who took the earlier order as verified should re-read this whole subsection.

#### What actually constrains the delete order

**`concepts` has no version column.** Concept identity is global across the
library (SDD 4.2.2), so "delete this book's concepts" is not expressible directly
— a concept may also be carried by another installed book, or by no book at all.
That fact was right, and it is the reason every other one matters.

Every reference to `concepts(concept_id)`, in
`backend/open_webui/retrieval/epub/store.py` (line numbers as of `origin/main`,
2026-09-17):

| Referencing column                                                         | On delete  | What it means for the uninstall                                              |
| -------------------------------------------------------------------------- | ---------- | ---------------------------------------------------------------------------- |
| `concept_mentions.concept_id` (:267)                                       | `RESTRICT` | Mentions must go first. Known, and correct.                                  |
| `concept_relations.subject_concept_id` (:334), `.object_concept_id` (:336) | `RESTRICT` | A relation another version still asserts vetoes the delete.                  |
| `concept_merges.target_concept_id` (:408)                                  | `RESTRICT` | A merge audit row vetoes the delete of its surviving target.                 |
| `concept_aliases.concept_id` (:255)                                        | `CASCADE`  | Deleting a concept silently destroys its alias spellings — no error to miss. |

The `CASCADE` is the one with nothing to raise, so it is the one to watch.
`merge_concepts` deliberately keeps a folded-away concept's canonical spelling as
an alias of the survivor, because that spelling is exactly what the next model
response will match on (:1983). Those aliases cannot be rebuilt from the audit
tables: `concept_merges.source_canonical_name` is one spelling, not an alias set,
and `concept_splits` records no alias movement at all. A concept deleted in error
takes vocabulary with it that nothing can reconstruct.

Two further facts about the `RESTRICT` pair, both of which the order has to
respect rather than discover at runtime:

- A `concept_merges` target can have **no mentions of its own**, which is exactly
  the shape the veto fires on.
- The relations veto is reachable through `split_concept`, which moves mentions
  but **deliberately never repoints relations** (:2281). A split that moves a
  concept's whole footprint in one version leaves that concept still anchoring a
  relation asserted there, with no mention of its own left to justify it.

And the orphan test has to be scoped because a mention-less concept is an
ordinary object here, not a leftover: `upsert_concept` (:1830), reachable over
`PUT /admin/concepts` (`routers/epub.py`:451), creates a concept with a
definition and a status and no mentions at all.

#### The corrected order

All of it inside **one** transaction, opened `BEGIN IMMEDIATE`.

0. **Capture the candidate set first**, before anything is deleted: the concepts
   holding at least one mention whose passage belongs to this version. After step
   1 that evidence is gone, and with it every way to scope the cleanup. This is
   what turns the global sweep into a version-scoped one.
1. Delete mentions whose passage belongs to this version.
2. Delete the relation assertions scoped to this version. Their evidence spans go
   with them — `concept_relation_evidence.assertion_id` is `ON DELETE CASCADE`
   (:363) — so deleting evidence separately is belt-and-braces, not a
   requirement.
3. Delete relations left with no assertion at all.
4. **From the candidate set only**, delete the concepts that now have no mentions,
   are not a `concept_merges` target, and are not an endpoint of any surviving
   relation. Both `RESTRICT` references are excluded _by the query_, so a foreign
   key never has to abort the transaction.
5. Apply the new overlay.

Sketch, with `:version_id` bound once:

```sql
CREATE TEMP TABLE uninstall_candidates AS          -- step 0, before any delete
SELECT DISTINCT m.concept_id
  FROM concept_mentions AS m
  JOIN passages AS p ON p.passage_id = m.passage_id
 WHERE p.version_id = :version_id;

DELETE FROM concept_mentions                       -- step 1
 WHERE passage_id IN (SELECT passage_id FROM passages WHERE version_id = :version_id);

DELETE FROM concept_relation_assertions            -- step 2 (evidence CASCADEs)
 WHERE version_id = :version_id;

DELETE FROM concept_relations                      -- step 3
 WHERE relation_id NOT IN (SELECT relation_id FROM concept_relation_assertions);

DELETE FROM concepts WHERE concept_id IN (         -- step 4
    SELECT concept_id FROM uninstall_candidates
    EXCEPT SELECT concept_id         FROM concept_mentions
    EXCEPT SELECT target_concept_id  FROM concept_merges
    EXCEPT SELECT subject_concept_id FROM concept_relations
    EXCEPT SELECT object_concept_id  FROM concept_relations
);
```

**A concept that survives step 4 because it is a merge target, or because a
relation still names it, is correct behaviour and not a leak.** In both cases
another durable record still depends on that identifier — the audit row saying
what an administrator folded into it, or a relation another installed version
still asserts. Deleting it would break that record; keeping it leaves a concept
with no mentions, which this schema already treats as ordinary. Report the count
so an operator can see it happened; do not treat it as an error to be cleaned up
later.

#### One transaction, and `BEGIN IMMEDIATE`

The steps above are not five statements issued in turn. They are **one
transaction**. `_write()` (:727) is already exactly that — `BEGIN`, yield,
`commit()`, with `rollback()` on any exception — so an abort inside it unwinds
the whole thing and leaves the store as it was. Run as separate statements, the
step-4 abort described above leaves the analysis layer half destroyed, with no
way to finish and no way to undo.

`_write()` opens with plain `BEGIN`, which is `DEFERRED`. Under WAL a deferred
transaction that reads before it writes — and step 0 is a read — takes its read
snapshot first; if another connection commits in between, the first write fails
with `SQLITE_BUSY_SNAPSHOT`. The busy handler is not invoked for that error, so
`PRAGMA busy_timeout = 5000` (:685) does not cover it and the only recovery is to
roll back and start over. **This operation must open `BEGIN IMMEDIATE`**, taking
the write lock up front so the snapshot is stable across the whole read-then-write
sequence. That is a requirement on how this one operation begins its transaction,
not a proposal to change `_write()` for every caller.

#### The overlay export is not a restore point

The update story assumes that reapplying an overlay reproduces the state. That is
true of the published analysis and false of the store, so "we can always reapply"
is not available as a fallback for a wrong uninstall:

- **No vectors.** `ConceptOverlay` (`overlay.py`) carries concepts, aliases,
  definitions, mentions and relations. Retrieval units and their embeddings are
  not in it. This costs nothing under D-9, because the parsed book is not deleted
  — but an overlay is not a backup of a book.
- **Unanchored mentions are dropped.** The export filters
  `m.start_codepoint IS NOT NULL` (:2738), so a mention recorded without offsets
  does not survive a round trip.
- **Mention-less concepts are excluded.** The export reaches concepts by joining
  through `concept_mentions` (:2709) — precisely the concepts step 4 deletes, and
  precisely the ones an administrator creates by hand. A curated concept with no
  mention in the version is not in the overlay and cannot come back from one.
- **`ADMIN` provenance is re-imported as `MODEL`.** `apply_overlay` writes every
  mention with `source='MODEL'` (:2946) and every alias likewise (:3024),
  deliberately, so published output can never masquerade as the local operator's
  decision. A round trip therefore launders provenance.
- **`apply_overlay` can never delete.** It is strictly additive; there is no
  `DELETE` in it. It cannot undo a merge, a split or a deletion — which is the
  whole reason D-9 exists.

So the uninstall must be right the first time.

#### If a version row is ever deleted

D-9 does not delete the book version, and should not. Any future path that does
must clear `books.current_version_id` in the same transaction.
`books.current_version_id` is a plain `TEXT` column with **no foreign key**
(:154), and `set_version_status` only ever _sets_ it, on the transition to
`READY` (:866) — nothing clears it. A deleted version therefore leaves a dangling
pointer that `PRAGMA foreign_key_check` cannot see, because there is no
constraint for it to check.

Related, and worth stating because it looks like a safety net and is not: the
`passages_content_is_immutable` trigger (:217) fires `BEFORE UPDATE OF`. It stops
a passage being rewritten. It does not fire on `DELETE` and protects nothing in
this operation.

**Do not delete the parsed book.** The EPUB is byte-identical across publications
by D-1, so re-importing would re-parse every passage and re-embed every retrieval
unit to reproduce exactly what is already on disk — minutes of work for no
change. Only the analysis layer is republished, so only the analysis layer is
replaced.

## 5. Model provisioning and progressive enhancement

Channel dependency is asymmetric, and that asymmetry is the whole opportunity:

| Channel    | Model               | Size            |
| ---------- | ------------------- | --------------- |
| A — graph  | **none**            | 0               |
| B — vector | BGE-small-zh-v1.5   | ~183 MB         |
| fused      | + BGE-reranker-base | ~1.1 GB         |
| Tier-2     | Qwen2.5-3B GGUF     | ~2 GB, optional |

Therefore: **a freshly installed book is searchable immediately on Channel A with
no model at all.** Desktop downloads models in the background; vector and fused
unlock as each lands. First-run experience is "search works now, and gets better",
not a 1.3 GB progress bar before anything happens.

This composes with the existing fail-closed design: a missing model already
produces an explicit `degraded` component rather than silence, so the UI can
drive capability display off a mechanism that already exists. (That mechanism is
what surfaced the CUDA device bug fixed in PR #30.)

Desktop already manages `llama.cpp` and `python-build-standalone` via
`generateDownloadUrl`; BGE models extend an existing pattern rather than adding
a new one.

## 6. Configuration simplification

The complaint is "too many settings". The cause is that two personas share one
configuration surface — 15 `EPUB_CONCEPT_*` variables plus RAG model settings.

| Persona                                          | Actually needs                                                                                     |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| **Publisher** (the owner, alone — D-8)           | Batch API key, endpoint, completion window, prompt profile, admin review UI, merge/split decisions |
| **Deployer** (several — D-12)                    | The library password, once. No Batch key, no prompt profiles, no curation                          |
| **Reader** (every colleague, daily, permanently) | Nothing                                                                                            |

Because D-8 makes this permanent, the publishing surface can be hidden on
consumer installs rather than merely de-emphasised. A clean way to decide which
install is which, without shipping two builds or adding a setting: **gate the
publishing surface on whether a Batch API key is configured.** With no key that
surface is inert anyway — no Batch job can be created — so hiding it is honest
rather than cosmetic, and the owner's own install lights up with no extra
configuration.

So the fix is not "reduce 15 settings to 5" — it is that **the reader path should
have zero settings**, and the publisher's settings should not be visible to
readers at all. Concretely: DB path defaults under `DATA_DIR`; device is already
automatic (PR #30); models are Desktop-managed; Tier-2 is optional and enabled by
presence; Batch and admin surfaces are hidden. The library URL is baked into the
build; the password is sent to colleagues out of band and entered once (D-6) --
one entry, ever, which does not reopen the "too many settings" complaint.

**Note the real obstacle was never the variable count** — it is the 1.3 GB of
model downloads, addressed by §5. Removing ten environment variables without
progressive enhancement would not have improved first-run experience at all.

### 6.1 Why a shared instance does not remove this work

A single machine serving everyone by browser would remove the catalog, the
password, the per-machine download and D-9 entirely. It was considered and does
not apply: books must stay off the public repositories (D-11), and several people
deploy independently on their own machines (D-12). The library client therefore
has to exist wherever a deployer runs, and locally installed books are the point
rather than a fallback.

What a shared instance still buys, for the consumers hanging off one deployer:
they install nothing, download no models and enter no password. That shape is
already supported — reads are `get_verified_user` — and needs no work.

## 6.2 Model capability varies by deployer (D-13)

**Implementation status (2026-09-17): open for review, not merged.**
`AuraPro-WebUI` PR #36 and `AuraPro-Desktop` PR #19 implement D-13 and are both
green in CI; neither has been merged. Shipped behaviour is still the dedicated
GGUF download, so the 2.1 GB saving is pending rather than banked, and the rest
of this section describes the design those two PRs implement.

**Correction worth stating, because the two are easy to conflate.** The dedicated
Qwen GGUF is used by `LlamaCppConceptResolver` for **Tier-2 concept resolution** —
picking one already-existing concept out of a supplied shortlist. It is _not_ the
reranker. Reranking is a BGE cross-encoder loaded in-process by
sentence-transformers (`routers/retrieval.py:200`) and **already shared** with
generic RAG. llama.cpp does not serve cross-encoders, so D-13 applies to Tier-2
only; reranking is out of its scope and has no duplication to remove.

Under D-13 the resolver's quality becomes a function of the deployer's hardware:
a large model on a strong machine, a small one elsewhere. Two properties already
in the design make that safe rather than alarming:

- **The model may only select, never invent.** It is handed a bounded shortlist
  and its answer is re-validated through the same Tier-1 matcher; a name that does
  not exist resolves nothing. A weaker model therefore abstains or picks a
  wrong-but-real concept — it cannot fabricate one.
- **Tier-2 affects recall, never citation integrity.** Every excerpt is a
  byte-exact slice of an immutable passage regardless of which concept was
  resolved. A poor resolution yields worse results, never a false citation.

So capability degrades gracefully across machines. Measured on Qwen2.5-3B: 0 of 17
confidently wrong, 7 useful resolutions, 6 abstentions where an answer existed —
and in 5 of those 6 the right concept was already in the shortlist, so the model
was the ceiling. A larger model should convert some of those; a smaller one will
abstain more. Neither breaks anything.

Implementation notes: stop naming a fixed model in `EPUB_CONCEPT_LOCAL_LLM_MODEL`
and discover what is loaded (the runtime descriptor, or llama.cpp's `/v1/models`).
Note llama.cpp runs with `--models-max 1`, so there is exactly one resident model
and no swap cost. With none loaded, Tier-2 already reports an explicit degraded
component rather than failing silently.

## 7. Phased plan

1. **Library works** — catalog format, Library page, one-click install
   (EPUB → import → overlay → apply), `parser_format_version` pre-check.
   No model work, no config work. Distribution path proven end to end.
2. **Mirror update semantics** (§4.4) — published-asset marking, version compare,
   replace-then-reapply, guard against discarding local curation.
3. **Progressive model enhancement** (§5) — Desktop-managed BGE models, capability
   display driven by `degraded`.
4. **Configuration collapse** (§6) — hide publisher settings from readers, defaults
   everywhere, library URL preset.

Publisher-side tooling is small: a script that, given a `version_id`, exports the
overlay, copies the EPUB, and updates `manifest.json` / `version.json`.

## 8. Open questions

_None outstanding for the design itself._ The two prerequisites that sat outside
it and blocked the one-click story are both resolved:

- ~~How colleagues reach a signed-in state~~ — **resolved** (§3.1): the WebView
  session persists across restarts, so the token is already present.
- ~~`migrateDataIfNeeded` wipes the data directory~~ — **resolved** (§3.2, item
  1): `AuraPro-Desktop` PR #17 merged on 2026-09-13 and replaced the wipe with
  additive seeding, so a `REQUIRED_DATA_VERSION` bump no longer destroys
  installed books.
