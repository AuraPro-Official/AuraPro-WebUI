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
is_same_host_socket_origin = socket_cors.is_same_host_socket_origin


class SocketCorsOriginsTests(unittest.TestCase):
    def test_empty_http_cors_uses_same_host_validator(self):
        validator = resolve_socketio_cors_origins([])

        self.assertTrue(callable(validator))
        self.assertTrue(validator('https://127.0.0.1:8080', {'HTTP_HOST': '127.0.0.1:8080'}))

    def test_wildcard_is_preserved(self):
        self.assertEqual(resolve_socketio_cors_origins(['*']), '*')

    def test_explicit_origins_are_preserved(self):
        origins = ['https://intranet.example', 'https://frp.example']
        self.assertEqual(resolve_socketio_cors_origins(origins), origins)

    def test_same_host_validator_ignores_engineio_scheme_mismatch(self):
        environ = {
            'HTTP_HOST': '127.0.0.1:8080',
            'wsgi.url_scheme': 'http',
        }

        self.assertTrue(is_same_host_socket_origin('https://127.0.0.1:8080', environ))

    def test_same_host_validator_rejects_different_host_or_port(self):
        environ = {'HTTP_HOST': '127.0.0.1:8080'}

        self.assertFalse(is_same_host_socket_origin('https://localhost:8080', environ))
        self.assertFalse(is_same_host_socket_origin('https://127.0.0.1:8443', environ))
        self.assertFalse(is_same_host_socket_origin('file://127.0.0.1:8080', environ))


if __name__ == '__main__':
    unittest.main()
