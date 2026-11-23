from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from open_webui import cli


class LocalHttpsCertificateTest(unittest.TestCase):
    def test_restart_reuses_valid_certificate_and_only_replaces_it_after_expiry(self):
        now = datetime.now(timezone.utc)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'AuraPro Local Network')])
        env_module = types.ModuleType('open_webui.env')
        env_module.UVICORN_WORKERS = 1
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                'sys.modules', {'open_webui.main': types.ModuleType('open_webui.main'), 'open_webui.env': env_module}
            ),
            patch.dict('os.environ', {'WEBUI_SECRET_KEY': 'isolated-local-https-test'}),
            patch.object(cli.uvicorn, 'run') as serve,
        ):
            cert_path = Path(directory) / 'aurapro-lan.cert.pem'
            key_path = Path(directory) / 'aurapro-lan.key.pem'
            key_bytes = key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
            for valid_for in (timedelta(days=7), timedelta(days=-1)):
                with self.subTest(valid_for=valid_for):
                    saved = (
                        x509.CertificateBuilder()
                        .subject_name(name)
                        .issuer_name(name)
                        .public_key(key.public_key())
                        .serial_number(x509.random_serial_number())
                        .not_valid_before(now - timedelta(days=10))
                        .not_valid_after(now + valid_for)
                        .sign(key, hashes.SHA256())
                        .public_bytes(serialization.Encoding.PEM)
                    )
                    cert_path.write_bytes(saved)
                    key_path.write_bytes(key_bytes)
                    cli.serve(ssl_autogen_dir=directory, ssl_hosts='192.0.2.1')
                    if valid_for.days > 0:
                        self.assertEqual(cert_path.read_bytes(), saved)
                        self.assertEqual(key_path.read_bytes(), key_bytes)
                        self.assertFalse((Path(directory) / 'trusted').exists())
                    else:
                        self.assertNotEqual(cert_path.read_bytes(), saved)
                        replaced = x509.load_pem_x509_certificate(saved)
                        fingerprint = replaced.fingerprint(hashes.SHA256()).hex().upper()
                        self.assertEqual((Path(directory) / 'trusted' / f'{fingerprint}.pem').read_bytes(), saved)
                    self.assertEqual(serve.call_args.kwargs['ssl_certfile'], str(cert_path))
