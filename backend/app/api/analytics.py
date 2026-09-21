from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.expense_report import Expense, ExpenseReport
from pydantic import BaseModel

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class AnalyticsSummary(BaseModel):
    total_reports: int
    verified_reports: int
    exception_reports: int
    verification_rate: float
    total_claimed: float


class EmployeeClaimedPoint(BaseModel):
    employee_name: str
    claimed_amount: float


@router.get("/summary", response_model=AnalyticsSummary)
def summary(db: Session = Depends(get_db)):
    total = db.execute(select(func.count(ExpenseReport.id))).scalar() or 0
    verified = db.execute(
        select(func.count(ExpenseReport.id)).where(ExpenseReport.overall_status == "ACCOUNT VERIFIED")
    ).scalar() or 0
    exceptions = db.execute(
        select(func.count(ExpenseReport.id)).where(ExpenseReport.overall_status == "EXCEPTION")
    ).scalar() or 0
    total_claimed = db.execute(select(func.coalesce(func.sum(ExpenseReport.total_claimed), 0))).scalar() or 0

    return AnalyticsSummary(
        total_reports=total,
        verified_reports=verified,
        exception_reports=exceptions,
        verification_rate=round((verified / total) * 100, 1) if total else 0.0,
        total_claimed=float(total_claimed),
    )


@router.get("/employee-claimed", response_model=list[EmployeeClaimedPoint])
def employee_claimed(db: Session = Depends(get_db)):
    rows = db.execute(
        select(ExpenseReport.employee_name, func.coalesce(func.sum(ExpenseReport.total_claimed), 0))
        .where(ExpenseReport.employee_name.is_not(None))
        .group_by(ExpenseReport.employee_name)
        .order_by(ExpenseReport.employee_name)
    ).all()
    return [EmployeeClaimedPoint(employee_name=n, claimed_amount=float(total)) for n, total in rows]
