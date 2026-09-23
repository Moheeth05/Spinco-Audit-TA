from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import ROLES, User
from app.schemas.user import (
    AdminSetPasswordRequest,
    UserCreateRequest,
    UserOut,
    UserRoleChangeRequest,
    UserStatusChangeRequest,
    UserUpdateRequest,
)
from app.services import audit
from app.services.auth import hash_password, invalidate_all_sessions_for_user, validate_password_policy
from app.services.permissions import MANAGE_ROLES, MANAGE_USERS, require_permission

router = APIRouter(prefix="/api/users", tags=["users"])


def _active_admin_count(db: Session, *, excluding_user_id: int | None = None) -> int:
    q = db.query(User).filter(User.role == "ADMIN", User.is_active.is_(True))
    if excluding_user_id is not None:
        q = q.filter(User.id != excluding_user_id)
    return q.count()


@router.get("", response_model=list[UserOut])
def list_users(user: User = Depends(require_permission(MANAGE_USERS)), db: Session = Depends(get_db)):
    return db.execute(select(User).order_by(User.name)).scalars().all()


@router.post("", response_model=UserOut, status_code=201)
def create_user(
    payload: UserCreateRequest,
    actor: User = Depends(require_permission(MANAGE_USERS)),
    db: Session = Depends(get_db),
):
    email = payload.email.strip().lower()
    if "@" not in email or not payload.name.strip():
        raise HTTPException(status_code=400, detail="A valid name and email are required.")
    if payload.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {', '.join(ROLES)}.")
    if db.query(User).filter(User.email == email).first() is not None:
        raise HTTPException(status_code=409, detail="A user with this email already exists.")
    error = validate_password_policy(payload.password)
    if error:
        raise HTTPException(status_code=400, detail=error)

    new_user = User(
        name=payload.name.strip(),
        email=email,
        role=payload.role,
        password_hash=hash_password(payload.password),
        is_active=True,
        must_change_password=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    audit.log_action(
        db, user_id=actor.id, action=audit.USER_CREATED, entity_type="User", entity_id=str(new_user.id),
        details=f"Created user {new_user.email!r} with role {new_user.role}",
    )
    return new_user


@router.put("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    actor: User = Depends(require_permission(MANAGE_USERS)),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")

    changes = []
    if payload.name is not None and payload.name.strip() and payload.name.strip() != target.name:
        changes.append(f"name: {target.name!r} -> {payload.name.strip()!r}")
        target.name = payload.name.strip()
    if payload.email is not None:
        new_email = payload.email.strip().lower()
        if "@" not in new_email:
            raise HTTPException(status_code=400, detail="A valid email is required.")
        if new_email != target.email:
            if db.query(User).filter(User.email == new_email, User.id != target.id).first() is not None:
                raise HTTPException(status_code=409, detail="A user with this email already exists.")
            changes.append(f"email: {target.email!r} -> {new_email!r}")
            target.email = new_email

    db.commit()
    db.refresh(target)
    if changes:
        audit.log_action(
            db, user_id=actor.id, action=audit.USER_UPDATED, entity_type="User", entity_id=str(target.id),
            details="; ".join(changes),
        )
    return target


@router.put("/{user_id}/role", response_model=UserOut)
def change_role(
    user_id: int,
    payload: UserRoleChangeRequest,
    actor: User = Depends(require_permission(MANAGE_ROLES)),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if payload.role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of {', '.join(ROLES)}.")

    if target.role == "ADMIN" and payload.role != "ADMIN" and _active_admin_count(db, excluding_user_id=target.id) == 0:
        raise HTTPException(status_code=400, detail="Cannot change the role of the final active administrator.")

    old_role = target.role
    target.role = payload.role
    db.commit()
    db.refresh(target)

    audit.log_action(
        db, user_id=actor.id, action=audit.USER_ROLE_CHANGED, entity_type="User", entity_id=str(target.id),
        details=f"old_role={old_role}; new_role={target.role}",
    )
    return target


@router.put("/{user_id}/status", response_model=UserOut)
def change_status(
    user_id: int,
    payload: UserStatusChangeRequest,
    actor: User = Depends(require_permission(MANAGE_USERS)),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")

    if (
        not payload.is_active
        and target.role == "ADMIN"
        and target.is_active
        and _active_admin_count(db, excluding_user_id=target.id) == 0
    ):
        raise HTTPException(status_code=400, detail="Cannot deactivate the final active administrator.")

    target.is_active = payload.is_active
    db.commit()
    db.refresh(target)

    if not payload.is_active:
        invalidate_all_sessions_for_user(db, target.id)

    audit.log_action(
        db, user_id=actor.id,
        action=audit.USER_ACTIVATED if payload.is_active else audit.USER_DEACTIVATED,
        entity_type="User", entity_id=str(target.id),
    )
    return target


@router.put("/{user_id}/password")
def admin_set_password(
    user_id: int,
    payload: AdminSetPasswordRequest,
    actor: User = Depends(require_permission(MANAGE_USERS)),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    error = validate_password_policy(payload.new_password)
    if error:
        raise HTTPException(status_code=400, detail=error)

    target.password_hash = hash_password(payload.new_password)
    target.must_change_password = True
    db.commit()
    invalidate_all_sessions_for_user(db, target.id)

    # Never include the new password itself in the audit trail.
    audit.log_action(
        db, user_id=actor.id, action=audit.ADMIN_PASSWORD_RESET, entity_type="User", entity_id=str(target.id),
        details=f"Password reset by admin for {target.email!r}",
    )
    return {"detail": "Password reset."}
