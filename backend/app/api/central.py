"""GET /api/central/status - which central-storage mode the backend runs in, so every client shows the right screens. Anyone signed in may read the mode (it decides what
the UI offers); configuration problems, queue counts and security warnings are administrators only."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.processing_job import ProcessingJob
from app.models.user import User
from app.schemas.storage import CentralStatusOut
from app.services import central_storage, processing_jobs, startup_checks
from app.services.permissions import ROLE_PERMISSIONS, VIEW_AUDIT_LOGS, get_current_user

router = APIRouter(prefix="/api/central", tags=["central"])


@router.get("/status", response_model=CentralStatusOut)
def central_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    cfg = central_storage.config()
    out = CentralStatusOut(mode=cfg.mode)
    if VIEW_AUDIT_LOGS not in ROLE_PERMISSIONS.get(user.role, set()):
        return out
    out.duplicate_retention = cfg.duplicate_retention
    out.config_error = cfg.error
    out.security_warnings = startup_checks.warnings()
    out.worker_running = processing_jobs._worker is not None
    if cfg.active:
        try:
            out.storage_ready = central_storage.status_details()["storage_ready"]
            out.job_counts = {state: count for state, count in db.execute(select(ProcessingJob.state, func.count()).group_by(ProcessingJob.state)).all()}
        except SQLAlchemyError:
            out.storage_ready = False
    return out
