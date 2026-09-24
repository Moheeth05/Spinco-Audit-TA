from pydantic import BaseModel

from app.schemas.common import UTCDateTime


class UploadResultItem(BaseModel):
    filename: str
    success: bool
    document_id: str | None = None
    processing_status: str
    error_message: str | None = None
    page_count: int | None = None
    document_number: str | None = None  # ER-xxxxx / TRIP-xxxxx when recognized from filename
    extraction_status: str | None = None


class UploadBatchResponse(BaseModel):
    results: list[UploadResultItem]


class UploadListItem(BaseModel):
    document_id: str
    source_type: str  # EXPENSE_REPORT | TRIP_REPORT
    filename: str
    original_filename: str
    document_number: str | None
    person_name: str | None
    employee_code: str | None
    file_size: int | None
    page_count: int | None
    processing_status: str
    error_message: str | None
    extraction_status: str
    uploaded_at: UTCDateTime
