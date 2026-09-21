from datetime import date

from pydantic import BaseModel, ConfigDict


class PolicyMasterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    reimbursement_basis: str
    limit_amount: float | None
    document_required: str
    claim_type: str
    status: str
    effective_date: date | None
    policy_version: int
