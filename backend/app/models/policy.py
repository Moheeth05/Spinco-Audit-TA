from sqlalchemy import Date, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PolicyMaster(Base):
    """One row per expense category. Values are transcribed verbatim from the
    company policy spec - DA / DNA / NR are stored as given, not reinterpreted."""

    __tablename__ = "policy_master"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    reimbursement_basis: Mapped[str] = mapped_column(String(20))  # "ACTUAL" or "LIMIT"
    limit_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    document_required: Mapped[str] = mapped_column(String(10))  # DA / DNA / YES / NO
    claim_type: Mapped[str] = mapped_column(String(20))  # NR / REGULAR
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    effective_date: Mapped[Date | None] = mapped_column(Date, nullable=True)
    policy_version: Mapped[int] = mapped_column(default=1)
