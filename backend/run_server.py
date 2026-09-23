"""Entry point for the packaged (PyInstaller) desktop backend.

Started by the Electron main process (see electron/main.js) as a plain
subprocess - never used by the dev workflow, which keeps using
`uvicorn app.main:app --reload --port 8001` directly.
"""

import os
import sys

import uvicorn


def main() -> None:
    # PyInstaller's onedir bundle puts this executable next to its bundled
    # `app` package - make sure it's importable regardless of the process's
    # working directory when Electron spawns it.
    if getattr(sys, "frozen", False):
        sys.path.insert(0, os.path.dirname(sys.executable))

    port = int(os.environ.get("SPINCO_BACKEND_PORT", "8001"))
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
