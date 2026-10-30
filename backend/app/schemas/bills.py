from datetime import date

from pydantic import BaseModel, ConfigDict


class BillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    page_number: int | None
    page_number_end: int | None
    vendor: str | None
    bill_number: str | None
    bill_date: date | None
    bill_date_raw: str | None = None
    bill_date_candidates: str | None = None
    bill_date_method: str | None = None
    bill_date_confidence: str | None = None
    transaction_reference: str | None = None
    transaction_time: str | None = None
    payment_status: str | None = None
    amount: float | None
    gstin: str | None
    description: str | None
    category_guess: str | None
    extraction_status: str
    extraction_confidence: str | None
    extraction_notes: str | None
    match_status: str
    matched_expense_id: int | None


class BillMatchCandidateOut(BaseModel):
    bill_id: int
    rank: int
    score: int
    reasons: str | None
    bill: BillOut | None = None


class ExpenseBillMatchOut(BaseModel):
    expense_id: int
    s_no: int | None
    category: str | None
    claimed_amount: float | None
    bill_match_status: str
    bill_match_score: int | None
    bill_match_confidence: str | None
    bill_match_reasons: str | None
    matched_bill: BillOut | None
    candidates: list[BillMatchCandidateOut]
