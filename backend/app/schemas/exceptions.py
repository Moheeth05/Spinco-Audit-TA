from datetime import date

from pydantic import BaseModel

from app.schemas.common import UTCDateTime


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
    policy_category: str | None = None
    expense_date: date | None
    bill_date: date | None
    bill_id: int | None
    trip_report_id: int | None
    source_document_id: str | None
    source_page: int | None
    status: str
    created_at: UTCDateTime
    verified_by_id: int | None = None
    verified_by_name: str | None = None
    verified_at: UTCDateTime | None = None
    verification_note: str | None = None


class VerifyExceptionIn(BaseModel):
    note: str | None = None


class ReportStatusOut(BaseModel):
    document_id: str
    overall_status: str | None
    total_expenses: int
    verified_expense_count: int
    exception_count: int
    review_required_count: int
