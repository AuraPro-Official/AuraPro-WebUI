# Dependency Security Review

Reviewed 2026-10-24. This is not an all-clear statement. No application version
was changed. npm locks and uv.lock were regenerated; existing platform wheel
exceptions are deliberate, not suppressed audit results.

Final scans: desktop npm audit has 0 entries; WebUI has 7 low entries described
below. OSV queried 390 normalized package/version pairs from uv.lock and both
requirements files: 6 matching pairs, with 10 GHSA and 11 PYSEC identifiers
(including aliases, not 21 independent issues). The legacy desktop torch
versions discussed below are additional runtime exceptions outside uv.lock.

## Fixed Versions

- Desktop Electron 43.8.3 covers the current certificate advisory
  [GHSA-jr5w-w6x2-84p9](https://github.com/electron/electron/security/advisories/GHSA-jr5w-w6x2-84p9)
  (fixed on the 43.x branch from 43.8.1), as well as the older contextBridge
  advisory. The [42.11.18 release notes](https://github.com/electron/electron/releases/tag/v42.11.18)
  explicitly mark 42.x end-of-support, so the supported 43.x line is selected.
  Electron 44 is intentionally excluded because it
  [removes macOS 12 support](https://www.electronjs.org/docs/latest/breaking-changes#removed-macos-12-support).
- WebUI ProseMirror View 1.42.8 exceeds the 1.42.3 fix for
  [GHSA-c8x8-7fp4-3x9w](https://github.com/advisories/GHSA-c8x8-7fp4-3x9w).
  Tiptap packages are aligned at 3.32.0. WebUI xterm dependencies were removed;
  desktop xterm remains necessary for generic logs.
- pypdf is pinned to 6.21.2 in pyproject.toml and backend/requirements.txt.
- Other npm matches were refreshed within compatible majors. Python direct
  pins and transitive constraints were updated together. filelock 3.20.3 is
  intentionally selected: OSV reports no match for it, while unstructured
  requires filelock <4 and the later vulnerable implementations need 4.2.1.
- Unused python-jose was removed from requirements-min.txt; backend Python
  sources use PyJWT and contain no jose imports.

## SvelteKit: Seven Low npm Entries, Two Underlying Advisories

Entries are @sveltejs/kit, @sveltejs/adapter-auto, @sveltejs/adapter-node,
@sveltejs/adapter-static, bits-ui, runed, and svelte-toolbelt. Only Kit has
direct advisory entries; the other six inherit its dependency finding.

- [GHSA-pfhg-q3qv-9q6r](https://github.com/advisories/GHSA-pfhg-q3qv-9q6r)
  requires enhanced form actions accepting paths beginning with `//`.
  Current src has no `$app/forms` import or `use:enhance`, and no server
  action route modules. Not applicable to the current application.
- [GHSA-27mw-82g8-phjj](https://github.com/advisories/GHSA-27mw-82g8-phjj)
  requires prerendering with attacker-controlled cache-control passed through
  setHeaders. Current src contains no setHeaders or cache-control forwarding;
  src/routes/+layout.js:10 disables SSR and svelte.config.js:15 selects the
  static SPA adapter with index.html fallback. No active prerender export was
  found. Not applicable to the current application. The only private-env
  match in src/lib/constants.ts:117 is a comment, not an import.

Kit 2.70.3 remains audited as affected. Fixing the dependency version requires
Kit 3 plus major adapter/Vite/TypeScript/plugin changes. Reassess these
exceptions whenever enhanced forms or prerendered header handling is added.

## Python Residuals and Platform Boundaries

- Intel Mac cryptography 48.0.1 retains the last universal2 wheels; 49/50
  publish no Intel Mac wheels. Other platforms use 50.0.2.
  [GHSA-jwv3-5hgf-82ww](https://github.com/advisories/GHSA-jwv3-5hgf-82ww)
  and [GHSA-m2h6-j472-rp4c](https://github.com/advisories/GHSA-m2h6-j472-rp4c)
  require the x509 certificate verifier; no direct PolicyBuilder,
  x509.verification, or ServerVerifier calls were found. Certificate generation
  in backend/open*webui/cli.py:90 is not certificate path verification.
  [GHSA-g6cj-pr64-35w5](https://github.com/advisories/GHSA-g6cj-pr64-35w5)
  needs PKCS#7 EnvelopedData decryption exposed as an adaptive oracle; no direct
  pkcs7_decrypt*\* calls were found. These are retained package matches, not
  proven reachable vulnerabilities; transitive callers were not exhaustively
  audited. Prefer patched-platform deployments for sensitive workloads.
- Linux ARM64 retains pyarrow 20.0.0 for its manylinux2014 wheel.
  [GHSA-rgxp-2hwp-jwgg](https://github.com/advisories/GHSA-rgxp-2hwp-jwgg)
  explicitly excludes Python bindings: the affected C++ IPC prebuffer API is
  not exposed there. The retained Arrow pin also holds datasets at 4.0.0 and
  fsspec at 2025.3.0. Their matches remain: dataset metadata path traversal
  [GHSA-379c-qx7v-6h59](https://github.com/advisories/GHSA-379c-qx7v-6h59)
  and untrusted ReferenceFileSystem templates
  [GHSA-27vj-qcqg-25rc](https://github.com/advisories/GHSA-27vj-qcqg-25rc).
  Do not load untrusted folder-builder metadata or reference JSON on this
  compatibility branch. Other platforms use Arrow 23.0.1 and datasets >=5.0.1.
- ChromaDB 1.5.9 has no patched published release for GHSA-2wm9-hf6c-p5cr,
  GHSA-36p7-vc44-83pf, GHSA-f4j7-r4q5-qw2c, or GHSA-xph7-9rjv-w5fr.
  These concern exposed Chroma HTTP APIs/tenant authorization/model loading.
  The default backend uses PersistentClient (retrieval/vector/dbs/chroma.py:51);
  CHROMA_HTTP_HOST enables a separately managed HTTP server instead (:40).
  Do not expose an affected Chroma HTTP server to untrusted clients.
- Optional mariadb 1.1.14 matches PYSEC-2026-217. The advisory concerns Connector/C
  mysql_real_escape_string with unvalidated input, text protocol, and big5;
  its fixed C-library versions cannot be equated with Python connector versions.
  A fixed Python package version is not specified. Check the linked native
  library before enabling this optional backend; no exploitable app path was
  established by the dependency audit.
- Desktop-managed legacy torch remains 2.2.2 on Intel Mac and 2.8.0 on Apple
  Silicon before macOS 14. These remain affected versions, including
  GHSA-53q9-r3pm-6pq6 on 2.2.2 (untrusted weights_only torch.load), and
  GHSA-qfhq-4f3w-5fph, GHSA-rrmf-rvhw-rf47, GHSA-vgrw-7cvw-pwgx.
  Do not treat model files as safe merely because weights_only is requested.
  The shared torch security floor excludes all Darwin because PEP 508 platform
  markers do not reliably express the OS wheel deployment minimum. The desktop
  installer chooses 2.13.0 on macOS 14+ Apple Silicon; standalone Mac installs
  must select a compatible wheel. The uv lock itself selects patched 2.13.0.

## Torch Wheels and Verification Limits

Official indexes were inspected for both cp311 and cp312. Patched 2.13.0 CPU
wheels exist for Windows x64/ARM64, Linux x64/ARM64, and macOS 14+ ARM64, not
Intel Mac. Desktop Windows ARM64 intentionally uses x64 Python emulation.
CUDA cu126/cu130 wheels exist for Windows/Linux x64; there are no 2.13 cu128
wheels. The installer uses cu126 for CUDA 12 selections, cu130 for CUDA 13,
CPU otherwise, and does not downgrade newer matching-flavor installations.
CUDA drivers and real GPU execution were not tested. Upgrades may download
large wheels. No user Python environment was mutated during this review.

Desktop node tests, torch installer fixtures, manifest synchronization tests,
and typechecks passed. Production builds, a clipboard-attribute browser regression (invalid
attributes rejected; valid text preserved), and an actual Electron 43.8.3
isolated-renderer/node-pty smoke test passed on Windows.
This is not validation of all operating systems or full ML model loading.
The coordinating reviewer reported 91 frontend tests passing and the backend
module-isolated suite completing 171 cases: 160 passes, 11 skips, no errors.

Untracked pnpm lock/workspace files predated this work and were preserved;
package-lock.json and uv.lock are the regenerated dependency authorities here.
