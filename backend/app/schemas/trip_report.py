from datetime import date, datetime

from pydantic import BaseModel


class TripReportMonthSummary(BaseModel):
    report_month: str
    report_month_label: str
    report_count: int


class TripReportListItem(BaseModel):
    id: int
    trip_id: str
    filename: str | None
    employee_name: str | None
    employee_code: str | None
    start_date: date | None
    end_date: date | None
    origin: str | None
    destination: str | None
    approval_status: str | None
    uploaded_at: datetime
