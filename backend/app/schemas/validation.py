from datetime import date

from pydantic import BaseModel, ConfigDict

from app.schemas.common import UTCDateTime


class ValidationResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    expense_id: int | None
    check_type: str
    result: str
    exception_type: str | None
    details: str | None
    remarks: str | None
    claimed_amount: float | None
    policy_limit: float | None
    bill_amount: float | None
    expense_date: date | None
    bill_date: date | None
    policy_category: str | None
    policy_version: int | None
    source_document_id: str | None
    source_page: int | None
    created_at: UTCDateTime
