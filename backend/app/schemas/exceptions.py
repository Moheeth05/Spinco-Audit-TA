from datetime import date, datetime

from pydantic import BaseModel


class ExceptionRecordOut(BaseModel):
    id: int
    expense_id: int | None
    s_no: int | None
    category: str | None
    merchant: str | None
    exception_type: str
    severity: str
    reason: str
    claimed_amount: float | None
    policy_limit: float | None
    bill_amount: float | None
    expense_date: date | None
    bill_date: date | None
    bill_id: int | None
    trip_report_id: int | None
    source_document_id: str | None
    source_page: int | None
    status: str
    created_at: datetime


class ReportStatusOut(BaseModel):
    document_id: str
    overall_status: str | None
    total_expenses: int
    verified_expense_count: int
    exception_count: int
    review_required_count: int
