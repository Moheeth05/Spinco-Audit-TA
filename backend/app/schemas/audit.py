from pydantic import BaseModel

from app.schemas.common import UTCDateTime


class AuditLogOut(BaseModel):
    id: int
    user_id: int | None
    user_name: str | None
    action: str
    entity_type: str
    entity_id: str | None
    details: str | None
    created_at: UTCDateTime


class AuditLogPageOut(BaseModel):
    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int
    total_pages: int
