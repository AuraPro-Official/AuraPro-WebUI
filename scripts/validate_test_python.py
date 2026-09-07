#!/usr/bin/env python3
"""Check that the running interpreter can host the EPUB test suite.

Two requirements are easy to miss and produce confusing failures much later:

  1. ``import xml.parsers.expat`` must work.  Homebrew's CPython builds on macOS
     are frequently broken here (libexpat.1.dylib symbol mismatch against the
     system copy), and the EPUB parser is built on expat.
  2. ``sqlite3.Connection.enable_load_extension`` must exist.  pyenv-built
     CPython omits it unless configured with
     ``--enable-loadable-sqlite-extensions``, and the sqlite-vec retrieval
     backend loads a SQLite extension at runtime.

Run it with the interpreter under test:

    some/python scripts/validate_test_python.py

Exit status 0 means the interpreter is usable.  Anything else means it is not,
and the reasons are written to stderr.

Nothing is ever written to stdout: scripts/epub_test_env.sh calls this from
inside a command substitution, so a stray stdout line would be captured as part
of an interpreter path.  Both this script and that one are used by
.github/workflows/backend.yaml, so CI validates exactly what developers do.
"""

from __future__ import annotations

import sqlite3
import sys

# Keep in sync with `requires-python` in pyproject.toml.
SUPPORTED_VERSIONS = ((3, 11), (3, 12))


def problems() -> list[str]:
    """Return one human-readable string per unmet requirement."""
    found: list[str] = []

    if sys.version_info[:2] not in SUPPORTED_VERSIONS:
        major, minor = sys.version_info[:2]
        found.append(f'python {major}.{minor} is outside pyproject requires-python ">= 3.11, < 3.13.0a1"')

    try:
        import xml.parsers.expat  # noqa: F401
    except Exception as exc:  # pragma: no cover - diagnostic path
        found.append(f'import xml.parsers.expat failed: {exc}')

    if not hasattr(sqlite3.Connection, 'enable_load_extension'):
        found.append(
            'sqlite3.Connection.enable_load_extension is missing '
            '(CPython built without --enable-loadable-sqlite-extensions)'
        )

    return found


def main() -> int:
    found = problems()
    if found:
        sys.stderr.write(f'interpreter unusable: {sys.executable}\n')
        for problem in found:
            sys.stderr.write(f'  - {problem}\n')
        return 1

    sys.stderr.write(
        f'interpreter ok: {sys.executable} ({sys.version.split()[0]}) -- '
        f'xml.parsers.expat imports, sqlite3 extension loading available\n'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
