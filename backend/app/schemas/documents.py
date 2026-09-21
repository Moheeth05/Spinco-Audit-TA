from datetime import date, datetime

from pydantic import BaseModel


class DocumentDetail(BaseModel):
    document_id: str
    source_type: str  # EXPENSE_REPORT | TRIP_REPORT
    filename: str
    original_filename: str
    file_type: str
    file_size: int | None
    page_count: int | None
    file_hash: str | None
    processing_status: str
    error_message: str | None
    uploaded_at: datetime

    document_number: str | None  # ER-xxxxx / TRIP-xxxxx
    person_name: str | None
    employee_code: str | None
    report_month_label: str | None
    submitted_on: date | None
