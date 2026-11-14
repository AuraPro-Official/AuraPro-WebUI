import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
PROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))


def requirements(path):
    return [
        Requirement(line.split(' #', 1)[0])
        for line in (ROOT / path).read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.startswith(('#', '-'))
    ]


def normalized(req):
    return (canonicalize_name(req.name), str(req.specifier), str(req.marker))


class SecurityDependenciesTest(unittest.TestCase):
    def test_wheel_metadata_enforces_upgraded_transitive_floors(self):
        project = {canonicalize_name(r.name): r for r in map(Requirement, PROJECT['project']['dependencies'])}
        for name, minimum in {
            'dnspython': '2.9.2',
            'filelock': '4.2.1',
            'joserfc': '1.7.6',
            'langgraph': '1.2.14',
            'langgraph-sdk': '0.4.6',
            'msgpack': '1.2.3',
            'oauthlib': '4.0.1',
            'opentelemetry-api': '1.45.2',
            'pyasn1': '0.6.5',
            'soupsieve': '4.0.0',
        }.items():
            req = project[name]
            self.assertTrue(req.specifier.contains(minimum), name)
            self.assertFalse(req.specifier.contains('0.0.1'), name)

    def test_docker_includes_the_requirements_constraint_file(self):
        dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
        copies = [line for line in dockerfile.splitlines() if line.startswith('COPY ') and 'requirements.txt' in line]
        self.assertTrue(any('./backend/requirements-security.txt' in line for line in copies))
        self.assertNotIn("'torch<=2.9.1'", dockerfile)
        self.assertEqual(
            dockerfile.count("'torch==2.13.0' torchvision torchaudio -r requirements.txt --torch-backend"), 2
        )
        self.assertIn('ARG USE_CUDA_VER=cu130', dockerfile)

    def test_pip_and_uv_constraints_agree(self):
        uv = {normalized(Requirement(r)) for r in PROJECT['tool']['uv']['constraint-dependencies']}
        pip = {normalized(r) for r in requirements('backend/requirements-security.txt')}
        self.assertEqual(uv, pip)
        for file in ('requirements.txt', 'requirements-min.txt'):
            self.assertIn('-c requirements-security.txt', (ROOT / 'backend' / file).read_text())

    def test_shared_direct_pins_agree_on_supported_platforms(self):
        project = [Requirement(r) for r in PROJECT['project']['dependencies']]
        for platform, machine in [('win32', 'AMD64'), ('darwin', 'x86_64'), ('darwin', 'arm64'), ('linux', 'aarch64')]:
            env = {'sys_platform': platform, 'platform_machine': machine, 'python_version': '3.12'}
            expected = {
                canonicalize_name(r.name): str(r.specifier) for r in project if not r.marker or r.marker.evaluate(env)
            }
            for file in ('requirements.txt', 'requirements-min.txt'):
                for req in requirements(f'backend/{file}'):
                    name = canonicalize_name(req.name)
                    if name in expected and req.specifier and (not req.marker or req.marker.evaluate(env)):
                        self.assertEqual(str(req.specifier), expected[name], f'{file}: {name} on {platform}/{machine}')

    def test_lock_contains_patched_pypdf_and_preserves_intel_crypto_exception(self):
        lock = tomllib.loads((ROOT / 'uv.lock').read_text(encoding='utf-8'))
        versions = {}
        for pkg in lock['package']:
            if 'version' in pkg:
                versions.setdefault(pkg['name'], set()).add(pkg['version'])
        floors = {
            'authlib': '1.9.1',
            'pypdf': '6.21.4',
            'fpdf2': '2.8.8',
            'stanza': '1.15.0',
            'dnspython': '2.9.2',
            'filelock': '4.2.1',
            'joserfc': '1.7.6',
            'langgraph': '1.2.14',
            'langgraph-sdk': '0.4.6',
            'msgpack': '1.2.3',
            'oauthlib': '4.0.1',
            'opentelemetry-api': '1.45.2',
            'pyasn1': '0.6.5',
            'pymongo': '4.18.3',
            'soupsieve': '4.0.0',
        }
        for name, minimum in floors.items():
            self.assertTrue(all(Version(v) >= Version(minimum) for v in versions[name]), name)
        self.assertEqual(versions['cryptography'], {'48.0.1', '50.0.2'})
        self.assertTrue(all(Version(v) >= Version('1.2.1.0') for v in versions['brotlicffi']))

    def test_torch_floor_does_not_break_legacy_macos(self):
        constraint = next(
            Requirement(r) for r in PROJECT['tool']['uv']['constraint-dependencies'] if Requirement(r).name == 'torch'
        )
        for machine in ('x86_64', 'arm64'):
            self.assertFalse(constraint.marker.evaluate({'sys_platform': 'darwin', 'platform_machine': machine}))
        self.assertTrue(constraint.marker.evaluate({'sys_platform': 'win32'}))


if __name__ == '__main__':
    unittest.main()
