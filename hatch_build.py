# noqa: INP001
import os
import shutil
import subprocess
from pathlib import Path
from sys import stderr

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        super().initialize(version, build_data)
        stderr.write('>>> Building Open WebUI frontend\n')
        npm = shutil.which('npm')
        if npm is None:
            raise RuntimeError('NodeJS `npm` is required for building Open WebUI but it was not found')
        stderr.write('### npm install\n')
        subprocess.run([npm, 'install', '--force'], check=True)  # noqa: S603
        stderr.write('\n### npm run build\n')
        os.environ['APP_BUILD_HASH'] = version
        subprocess.run([npm, 'run', 'build'], check=True)  # noqa: S603
        self._prune_frontend_build()

    def _prune_frontend_build(self):
        build_dir = Path('build')
        if not build_dir.exists():
            return

        removed_bytes = 0
        removed_files = 0
        patterns = [
            '**/*.map',
        ]

        stderr.write('\n### Pruning frontend files not needed at runtime\n')
        for pattern in patterns:
            for file_path in build_dir.glob(pattern):
                if not file_path.is_file():
                    continue
                try:
                    removed_bytes += file_path.stat().st_size
                    file_path.unlink()
                    removed_files += 1
                except OSError as e:
                    stderr.write(f'Failed to remove {file_path}: {e}\n')

        if removed_files:
            removed_mb = removed_bytes / 1024 / 1024
            stderr.write(f'Removed {removed_files} frontend build files ({removed_mb:.1f} MB)\n')
        else:
            stderr.write('No frontend build files pruned\n')
