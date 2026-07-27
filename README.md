# AuraPro WebUI

AuraPro WebUI is the web interface package used by AuraPro.

This package is an AuraPro distribution/fork derived from [Open WebUI](https://github.com/open-webui/open-webui). It keeps the upstream Python namespace (`open_webui`) for compatibility with upstream code, plugins, migrations, and existing integrations, while the installable package and public command are named `aurapro-webui`.

AuraPro-specific changes focus on the AuraPro desktop workflow, local runtime integration, glossary-driven translation modes, speech features, and product simplification.

## Attribution

Portions of this project are based on Open WebUI.

- Upstream project: [open-webui/open-webui](https://github.com/open-webui/open-webui)
- Open WebUI copyright: Copyright (c) 2023- Open WebUI Inc. [Created by Timothy Jaeryang Baek]
- AuraPro WebUI distribution: maintained by AuraPro contributors

AuraPro is not endorsed by Open WebUI Inc. unless explicitly stated in writing. Open WebUI names, notices, and license history are retained so this distribution remains clear about its origin and applicable license terms.

## License

This repository contains code governed by multiple license terms inherited from Open WebUI.

See these files before redistributing source or binary builds:

- [LICENSE](./LICENSE)
- [LICENSE_NOTICE](./LICENSE_NOTICE)
- [LICENSE_HISTORY](./LICENSE_HISTORY)
- [NOTICE](./NOTICE)

The current upstream Open WebUI License requires retaining the Open WebUI copyright notice, license conditions, and Open WebUI branding except in the specific cases allowed by that license. Historical portions of the codebase retain the terms listed in `LICENSE_HISTORY`.

The AuraPro desktop application is licensed separately in the desktop project. This repository is the AuraPro WebUI package and follows the license files in this directory.

## Installation

AuraPro Desktop installs and manages this package automatically for most users.

For development or manual testing:

```bash
pip install aurapro-webui
aurapro-webui serve
```

The server starts on `http://localhost:8080` by default.

## Development

```bash
npm install
npm run dev
```

Build the frontend and wheel through the existing project build pipeline:

```bash
npm run build
python -m build
```

## Package Notes

- PyPI package name: `aurapro-webui`
- Console command: `aurapro-webui`
- Python module namespace: `open_webui` for upstream compatibility
- Upstream attribution: Open WebUI

If you copy changes from upstream Open WebUI, keep the upstream copyright, license, and attribution files intact.
