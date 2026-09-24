from datetime import date

from pydantic import BaseModel

from app.schemas.common import UTCDateTime


class ExpenseReportMonthSummary(BaseModel):
    report_month: str
    report_month_label: str
    report_count: int


class ExpenseReportListItem(BaseModel):
    document_id: str
    er_id: str | None
    filename: str
    original_filename: str
    employee_name: str | None
    employee_code: str | None
    report_month_label: str | None
    total_expenses: int
    total_claimed: float | None
    verified_expense_count: int
    exception_count: int
    review_required_count: int
    overall_status: str | None
    processing_status: str
    error_message: str | None
    extraction_status: str
    extraction_confidence: str | None
    file_size: int | None
    page_count: int | None
    uploaded_at: UTCDateTime
    submitted_on: date | None


class ExpenseLineItemOut(BaseModel):
    id: int
    s_no: int | None
    expense_date: date | None
    category: str | None
    merchant: str | None
    expense_details: str | None
    claimed_amount: float | None
    source_page: int | None
    extraction_status: str | None

    bill_found: bool
    bill_amount: float | None
    bill_date: date | None
    bill_match_status: str
    bill_match_score: int | None
    bill_match_confidence: str | None

    policy_basis: str | None
    policy_limit: float | None
    validation_status: str | None

    trip_report_id: int | None
    trip_match_status: str
    trip_match_score: int | None
    trip_match_confidence: str | None
    trip_employee_mismatch: bool
