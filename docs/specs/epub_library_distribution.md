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

| #   | Decision                                                                                       | Rationale                                                                                                                                                                                                                                                                                                              |
| --- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D-1 | The server hosts **both the EPUB and the overlay**                                             | Best experience; also makes `epub_sha256` match by construction, so the overlay can never fail to attach because a colleague's copy differs by a byte. The copyright exposure of redistributing the work was raised and accepted by the owner; mitigate with password-protected access limited to internal colleagues. |
| D-2 | **Each colleague installs their own Desktop** (no shared server)                               | Removes shared-library isolation entirely. One store per machine, and the local user is that machine's administrator.                                                                                                                                                                                                  |
| D-3 | **Manual publish, direct-link fetch, password-protected**                                      | Operator uploads EPUB + overlay by hand. The client checks for updates and downloads over a direct link behind a password. Reuse the Desktop official-glossary mechanism rather than inventing one.                                                                                                                    |
| D-4 | **Models download on first launch**, not bundled                                               | Keeps the installer small. Paired with progressive enhancement (§5) so first use is not a 1.3 GB wait.                                                                                                                                                                                                                 |
| D-5 | **Published books use mirror semantics**; locally built graphs keep today's additive semantics | See §4.                                                                                                                                                                                                                                                                                                                |

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

**Permission note:** `POST /admin/import` and `POST /admin/overlays` are
admin-gated today. Under D-2 the local user is that machine's administrator, so
these are expected to be reusable as-is — the change is a new **UI entry point**
("Library"), not a new permission boundary. _Confirm the Desktop WebUI auth
model before relying on this._

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

**Assumption to state explicitly, and to enforce:** a book installed from the
library is not also locally curated. If a reader ever does curate one, mirror
semantics would discard that work. Either forbid curation on published assets in
the UI, or detect local `ADMIN` rows and refuse the replace with a clear message.
Silently discarding a colleague's work is the one outcome this design must not
allow.

**Open sub-question:** whether replace is implemented as _uninstall + reinstall_
(simplest; delete all rows for that version, re-run apply) or as a computed diff.
Uninstall+reinstall is recommended first — it has one code path and cannot drift
from a fresh install, which is the failure mode a diff implementation invites.

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

| Persona                                  | Actually needs                                                                                     |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------- |
| **Publisher** (one person, occasionally) | Batch API key, endpoint, completion window, prompt profile, admin review UI, merge/split decisions |
| **Reader** (every colleague, daily)      | Nothing                                                                                            |

So the fix is not "reduce 15 settings to 5" — it is that **the reader path should
have zero settings**, and the publisher's settings should not be visible to
readers at all. Concretely: DB path defaults under `DATA_DIR`; device is already
automatic (PR #30); models are Desktop-managed; Tier-2 is optional and enabled by
presence; Batch and admin surfaces are hidden. The library URL is baked into the
build; the password is entered once.

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

- Desktop WebUI auth model — is the local user an administrator? (§3)
- Replace via uninstall+reinstall, or computed diff? (§4.4)
- What happens if a reader curates a published book? Forbid, or detect and refuse? (§4.4)
- Password distribution to colleagues — baked per-build, or entered once and stored?
- Does the catalog need to express "this overlay supersedes versions < N", or is a
  single monotonic `overlay_version` enough?
