import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.datastructures import MutableHeaders

from app import models  # noqa: F401  (register all models on Base before create_all)
from app.api import (
    analytics,
    audit_logs,
    auth,
    dashboard,
    documents,
    excel_files,
    exceptions,
    expense_reports,
    expenses,
    health,
    policy,
    search,
    storage,
    trip_reports,
    uploads,
    users,
)
from app.api import central
from app.database import IS_SQLITE, Base, SessionLocal, engine
from app.seed import seed_all
from app.logging_config import configure_logging
from app.services import central_storage, hardening, processing_jobs, startup_checks
from app.services.db_migrations import run_light_migrations

configure_logging()  # message redaction (always) + optional SPINCO_LOG_LEVEL / SPINCO_LOG_DIR

app = FastAPI(title="SPINCO TA / Travel Expense Audit & Automation System")

# Explicit development origins - never combined with allow_origins=["*"]
# while allow_credentials=True (the browser rejects wildcard-origin
# credentialed requests anyway, but being explicit here documents the
# constraint rather than relying on that browser behavior silently).
app.add_middleware(
    CORSMiddleware,
    allow_origins=hardening.cors_origins(),  # SPINCO_CORS_ORIGINS; unset = the Vite dev origins on a loopback backend, none on a network-reachable one
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
app.add_middleware(hardening.RequestSizeLimitMiddleware)  # an upload that declares a body above SPINCO_MAX_REQUEST_MB is refused with 413 before it is read

# Central backend: only answer requests addressed to the configured host NAME(S) (SPINCO_ALLOWED_HOSTS, comma separated). Unset (local desktop / development) = no restriction.
_allowed_hosts = startup_checks.allowed_hosts()
if _allowed_hosts:
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts + ["localhost", "127.0.0.1"])


@app.exception_handler(OperationalError)
async def database_busy(request, exc: OperationalError):
    """A write that could not get the SQLite lock in time is a TEMPORARY condition: answer 503 + Retry-After (the caller simply retries) instead of an opaque 500.
    Any other database error is not handled here and stays a 500, exactly as before."""
    if "database is locked" not in str(exc).lower():
        raise exc
    return JSONResponse(status_code=503, content={"detail": "The server is busy. Please try again in a moment."}, headers={"Retry-After": "2"})


class SchemaNotReady(RuntimeError):
    pass


def _verify_schema_strict() -> None:
    """Central modes (and SPINCO_SCHEMA_MODE=strict): the database must ALREADY be in the shape the code needs. Nothing is created, altered or repaired here - the schema
    is changed only on purpose, by an operator (backend/tools/init_central_staging_db.py for a new disposable database, or a migration run). Anything else refuses to start."""
    problems = central_storage.schema_problems()
    if problems:
        raise SchemaNotReady("The database schema is not ready for central storage (" + "; ".join(problems) + "). Nothing was changed. See docs/CENTRAL-BACKEND.md.")


@app.on_event("startup")
def on_startup():
    central = central_storage.config()
    # A server database (PostgreSQL) is ALWAYS strict: the application never creates or alters tables in it - the schema is created and migrated by an operator (see docs).
    strict = central.active or not IS_SQLITE or os.environ.get("SPINCO_SCHEMA_MODE", "").strip().lower() == "strict"
    if strict:
        _verify_schema_strict()  # never create_all / light migrations on a database that holds (or will hold) central records
    else:
        run_light_migrations(engine)
        Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        startup_checks.run_checks(db)  # a network-reachable backend refuses development defaults (see services/startup_checks.py)
    seed_all()
    if processing_jobs.worker_enabled():
        processing_jobs.start_worker()  # durable background work: extraction and the shadow copy to Drive; restart-safe (jobs are rows)


@app.on_event("shutdown")
def on_shutdown():
    processing_jobs.stop_worker()


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(policy.router)
app.include_router(users.router)
app.include_router(audit_logs.router)
app.include_router(dashboard.router)
app.include_router(expense_reports.router)
app.include_router(exceptions.router)
app.include_router(trip_reports.router)
app.include_router(excel_files.router)
app.include_router(analytics.router)
app.include_router(uploads.router)
app.include_router(documents.router)
app.include_router(expenses.router)
app.include_router(search.router)
app.include_router(storage.router)
app.include_router(central.router)


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
class NoStoreStaticFiles(StaticFiles):
    """Every build/install of the packaged app shares the same fixed
    localhost origin (Electron's BrowserWindow always points at
    127.0.0.1:<port>) AND the same persistent Chromium profile (Electron's
    userData path comes from the product name, which never changes between
    builds) - see docs/RELEASE_AND_UPDATES.md. Starlette's default
    StaticFiles response carries only ETag/Last-Modified, no Cache-Control,
    which makes it heuristically cacheable (RFC 7234 §4.2.2): the renderer
    is free to keep serving an OLD build's index.html/JS from its own disk
    cache indefinitely, never even asking this (fully up to date) server,
    regardless of which install/version is actually running. Confirmed as
    the real cause of "the new build is correct but the app still shows old
    behavior" reports. no-store, not a long-lived immutable cache, so every
    load is guaranteed to reflect whatever is actually installed."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


_frontend_dist = os.environ.get("SPINCO_FRONTEND_DIST")
if _frontend_dist:
    _dist_path = Path(_frontend_dist)
    _assets_dir = _dist_path / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", NoStoreStaticFiles(directory=_assets_dir), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str):
        candidate = _dist_path / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate, headers={"Cache-Control": "no-store"})
        # React Router client-side routes (e.g. /expense-reports) have no
        # matching file on disk - always fall back to index.html so a
        # hard refresh/direct load on a deep route works.
        return FileResponse(_dist_path / "index.html", headers={"Cache-Control": "no-store"})
