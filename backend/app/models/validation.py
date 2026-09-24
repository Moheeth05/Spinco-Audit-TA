from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ValidationResult(Base):
    """One row per deterministic policy check performed against an expense
    (or, for the total check, the report as a whole - expense_id is null
    there). Replaced wholesale on every (re)validation run, the same
    pattern used for extraction/bill matching in earlier phases."""

    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int] = mapped_column(ForeignKey("expense_reports.id"), index=True)
    expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"), nullable=True, index=True)

    check_type: Mapped[str] = mapped_column(String(100))
    details: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # PASS | EXCEPTION | REVIEW_REQUIRED | NOT_APPLICABLE
    result: Mapped[str] = mapped_column(String(20))
    remarks: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Set only when result == EXCEPTION - OVER LIMIT | AMOUNT MISMATCH | DATE MISMATCH |
    # MISSING BILL | CATEGORY MISMATCH | DUPLICATE BILL | CATERING INCLUDED | TOTAL MISMATCH
    exception_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    claimed_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    policy_limit: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    bill_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    expense_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bill_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # The category actually used for the policy lookup (post-normalization) and the
    # exact policy_version in force at validation time - so a later Policy Master
    # edit never silently changes what a historical validation meant.
    policy_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    policy_version: Mapped[int | None] = mapped_column(nullable=True)

    source_document_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_page: Mapped[int | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class ExceptionRecord(Base):
    """One row per logical exception/review-required condition the Phase 7
    Exception Engine (services/exception_engine.py) derives from this
    report's ValidationResult rows. That module is the sole writer -
    extends, not duplicates, the Phase 5 scaffolding. Replaced wholesale on
    every (re)evaluation, same pattern as every other Phase 4-6 table."""

    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int] = mapped_column(ForeignKey("expense_reports.id"), index=True)
    expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"), nullable=True)

    exception_type: Mapped[str] = mapped_column(String(100))
    # EXCEPTION | REVIEW REQUIRED | INFORMATIONAL - derived directly from the
    # originating ValidationResult.result, never a subjective judgement.
    severity: Mapped[str] = mapped_column(String(20), default="EXCEPTION")
    reason: Mapped[str] = mapped_column(String(1000))
    source_pdf: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_page: Mapped[int | None] = mapped_column(nullable=True)

    # Traceability back to the exact evidence this exception was derived from.
    validation_result_id: Mapped[int | None] = mapped_column(ForeignKey("validation_results.id"), nullable=True)
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    trip_report_id: Mapped[int | None] = mapped_column(ForeignKey("trip_reports.id"), nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # OPEN | RESOLVED
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    # Populated from ExceptionVerification (below) each time this row is
    # rebuilt - see exception_engine.py's evaluate_report(). Never set
    # directly; ExceptionVerification is the durable source of truth that
    # survives this table's wholesale replacement on every (re)evaluation.
    verified_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class ExceptionVerification(Base):
    """Persists a human auditor's manual verification of a REVIEW REQUIRED
    condition across (re)evaluation cycles. ExceptionRecord itself is
    replaced wholesale on every (re)evaluation (see its own docstring
    above) - if verification lived only there, reprocessing a document
    would silently erase the fact that a human already reviewed and
    approved a condition (see the post-UAT correction spec's explicit "do
    not delete audit history" requirement). Keyed on the identity that
    actually survives regeneration - (expense_report_id, expense_id,
    exception_type) - never the ExceptionRecord row id, which changes
    every time exceptions are rebuilt.

    Only ever applies to a REVIEW REQUIRED condition, never a confirmed
    EXCEPTION - a verification is an auditor's judgement that a genuinely
    ambiguous condition has been manually checked and is acceptable, not
    permission to make a real policy violation disappear (see
    services/exception_engine.py, which enforces this).
    """

    __tablename__ = "exception_verifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_report_id: Mapped[int] = mapped_column(ForeignKey("expense_reports.id"), index=True)
    expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id"), nullable=True, index=True)
    exception_type: Mapped[str] = mapped_column(String(100))

    # What the condition looked like at the moment it was verified - kept
    # even though the live ExceptionRecord's own reason may later reword
    # itself (e.g. after an extraction-logic improvement), so the audit
    # trail always shows exactly what the auditor actually approved.
    original_severity: Mapped[str] = mapped_column(String(20))
    original_reason: Mapped[str] = mapped_column(String(1000))

    verified_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    verified_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    verification_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
