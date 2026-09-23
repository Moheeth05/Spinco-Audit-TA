from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.policy import PolicyMaster
from app.models.user import User
from app.schemas.policy import PolicyMasterOut
from app.services.permissions import get_current_user

router = APIRouter(prefix="/api/policy-master", tags=["policy"])


@router.get("", response_model=list[PolicyMasterOut])
def list_policies(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    rows = db.execute(select(PolicyMaster).order_by(PolicyMaster.category)).scalars().all()
    return rows
