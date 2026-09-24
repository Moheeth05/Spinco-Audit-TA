from pydantic import BaseModel

from app.schemas.common import UTCDateTime


class DashboardKPIs(BaseModel):
    total_expense_reports: int
    total_trip_reports: int
    total_claimed_amount: float
    account_verified_reports: int
    exception_reports: int
    review_required_reports: int
    total_expenses: int


class MonthlyUploadPoint(BaseModel):
    month: str
    month_label: str
    expense_reports: int
    trip_reports: int


class MonthlyClaimedPoint(BaseModel):
    month: str
    month_label: str
    claimed_amount: float


class CategoryBreakdownPoint(BaseModel):
    category: str
    expense_count: int
    claimed_amount: float


class RecentUploadOut(BaseModel):
    document_id: str
    filename: str
    person_name: str | None
    file_type: str
    report_month_label: str | None
    status: str
    uploaded_at: UTCDateTime
