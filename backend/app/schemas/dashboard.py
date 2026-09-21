from datetime import datetime

from pydantic import BaseModel


class DashboardKPIs(BaseModel):
    total_expense_reports: int
    total_trip_reports: int
    total_claimed_amount: float
    account_verified_reports: int
    exception_reports: int


class MonthlyUploadPoint(BaseModel):
    month: str
    expense_reports: int
    trip_reports: int


class MonthlyClaimedPoint(BaseModel):
    month: str
    claimed_amount: float


class CategoryBreakdownPoint(BaseModel):
    category: str
    claimed_amount: float


class RecentUploadOut(BaseModel):
    filename: str
    person_name: str | None
    file_type: str
    report_month_label: str | None
    status: str
    uploaded_at: datetime
