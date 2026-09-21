from datetime import date, datetime

from pydantic import BaseModel


class ExpenseReportMonthSummary(BaseModel):
    report_month: str
    report_month_label: str
    report_count: int


class ExpenseReportListItem(BaseModel):
    id: int
    er_id: str
    filename: str
    employee_name: str | None
    employee_code: str | None
    report_month_label: str | None
    total_expenses: int
    total_claimed: float | None
    verified_expense_count: int
    exception_count: int
    overall_status: str
    uploaded_at: datetime
    submitted_on: date | None
