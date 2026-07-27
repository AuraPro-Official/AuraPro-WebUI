import importlib.util
import unittest
from pathlib import Path

module_path = Path(__file__).resolve().parents[1] / 'open_webui' / 'socket' / 'cors.py'
spec = importlib.util.spec_from_file_location('socket_cors', module_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f'Unable to load {module_path}')

socket_cors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(socket_cors)
resolve_socketio_cors_origins = socket_cors.resolve_socketio_cors_origins


class SocketCorsOriginsTests(unittest.TestCase):
    def test_empty_http_cors_uses_socketio_same_origin_mode(self):
        self.assertIsNone(resolve_socketio_cors_origins([]))

    def test_wildcard_is_preserved(self):
        self.assertEqual(resolve_socketio_cors_origins(['*']), '*')

    def test_explicit_origins_are_preserved(self):
        origins = ['https://intranet.example', 'https://frp.example']
        self.assertEqual(resolve_socketio_cors_origins(origins), origins)


if __name__ == '__main__':
    unittest.main()
