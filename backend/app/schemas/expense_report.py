from datetime import date, datetime

from pydantic import BaseModel


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
    overall_status: str | None
    processing_status: str
    error_message: str | None
    file_size: int | None
    page_count: int | None
    uploaded_at: datetime
    submitted_on: date | None
