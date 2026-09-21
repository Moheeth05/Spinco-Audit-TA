from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.policy import PolicyMaster
from app.schemas.policy import PolicyMasterOut

router = APIRouter(prefix="/api/policy-master", tags=["policy"])


@router.get("", response_model=list[PolicyMasterOut])
def list_policies(db: Session = Depends(get_db)):
    rows = db.execute(select(PolicyMaster).order_by(PolicyMaster.category)).scalars().all()
    return rows
