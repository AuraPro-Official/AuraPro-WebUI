#!/usr/bin/env bash
#
# Provision a durable, minimal Python environment for the EPUB test suite.
#
# The EPUB tests need an interpreter that satisfies BOTH of these, which is
# surprisingly rare on developer machines:
#
#   1. `import xml.parsers.expat` works.  Homebrew's CPython builds on macOS are
#      frequently broken here (libexpat.1.dylib symbol mismatch against the
#      system copy), and the EPUB parser is built on expat.
#   2. `sqlite3.Connection.enable_load_extension` exists.  pyenv-built CPython
#      omits it unless configured with --enable-loadable-sqlite-extensions, and
#      the sqlite-vec retrieval backend loads a SQLite extension at runtime.
#
# The python-build-standalone CPython 3.12 distribution satisfies both, and is
# also exactly what AuraPro Desktop ships to end users (see
# AuraPro-Desktop/src/main/utils/index.ts :: generateDownloadUrl), so testing on
# it tests what users actually run.
#
# Usage:
#   ./scripts/epub_test_env.sh
#
# Environment:
#   AURAPRO_EPUB_TEST_PYTHON   Use this interpreter instead of probing/downloading.
#   AURAPRO_EPUB_TEST_VENV     Where to build the venv.
#                              Default: $HOME/.cache/aurapro/epub-test-venv
#
# Re-running is cheap: an already-valid venv is re-validated, not rebuilt.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

CACHE_ROOT="${HOME}/.cache/aurapro"
VENV_DIR="${AURAPRO_EPUB_TEST_VENV:-${CACHE_ROOT}/epub-test-venv}"
STANDALONE_ROOT="${CACHE_ROOT}/python-standalone"

# Must match AuraPro-Desktop/src/main/utils/index.ts :: generateDownloadUrl so
# the test environment matches the shipped runtime.
PBS_RELEASE_DATE="20260310"
PBS_PYTHON_VERSION="3.12.13"
PBS_BASE_URL="https://github.com/astral-sh/python-build-standalone/releases/download"

die() {
	echo "ERROR: $*" >&2
	exit 1
}

log() {
	echo "[epub-test-env] $*"
}

# ── pinned versions ───────────────────────────────────────────────────────────
# Read pins straight out of pyproject.toml so this script cannot drift from the
# production dependency set.
#
# read_pin <distribution> <fallback-version>
read_pin() {
	local dist="$1" fallback="$2"
	local pyproject="${REPO_ROOT}/pyproject.toml"
	local pin=""
	if [ -f "${pyproject}" ]; then
		pin="$(sed -n "s/^[[:space:]]*\"\\(${dist}==[^\"]*\\)\".*/\\1/p" "${pyproject}" | head -n 1)"
	fi
	if [ -z "${pin}" ]; then
		pin="${dist}==${fallback}"
		echo "[epub-test-env] WARNING: could not read the ${dist} pin from ${pyproject}; falling back to ${pin}" >&2
	fi
	printf '%s' "${pin}"
}

# Fallbacks mirror the pyproject.toml pins as of this writing.
SQLITE_VEC_PIN="$(read_pin sqlite-vec 0.1.9)"
MULTIPART_PIN="$(read_pin python-multipart 0.0.32)"
JIEBA_PIN="$(read_pin jieba 0.42.1)"
# ruff is declared as a range (`ruff>=0.15.5`) in [dependency-groups] dev rather
# than an `==` pin, so read_pin cannot find it; mirror the declared floor.
RUFF_PIN="ruff>=0.15.5"

# Minimal set only.  Deliberately NOT `pip install -e .` -- that drags in torch
# and several hundred packages that no EPUB test touches.
#   typer, uvicorn   -> backend/open_webui/__init__.py imports them at package
#                       import time, so any test importing open_webui.* needs them
#   fastapi, httpx   -> fastapi.testclient.TestClient in test_epub_api
#   python-multipart -> backend/open_webui/routers/epub.py declares UploadFile
#                       form params; FastAPI raises at import time without it
#   sqlite-vec       -> the loadable SQLite extension under test
#   jieba            -> Tier-1 matching requires query word boundaries; without
#                       it the search tests would only ever exercise the
#                       degraded fallback and the real rule would go untested
#   ruff             -> the F821 guard below; names used only in `except`
#                       clauses are invisible to a passing test suite
REQUIREMENTS=(typer uvicorn fastapi httpx "${MULTIPART_PIN}" "${SQLITE_VEC_PIN}" "${JIEBA_PIN}" "${RUFF_PIN}")

# Pyflakes-only, and deliberately so. The repo-wide ruff style rules (single
# quotes, line length) now DO apply to the EPUB modules and are enforced on
# their own by `ruff format --check` in CI, so there is nothing left for this
# invocation to add there. Narrowing it to F keeps the finding that matters --
# a name reachable only from an `except` clause and therefore invisible to a
# passing test suite -- from being buried in style output.
RUFF_PATHS=(
	backend/open_webui/retrieval/epub
	backend/open_webui/retrieval/parsers/epub
	backend/open_webui/services/epub_concept.py
	backend/open_webui/services/epub_runtime.py
	backend/open_webui/routers/epub.py
)

# ── interpreter validation ────────────────────────────────────────────────────
VALIDATION_SNIPPET='
import sys, sqlite3
problems = []
if sys.version_info[:2] not in ((3, 11), (3, 12)):
    problems.append(
        "python %d.%d is outside pyproject requires-python \">= 3.11, < 3.13.0a1\""
        % sys.version_info[:2]
    )
try:
    import xml.parsers.expat  # noqa: F401
except Exception as exc:  # pragma: no cover - diagnostic path
    problems.append("import xml.parsers.expat failed: %s" % (exc,))
if not hasattr(sqlite3.Connection, "enable_load_extension"):
    problems.append(
        "sqlite3.Connection.enable_load_extension is missing "
        "(CPython built without --enable-loadable-sqlite-extensions)"
    )
if problems:
    for p in problems:
        sys.stderr.write("  - %s\n" % p)
    raise SystemExit(1)
sys.stderr.write("")
'

# validate_python <interpreter> [quiet]
# Returns 0 if the interpreter satisfies every hard requirement.
validate_python() {
	local py="$1"
	local quiet="${2:-}"
	[ -x "${py}" ] || return 1
	if [ -n "${quiet}" ]; then
		"${py}" -c "${VALIDATION_SNIPPET}" >/dev/null 2>&1
	else
		"${py}" -c "${VALIDATION_SNIPPET}"
	fi
}

# ── python-build-standalone download ──────────────────────────────────────────
pbs_asset_name() {
	local uname_s uname_m arch_string platform_string
	uname_s="$(uname -s)"
	uname_m="$(uname -m)"

	case "${uname_s}" in
	Darwin) platform_string="apple-darwin" ;;
	Linux) platform_string="unknown-linux-gnu" ;;
	*) die "unsupported host OS '${uname_s}'. Set AURAPRO_EPUB_TEST_PYTHON to a CPython 3.12 that has working xml.parsers.expat and sqlite3 extension loading." ;;
	esac

	case "${uname_m}" in
	arm64 | aarch64) arch_string="aarch64" ;;
	x86_64 | amd64) arch_string="x86_64" ;;
	*) die "unsupported host architecture '${uname_m}'. Set AURAPRO_EPUB_TEST_PYTHON to a suitable CPython 3.12." ;;
	esac

	if [ "${platform_string}" = "unknown-linux-gnu" ] && [ "${arch_string}" != "x86_64" ] && [ "${arch_string}" != "aarch64" ]; then
		die "unsupported linux architecture '${uname_m}'."
	fi

	printf 'cpython-%s+%s-%s-%s-install_only.tar.gz' \
		"${PBS_PYTHON_VERSION}" "${PBS_RELEASE_DATE}" "${arch_string}" "${platform_string}"
}

download_standalone_python() {
	local asset url dest_dir tarball python_bin
	asset="$(pbs_asset_name)"
	url="${PBS_BASE_URL}/${PBS_RELEASE_DATE}/${asset}"
	dest_dir="${STANDALONE_ROOT}/${asset%.tar.gz}"
	python_bin="${dest_dir}/python/bin/python3"

	if [ -x "${python_bin}" ]; then
		printf '%s' "${python_bin}"
		return 0
	fi

	log "downloading python-build-standalone CPython ${PBS_PYTHON_VERSION} (${asset})" >&2
	mkdir -p "${dest_dir}"
	tarball="${STANDALONE_ROOT}/${asset}"

	if command -v curl >/dev/null 2>&1; then
		curl --fail --location --progress-bar --output "${tarball}.part" "${url}" >&2 ||
			die "download failed: ${url}"
	elif command -v wget >/dev/null 2>&1; then
		wget --output-document "${tarball}.part" "${url}" >&2 ||
			die "download failed: ${url}"
	else
		die "neither curl nor wget is available; cannot download ${url}"
	fi
	mv "${tarball}.part" "${tarball}"

	log "extracting into ${dest_dir}" >&2
	tar -xzf "${tarball}" -C "${dest_dir}" >&2 || die "failed to extract ${tarball}"
	rm -f "${tarball}"

	[ -x "${python_bin}" ] || die "expected interpreter not found after extraction: ${python_bin}"
	printf '%s' "${python_bin}"
}

# ── interpreter selection ─────────────────────────────────────────────────────
select_python() {
	local candidate

	if [ -n "${AURAPRO_EPUB_TEST_PYTHON:-}" ]; then
		log "using AURAPRO_EPUB_TEST_PYTHON=${AURAPRO_EPUB_TEST_PYTHON}" >&2
		if ! validate_python "${AURAPRO_EPUB_TEST_PYTHON}"; then
			die "AURAPRO_EPUB_TEST_PYTHON=${AURAPRO_EPUB_TEST_PYTHON} is not usable (see the problems above). Unset it to let this script provision a python-build-standalone CPython ${PBS_PYTHON_VERSION} instead."
		fi
		printf '%s' "${AURAPRO_EPUB_TEST_PYTHON}"
		return 0
	fi

	# Known-good locations, best first.  Homebrew and pyenv interpreters are
	# intentionally absent: on this project's reference machine both fail one of
	# the two hard checks.  They are still probed via PATH below, and validated
	# like everything else, so a good one is used if present.
	local candidates=(
		"${STANDALONE_ROOT}"/cpython-*-install_only/python/bin/python3
		"/private/tmp/aurapro-desktop-e2e/python/bin/python3.12"
		"${HOME}/.aurapro/python/bin/python3"
		"${HOME}/Library/Application Support/AuraPro/python/bin/python3"
		"python3.12"
		"python3.11"
		"python3"
	)

	for candidate in "${candidates[@]}"; do
		# Resolve bare names through PATH; skip unexpanded globs.
		case "${candidate}" in
		*/*)
			[ -x "${candidate}" ] || continue
			;;
		*)
			candidate="$(command -v "${candidate}" 2>/dev/null || true)"
			[ -n "${candidate}" ] || continue
			;;
		esac
		if validate_python "${candidate}" quiet; then
			log "found a usable interpreter: ${candidate}" >&2
			printf '%s' "${candidate}"
			return 0
		fi
		log "rejected ${candidate} (fails expat and/or sqlite3 extension-loading check)" >&2
	done

	log "no usable interpreter on this machine; provisioning one" >&2
	local downloaded
	downloaded="$(download_standalone_python)"
	if ! validate_python "${downloaded}"; then
		die "the downloaded interpreter ${downloaded} still fails validation (see above). This should not happen -- please report it."
	fi
	printf '%s' "${downloaded}"
}

# ── venv provisioning ─────────────────────────────────────────────────────────
VENV_PYTHON="${VENV_DIR}/bin/python"

venv_is_complete() {
	[ -x "${VENV_PYTHON}" ] || return 1
	[ -x "${VENV_DIR}/bin/ruff" ] || return 1
	validate_python "${VENV_PYTHON}" quiet || return 1
	AURAPRO_REQUIRED_PINS="${SQLITE_VEC_PIN} ${MULTIPART_PIN} ${JIEBA_PIN}" \
		"${VENV_PYTHON}" - <<'PY' >/dev/null 2>&1 || return 1
import os
from importlib import metadata

for module in ("typer", "uvicorn", "fastapi", "httpx", "multipart", "sqlite_vec", "jieba"):
    __import__(module)

for requirement in os.environ["AURAPRO_REQUIRED_PINS"].split():
    dist, _, version = requirement.partition("==")
    if version and metadata.version(dist) != version:
        raise SystemExit(1)
PY
	return 0
}

main() {
	mkdir -p "${CACHE_ROOT}" "${STANDALONE_ROOT}"

	log "repo root:  ${REPO_ROOT}"
	log "venv:       ${VENV_DIR}"
	log "sqlite-vec: ${SQLITE_VEC_PIN} (pinned in pyproject.toml)"

	if venv_is_complete; then
		log "existing venv is valid and complete; nothing to do"
	else
		local base_python
		base_python="$(select_python)"
		log "base interpreter: ${base_python} ($("${base_python}" --version 2>&1))"

		# A venv keeps only a symlink to its base interpreter's stdlib, so if the
		# base is deleted (e.g. a /private/tmp interpreter after a reboot) the
		# venv dies with it. Always start clean rather than trying to patch one up.
		if [ -d "${VENV_DIR}" ]; then
			log "existing venv is stale or incomplete; recreating ${VENV_DIR}"
			rm -rf "${VENV_DIR}"
		fi

		log "creating venv"
		mkdir -p "$(dirname -- "${VENV_DIR}")"
		"${base_python}" -m venv "${VENV_DIR}" || die "failed to create the venv at ${VENV_DIR}"

		[ -x "${VENV_PYTHON}" ] || die "venv python not found at ${VENV_PYTHON}"

		log "upgrading pip"
		"${VENV_PYTHON}" -m pip install --quiet --upgrade pip || die "failed to upgrade pip"

		log "installing: ${REQUIREMENTS[*]}"
		"${VENV_PYTHON}" -m pip install --quiet "${REQUIREMENTS[@]}" ||
			die "failed to install the test requirements"

		venv_is_complete || die "the venv at ${VENV_DIR} is still incomplete after installation"
		log "venv ready"
	fi

	# Re-validate every run, even on the fast path.
	log "validating ${VENV_PYTHON}"
	validate_python "${VENV_PYTHON}" ||
		die "the venv interpreter ${VENV_PYTHON} fails validation. Delete ${VENV_DIR} and re-run this script."
	log "validation ok: xml.parsers.expat imports, sqlite3 extension loading available"

	echo
	echo "EPUB test environment ready."
	echo
	echo "Run the suite from the repo root:"
	echo
	echo "  cd ${REPO_ROOT} && \"${VENV_PYTHON}\" -m unittest discover -s test -p 'test_epub_*.py'"
	echo
	echo "Guard the error paths (must report \"All checks passed!\"):"
	echo
	echo "  cd ${REPO_ROOT} && \"${VENV_DIR}/bin/ruff\" check --select F ${RUFF_PATHS[*]}"
	echo
}

main "$@"
