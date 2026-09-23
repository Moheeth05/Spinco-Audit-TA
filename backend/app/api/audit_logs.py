import math
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.logs import AuditLog
from app.models.user import User
from app.schemas.audit import AuditLogOut, AuditLogPageOut
from app.services.permissions import VIEW_AUDIT_LOGS, require_permission

router = APIRouter(prefix="/api/audit-logs", tags=["audit-logs"])

MAX_PAGE_SIZE = 200


@router.get("", response_model=AuditLogPageOut)
def list_audit_logs(
    page: int = 1,
    page_size: int = 50,
    user_id: int | None = None,
    action: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission(VIEW_AUDIT_LOGS)),
):
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    query = db.query(AuditLog)
    if user_id is not None:
        query = query.filter(AuditLog.user_id == user_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if date_from is not None:
        query = query.filter(AuditLog.created_at >= datetime.combine(date_from, time.min))
    if date_to is not None:
        query = query.filter(AuditLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min))

    total = query.count()
    rows = (
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    user_ids = {r.user_id for r in rows if r.user_id is not None}
    names = {}
    if user_ids:
        names = {u.id: u.name for u in db.query(User).filter(User.id.in_(user_ids)).all()}

    items = [
        AuditLogOut(
            id=r.id,
            user_id=r.user_id,
            user_name=names.get(r.user_id),
            action=r.action,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            details=r.details,
            created_at=r.created_at,
        )
        for r in rows
    ]

    return AuditLogPageOut(
        items=items, total=total, page=page, page_size=page_size, total_pages=max(1, math.ceil(total / page_size))
    )
