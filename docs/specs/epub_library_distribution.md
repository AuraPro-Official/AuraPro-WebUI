# EPUB Wiki Library Distribution — Design Proposal

**Status:** Proposal, under discussion. No code written yet.
**Last updated:** 2026-09-07
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

| #    | Decision                                                                                                                    | Rationale                                                                                                                                                                                                                                                                                                              |
| ---- | --------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D-1  | The server hosts **both the EPUB and the overlay**                                                                          | Best experience; also makes `epub_sha256` match by construction, so the overlay can never fail to attach because a colleague's copy differs by a byte. The copyright exposure of redistributing the work was raised and accepted by the owner; mitigate with password-protected access limited to internal colleagues. |
| D-2  | **Each colleague installs their own Desktop** (no shared server)                                                            | Removes shared-library isolation entirely. One store per machine, and the local user is that machine's administrator.                                                                                                                                                                                                  |
| D-3  | **Manual publish, direct-link fetch, password-protected**                                                                   | Operator uploads EPUB + overlay by hand. The client checks for updates and downloads over a direct link behind a password. Reuse the Desktop official-glossary mechanism rather than inventing one.                                                                                                                    |
| D-4  | **Models download on first launch**, not bundled                                                                            | Keeps the installer small. Paired with progressive enhancement (§5) so first use is not a 1.3 GB wait.                                                                                                                                                                                                                 |
| D-5  | **Published books use mirror semantics**; locally built graphs keep today's additive semantics                              | See §4.                                                                                                                                                                                                                                                                                                                |
| D-6  | The library password is **distributed out of band** and entered once by the reader                                          | Not baked into the build: a build-embedded secret cannot be rotated without shipping a new installer, and it leaks to anyone who unpacks the app. The operator sends it through a separate channel; the client stores it after first entry, exactly as the official-glossary flow already does.                        |
| D-7  | **Readers may not curate a published book.** Enforced by the service refusing curation on a published version               | Makes the one unacceptable outcome — silently discarding a colleague's work — structurally unreachable instead of merely detected.                                                                                                                                                                                     |
| D-8  | **Exactly one publisher.** The owner alone runs the Batch extraction and uploads; every colleague is permanently a consumer | Settles the persona split in §6 as a permanent property of the deployment, not a default some installs might invert.                                                                                                                                                                                                   |
| D-9  | **Update by uninstall-then-reinstall of the analysis layer only** — not a computed diff, and not a full re-import           | One code path that cannot drift from a fresh install, which is the failure mode a diff invites. Scoped to the analysis because the EPUB bytes are identical by D-1, so re-parsing and re-embedding would cost minutes to reproduce what is already there.                                                              |
| D-10 | **A single monotonic `overlay_version` is enough**; the catalog does not express supersession                               | Under D-9 an update is a whole replacement, not a replay of increments, so a client jumping v1→v5 lands in exactly the state as one that took every step. Supersession would only matter if the semantics ever became incremental.                                                                                     |

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

1. **A real data-loss risk.** `migrateDataIfNeeded` runs on every startup and,
   while `dataVersion < 3`, **deletes the entire data directory** and re-copies
   the bundled one, preserving only glossary files by name. Imported EPUBs and
   applied overlays live in `webui.db` under `DATA_DIR`. **A future
   `requiredDataVersion` bump would destroy every book a colleague installed.**
   Extend the preserve-list before the Library ships, or keep library content
   outside the wiped path.
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
code path, no diff to keep in step with a fresh install.

Two schema facts constrain the delete order, and both were verified rather than
assumed:

- **`concepts` has no version column.** Concept identity is global across the
  library (SDD 4.2.2), so "delete this book's concepts" is not expressible — a
  concept may also be carried by another installed book.
- **`concept_mentions.concept_id` is `ON DELETE RESTRICT`.** A concept cannot be
  deleted while any mention still references it, so mentions must go first.

Therefore the uninstall step is:

1. delete mentions whose passage belongs to this version;
2. delete relation assertions scoped to this version, and their evidence spans;
3. delete relations left with no assertion;
4. delete concepts left with **no mentions at all** — orphan cleanup, now
   permitted because step 1 satisfied the `RESTRICT`;
5. apply the new overlay.

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

_None outstanding for the design itself._ Two prerequisites sit outside it and
block the one-click story:

- ~~How colleagues reach a signed-in state~~ — **resolved** (§3.1): the WebView
  session persists across restarts, so the token is already present.
- **`migrateDataIfNeeded` wipes the data directory** on a future
  `requiredDataVersion` bump, destroying installed books (§3.2, item 1).
