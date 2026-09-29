"""Regression test for the packaged-app stale-frontend bug: every build/
install shares one fixed localhost origin and one persistent Chromium
profile (Electron's userData path comes from the product name, not the
install location), so a frontend response with no Cache-Control header is
heuristically cacheable (RFC 7234 §4.2.2) - the renderer can keep serving an
OLD build's index.html/JS from its own disk cache indefinitely, never even
asking this (fully up to date) server. See app/main.py's NoStoreStaticFiles
and serve_frontend for the fix: every frontend-serving response must carry
Cache-Control: no-store.

app.main only registers the frontend-serving routes when SPINCO_FRONTEND_DIST
is set (see its own comment - never set by the plain `uvicorn --reload` dev
workflow), so this test builds a throwaway dist/ directory and reloads the
module with that env var set, rather than relying on the shared `client`
fixture's app instance (which never has these routes registered).
"""

import importlib

from fastapi.testclient import TestClient


def test_frontend_responses_are_not_cached(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    assets_dir = dist / "assets"
    assets_dir.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body>test</body></html>")
    bundle_name = "index-testhash.js"
    (assets_dir / bundle_name).write_text("console.log('test bundle');")

    monkeypatch.setenv("SPINCO_FRONTEND_DIST", str(dist))
    import app.main as main_module

    importlib.reload(main_module)
    try:
        with TestClient(main_module.app) as c:
            index_resp = c.get("/")
            assert index_resp.status_code == 200
            assert index_resp.headers.get("cache-control") == "no-store", (
                f"GET / must send Cache-Control: no-store, got {index_resp.headers.get('cache-control')!r}"
            )

            # A React Router client-side path with no matching file on disk
            # falls back to index.html - must be equally uncached.
            spa_route_resp = c.get("/expense-reports")
            assert spa_route_resp.status_code == 200
            assert spa_route_resp.headers.get("cache-control") == "no-store"

            asset_resp = c.get(f"/assets/{bundle_name}")
            assert asset_resp.status_code == 200
            assert asset_resp.headers.get("cache-control") == "no-store", (
                f"GET /assets/<bundle> must send Cache-Control: no-store, got {asset_resp.headers.get('cache-control')!r}"
            )
    finally:
        monkeypatch.delenv("SPINCO_FRONTEND_DIST", raising=False)
        importlib.reload(main_module)
