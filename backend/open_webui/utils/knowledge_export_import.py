import asyncio
import io
import json
import logging
import os
import stat
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Any, BinaryIO

import numpy as np

from open_webui.models.knowledge import Knowledges
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], Awaitable[None]] | None

_MIB = 1024 * 1024
_MAX_ARCHIVE_BYTES = int(os.getenv('KNOWLEDGE_IMPORT_MAX_ARCHIVE_MB', '512')) * _MIB
_MAX_UNCOMPRESSED_BYTES = int(os.getenv('KNOWLEDGE_IMPORT_MAX_UNCOMPRESSED_MB', '1024')) * _MIB
_MAX_JSON_BYTES = int(os.getenv('KNOWLEDGE_IMPORT_MAX_JSON_MB', '256')) * _MIB
_MAX_ARCHIVE_ENTRIES = int(os.getenv('KNOWLEDGE_IMPORT_MAX_ENTRIES', '10000'))
_MAX_VECTOR_ITEMS = int(os.getenv('KNOWLEDGE_IMPORT_MAX_VECTOR_ITEMS', '2000000'))
_MAX_EMBEDDING_DIM = int(os.getenv('KNOWLEDGE_IMPORT_MAX_EMBEDDING_DIM', '65536'))
_MAX_COMPRESSION_RATIO = 200

_EXPORT_BATCH_SIZE = int(os.getenv('KNOWLEDGE_EXPORT_BATCH_SIZE', '3000'))

# --- Archive layout -------------------------------------------------------
#
# v2 (current):
#   knowledge.json          - knowledge base row, as before
#   vectors/manifest.json   - {"format": "knowledge-export-v2", "count": N,
#                              "dim": D, "dtype": "float32", "batch_size": B}
#   vectors/embeddings.npy  - float32 ndarray, shape (N, D), binary
#   vectors/records.jsonl   - one JSON object per line, in the SAME row
#                              order as embeddings.npy: {"id", "document",
#                              "metadata"}
#
# v1 (legacy, import-only):
#   knowledge.json
#   metadatas.json          - {"ids": [...], "documents": [...],
#                              "metadatas": [...], "embeddings": [...]}
#                              embeddings stored as JSON arrays of floats,
#                              which is why old exports were huge - kept
#                              purely so previously-exported archives can
#                              still be imported.
_MANIFEST_MEMBER_NAME = 'vectors/manifest.json'
_EMBEDDINGS_MEMBER_NAME = 'vectors/embeddings.npy'
_RECORDS_MEMBER_NAME = 'vectors/records.jsonl'
_LEGACY_VECTORS_MEMBER_NAME = 'metadatas.json'
_EXPORT_FORMAT = 'knowledge-export-v2'


class KnowledgeImportError(ValueError):
    pass


def get_knowledge_import_max_archive_bytes() -> int:
    return _MAX_ARCHIVE_BYTES


def is_bilingual_knowledge(knowledge: Any) -> bool:
    """Return True when the knowledge base is of bilingual type.

    Bilingual knowledge bases carry ``meta.knowledge_type == 'bilingual'``;
    everything else (general collections, missing/empty meta, or a missing
    knowledge_type key) is treated as non-bilingual.
    """
    meta = getattr(knowledge, 'meta', None) or {}
    return meta.get('knowledge_type') == 'bilingual'


def _validate_archive(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise KnowledgeImportError('The knowledge archive contains too many entries.')

    total_uncompressed = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise KnowledgeImportError('Encrypted knowledge archives are not supported.')

        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise KnowledgeImportError('Symbolic links are not allowed in knowledge archives.')

        total_uncompressed += info.file_size
        if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
            raise KnowledgeImportError('The expanded knowledge archive is too large.')

        if info.file_size > 10 * _MIB and info.file_size > max(info.compress_size, 1) * _MAX_COMPRESSION_RATIO:
            raise KnowledgeImportError('The knowledge archive has an unsafe compression ratio.')

        path = PurePosixPath(info.filename.replace('\\', '/'))
        if path.is_absolute() or '..' in path.parts:
            raise KnowledgeImportError('The knowledge archive contains an unsafe path.')


def _read_json_member(
    zf: zipfile.ZipFile,
    name: str,
    *,
    required: bool = False,
) -> Any:
    try:
        info = zf.getinfo(name)
    except KeyError:
        if required:
            raise KnowledgeImportError(f'The knowledge archive is missing {name}.')
        return None

    if info.file_size > _MAX_JSON_BYTES:
        raise KnowledgeImportError(f'{name} exceeds the import limit.')

    try:
        with zf.open(info) as source:
            return json.load(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KnowledgeImportError(f'{name} is not valid JSON.') from exc


# --------------------------------------------------------------------------
# Export (v2: npy embeddings + streamed jsonl records)
# --------------------------------------------------------------------------
async def create_knowledge_export_zip(
    knowledge_id: str,
    progress_callback: ProgressCallback = None,
) -> BinaryIO:
    if progress_callback:
        await progress_callback(5, '正在读取知识库信息...')

    knowledge = await Knowledges.get_knowledge_by_id(knowledge_id)
    if not knowledge:
        raise ValueError(f'Knowledge base {knowledge_id} not found')

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        if progress_callback:
            await progress_callback(10, '正在写入知识库信息...')
        zf.writestr('knowledge.json', json.dumps(knowledge.model_dump(mode='json'), indent=2))

        if progress_callback:
            await progress_callback(20, '正在读取向量数据...')

        # NOTE: this still fetches the whole collection in one call because
        # ASYNC_VECTOR_DB_CLIENT.get() doesn't expose pagination today. The
        # win here is purely in how we *serialize* what comes back (binary
        # embeddings + streamed jsonl instead of one indented JSON blob).
        result = await ASYNC_VECTOR_DB_CLIENT.get(collection_name=knowledge_id)

        # Some backends return get() results wrapped in an extra "batch/group"
        # layer (e.g. [[id1, id2, ...]] instead of the flat [id1, id2, ...]
        # we actually want). Flatten defensively here rather than depending
        # on the vector client's internal return shape.
        def _flatten(field: list, name: str) -> list:
            if not field:
                return []
            if isinstance(field[0], list):
                flat: list = []
                for group in field:
                    if not isinstance(group, list):
                        raise ValueError(
                            f'Inconsistent nested structure in {name} for collection '
                            f'{knowledge_id}: expected all groups to be lists.'
                        )
                    flat.extend(group)
                return flat
            return field

        ids = _flatten(result.ids or [], 'ids')
        documents = _flatten(result.documents or [], 'documents')
        metadatas = _flatten(result.metadatas or [], 'metadatas')
        embeddings = _flatten(result.embeddings or [], 'embeddings')

        count = len(ids)
        if count and len(embeddings) != count:
            raise ValueError(
                f'Vector store returned {len(embeddings)} embeddings for {count} records '
                f'in collection {knowledge_id} after flattening; data may be corrupted.'
            )

        dim = len(embeddings[0]) if count and embeddings[0] is not None else 0

        if progress_callback:
            await progress_callback(45, f'正在编码 {count} 条向量...')

        embeddings_array = np.asarray(embeddings, dtype=np.float32) if count else np.zeros((0, 0), dtype=np.float32)
        if embeddings_array.ndim != 2:
            raise ValueError(
                f'Fetched embeddings for collection {knowledge_id} do not form a 2D array '
                f'(got shape {embeddings_array.shape}); export aborted.'
            )

        manifest = {
            'format': _EXPORT_FORMAT,
            'count': count,
            'dim': dim,
            'dtype': 'float32',
            'batch_size': _EXPORT_BATCH_SIZE,
        }
        zf.writestr(_MANIFEST_MEMBER_NAME, json.dumps(manifest))

        if progress_callback:
            await progress_callback(55, '正在写入向量数据 (embeddings.npy)...')

        npy_buffer = io.BytesIO()
        np.save(npy_buffer, embeddings_array, allow_pickle=False)
        zf.writestr(_EMBEDDINGS_MEMBER_NAME, npy_buffer.getvalue())
        npy_buffer.close()

        if progress_callback:
            await progress_callback(65, '正在写入记录数据 (records.jsonl)...')

        # Stream the jsonl entry straight into the zip member instead of
        # building one giant string for hundreds of thousands of rows.
        with zf.open(_RECORDS_MEMBER_NAME, 'w') as records_stream:
            for start in range(0, count, _EXPORT_BATCH_SIZE):
                end = min(start + _EXPORT_BATCH_SIZE, count)
                for i in range(start, end):
                    line = json.dumps(
                        {
                            'id': ids[i],
                            'document': documents[i] if i < len(documents) else None,
                            'metadata': metadatas[i] if i < len(metadatas) else None,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                    records_stream.write(line.encode('utf-8'))
                    records_stream.write(b'\n')

                if progress_callback and count:
                    percent = 65 + int(25 * end / count)
                    await progress_callback(percent, f'已写入 {end}/{count} 条记录...')

    zip_buffer.seek(0)
    if progress_callback:
        await progress_callback(100, '导出完成')
    return zip_buffer


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


def _validate_legacy_vectors(vectors_data: Any) -> tuple[list, list, list, list] | None:
    # The legacy exporter (create_knowledge_export_zip, v1) wrote FLAT lists
    # straight from the vector client's get() result: vectors_data['ids'][i]
    # / ['documents'][i] / ['metadatas'][i] / ['embeddings'][i] all describe
    # the same single row i. An earlier version of this validator assumed a
    # nested list-of-groups shape that the v1 writer never actually
    # produced, which meant real legacy archives could never pass
    # validation. This validates the shape that was truly written to disk.
    if vectors_data is None:
        return None
    if not isinstance(vectors_data, dict):
        raise KnowledgeImportError('Vector metadata has an invalid structure.')
    if not vectors_data:
        return None

    keys = ('embeddings', 'metadatas', 'documents', 'ids')
    if any(key not in vectors_data for key in keys):
        raise KnowledgeImportError('Vector metadata is incomplete.')

    embeddings, metadatas, documents, ids = (vectors_data[key] for key in keys)
    if not all(isinstance(value, list) for value in (embeddings, metadatas, documents, ids)):
        raise KnowledgeImportError('Vector metadata has an invalid structure.')
    if len({len(embeddings), len(metadatas), len(documents), len(ids)}) != 1:
        raise KnowledgeImportError('Vector metadata fields do not have matching lengths.')
    if len(ids) > _MAX_VECTOR_ITEMS:
        raise KnowledgeImportError('The knowledge archive contains too many vector entries.')

    return embeddings, metadatas, documents, ids


async def _insert_batch(knowledge_id: str, batch: list[dict]) -> None:
    if not batch:
        return

    VECTOR_DB_CLIENT.insert(
        collection_name=knowledge_id,
        items=batch,
    )


def _load_embeddings_npy(zf: zipfile.ZipFile, manifest: dict) -> np.ndarray:
    try:
        info = zf.getinfo(_EMBEDDINGS_MEMBER_NAME)
    except KeyError:
        raise KnowledgeImportError('The knowledge archive is missing embeddings.npy.')

    # Already bounded by _validate_archive's total-uncompressed-size check,
    # this is just an extra explicit guard for this specific member.
    if info.file_size > _MAX_UNCOMPRESSED_BYTES:
        raise KnowledgeImportError('embeddings.npy exceeds the import limit.')

    with zf.open(info) as source:
        raw = source.read()

    try:
        array = np.load(io.BytesIO(raw), allow_pickle=False)
    except Exception as exc:
        raise KnowledgeImportError('embeddings.npy is not a valid numpy array.') from exc

    if array.ndim != 2:
        raise KnowledgeImportError('embeddings.npy must be a 2D array.')

    count, dim = array.shape
    if count != manifest.get('count'):
        raise KnowledgeImportError('embeddings.npy row count does not match the manifest.')
    if dim > _MAX_EMBEDDING_DIM:
        raise KnowledgeImportError('The embedding dimension exceeds the import limit.')
    if count > _MAX_VECTOR_ITEMS:
        raise KnowledgeImportError('The knowledge archive contains too many vector entries.')

    return array.astype(np.float32, copy=False)


async def _import_v2_vectors(
    knowledge_id: str,
    zf: zipfile.ZipFile,
    manifest: dict,
    progress_callback: ProgressCallback,
) -> None:
    count = manifest.get('count', 0)
    if not isinstance(count, int) or count < 0:
        raise KnowledgeImportError('The knowledge archive manifest is invalid.')
    if count == 0:
        return

    if progress_callback:
        await progress_callback(15, '正在加载向量数据 (embeddings.npy)...')
    embeddings_array = _load_embeddings_npy(zf, manifest)

    try:
        records_info = zf.getinfo(_RECORDS_MEMBER_NAME)
    except KeyError:
        raise KnowledgeImportError('The knowledge archive is missing records.jsonl.')

    if records_info.file_size > _MAX_UNCOMPRESSED_BYTES:
        raise KnowledgeImportError('records.jsonl exceeds the import limit.')

    if progress_callback:
        await progress_callback(40, f'正在导入 {count} 条记录...')

    batch: list[dict] = []
    row_index = 0
    with zf.open(records_info) as raw_stream:
        text_stream = io.TextIOWrapper(raw_stream, encoding='utf-8')
        for line in text_stream:
            line = line.strip()
            if not line:
                continue
            if row_index >= count:
                raise KnowledgeImportError('records.jsonl has more rows than the manifest declares.')

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise KnowledgeImportError(f'records.jsonl line {row_index + 1} is not valid JSON.') from exc

            if not isinstance(record, dict) or 'id' not in record:
                raise KnowledgeImportError(f'records.jsonl line {row_index + 1} has an invalid structure.')

            batch.append(
                {
                    'id': record.get('id'),
                    'text': record.get('document'),
                    'vector': embeddings_array[row_index].tolist(),
                    'metadata': record.get('metadata'),
                }
            )
            row_index += 1

            if len(batch) >= _EXPORT_BATCH_SIZE:
                await _insert_batch(knowledge_id, batch)
                batch = []
                if progress_callback:
                    await progress_callback(40 + int(55 * row_index / count), f'已导入 {row_index}/{count} 条记录...')

    await _insert_batch(knowledge_id, batch)
    if progress_callback and batch:
        await progress_callback(40 + int(55 * row_index / count), f'已导入 {row_index}/{count} 条记录...')

    if row_index != count:
        raise KnowledgeImportError('records.jsonl row count does not match the manifest.')


async def import_knowledge_from_zip(
    knowledge_id: str,
    zip_buffer: BinaryIO,
    progress_callback: ProgressCallback = None,
) -> str:
    try:
        with zipfile.ZipFile(zip_buffer, 'r') as zf:
            _validate_archive(zf)

            manifest = _read_json_member(zf, _MANIFEST_MEMBER_NAME)
            if manifest is not None:
                if not isinstance(manifest, dict) or manifest.get('format') != _EXPORT_FORMAT:
                    raise KnowledgeImportError('Unrecognized knowledge archive format.')
                await _import_v2_vectors(knowledge_id, zf, manifest, progress_callback)

        if progress_callback:
            await progress_callback(100, '导入完成')

        log.info('Successfully imported knowledge base %s', knowledge_id)
        return knowledge_id
    except KnowledgeImportError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise KnowledgeImportError('The uploaded file is not a valid knowledge archive.') from exc
