import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.database import engine
from app.services import central_storage, processing_jobs, startup_checks

router = APIRouter(prefix="/api/health", tags=["health"])


def environment_label(env=None) -> str:
    """What this backend calls itself: the value of SPINCO_ENVIRONMENT (dev / staging / production ...), `local` for the standalone desktop backend (loopback, nothing
    set), `unspecified` for a network-reachable backend that was not given a name. The desktop app shows it next to the connection status."""
    env = os.environ if env is None else env
    value = str(env.get("SPINCO_ENVIRONMENT", "")).strip().lower()
    if value:
        return value[:32]
    return "unspecified" if startup_checks.network_exposed(env) else "local"


@router.get("")
def health():
    """Liveness (no authentication, no secrets, no database access): the process answers. `status` is the field the desktop shell waits for; `environment` and
    `central_storage` only say what KIND of backend this is, so a client can show 'Local' / 'Staging' / 'Production' truthfully."""
    return {"status": "ok", "environment": environment_label(), "central_storage": central_storage.config().mode}


@router.get("/ready")
def ready():
    """Readiness for monitoring (no authentication; booleans only - never a path, id, credential or error text): is the database reachable, is the schema ready, is the
    job worker running, is Google Drive configured. 200 when everything the current mode needs is in place, otherwise 503 with the same booleans."""
    checks: dict[str, bool] = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:  # noqa: BLE001 - reported as a boolean, the reason belongs in the server log
        checks["database"] = False
    cfg = central_storage.config()
    if cfg.active:
        try:
            checks["schema"] = bool(checks["database"] and not central_storage.schema_problems())
        except Exception:  # noqa: BLE001
            checks["schema"] = False
        if processing_jobs.worker_enabled():
            checks["job_worker"] = processing_jobs.worker_running()
        checks["drive_configured"] = central_storage.drive_configured()
    ok = all(checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ready" if ok else "not_ready", "checks": checks})
