import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

from app import models  # noqa: F401  (register all models on Base before create_all)
from app.api import (
    analytics,
    audit_logs,
    auth,
    dashboard,
    documents,
    excel_files,
    expense_reports,
    expenses,
    health,
    policy,
    search,
    trip_reports,
    uploads,
    users,
)
from app.database import Base, engine
from app.seed import seed_all
from app.services.db_migrations import run_light_migrations

app = FastAPI(title="SPINCO TA / Travel Expense Audit & Automation System")

# Explicit development origins - never combined with allow_origins=["*"]
# while allow_credentials=True (the browser rejects wildcard-origin
# credentialed requests anyway, but being explicit here documents the
# constraint rather than relying on that browser behavior silently).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5200", "http://127.0.0.1:5200"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware:
    """Plain ASGI middleware, deliberately NOT `@app.middleware("http")`
    (Starlette's BaseHTTPMiddleware) - BaseHTTPMiddleware buffers the
    request/response through an anyio memory stream, which is documented to
    deadlock a sync (`def`, not `async def`) route handler reading a large
    UploadFile body (e.g. a multi-page PDF) via FastAPI's threadpool
    executor. Confirmed here: file uploads for smaller PDFs went through
    fine, but a larger real Expense Report PDF deterministically hung the
    request forever (0% CPU - genuinely blocked, not slow) until this was
    changed to a raw ASGI middleware, which streams the body through
    untouched. No CSP here - a strict CSP would need to be built around
    Vite's dev-time inline module/HMR scripts and this app's runtime
    origins, and an incorrectly-scoped CSP silently breaking the dev
    frontend is worse than not shipping one yet.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "same-origin"
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


@app.on_event("startup")
def on_startup():
    run_light_migrations(engine)
    Base.metadata.create_all(bind=engine)
    seed_all()


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(policy.router)
app.include_router(users.router)
app.include_router(audit_logs.router)
app.include_router(dashboard.router)
app.include_router(expense_reports.router)
app.include_router(trip_reports.router)
app.include_router(excel_files.router)
app.include_router(analytics.router)
app.include_router(uploads.router)
app.include_router(documents.router)
app.include_router(expenses.router)
app.include_router(search.router)


# ---------- Phase 11: serve the production React build (desktop packaging) ----------
#
# Dev workflow is completely unaffected: SPINCO_FRONTEND_DIST is only ever set
# by the packaged Electron app (see electron/main.js), never by `uvicorn
# app.main:app --reload` during development, which continues to be paired
# with the separate Vite dev server on :5200 exactly as before. When it *is*
# set, this backend becomes a single self-contained localhost server Electron
# can point its BrowserWindow at - same-origin with the API, so the existing
# cookie-based session/CSRF design and the frontend's relative `/api/...`
# fetch calls work completely unchanged, with no extra CORS/config needed.
_frontend_dist = os.environ.get("SPINCO_FRONTEND_DIST")
if _frontend_dist:
    _dist_path = Path(_frontend_dist)
    _assets_dir = _dist_path / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str):
        candidate = _dist_path / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        # React Router client-side routes (e.g. /expense-reports) have no
        # matching file on disk - always fall back to index.html so a
        # hard refresh/direct load on a deep route works.
        return FileResponse(_dist_path / "index.html")
