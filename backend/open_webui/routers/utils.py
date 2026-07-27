from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import AsyncGenerator, Optional

import aiofiles
import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from open_webui.config import DATA_DIR, ENABLE_ADMIN_EXPORT
from open_webui.constants import ERROR_MESSAGES
from open_webui.models.chats import ChatTitleMessagesForm
from open_webui.models.config import Config
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.misc import get_gravatar_url
from pydantic import BaseModel, Field, field_validator
from starlette.responses import FileResponse, StreamingResponse

log = logging.getLogger(__name__)

router = APIRouter()


@router.get('/gravatar')
async def get_gravatar(email: str, user=Depends(get_verified_user)):
    return get_gravatar_url(email)


class CodeForm(BaseModel):
    code: str


@router.post('/code/format')
async def format_code(form_data: CodeForm, user=Depends(get_admin_user)):
    import black

    try:
        formatted_code = black.format_str(form_data.code, mode=black.Mode())
        return {'code': formatted_code}
    except black.NothingChanged:
        return {'code': form_data.code}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post('/code/execute')
async def execute_code(request: Request, form_data: CodeForm, user=Depends(get_verified_user)):
    from open_webui.utils.code_interpreter import execute_code_jupyter

    if not await Config.get('code_execution.enable'):
        raise HTTPException(
            status_code=403,
            detail=ERROR_MESSAGES.FEATURE_DISABLED('Code execution'),
        )

    if await Config.get('code_execution.engine') == 'jupyter':
        output = await execute_code_jupyter(
            await Config.get('code_execution.jupyter.url'),
            form_data.code,
            (
                await Config.get('code_execution.jupyter.auth_token')
                if await Config.get('code_execution.jupyter.auth') == 'token'
                else None
            ),
            (
                await Config.get('code_execution.jupyter.auth_password')
                if await Config.get('code_execution.jupyter.auth') == 'password'
                else None
            ),
            await Config.get('code_execution.jupyter.timeout'),
        )

        return output
    else:
        raise HTTPException(
            status_code=400,
            detail=ERROR_MESSAGES.DEFAULT('Code execution engine not supported'),
        )


class ChatForm(BaseModel):
    title: str
    messages: list[dict]


@router.post('/pdf')
async def download_chat_as_pdf(form_data: ChatTitleMessagesForm, user=Depends(get_verified_user)):
    from open_webui.utils.pdf_generator import PDFGenerator

    try:
        pdf_bytes = PDFGenerator(form_data).generate_chat_pdf()

        return Response(
            content=pdf_bytes,
            media_type='application/pdf',
            headers={'Content-Disposition': 'attachment;filename=chat.pdf'},
        )
    except Exception as e:
        log.exception(f'Error generating PDF: {e}')
        raise HTTPException(status_code=400, detail='The PDF could not be generated.')


@router.get('/db/download')
async def download_db(user=Depends(get_admin_user)):
    """Download the raw SQLite database file (admin-only, SQLite deployments only)."""
    if not ENABLE_ADMIN_EXPORT:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)

    # Lazy import avoids circular dependency at module load time
    from open_webui.internal.db import engine

    if engine.name != 'sqlite':
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DB_NOT_SQLITE)

    return FileResponse(
        str(engine.url.database),
        media_type='application/octet-stream',
        filename='webui.db',
    )


class SnapshotDownloadRequest(BaseModel):
    repo_id: str
    token: Optional[str] = None
    ignore_patterns: list[str] = Field(default_factory=list, max_length=64)

    @field_validator('repo_id')
    @classmethod
    def validate_repo_id(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 192 or not re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?',
            value,
        ):
            raise ValueError('Invalid Hugging Face model repository ID')
        if any(part in {'.', '..'} for part in value.split('/')):
            raise ValueError('Invalid Hugging Face model repository ID')
        return value

    @field_validator('ignore_patterns')
    @classmethod
    def validate_ignore_patterns(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 256 for value in values):
            raise ValueError('Invalid ignore pattern')
        return values


# ── 工具函数 ──────────────────────────────────────────────────────────────────
HF_CACHE_DIR = Path(os.environ.get('HF_HOME', Path.home() / '.cache' / 'huggingface' / 'hub'))
MODEL_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)


def _safe_child_path(root: Path, relative_path: str) -> Path:
    posix_path = PurePosixPath(relative_path)
    if (
        posix_path.is_absolute()
        or '\\' in relative_path
        or '\x00' in relative_path
        or any(part in {'', '.', '..'} for part in posix_path.parts)
    ):
        raise ValueError('Unsafe model file path')

    resolved_root = root.resolve()
    candidate = (resolved_root / Path(*posix_path.parts)).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError('Unsafe model file path')
    return candidate


def _repo_cache_dir(repo_id: str) -> Path:
    """返回模型缓存目录，与 huggingface_hub 的目录结构保持一致"""
    slug = 'models--' + repo_id.replace('/', '--')
    repo_dir = _safe_child_path(HF_CACHE_DIR, slug)

    # 尝试读取 refs/main 获取真实 commit hash
    refs_main = repo_dir / 'refs' / 'main'
    if refs_main.exists():
        commit_hash = refs_main.read_text(encoding='utf-8').strip()
        if re.fullmatch(r'[0-9a-f]{40,64}', commit_hash):
            return repo_dir / 'snapshots' / commit_hash

    # fallback：找 snapshots 下第一个存在的目录
    snapshots_dir = repo_dir / 'snapshots'
    if snapshots_dir.exists():
        candidates = [
            d for d in snapshots_dir.iterdir() if d.is_dir() and d.resolve().is_relative_to(repo_dir.resolve())
        ]
        if candidates:
            main_dir = snapshots_dir / 'main'
            if main_dir.exists():
                return main_dir
            return candidates[0]

    # 首次下载，还没有 refs，默认用 main（Linux）或创建 hash 目录
    return repo_dir / 'snapshots' / 'main'


async def _get_repo_files(
    repo_id: str,
    token: Optional[str],
) -> list[dict]:
    """
    通过 HF API 获取仓库文件列表。
    返回 [{"rfilename": str, "size": int}, ...]
    """
    url = f'https://huggingface.co/api/models/{repo_id}'
    headers = {'Authorization': f'Bearer {token}'} if token else {}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get(url, headers=headers) as resp:
            if not resp.ok:
                raise HTTPException(
                    status_code=resp.status,
                    detail=f'HuggingFace API error: {resp.status} {resp.reason}',
                )
            data = await resp.json()
            siblings = data.get('siblings', [])
            files = []
            for sibling in siblings:
                filename = sibling.get('rfilename')
                if not isinstance(filename, str):
                    continue
                _safe_child_path(HF_CACHE_DIR, filename)
                files.append({'rfilename': filename, 'size': max(0, int(sibling.get('size') or 0))})
            return files


def _should_ignore(filename: str, ignore_patterns: list[str]) -> bool:
    import fnmatch

    return any(fnmatch.fnmatch(filename, pat) for pat in ignore_patterns)


async def _download_file(
    session: aiohttp.ClientSession,
    repo_id: str,
    filename: str,
    dest: Path,
    token: Optional[str],
) -> AsyncGenerator[int, None]:
    """
    下载单个文件，yield 每次写入的字节数（用于进度累加）。
    支持断点续传。
    """
    from urllib.parse import quote

    encoded = '/'.join(quote(p, safe='') for p in filename.split('/'))
    url = f'https://huggingface.co/{repo_id}/resolve/main/{encoded}'

    cache_root = HF_CACHE_DIR.resolve()
    dest = dest.resolve()
    if not dest.is_relative_to(cache_root):
        raise ValueError('Unsafe model destination path')
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.tmp')

    resume_bytes = tmp.stat().st_size if tmp.exists() else 0
    headers = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if resume_bytes > 0:
        headers['Range'] = f'bytes={resume_bytes}-'

    async with session.get(url, headers=headers, allow_redirects=True) as resp:
        # 416 = 文件已完整下载
        if resp.status == 416:
            tmp.rename(dest)
            return

        # 服务器忽略了 Range
        if resume_bytes > 0 and resp.status != 206:
            tmp.unlink(missing_ok=True)
            resume_bytes = 0

        if not resp.ok:
            raise IOError(f'Failed to download {filename}: {resp.status} {resp.reason}')

        write_flag = 'ab' if resume_bytes > 0 else 'wb'
        async with aiofiles.open(tmp, write_flag) as f:
            async for chunk in resp.content.iter_chunked(256 * 1024):
                await f.write(chunk)
                yield len(chunk)

    tmp.rename(dest)


async def _snapshot_stream(
    req: SnapshotDownloadRequest,
) -> AsyncGenerator[str, None]:
    """
    NDJSON 流，每行一个 JSON，格式兼容前端 downloadModelWithProgress：

      下载中：{"status":"downloading","filename":"...","completed":<累计字节>,"total":<总字节>}
      完成：  {"status":"completed","path":"<缓存目录>"}
      出错：  {"error":"<错误信息>"}
    """

    def emit(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False) + '\n'

    try:
        # 1. 获取文件列表
        yield emit({'status': 'fetching_file_list'})
        files = await _get_repo_files(req.repo_id, req.token)

        # 过滤忽略项
        if req.ignore_patterns:
            files = [f for f in files if not _should_ignore(f['rfilename'], req.ignore_patterns)]

        if not files:
            yield emit({'error': f'No files found in {req.repo_id}'})
            return

        total_bytes = sum(f['size'] for f in files)
        completed_bytes = 0
        cache_dir = _repo_cache_dir(req.repo_id)

        async with aiohttp.ClientSession(timeout=MODEL_DOWNLOAD_TIMEOUT) as session:
            for file_info in files:
                filename = file_info['rfilename']
                dest = _safe_child_path(cache_dir, filename)

                # 已缓存则直接计入进度
                if dest.exists():
                    size = dest.stat().st_size
                    if size > 0 and (file_info['size'] == 0 or size == file_info['size']):
                        completed_bytes += file_info['size']
                        yield emit(
                            {
                                'status': 'downloading',
                                'filename': filename,
                                'completed': completed_bytes,
                                'total': total_bytes,
                                'cached': True,
                            }
                        )
                        continue

                # 下载并推送进度
                async for chunk_size in _download_file(session, req.repo_id, filename, dest, req.token):
                    completed_bytes += chunk_size
                    yield emit(
                        {
                            'status': 'downloading',
                            'filename': filename,
                            'completed': completed_bytes,
                            'total': total_bytes,
                        }
                    )

        yield emit(
            {
                'status': 'completed',
                'path': str(cache_dir),
                'repo_id': req.repo_id,
            }
        )

    except asyncio.CancelledError:
        yield emit({'status': 'cancelled'})
    except HTTPException as e:
        yield emit({'error': e.detail})
    except Exception as e:
        yield emit({'error': str(e)})


@router.post('/huggingface/download')
async def huggingface_snapshot_download(
    req: SnapshotDownloadRequest,
    user=Depends(get_admin_user),  # 仅管理员可下载
):
    """
    从 HuggingFace 下载整个模型仓库，以 NDJSON 流实时推送进度。

    与前端 downloadModelWithProgress 完全兼容：
    - 进度通过 completed / total 字段计算百分比
    - status === "completed" 时触发完成回调

    请求体：
    {
        "repo_id": "BAAI/bge-reranker-v2-m3",
        "token": "hf_xxx",            // 可选
        "ignore_patterns": ["*.msgpack", "flax_model*"]  // 可选
    }
    """

    async def generate():
        async for chunk in _snapshot_stream(req):
            yield chunk.encode('utf-8')
            yield b'\n'

    return StreamingResponse(
        generate(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


@router.get('/huggingface/cache')
async def get_cached_models(user=Depends(get_admin_user)):
    """列出本地已缓存的模型"""
    if not HF_CACHE_DIR.exists():
        return []

    result = []
    for model_dir in HF_CACHE_DIR.iterdir():
        if not model_dir.name.startswith('models--'):
            continue
        repo_id = model_dir.name[len('models--') :].replace('--', '/')
        snapshot_dir = model_dir / 'snapshots' / 'main'
        if snapshot_dir.exists():
            files = list(snapshot_dir.rglob('*'))
            total_size = sum(f.stat().st_size for f in files if f.is_file())
            result.append(
                {
                    'repo_id': repo_id,
                    'path': str(snapshot_dir),
                    'file_count': len([f for f in files if f.is_file()]),
                    'total_size': total_size,
                }
            )
    return result
