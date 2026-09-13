"""Which files a Hugging Face model download is allowed to skip.

`snapshot_download` fetches an entire repository unless it is told otherwise,
and the retrieval repositories carry the same weights several times over: once
per serving framework (ONNX, OpenVINO, TensorFlow, Flax, Rust) and once more
for the legacy pickle build of the torch copy.  Only one of them is ever
loaded.  `BAAI/bge-reranker-base` is the extreme case at 3.1 GB on disk, of
which three ~1.03 GiB files are the same 278M parameters.

Two rules, with different prerequisites:

* framework-alien formats can be skipped blind -- neither sentence-transformers
  nor the transformers `CrossEncoder` loads them under any backend we expose;
* the pickle build can only be skipped once the repository is known to ship a
  safetensors twin, because plenty of repositories ship `pytorch_model.bin`
  and nothing else.  Preferring safetensors also avoids unpickling.

The decision is a pure function over a file listing so it can be unit tested
without network access, `huggingface_hub` or torch.
"""

from __future__ import annotations

import fnmatch
import logging
import posixpath
from collections.abc import Iterable, Sequence

log = logging.getLogger(__name__)

TORCH_BACKEND = 'torch'
ONNX_BACKEND = 'onnx'
OPENVINO_BACKEND = 'openvino'

# Serving formats nothing in this application can load, whatever the configured
# sentence-transformers backend.  Skipping these never costs a usable weight.
ALIEN_FRAMEWORK_PATTERNS: tuple[str, ...] = (
    # TensorFlow / Keras
    '*.h5',
    '*.pb',
    'saved_model/*',
    'tf_model*',
    # Flax / JAX
    '*.msgpack',
    'flax_model*',
    # rust-bert / candle
    '*.ot',
    'rust_model*',
    # TensorFlow Lite
    '*.tflite',
    # Core ML
    '*.mlmodel',
    'coreml/*',
)

# Skipped unless the matching backend is configured.  `onnx/*` rather than
# `onnx/*.onnx` on purpose: those directories also hold a duplicate tokenizer
# and extension-less graph data.
ONNX_PATTERNS: tuple[str, ...] = ('*.onnx', '*.onnx_data', 'onnx/*')
OPENVINO_PATTERNS: tuple[str, ...] = ('openvino/*', 'openvino_model*')

# The pickle build of the torch weights.  Only ever applied blind on a
# `local_files_only` call, where patterns cannot remove anything -- see
# `select_ignore_patterns`.
PICKLE_WEIGHT_PATTERNS: tuple[str, ...] = (
    '*pytorch_model*.bin',
    '*pytorch_model.bin.index.json',
    'model.bin',
    'model-*-of-*.bin',
    'model.bin.index.json',
)

_SAFETENSORS_SUFFIX = '.safetensors'
_PICKLE_WEIGHT_STEMS = frozenset({'model', 'pytorch_model'})
_PICKLE_WEIGHT_INDEXES = frozenset({'model.bin.index.json', 'pytorch_model.bin.index.json'})
_FNMATCH_METACHARACTERS = frozenset('*?[]')


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    """True when `path` matches one of the fnmatch `patterns`.

    `huggingface_hub.utils.filter_repo_objects` matches with
    `fnmatch.fnmatchcase`, where `*` crosses `/` and the comparison is
    case-sensitive on every platform.  Using the same call keeps a local filter
    from ever disagreeing with the one inside `snapshot_download`.
    """
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _normalise_backends(backends: Iterable[str]) -> set[str]:
    selected = {backend.strip().lower() for backend in backends if backend and backend.strip()}
    return selected or {TORCH_BACKEND}


def _framework_patterns(backends: set[str]) -> list[str]:
    patterns = list(ALIEN_FRAMEWORK_PATTERNS)
    if ONNX_BACKEND not in backends:
        patterns.extend(ONNX_PATTERNS)
    if OPENVINO_BACKEND not in backends:
        patterns.extend(OPENVINO_PATTERNS)
    return patterns


def _is_safetensors_weight(name: str) -> bool:
    return name.endswith(_SAFETENSORS_SUFFIX)


def _is_pickle_weight(name: str) -> bool:
    """Torch's pickle weight spellings, sharded ones included.

    Deliberately narrower than `*.bin`: `openvino_model.bin` and quantised
    variants are weights for other runtimes, not duplicates of this one.
    """
    if not name.endswith('.bin'):
        return False
    stem = name[: -len('.bin')]
    return stem in _PICKLE_WEIGHT_STEMS or stem.startswith(('model-', 'pytorch_model-'))


def _redundant_pickle_weights(repo_files: Sequence[str]) -> list[str]:
    """Pickle weights that have a safetensors twin in the same directory.

    sentence-transformers keeps module weights in subdirectories, and a
    subdirectory can ship only the pickle build while the repository root ships
    both -- so the comparison is per directory, not per repository.
    """
    safetensors_dirs = {
        posixpath.dirname(path) for path in repo_files if _is_safetensors_weight(posixpath.basename(path))
    }

    redundant = []
    for path in repo_files:
        if posixpath.dirname(path) not in safetensors_dirs:
            continue
        name = posixpath.basename(path)
        if not (_is_pickle_weight(name) or name in _PICKLE_WEIGHT_INDEXES):
            continue
        if _FNMATCH_METACHARACTERS & set(path):
            # The exact path doubles as its own fnmatch pattern, so a
            # metacharacter in the name would make it mean something else.
            continue
        redundant.append(path)
    return redundant


def _has_loadable_weights(paths: Iterable[str], backends: set[str]) -> bool:
    for path in paths:
        name = posixpath.basename(path)
        if _is_safetensors_weight(name) or _is_pickle_weight(name):
            return True
        if ONNX_BACKEND in backends and name.endswith('.onnx'):
            return True
    return False


def select_ignore_patterns(
    repo_files: Sequence[str] | None = None,
    *,
    backends: Iterable[str] = (TORCH_BACKEND,),
    local_files_only: bool = False,
) -> list[str]:
    """fnmatch patterns `snapshot_download` should skip for a model repository.

    `repo_files` is the repository's full file listing, or None when it is not
    available -- offline mode, or a listing call that failed.  Without it the
    framework-alien globs still apply, but the safetensors preference cannot:
    a blind pickle skip would leave a `pytorch_model.bin`-only repository with
    no weights at all.

    `local_files_only` marks a call that downloads nothing.  There the patterns
    cannot fetch *or* delete a file; they only narrow the completeness check
    `snapshot_download` runs over the cached snapshot.  So the pickle globs are
    applied blind there -- and have to be, or a cache populated online with the
    safetensors preference would be reported incomplete on the next offline
    start.
    """
    selected_backends = _normalise_backends(backends)
    patterns = _framework_patterns(selected_backends)

    if repo_files is None:
        if local_files_only:
            patterns.extend(PICKLE_WEIGHT_PATTERNS)
        return patterns

    known_files = [path for path in repo_files if isinstance(path, str) and path]
    surviving = [path for path in known_files if not matches_any(path, patterns)]
    patterns.extend(_redundant_pickle_weights(surviving))

    if not _has_loadable_weights((path for path in known_files if not matches_any(path, patterns)), selected_backends):
        # An unfamiliar layout: filtering left nothing this application could
        # load.  Fetch the whole repository rather than guess.
        log.info('No recognised weight file would survive filtering; downloading the full repository')
        return []

    return patterns


def resolve_repo_files(
    model: str,
    *,
    revision: str | None = None,
    local_files_only: bool = False,
) -> list[str] | None:
    """The repository's file listing, or None when it cannot be determined.

    Never touches the network when `local_files_only` is set.  OFFLINE_MODE
    forces that flag (and `HF_HUB_OFFLINE=1`), and an offline start must not
    turn into a network call or an exception, so it falls back to the patterns
    that need no listing.
    """
    if local_files_only:
        return None

    try:
        from huggingface_hub import HfApi

        return list(HfApi().list_repo_files(repo_id=model, revision=revision))
    except Exception as e:
        log.warning(
            'Could not list files for Hugging Face model %s (%s); every weight format will be fetched',
            model,
            type(e).__name__,
        )
        return None
