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
    # Loopback unless an operator deliberately runs this as the shared CENTRAL backend (SPINCO_BIND_HOST=0.0.0.0 or a specific interface). A network-reachable
    # backend refuses to start with development defaults - see app/services/startup_checks.py and docs/CENTRAL-BACKEND.md.
    host = os.environ.get("SPINCO_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    # Behind a reverse proxy the client address comes from X-Forwarded-For. uvicorn already trusts a proxy on 127.0.0.1; SPINCO_TRUSTED_PROXY_IPS (comma separated)
    # names other proxies. Sign-in throttling and the audit trail then see the real client address instead of the proxy's.
    proxies = os.environ.get("SPINCO_TRUSTED_PROXY_IPS", "").strip()
    extra = {"forwarded_allow_ips": proxies} if proxies else {}
    uvicorn.run("app.main:app", host=host, port=port, log_level="info", **extra)


if __name__ == "__main__":
    main()
