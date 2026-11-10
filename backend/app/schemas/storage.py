from pydantic import BaseModel, ConfigDict

from app.schemas.common import UTCDateTime


class StorageRecordOut(BaseModel):
    """Deliberately has NO Drive id, location, checksum or URL: ordinary and admin users alike address documents by `id` only."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    original_filename: str
    size: int
    state: str
    last_error: str | None = None
    document_kind: str | None = None
    document_pk: int | None = None
    created_at: UTCDateTime
    stored_at: UTCDateTime | None = None
    archived_at: UTCDateTime | None = None
    restored_at: UTCDateTime | None = None
    missing_detected_at: UTCDateTime | None = None
    last_verified_at: UTCDateTime | None = None


class StorageRecordAdminOut(StorageRecordOut):
    """What ADMINISTRATORS additionally see about a record's upload progress. For anyone else these stay None (they are never filled in)."""

    failure_class: str | None = None
    attempts: int | None = None
    deferrals: int | None = None
    next_attempt_at: UTCDateTime | None = None
    needs_attention: str | None = None


class ProcessingJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_kind: str
    document_id: str
    job_type: str
    state: str
    attempts: int
    failure_class: str | None = None
    last_error: str | None = None
    next_attempt_at: UTCDateTime | None = None
    updated_at: UTCDateTime


class CentralStatusOut(BaseModel):
    mode: str  # off | shadow | serve  (anyone signed in may read this: the UI only needs to know which screens to show)
    duplicate_retention: str | None = None  # administrators only
    config_error: str | None = None  # administrators only
    storage_ready: bool | None = None  # administrators only
    job_counts: dict[str, int] | None = None  # administrators only
    worker_running: bool | None = None  # administrators only
    security_warnings: list[str] | None = None  # administrators only


class StorageStatusOut(BaseModel):
    enabled: bool
    configured: bool
    config_error: str | None = None
    counts: dict[str, int]
    schema_ok: bool = True  # False until the database migration for the storage tables has been applied (see app/services/drive/schema.py)
    schema_status: str = "ok"  # ok | needs_migration | absent | drift
