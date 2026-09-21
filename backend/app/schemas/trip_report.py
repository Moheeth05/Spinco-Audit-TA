from datetime import date, datetime

from pydantic import BaseModel


class TripReportMonthSummary(BaseModel):
    report_month: str
    report_month_label: str
    report_count: int


class TripReportListItem(BaseModel):
    document_id: str
    trip_id: str | None
    filename: str | None
    original_filename: str | None
    employee_name: str | None
    employee_code: str | None
    start_date: date | None
    end_date: date | None
    origin: str | None
    destination: str | None
    approval_status: str | None
    processing_status: str
    error_message: str | None
    file_size: int | None
    page_count: int | None
    uploaded_at: datetime
