from collections.abc import Sequence


def resolve_socketio_cors_origins(
    cors_origins: Sequence[str],
) -> str | list[str] | None:
    if list(cors_origins) == ['*']:
        return '*'

    origins = list(cors_origins)
    return origins or None
