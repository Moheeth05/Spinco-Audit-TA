from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.bill import Bill
from app.models.bill_match import BillMatchCandidate
from app.models.expense_report import Expense
from app.schemas.bills import BillMatchCandidateOut, BillOut, ExpenseBillMatchOut

router = APIRouter(prefix="/api/expenses", tags=["expenses"])


@router.get("/{expense_id}/bill-match", response_model=ExpenseBillMatchOut)
def get_bill_match(expense_id: int, db: Session = Depends(get_db)):
    expense = db.get(Expense, expense_id)
    if expense is None:
        raise HTTPException(status_code=404, detail="Expense not found.")

    matched_bill = db.get(Bill, expense.bill_id) if expense.bill_id else None

    candidate_rows = db.execute(
        select(BillMatchCandidate).where(BillMatchCandidate.expense_id == expense_id).order_by(BillMatchCandidate.rank)
    ).scalars().all()
    candidates = []
    for c in candidate_rows:
        bill = db.get(Bill, c.bill_id)
        candidates.append(
            BillMatchCandidateOut(
                bill_id=c.bill_id,
                rank=c.rank,
                score=c.score,
                reasons=c.reasons,
                bill=BillOut.model_validate(bill) if bill else None,
            )
        )

    return ExpenseBillMatchOut(
        expense_id=expense.id,
        s_no=expense.s_no,
        category=expense.category,
        claimed_amount=expense.claimed_amount,
        bill_match_status=expense.bill_match_status,
        bill_match_score=expense.bill_match_score,
        bill_match_confidence=expense.bill_match_confidence,
        bill_match_reasons=expense.bill_match_reasons,
        matched_bill=BillOut.model_validate(matched_bill) if matched_bill else None,
        candidates=candidates,
    )
