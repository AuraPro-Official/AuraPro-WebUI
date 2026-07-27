import asyncio
import base64
import io
import logging
import mimetypes
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    UploadFile,
)
from open_webui.env import (
    AIOHTTP_CLIENT_ALLOW_REDIRECTS,
    AIOHTTP_CLIENT_SESSION_SSL,
    ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK,
)
from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.retrieval.web.security import get_ssrf_safe_session, validate_url
from open_webui.routers.files import upload_file_handler
from open_webui.utils.access_control.files import has_access_to_file
from open_webui.routers.images import (
    get_image_data,
    upload_image,
)
from open_webui.storage.provider import Storage

BASE64_IMAGE_URL_PREFIX = re.compile(r'data:image/\w+;base64,', re.IGNORECASE)
MARKDOWN_IMAGE_URL_PATTERN = re.compile(r'!\[(.*?)\]\((.+?)\)', re.IGNORECASE)
log = logging.getLogger(__name__)

OPENAI_AUDIO_FORMAT_BY_CONTENT_TYPE = {
    'audio/wav': 'wav',
    'audio/x-wav': 'wav',
    'audio/wave': 'wav',
    'audio/mpeg': 'mp3',
    'audio/mp3': 'mp3',
}

# Extension-based MIME fallback, only used when ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK is True.
_IMAGE_MIME_FALLBACK = {
    '.webp': 'image/webp',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.bmp': 'image/bmp',
    '.tiff': 'image/tiff',
    '.tif': 'image/tiff',
    '.ico': 'image/x-icon',
    '.heic': 'image/heic',
    '.heif': 'image/heif',
    '.avif': 'image/avif',
}


async def get_image_base64_from_url(url: str, user=None) -> Optional[str]:
    try:
        if url.startswith('http'):
            # Validate URL to prevent SSRF attacks against local/private networks.
            # allow_redirects=False prevents redirect-based SSRF: validate_url() is
            # called only on the originally-submitted URL; following 3xx redirects
            # without re-validation would let an attacker reach private IPs via a
            # public host that redirects internally (e.g. cloud-metadata exfil).
            await asyncio.to_thread(validate_url, url)
            # Fetch through an SSRF-safe session that re-checks the connect-time IP, so a
            # rebinding DNS answer that passed validate_url cannot reach an internal address.
            async with get_ssrf_safe_session() as session:
                async with session.get(
                    url, ssl=AIOHTTP_CLIENT_SESSION_SSL, allow_redirects=AIOHTTP_CLIENT_ALLOW_REDIRECTS
                ) as response:
                    response.raise_for_status()
                    image_data = await response.read()
                    encoded_string = base64.b64encode(image_data).decode('utf-8')
                    content_type = response.headers.get('Content-Type', 'image/png')
                    return f'data:{content_type};base64,{encoded_string}'
        else:
            # Non-URL string — treat as file_id. Delegate to the canonical
            # file-ID resolver which enforces ownership/access checks.
            return await get_image_base64_from_file_id(url, user=user)

    except Exception as e:
        return None


async def get_image_url_from_base64(request, base64_image_string, metadata, user):
    if BASE64_IMAGE_URL_PREFIX.match(base64_image_string):
        image_url = ''
        # Extract base64 image data from the line
        image_data, content_type = await get_image_data(base64_image_string)
        if image_data is not None:
            _, image_url = await upload_image(
                request,
                image_data,
                content_type,
                metadata,
                user,
            )

        return image_url
    return None


async def convert_markdown_base64_images(request, content: str, metadata, user):
    MIN_REPLACEMENT_URL_LENGTH = 1024
    result_parts = []
    last_end = 0

    for match in MARKDOWN_IMAGE_URL_PATTERN.finditer(content):
        result_parts.append(content[last_end : match.start()])
        base64_string = match.group(2)
        if len(base64_string) > MIN_REPLACEMENT_URL_LENGTH:
            url = await get_image_url_from_base64(request, base64_string, metadata, user)
            if url:
                result_parts.append(f'![{match.group(1)}]({url})')
            else:
                result_parts.append(match.group(0))
        else:
            result_parts.append(match.group(0))
        last_end = match.end()

    result_parts.append(content[last_end:])
    return ''.join(result_parts)


def load_b64_audio_data(b64_str):
    try:
        if ',' in b64_str:
            header, b64_data = b64_str.split(',', 1)
        else:
            b64_data = b64_str
            header = 'data:audio/wav;base64'
        audio_data = base64.b64decode(b64_data)
        content_type = header.split(';')[0].split(':')[1] if ';' in header else 'audio/wav'
        return audio_data, content_type
    except Exception as e:
        print(f'Error decoding base64 audio data: {e}')
        return None, None


def get_openai_audio_format(content_type: str | None) -> Optional[str]:
    if not content_type:
        return None
    return OPENAI_AUDIO_FORMAT_BY_CONTENT_TYPE.get(content_type.lower().split(';', 1)[0].strip())


def _audio_suffix_from_content_type(content_type: str | None) -> str:
    if content_type:
        suffix = mimetypes.guess_extension(content_type.split(';', 1)[0].strip())
        if suffix:
            return suffix
        audio_type = content_type.split(';', 1)[0].strip().split('/', 1)
        if len(audio_type) == 2:
            return f'.{audio_type[1]}'
    return '.webm'


def _transcode_audio_to_mp3(audio_data: bytes, content_type: str | None) -> Optional[bytes]:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        log.warning('ffmpeg not found; cannot convert audio to mp3 for multimodal input')
        return None

    suffix = _audio_suffix_from_content_type(content_type)
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / f'input{suffix}'
        output_path = Path(tmpdir) / 'output.mp3'
        input_path.write_bytes(audio_data)

        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    '-y',
                    '-nostdin',
                    '-loglevel',
                    'error',
                    '-i',
                    str(input_path),
                    '-vn',
                    '-ac',
                    '1',
                    '-ar',
                    '16000',
                    '-codec:a',
                    'libmp3lame',
                    '-b:a',
                    '64k',
                    str(output_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            log.warning('ffmpeg timed out while converting audio to mp3')
            return None
        if result.returncode != 0 or not output_path.exists():
            log.warning('ffmpeg failed to convert audio to mp3: %s', result.stderr.strip())
            return None

        return output_path.read_bytes()


async def get_openai_audio_base64(audio_data: bytes, content_type: str | None) -> Optional[str]:
    audio_format = get_openai_audio_format(content_type)
    if audio_format:
        normalized_type = 'audio/wav' if audio_format == 'wav' else 'audio/mpeg'
        encoded_string = base64.b64encode(audio_data).decode('utf-8')
        return f'data:{normalized_type};base64,{encoded_string}'

    mp3_data = await asyncio.to_thread(_transcode_audio_to_mp3, audio_data, content_type)
    if not mp3_data:
        return None

    encoded_string = base64.b64encode(mp3_data).decode('utf-8')
    return f'data:audio/mpeg;base64,{encoded_string}'


async def get_openai_audio_base64_from_data_url(data_url: str) -> Optional[str]:
    audio_data, content_type = load_b64_audio_data(data_url)
    if audio_data is None:
        return None
    return await get_openai_audio_base64(audio_data, content_type)


async def upload_audio(request, audio_data, content_type, metadata, user):
    audio_format = mimetypes.guess_extension(content_type)
    file = UploadFile(
        file=io.BytesIO(audio_data),
        filename=f'generated-{audio_format}',  # will be converted to a unique ID on upload_file
        headers={
            'content-type': content_type,
        },
    )
    file_item = await upload_file_handler(
        request,
        file=file,
        metadata=metadata,
        process=False,
        user=user,
    )
    url = request.app.url_path_for('get_file_content_by_id', id=file_item.id)
    return url


async def get_audio_url_from_base64(request, base64_audio_string, metadata, user):
    if 'data:audio/wav;base64' in base64_audio_string:
        audio_url = ''
        # Extract base64 audio data from the line
        audio_data, content_type = load_b64_audio_data(base64_audio_string)
        if audio_data is not None:
            audio_url = await upload_audio(
                request,
                audio_data,
                content_type,
                metadata,
                user,
            )
        return audio_url
    return None


async def get_file_url_from_base64(request, base64_file_string, metadata, user):
    if BASE64_IMAGE_URL_PREFIX.match(base64_file_string):
        return await get_image_url_from_base64(request, base64_file_string, metadata, user)
    elif 'data:audio/wav;base64' in base64_file_string:
        return await get_audio_url_from_base64(request, base64_file_string, metadata, user)
    return None


async def get_image_base64_from_file_id(id: str, user=None) -> Optional[str]:
    file = await Files.get_file_by_id(id)
    if not file:
        return None

    # Gate file-by-id resolution by ownership to prevent exfiltration.
    # A caller could place another user's file_id in an image_url field;
    # without this check the server reads the file from disk, inlines it
    # base64 into the LLM request, and the content leaks via OCR/describe.
    # Owner, admin, and explicit read-grant holders are allowed.
    if user is None:
        return None
    if file.user_id != user.id and user.role != 'admin' and not await has_access_to_file(file.id, 'read', user):
        return None

    try:
        file_path = await asyncio.to_thread(Storage.get_file, file.path)
        file_path = Path(file_path)

        # Check if the file already exists in the cache
        if file_path.is_file():
            with open(file_path, 'rb') as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                content_type = mimetypes.guess_type(file_path.name)[0] or (file.meta or {}).get('content_type')
                if not content_type and ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK:
                    content_type = _IMAGE_MIME_FALLBACK.get(file_path.suffix.lower())
                if not content_type:
                    return None
                return f'data:{content_type};base64,{encoded_string}'
        else:
            return None
    except Exception as e:
        return None


async def get_audio_base64_from_file_id(id: str) -> Optional[str]:
    file = await Files.get_file_by_id(id)
    if not file:
        return None

    try:
        file_path = await asyncio.to_thread(Storage.get_file, file.path)
        file_path = Path(file_path)

        if file_path.is_file():
            with open(file_path, 'rb') as audio_file:
                audio_data = audio_file.read()
                content_type = (file.meta or {}).get('content_type') or mimetypes.guess_type(file_path.name)[0]
                if not content_type or not content_type.startswith('audio/'):
                    return None
                return await get_openai_audio_base64(audio_data, content_type)
        else:
            return None
    except Exception:
        return None
