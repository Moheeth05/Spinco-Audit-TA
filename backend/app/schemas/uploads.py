from pydantic import BaseModel

from app.schemas.common import UTCDateTime


class DuplicateOf(BaseModel):
    """The canonical (original) document an exact duplicate was recognised against."""

    document_id: str
    original_filename: str | None = None
    uploaded_at: UTCDateTime | None = None


class UploadResultItem(BaseModel):
    filename: str
    success: bool
    document_id: str | None = None
    processing_status: str
    error_message: str | None = None
    page_count: int | None = None
    document_number: str | None = None  # ER-xxxxx / TRIP-xxxxx when recognized from filename
    extraction_status: str | None = None
    # --- central storage phase (all additive) ---
    result: str | None = None  # SUCCESS | DUPLICATE | FAILED | DEFERRED  (DEFERRED: nothing was accepted because central storage is temporarily unavailable - retry)
    storage_state: str | None = None  # NOT_CENTRAL | PENDING | STORED | FAILED | ARCHIVED | MISSING_EXTERNAL
    attempt_id: int | None = None
    uploaded_at: UTCDateTime | None = None  # the server's authoritative receive time (UTC)
    uploaded_at_ist: str | None = None  # the same moment in IST (+05:30), for display
    duplicate_of: DuplicateOf | None = None
    retryable: bool | None = None


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
    # --- central storage phase (all additive) ---
    uploaded_at_ist: str | None = None
    uploaded_by_id: int | None = None
    uploaded_by_name: str | None = None
    storage_state: str | None = None  # NOT_CENTRAL | PENDING | STORED | FAILED | ARCHIVED | MISSING_EXTERNAL
    processing_state: str | None = None  # NONE | QUEUED | RUNNING | DONE | DEFERRED | FAILED (background extraction job)
    duplicate_count: int = 0  # exact duplicates recorded against this document


class UploadAttemptItem(BaseModel):
    id: int
    uploaded_at: UTCDateTime
    uploaded_at_ist: str | None = None
    uploaded_by_id: int | None = None
    uploaded_by_name: str | None = None
    original_filename: str
    file_size: int
    document_type: str
    result: str
    error_message: str | None = None
    duplicate_reason: str | None = None
    duplicate_of: DuplicateOf | None = None
    retained_copy: bool = False
    storage_state: str | None = None
    client: str | None = None
    sha256_short: str | None = None  # first 12 hex characters only
