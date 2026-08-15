import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
OPEN_WEBUI = BACKEND / 'open_webui'

package = types.ModuleType('open_webui')
package.__path__ = [str(OPEN_WEBUI)]
sys.modules['open_webui'] = package

storage_package = types.ModuleType('open_webui.storage')
storage_package.__path__ = [str(OPEN_WEBUI / 'storage')]
sys.modules['open_webui.storage'] = storage_package

config_module = types.ModuleType('open_webui.config')
for name in (
    'AZURE_STORAGE_CONTAINER_NAME',
    'AZURE_STORAGE_ENDPOINT',
    'AZURE_STORAGE_KEY',
    'GCS_BUCKET_NAME',
    'GOOGLE_APPLICATION_CREDENTIALS_JSON',
    'S3_ACCESS_KEY_ID',
    'S3_ADDRESSING_STYLE',
    'S3_BUCKET_NAME',
    'S3_ENABLE_TAGGING',
    'S3_ENDPOINT_URL',
    'S3_KEY_PREFIX',
    'S3_REGION_NAME',
    'S3_SECRET_ACCESS_KEY',
    'S3_USE_ACCELERATE_ENDPOINT',
    'STORAGE_PROVIDER',
):
    setattr(config_module, name, None)
config_module.STORAGE_PROVIDER = 'local'
config_module.UPLOAD_DIR = Path(tempfile.gettempdir()) / 'aurapro-storage-tests'
sys.modules['open_webui.config'] = config_module

constants_module = types.ModuleType('open_webui.constants')
constants_module.ERROR_MESSAGES = types.SimpleNamespace(EMPTY_CONTENT='Empty content')
sys.modules['open_webui.constants'] = constants_module

module_path = OPEN_WEBUI / 'storage' / 'provider.py'
spec = importlib.util.spec_from_file_location('open_webui.storage.provider', module_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f'Unable to load {module_path}')

provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)
_portable_storage_name = provider._portable_storage_name


class PortableStorageNameTests(unittest.TestCase):
    def test_replaces_windows_invalid_time_characters(self):
        self.assertEqual(
            _portable_storage_name('recording_14 22:11.webm'),
            'recording_14 22_11.webm',
        )

    def test_keeps_compact_recording_timestamp(self):
        self.assertEqual(
            _portable_storage_name('Recording-20260814221159.webm'),
            'Recording-20260814221159.webm',
        )

    def test_guards_windows_reserved_device_names(self):
        self.assertEqual(_portable_storage_name('CON.txt'), '_CON.txt')

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            _portable_storage_name('../recording.webm')

    def test_local_upload_replaces_invalid_name_before_atomic_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_upload_dir = provider.UPLOAD_DIR
            provider.UPLOAD_DIR = Path(directory)
            try:
                result = provider.LocalStorageProvider.upload_file(
                    io.BytesIO(b'audio'),
                    'recording_14 22:11.webm',
                    {},
                )
            finally:
                provider.UPLOAD_DIR = previous_upload_dir

            stored_path = Path(result.path)
            self.assertEqual(stored_path.name, 'recording_14 22_11.webm')
            self.assertEqual(stored_path.read_bytes(), b'audio')


if __name__ == '__main__':
    unittest.main()
