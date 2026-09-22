from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.bill import Bill
from app.models.bill_match import BillMatchCandidate
from app.models.expense_report import Expense
from app.models.trip import TripReport
from app.models.trip_match import TripMatchCandidate
from app.schemas.bills import BillMatchCandidateOut, BillOut, ExpenseBillMatchOut
from app.schemas.trip_match import ExpenseTripMatchOut, TripCandidateSummaryOut, TripMatchCandidateOut
from app.models.validation import ValidationResult
from app.schemas.validation import ValidationResultOut

router = APIRouter(prefix="/api/expenses", tags=["expenses"])


@router.get("/{expense_id}/validations", response_model=list[ValidationResultOut])
def get_expense_validations(expense_id: int, db: Session = Depends(get_db)):
    expense = db.get(Expense, expense_id)
    if expense is None:
        raise HTTPException(status_code=404, detail="Expense not found.")
    results = db.execute(
        select(ValidationResult).where(ValidationResult.expense_id == expense_id).order_by(ValidationResult.id)
    ).scalars().all()
    return results


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


@router.get("/{expense_id}/trip-match", response_model=ExpenseTripMatchOut)
def get_trip_match(expense_id: int, db: Session = Depends(get_db)):
    expense = db.get(Expense, expense_id)
    if expense is None:
        raise HTTPException(status_code=404, detail="Expense not found.")

    matched_trip = db.get(TripReport, expense.trip_report_id) if expense.trip_report_id else None

    candidate_rows = db.execute(
        select(TripMatchCandidate).where(TripMatchCandidate.expense_id == expense_id).order_by(TripMatchCandidate.rank)
    ).scalars().all()
    candidates = []
    for c in candidate_rows:
        trip = db.get(TripReport, c.trip_report_id)
        candidates.append(
            TripMatchCandidateOut(
                trip_report_id=c.trip_report_id,
                rank=c.rank,
                score=c.score,
                reasons=c.reasons,
                trip=TripCandidateSummaryOut.model_validate(trip) if trip else None,
            )
        )

    return ExpenseTripMatchOut(
        expense_id=expense.id,
        s_no=expense.s_no,
        category=expense.category,
        expense_date=expense.expense_date,
        claimed_amount=expense.claimed_amount,
        trip_match_status=expense.trip_match_status,
        trip_match_score=expense.trip_match_score,
        trip_match_confidence=expense.trip_match_confidence,
        trip_match_reasons=expense.trip_match_reasons,
        trip_employee_mismatch=expense.trip_employee_mismatch,
        matched_trip=TripCandidateSummaryOut.model_validate(matched_trip) if matched_trip else None,
        candidates=candidates,
    )
