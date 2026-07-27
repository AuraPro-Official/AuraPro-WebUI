import asyncio
import io
import json
import logging
import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, Optional

from open_webui.models.files import FileForm, Files
from open_webui.models.knowledge import Knowledges
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
from open_webui.storage.provider import Storage

log = logging.getLogger(__name__)

_BODIES_DIR = 'file_bodies'
_MIB = 1024 * 1024
_MAX_ARCHIVE_BYTES = int(os.getenv('KNOWLEDGE_IMPORT_MAX_ARCHIVE_MB', '512')) * _MIB
_MAX_UNCOMPRESSED_BYTES = int(os.getenv('KNOWLEDGE_IMPORT_MAX_UNCOMPRESSED_MB', '1024')) * _MIB
_MAX_JSON_BYTES = int(os.getenv('KNOWLEDGE_IMPORT_MAX_JSON_MB', '256')) * _MIB
_MAX_FILE_BYTES = int(os.getenv('KNOWLEDGE_IMPORT_MAX_FILE_MB', '512')) * _MIB
_MAX_ARCHIVE_ENTRIES = int(os.getenv('KNOWLEDGE_IMPORT_MAX_ENTRIES', '10000'))
_MAX_VECTOR_ITEMS = int(os.getenv('KNOWLEDGE_IMPORT_MAX_VECTOR_ITEMS', '2000000'))
_MAX_COMPRESSION_RATIO = 200
_SAFE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


class KnowledgeImportError(ValueError):
    pass


def get_knowledge_import_max_archive_bytes() -> int:
    return _MAX_ARCHIVE_BYTES


def _safe_filename(filename: str, fallback: str = 'imported_file') -> str:
    basename = PurePosixPath(str(filename).replace('\\', '/')).name
    basename = ''.join(char for char in basename if char >= ' ' and char != '\x7f').strip(' .')
    return (basename or fallback)[:180]


def _safe_archive_path(file_id: str, filename: str) -> str:
    safe_id = file_id if _SAFE_ID_RE.fullmatch(file_id) else 'file'
    return f'{_BODIES_DIR}/{safe_id}/{_safe_filename(filename, safe_id)}'


def _validate_archive(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise KnowledgeImportError('The knowledge archive contains too many entries.')

    total_uncompressed = 0
    body_members: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        if info.flag_bits & 0x1:
            raise KnowledgeImportError('Encrypted knowledge archives are not supported.')

        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise KnowledgeImportError('Symbolic links are not allowed in knowledge archives.')

        total_uncompressed += info.file_size
        if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
            raise KnowledgeImportError('The expanded knowledge archive is too large.')

        if (
            info.file_size > 10 * _MIB
            and info.file_size > max(info.compress_size, 1) * _MAX_COMPRESSION_RATIO
        ):
            raise KnowledgeImportError('The knowledge archive has an unsafe compression ratio.')

        path = PurePosixPath(info.filename.replace('\\', '/'))
        if path.is_absolute() or '..' in path.parts:
            raise KnowledgeImportError('The knowledge archive contains an unsafe path.')

        if len(path.parts) >= 3 and path.parts[0] == _BODIES_DIR:
            file_id = path.parts[1]
            if not _SAFE_ID_RE.fullmatch(file_id):
                raise KnowledgeImportError('The knowledge archive contains an invalid file identifier.')
            if file_id in body_members:
                raise KnowledgeImportError('The knowledge archive contains duplicate file bodies.')
            if info.file_size > _MAX_FILE_BYTES:
                raise KnowledgeImportError('A file in the knowledge archive exceeds the import limit.')
            body_members[file_id] = info

    return body_members


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


def _validate_vectors(vectors_data: Any) -> tuple[list, list, list, list] | None:
    if not vectors_data:
        return None
    if not isinstance(vectors_data, dict):
        raise KnowledgeImportError('Vector metadata has an invalid structure.')

    keys = ('embeddings', 'metadatas', 'documents', 'ids')
    if any(key not in vectors_data for key in keys):
        raise KnowledgeImportError('Vector metadata is incomplete.')

    embeddings, metadatas, documents, ids = (vectors_data[key] for key in keys)
    if not all(isinstance(value, list) for value in (embeddings, metadatas, documents, ids)):
        raise KnowledgeImportError('Vector metadata has an invalid structure.')
    if len({len(embeddings), len(metadatas), len(documents), len(ids)}) != 1:
        raise KnowledgeImportError('Vector metadata groups do not have matching lengths.')

    item_count = 0
    for group_index, text_group in enumerate(documents):
        grouped_values = (
            text_group,
            ids[group_index],
            embeddings[group_index],
            metadatas[group_index],
        )
        if not all(isinstance(value, list) for value in grouped_values):
            raise KnowledgeImportError('Vector metadata contains an invalid group.')
        if len({len(value) for value in grouped_values}) != 1:
            raise KnowledgeImportError('Vector metadata entries do not have matching lengths.')
        item_count += len(text_group)
        if item_count > _MAX_VECTOR_ITEMS:
            raise KnowledgeImportError('The knowledge archive contains too many vector entries.')

    return embeddings, metadatas, documents, ids


async def __export_knowledge_with_vectors(knowledge_id: str) -> Dict[str, Any]:
    knowledge = await Knowledges.get_knowledge_by_id(knowledge_id)
    if not knowledge:
        raise ValueError(f'Knowledge base {knowledge_id} not found')

    slim_files = await Knowledges.get_files_by_id(knowledge_id)
    files_meta: list[dict] = []
    file_sources: dict[str, str | bytes] = {}

    for slim in slim_files:
        file = await Files.get_file_by_id(slim.id)
        if not file:
            log.warning('File row %s missing from DB, skipping.', slim.id)
            continue

        files_meta.append(
            {
                'id': file.id,
                'filename': file.filename,
                # Server-local paths are intentionally never exported.
                'path': '',
                'metadata': file.meta or {},
                'data': file.data or {},
            }
        )

        if file.path:
            try:
                resolved_path = await asyncio.to_thread(Storage.get_file, file.path)
                if os.path.isfile(resolved_path):
                    file_sources[file.id] = resolved_path
                    continue
            except Exception as exc:
                log.warning(
                    'Could not resolve file body %s (%s)',
                    file.id,
                    type(exc).__name__,
                )

        content = (file.data or {}).get('content', '')
        file_sources[file.id] = content.encode('utf-8') if content else b''

    collection = await ASYNC_VECTOR_DB_CLIENT.get(collection_name=knowledge_id)
    return {
        'knowledge': knowledge.model_dump(mode='json'),
        'files': files_meta,
        'metadata': {
            'ids': collection.ids,
            'documents': collection.documents,
            'metadatas': collection.metadatas,
            'embeddings': collection.embeddings,
        },
        '_file_sources': file_sources,
    }


async def __import_knowledge_with_vectors(
    knowledge_id: str,
    import_data: Dict[str, Any],
    *,
    zf: zipfile.ZipFile,
    body_members: dict[str, zipfile.ZipInfo],
    db: Optional[Any] = None,
    user_id: Optional[str] = None,
) -> str:
    knowledge_meta = import_data.get('knowledge', {})
    files_meta = import_data.get('files', [])
    vectors = _validate_vectors(import_data.get('metadata', {}))

    if not isinstance(knowledge_meta, dict) or not isinstance(files_meta, list):
        raise KnowledgeImportError('The knowledge archive metadata is invalid.')
    if len(files_meta) > _MAX_ARCHIVE_ENTRIES:
        raise KnowledgeImportError('The knowledge archive contains too many files.')

    owner_id = user_id or knowledge_meta.get('user_id', '')
    if not owner_id:
        raise KnowledgeImportError('The knowledge archive does not have a valid owner.')

    seen_ids: set[str] = set()
    for file_info in files_meta:
        if not isinstance(file_info, dict):
            raise KnowledgeImportError('The knowledge archive contains invalid file metadata.')

        file_id = file_info.get('id')
        if not isinstance(file_id, str) or not _SAFE_ID_RE.fullmatch(file_id):
            raise KnowledgeImportError('The knowledge archive contains an invalid file identifier.')
        if file_id in seen_ids:
            raise KnowledgeImportError('The knowledge archive contains duplicate file identifiers.')
        seen_ids.add(file_id)

        file_name = _safe_filename(file_info.get('filename', 'imported_file'))
        file_meta = file_info.get('metadata') or {}
        file_data = file_info.get('data') or {}
        if not isinstance(file_meta, dict) or not isinstance(file_data, dict):
            raise KnowledgeImportError('The knowledge archive contains invalid file metadata.')

        existing_file = await Files.get_file_by_id(file_id)
        if existing_file and existing_file.user_id != owner_id:
            raise KnowledgeImportError('A file identifier in the archive conflicts with an existing file.')

        if not existing_file:
            restored_path = ''
            body_info = body_members.get(file_id)
            if body_info and body_info.file_size:
                storage_filename = f'{file_id}_{file_name}'
                with zf.open(body_info) as body:
                    upload_result = await asyncio.to_thread(
                        Storage.upload_file,
                        body,
                        storage_filename,
                        {'AuraPro-File-Id': file_id, 'AuraPro-User-Id': owner_id},
                        _MAX_FILE_BYTES,
                    )
                restored_path = upload_result.path
                file_meta = {
                    **file_meta,
                    'size': upload_result.size,
                    'file_hash': upload_result.sha256,
                }

            form_data = FileForm(
                id=file_id,
                filename=file_name,
                path=restored_path,
                data=file_data,
                meta=file_meta,
            )
            await Files.insert_new_file(owner_id, form_data, db=db)
            log.info('Created imported file record %s (%s)', file_id, file_name)

        if not await Knowledges.has_file(knowledge_id, file_id, db=db):
            await Knowledges.add_file_to_knowledge_by_id(
                knowledge_id=knowledge_id,
                file_id=file_id,
                user_id=owner_id,
                db=db,
            )

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


async def create_knowledge_export_zip(knowledge_id: str, include_vectors: bool = True) -> io.BytesIO:
    export_data = await __export_knowledge_with_vectors(knowledge_id)
    file_sources: dict[str, str | bytes] = export_data.pop('_file_sources', {})

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.writestr('knowledge.json', json.dumps(export_data['knowledge'], indent=2))
        zf.writestr('files.json', json.dumps(export_data['files'], indent=2))

        if include_vectors:
            zf.writestr(
                'metadata.json',
                json.dumps(export_data.get('metadata', {}), indent=2, ensure_ascii=False, default=str),
            )

        for file_info in export_data['files']:
            file_id = file_info['id']
            zip_path = _safe_archive_path(file_id, file_info.get('filename', file_id))
            source = file_sources.get(file_id, b'')
            if isinstance(source, str):
                zf.write(source, zip_path)
            else:
                zf.writestr(zip_path, source)

    zip_buffer.seek(0)
    return zip_buffer


async def import_knowledge_from_zip(
    knowledge_id: str,
    zip_buffer: BinaryIO,
    db: Optional[Any] = None,
    user_id: Optional[str] = None,
) -> str:
    try:
        with zipfile.ZipFile(zip_buffer, 'r') as zf:
            body_members = _validate_archive(zf)
            import_data = {
                'knowledge': _read_json_member(zf, 'knowledge.json', required=True),
                'files': _read_json_member(zf, 'files.json', required=True),
                'metadata': _read_json_member(zf, 'metadata.json') or {},
            }
            knowledge_base_id = await __import_knowledge_with_vectors(
                knowledge_id,
                import_data,
                zf=zf,
                body_members=body_members,
                db=db,
                user_id=user_id,
            )

        log.info('Successfully imported knowledge base %s', knowledge_base_id)
        return knowledge_base_id
    except KnowledgeImportError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise KnowledgeImportError('The uploaded file is not a valid knowledge archive.') from exc
