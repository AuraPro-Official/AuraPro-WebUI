import base64
import os
import secrets
from pathlib import Path
from typing import Annotated
import ipaddress
import datetime

import typer
import uvicorn

app = typer.Typer()

KEY_FILE = Path.cwd() / '.webui_secret_key'
DEFAULT_HOST = '127.0.0.1'
FORWARDED_ALLOW_IPS = os.getenv('FORWARDED_ALLOW_IPS', '127.0.0.1,::1')


def version_callback(value: bool) -> None:
    if value:
        from open_webui.env import VERSION

        typer.echo(f'Open WebUI distribution version: {VERSION}')
        raise typer.Exit()


@app.command()
def main(
    version: Annotated[bool | None, typer.Option('--version', callback=version_callback)] = None,
):
    pass


@app.command()
def serve(
    host: str = DEFAULT_HOST,
    port: int = 8080,
    ssl_keyfile: str | None = None,
    ssl_certfile: str | None = None,
    ssl_autogen_dir: str | None = None,
    ssl_hosts: str = '',
):
    os.environ['FROM_INIT_PY'] = 'true'
    if not os.getenv('WEBUI_SECRET_KEY'):
        typer.echo('Loading WEBUI_SECRET_KEY from file, not provided as an environment variable.')
        if not KEY_FILE.exists():
            typer.echo(f'Generating a new secret key and saving it to {KEY_FILE}')
            KEY_FILE.write_bytes(base64.urlsafe_b64encode(secrets.token_bytes(32)))
            try:
                KEY_FILE.chmod(0o600)
            except OSError:
                pass
        typer.echo(f'Loading WEBUI_SECRET_KEY from {KEY_FILE}')
        os.environ['WEBUI_SECRET_KEY'] = KEY_FILE.read_text()

    if os.getenv('USE_CUDA_DOCKER', 'false') == 'true':
        typer.echo('CUDA is enabled, appending LD_LIBRARY_PATH to include torch/cudnn & cublas libraries.')
        LD_LIBRARY_PATH = os.getenv('LD_LIBRARY_PATH', '').split(':')
        os.environ['LD_LIBRARY_PATH'] = ':'.join(
            LD_LIBRARY_PATH
            + [
                '/usr/local/lib/python3.11/site-packages/torch/lib',
                '/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib',
            ]
        )
        try:
            import torch

            if not torch.cuda.is_available():
                raise RuntimeError('CUDA not available')
            typer.echo('CUDA seems to be working')
        except Exception as e:
            typer.echo(
                'Error when testing CUDA but USE_CUDA_DOCKER is true. '
                'Resetting USE_CUDA_DOCKER to false and removing '
                f'LD_LIBRARY_PATH modifications: {e}'
            )
            os.environ['USE_CUDA_DOCKER'] = 'false'
            os.environ['LD_LIBRARY_PATH'] = ':'.join(LD_LIBRARY_PATH)

    import open_webui.main  # noqa: F401
    from open_webui.env import UVICORN_WORKERS

    if ssl_autogen_dir and (not ssl_keyfile or not ssl_certfile):
        cert_dir = Path(ssl_autogen_dir)
        cert_dir.mkdir(parents=True, exist_ok=True)
        ssl_keyfile = str(cert_dir / 'aurapro-lan.key.pem')
        ssl_certfile = str(cert_dir / 'aurapro-lan.cert.pem')

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key_path = Path(ssl_keyfile)
        cert_path = Path(ssl_certfile)
        should_generate = not key_path.is_file() or not cert_path.is_file()
        now = datetime.datetime.now(datetime.timezone.utc)

        if not should_generate:
            try:
                existing_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
                existing_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
                cert_public_key = existing_cert.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                private_public_key = existing_key.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                expires_at = getattr(existing_cert, 'not_valid_after_utc', None)
                if expires_at is None:
                    expires_at = existing_cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
                should_generate = cert_public_key != private_public_key or expires_at <= now + datetime.timedelta(
                    days=30
                )
            except (OSError, TypeError, ValueError):
                should_generate = True

        if not should_generate:
            typer.echo(f'Reusing local HTTPS certificate: {ssl_certfile}')
        else:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            names = {'localhost', '127.0.0.1', '::1', host}
            names.update(item.strip() for item in ssl_hosts.split(',') if item.strip())

            alt_names = []
            for name in names:
                if not name:
                    continue
                try:
                    ip_address = ipaddress.ip_address(name)
                    if not ip_address.is_unspecified:
                        alt_names.append(x509.IPAddress(ip_address))
                except ValueError:
                    alt_names.append(x509.DNSName(name))

            subject = issuer = x509.Name(
                [
                    x509.NameAttribute(NameOID.COMMON_NAME, 'AuraPro Local Network'),
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'AuraPro'),
                ]
            )
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=825))
                .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
                .sign(key, hashes.SHA256())
            )

            key_path.write_bytes(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            typer.echo(f'Generated local HTTPS certificate: {ssl_certfile}')
    uvicorn.run(
        'open_webui.main:app',
        host=host,
        port=port,
        forwarded_allow_ips=FORWARDED_ALLOW_IPS,
        workers=UVICORN_WORKERS,
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
    )


@app.command()
def dev(
    host: str = DEFAULT_HOST,
    port: int = 8080,
    reload: bool = True,
):
    uvicorn.run(
        'open_webui.main:app',
        host=host,
        port=port,
        reload=reload,
        forwarded_allow_ips=FORWARDED_ALLOW_IPS,
    )


if __name__ == '__main__':
    app()
