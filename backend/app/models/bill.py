from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int] = mapped_column(ForeignKey("expense_reports.id"), index=True)

    page_number: Mapped[int | None] = mapped_column(nullable=True)
    # Equal to page_number (or NULL) for every single-page bill - the entire
    # real corpus, unchanged by this column's existence. Set to the LAST
    # page only when bill_grouping.py's conservative continuation evidence
    # grouped 2+ consecutive pages into one logical bill - page_number
    # itself stays the group's FIRST page, so every existing "View Source"
    # consumer (frontend jump-to-page, API) keeps working unchanged; this
    # column is the (coarse, page-range rather than per-field) provenance
    # record for a grouped bill's full page span.
    page_number_end: Mapped[int | None] = mapped_column(nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bill_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # NULL when the printed day/month order could not be established from
    # evidence - never a guess. The audit trail for how this date was read:
    #   bill_date_raw        the date exactly as printed ("07/02/2026")
    #   bill_date_candidates every valid reading, "|"-separated ISO dates
    #   bill_date_method     iso | month_name | numeric | cue | same_document | report_group | unresolved
    #   bill_date_confidence HIGH | MEDIUM (NULL when unresolved)
    bill_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bill_date_raw: Mapped[str | None] = mapped_column(String(40), nullable=True)
    bill_date_candidates: Mapped[str | None] = mapped_column(String(60), nullable=True)
    bill_date_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bill_date_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category_guess: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # Populated only for bills created via the document_classifier.py fallback
    # (a page bill_parser.py's own deterministic rules couldn't make sense of
    # at all - see bill_pipeline.py) - supporting evidence classification,
    # never a validation/matching decision on its own. NULL for every bill
    # extracted the normal deterministic way, which never needed this.
    # BILL | RECEIPT | TICKET | TRANSACTION_SCREENSHOT | INVOICE | OTHER
    document_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    document_type_confidence: Mapped[float | None] = mapped_column(nullable=True)  # 0.0-1.0, classifier's own estimate
    transaction_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # UPI / wallet screenshots (extraction/payment_evidence.py): COMPLETED |
    # PENDING | FAILED (NULL when the page prints no status) and the HH:MM
    # transaction time (NULL unless a clock time is actually readable).
    payment_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    transaction_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # A classifier-derived fallback source for Catering Check when the
    # deterministic regex (category_map.detect_catering_amount) finds
    # nothing on this bill's raw_text - see validation/checks.py::check_catering.
    catering_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    # Full-text evidence (raw_text is capped at 2,000 characters, a charge can be
    # printed after that): bill_parser.parse_bill_text reads the whole bill and
    # stores the catering / lodging room-service-food charge. For an ordinary
    # bill catering_amount above is now also written from the full text (the
    # classifier fallback still owns it for document_type bills). The *_mentioned
    # flags record that the charge is on the bill even when its amount could not
    # be read - never a guessed amount. room_service_food_amount is the RECONCILED
    # total of every food/room-service charge row (see category_map.
    # analyze_room_service_food); NULL with the flag set means the charges could
    # not be reconciled (or no amount was readable) and nothing may be excluded -
    # for a row carrying the flag that stored NULL is final, it never falls back
    # to raw_text. NULL flag = a row extracted before these
    # columns existed: consumers then fall back to detecting on raw_text (see
    # category_map.resolve_*).
    catering_mentioned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    room_service_food_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    room_service_food_mentioned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Phase 4 extraction outcome for this bill record only - EXTRACTED | REVIEW_REQUIRED.
    # Separate from match_status below, which describes matching against an expense.
    extraction_status: Mapped[str] = mapped_column(String(20), default="REVIEW_REQUIRED")
    extraction_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)  # HIGH | MEDIUM | LOW
    extraction_notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # UNMATCHED | MATCHED - whether some expense has claimed this bill as its best match.
    # (The DUPLICATE business exception itself is a later validation-phase concern.)
    match_status: Mapped[str] = mapped_column(String(20), default="UNMATCHED")
    # Was a plain, unconstrained int (no referential integrity at all) until
    # Phase 1 hardening - confirmed zero orphaned values in dev/production
    # before adding this FK (see docs/CENTRALIZED_ARCHITECTURE.md).
    matched_expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"), nullable=True)

    expense_report: Mapped["ExpenseReport"] = relationship(back_populates="bills")
