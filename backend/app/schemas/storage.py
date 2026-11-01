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


class StorageStatusOut(BaseModel):
    enabled: bool
    configured: bool
    config_error: str | None = None
    counts: dict[str, int]
