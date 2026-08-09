from collections.abc import Sequence
from typing import Any, Callable
from urllib.parse import urlsplit

SocketOriginValidator = Callable[[str | None, dict[str, Any]], bool]


def is_same_host_socket_origin(origin: str | None, environ: dict[str, Any]) -> bool:
    """Allow a browser origin when its host and port match the request.

    python-engineio currently derives the ASGI URL scheme only from
    X-Forwarded-Proto and otherwise assumes HTTP. That makes its built-in
    same-origin check reject direct HTTPS connections even when the Origin
    and Host headers point to the same AuraPro server.
    """
    if origin is None:
        return True

    request_host = str(environ.get('HTTP_HOST') or '').split(',', 1)[0].strip()
    if not request_host:
        return False

    try:
        parsed_origin = urlsplit(origin)
        parsed_request_host = urlsplit(f'//{request_host}')
        if parsed_origin.scheme not in {'http', 'https'}:
            return False

        origin_port = parsed_origin.port or (443 if parsed_origin.scheme == 'https' else 80)
        request_port = parsed_request_host.port or origin_port
    except ValueError:
        return False

    return (
        parsed_origin.hostname is not None
        and parsed_request_host.hostname is not None
        and parsed_origin.hostname.casefold() == parsed_request_host.hostname.casefold()
        and origin_port == request_port
    )


def resolve_socketio_cors_origins(
    cors_origins: Sequence[str],
) -> str | list[str] | SocketOriginValidator:
    if list(cors_origins) == ['*']:
        return '*'

    origins = list(cors_origins)
    return origins or is_same_host_socket_origin
