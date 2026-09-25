from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TripMatchCandidate(Base):
    """Top-N scored (expense, trip) candidates from the Batta-to-Trip matcher -
    mirrors BillMatchCandidate exactly. Always populated (even for a clean
    MATCHED outcome, where there's just one), so the UI can show why a trip
    was picked over the alternatives, and so MULTIPLE_CANDIDATES has real
    candidates to show for auditor review. Replaced wholesale on every
    (re)match run."""

    __tablename__ = "trip_match_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"), index=True)
    trip_report_id: Mapped[int] = mapped_column(ForeignKey("trip_reports.id"), index=True)
    rank: Mapped[int] = mapped_column()
    score: Mapped[int] = mapped_column()
    reasons: Mapped[str | None] = mapped_column(String(1000), nullable=True)
