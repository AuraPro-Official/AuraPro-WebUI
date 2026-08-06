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
_MAX_COMPRESSION_RATIO = 200


class KnowledgeImportError(ValueError):
    pass


def get_knowledge_import_max_archive_bytes() -> int:
    return _MAX_ARCHIVE_BYTES


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


def _validate_group(
    group_index: int,
    documents: list,
    ids: list,
    embeddings: list,
    metadatas: list,
) -> int:
    grouped_values = (
        documents[group_index],
        ids[group_index],
        embeddings[group_index],
        metadatas[group_index],
    )
    if not all(isinstance(value, list) for value in grouped_values):
        raise KnowledgeImportError('Vector metadata contains an invalid group.')
    if len({len(value) for value in grouped_values}) != 1:
        raise KnowledgeImportError('Vector metadata entries do not have matching lengths.')
    return len(documents[group_index])


def _validate_vectors(vectors_data: Any) -> tuple[list, list, list, list] | None:
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
        raise KnowledgeImportError('Vector metadata groups do not have matching lengths.')

    item_count = 0
    for group_index in range(len(documents)):
        item_count += _validate_group(group_index, documents, ids, embeddings, metadatas)
        if item_count > _MAX_VECTOR_ITEMS:
            raise KnowledgeImportError('The knowledge archive contains too many vector entries.')

    return embeddings, metadatas, documents, ids


async def __export_knowledge_with_vectors(
    knowledge_id: str,
    progress_callback: ProgressCallback = None,
) -> dict[str, Any]:
    if progress_callback:
        await progress_callback(5, '正在读取知识库信息...')

    knowledge = await Knowledges.get_knowledge_by_id(knowledge_id)
    if not knowledge:
        raise ValueError(f'Knowledge base {knowledge_id} not found')

    if progress_callback:
        await progress_callback(15, '正在读取向量数据...')

    collection = await ASYNC_VECTOR_DB_CLIENT.get(collection_name=knowledge_id)

    if progress_callback:
        await progress_callback(45, '向量数据读取完成')

    return {
        'knowledge': knowledge.model_dump(mode='json'),
        'metadata': {
            'ids': collection.ids,
            'documents': collection.documents,
            'metadatas': collection.metadatas,
            'embeddings': collection.embeddings,
        },
    }


async def create_knowledge_export_zip(
    knowledge_id: str,
    include_vectors: bool = True,
    progress_callback: ProgressCallback = None,
) -> io.BytesIO:
    export_data = await __export_knowledge_with_vectors(knowledge_id, progress_callback)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        if progress_callback:
            await progress_callback(55, '正在写入知识库信息...')
        zf.writestr('knowledge.json', json.dumps(export_data['knowledge'], indent=2))

        if include_vectors:
            if progress_callback:
                await progress_callback(60, '正在写入向量数据...')
            zf.writestr(
                'metadata.json',
                json.dumps(export_data.get('metadata', {}), indent=2, ensure_ascii=False, default=str),
            )

    zip_buffer.seek(0)

    if progress_callback:
        await progress_callback(100, '导出完成')

    return zip_buffer


async def __import_knowledge_with_vectors(
    knowledge_id: str,
    vectors_data: Any,
) -> str:
    vectors = _validate_vectors(vectors_data)

    if vectors:
        embeddings, metadatas, documents, ids = vectors
        items = []
        for group_index, texts in enumerate(documents):
            items.extend(
                {
                    'id': ids[group_index][item_index],
                    'text': text,
                    'vector': embeddings[group_index][item_index],
                    'metadata': metadatas[group_index][item_index],
                }
                for item_index, text in enumerate(texts)
            )

        if items:
            await asyncio.to_thread(
                VECTOR_DB_CLIENT.insert,
                collection_name=knowledge_id,
                items=items,
            )

    return knowledge_id


async def import_knowledge_from_zip(
    knowledge_id: str,
    zip_buffer: BinaryIO,
) -> str:
    try:
        with zipfile.ZipFile(zip_buffer, 'r') as zf:
            _validate_archive(zf)

            knowledge_data = _read_json_member(zf, 'knowledge.json', required=True)
            if not isinstance(knowledge_data, dict):
                raise KnowledgeImportError('The knowledge archive has an invalid structure.')

            vectors_data = _read_json_member(zf, 'metadata.json')
            knowledge_base_id = await __import_knowledge_with_vectors(
                knowledge_id,
                vectors_data,
            )

        log.info('Successfully imported knowledge base %s', knowledge_base_id)
        return knowledge_base_id
    except KnowledgeImportError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise KnowledgeImportError('The uploaded file is not a valid knowledge archive.') from exc
