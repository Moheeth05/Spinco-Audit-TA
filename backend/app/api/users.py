from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.user import UserOut

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return db.execute(select(User).order_by(User.name)).scalars().all()


@router.get("/me", response_model=UserOut)
def current_user(db: Session = Depends(get_db)):
    """Stub identity endpoint. Real authentication/session handling is a later phase;
    for now the app operates as the single seeded ADMIN user."""
    user = db.execute(select(User).order_by(User.id)).scalars().first()
    return user
