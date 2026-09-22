"""Phase 9 - read-only aggregation layer for the Home Dashboard and Analytics
page. Every function here only queries existing Phase 1-8 tables via SQL
aggregation (COUNT/SUM/GROUP BY) - it never re-runs bill matching, policy
validation, trip matching, or the exception engine, and never invents a
number that isn't derivable from the current database state.

Date/month convention (documented once, applied consistently everywhere in
this module - see the Phase 9 spec's explicit warning against mixing
created_at with business dates):

  - "Report month" for an Expense Report is `ExpenseReport.report_month`,
    the plurality month of its own expense lines' dates (computed once at
    extraction time in services/extraction/pipeline.py). This is the
    business month, NOT `uploaded_at`.
  - "Report month" for a Trip Report is `TripReport.report_month`, derived
    from its own `start_date`.
  - Every monthly-grouped metric in this module - including the exception
    monthly trend - groups by the parent ExpenseReport's `report_month`,
    never by `ValidationResult.created_at`/`ExceptionRecord.created_at`,
    so a chart showing "exceptions in June 2026" always means the same
    June 2026 as every other chart on these pages.
  - `month` filter parameters accept an exact "YYYY-MM" string (matching
    the stored representation exactly) - there is no underlying date-range
    column to filter against, so no from_date/to_date range filtering is
    implemented; that would be fabricating query capability the schema
    doesn't actually have.

Claimed-amount convention: report-level totals (KPIs, monthly claimed
trend) sum `ExpenseReport.total_claimed` - the document's own stated total
- exactly matching the convention already established by Phase 1's
dashboard/analytics endpoints, not `SUM(Expense.claimed_amount)`. The two
can differ on a report with an unresolved TOTAL MISMATCH exception; this
module never "fixes" that discrepancy, it just consistently uses the same
already-established source everywhere. Category-level breakdowns
necessarily use `Expense.claimed_amount` instead, since no category
concept exists at the report level - this is a different granularity, not
a different definition of "claimed amount".

Trip counts exclude `TripReport.is_embedded == True` rows everywhere,
matching the filter Phase 1's own dashboard/trip-reports endpoints already
apply (an embedded TRIP SUMMARY is evidence attached to its parent Expense
Report, not a standalone Trip Report document).
"""

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.expense_report import Expense, ExpenseReport
from app.models.trip import TripReport
from app.models.validation import ExceptionRecord, ValidationResult
from app.services.category_map import normalize_category

REPORT_STATUSES = ("ACCOUNT VERIFIED", "EXCEPTION", "REVIEW REQUIRED")
BILL_MATCH_STATUSES = ("MATCHED", "MULTIPLE_CANDIDATES", "NO_MATCH", "REVIEW_REQUIRED", "NOT_PROCESSED")
TRIP_MATCH_STATUSES = ("MATCHED", "MULTIPLE_CANDIDATES", "NO_MATCH", "REVIEW_REQUIRED", "NOT_PROCESSED")


def _month_label(report_month: str) -> str:
    import calendar

    year, month = report_month.split("-")
    return f"{calendar.month_abbr[int(month)]} {year}"


def _sorted_months(months: set[str]) -> list[str]:
    # "YYYY-MM" strings already sort chronologically as plain strings, but
    # doing it via int(year)/int(month) is explicit and immune to any future
    # change in the stored format.
    return sorted(months, key=lambda m: tuple(int(p) for p in m.split("-")))


def report_status_summary(db: Session, month: str | None = None) -> dict:
    stmt = select(ExpenseReport)
    if month:
        stmt = stmt.where(ExpenseReport.report_month == month)
    reports = db.execute(stmt).scalars().all()

    total = len(reports)
    by_status = {s: 0 for s in REPORT_STATUSES}
    for r in reports:
        if r.overall_status in by_status:
            by_status[r.overall_status] += 1
    total_claimed = sum(float(r.total_claimed) for r in reports if r.total_claimed is not None)

    # A live COUNT of actual Expense rows - not ExpenseReport.total_expenses
    # (a cached counter only ever written at extraction time), so this can
    # never drift from what's actually in the expenses table.
    expenses_count_stmt = select(func.count(Expense.id))
    if month:
        expenses_count_stmt = expenses_count_stmt.join(
            ExpenseReport, Expense.expense_report_id == ExpenseReport.id
        ).where(ExpenseReport.report_month == month)
    total_expenses = db.execute(expenses_count_stmt).scalar() or 0

    return {
        "total_reports": total,
        "account_verified_reports": by_status["ACCOUNT VERIFIED"],
        "exception_reports": by_status["EXCEPTION"],
        "review_required_reports": by_status["REVIEW REQUIRED"],
        "total_claimed_amount": round(total_claimed, 2),
        "total_expenses": total_expenses,
    }


def monthly_report_volume(db: Session) -> list[dict]:
    er_rows = dict(
        db.execute(
            select(ExpenseReport.report_month, func.count(ExpenseReport.id))
            .where(ExpenseReport.report_month.is_not(None))
            .group_by(ExpenseReport.report_month)
        ).all()
    )
    trip_rows = dict(
        db.execute(
            select(TripReport.report_month, func.count(TripReport.id))
            .where(TripReport.report_month.is_not(None), TripReport.is_embedded.is_(False))
            .group_by(TripReport.report_month)
        ).all()
    )
    months = _sorted_months(set(er_rows) | set(trip_rows))
    return [
        {
            "month": m,
            "month_label": _month_label(m),
            "expense_reports": er_rows.get(m, 0),
            "trip_reports": trip_rows.get(m, 0),
        }
        for m in months
    ]


def monthly_claimed_trend(db: Session) -> list[dict]:
    rows = db.execute(
        select(ExpenseReport.report_month, func.coalesce(func.sum(ExpenseReport.total_claimed), 0))
        .where(ExpenseReport.report_month.is_not(None))
        .group_by(ExpenseReport.report_month)
    ).all()
    months = _sorted_months({m for m, _ in rows})
    by_month = dict(rows)
    return [
        {"month": m, "month_label": _month_label(m), "claimed_amount": round(float(by_month[m]), 2)} for m in months
    ]


def category_breakdown(db: Session, month: str | None = None, status: str | None = None) -> list[dict]:
    stmt = (
        select(
            Expense.category,
            func.count(Expense.id),
            func.coalesce(func.sum(Expense.claimed_amount), 0),
        )
        .join(ExpenseReport, Expense.expense_report_id == ExpenseReport.id)
        .where(Expense.category.is_not(None))
    )
    if month:
        stmt = stmt.where(ExpenseReport.report_month == month)
    if status:
        stmt = stmt.where(ExpenseReport.overall_status == status)
    stmt = stmt.group_by(Expense.category)

    rows = db.execute(stmt).all()
    return [
        {"category": category, "expense_count": count, "claimed_amount": round(float(total), 2)}
        for category, count, total in rows
    ]


def exception_type_breakdown(db: Session, month: str | None = None) -> list[dict]:
    stmt = select(ExceptionRecord.exception_type, ExceptionRecord.severity, func.count(ExceptionRecord.id))
    if month:
        stmt = stmt.join(ExpenseReport, ExceptionRecord.expense_report_id == ExpenseReport.id).where(
            ExpenseReport.report_month == month
        )
    stmt = stmt.group_by(ExceptionRecord.exception_type, ExceptionRecord.severity)
    rows = db.execute(stmt).all()
    return [{"exception_type": t, "severity": sev, "count": c} for t, sev, c in rows]


def exception_monthly_trend(db: Session) -> list[dict]:
    # Grouped by the parent report's report_month (the business month), not
    # ExceptionRecord.created_at (when the row was last (re)computed) - see
    # module docstring.
    rows = db.execute(
        select(ExpenseReport.report_month, func.count(ExceptionRecord.id))
        .join(ExceptionRecord, ExceptionRecord.expense_report_id == ExpenseReport.id)
        .where(ExpenseReport.report_month.is_not(None), ExceptionRecord.severity == "EXCEPTION")
        .group_by(ExpenseReport.report_month)
    ).all()
    months = _sorted_months({m for m, _ in rows})
    by_month = dict(rows)
    return [{"month": m, "month_label": _month_label(m), "exception_count": by_month[m]} for m in months]


def bill_match_summary(db: Session, month: str | None = None) -> list[dict]:
    stmt = select(Expense.bill_match_status, func.count(Expense.id))
    if month:
        stmt = stmt.join(ExpenseReport, Expense.expense_report_id == ExpenseReport.id).where(
            ExpenseReport.report_month == month
        )
    stmt = stmt.group_by(Expense.bill_match_status)
    rows = dict(db.execute(stmt).all())
    # Report every status that has at least one row, in a stable order -
    # never fabricate a bucket that has zero underlying expenses just to
    # pad out a fixed list, but always show the four real matching outcomes
    # when any expense has one.
    ordered = [s for s in BILL_MATCH_STATUSES if s in rows] + [s for s in rows if s not in BILL_MATCH_STATUSES]
    return [{"status": s, "count": rows[s]} for s in ordered]


def trip_match_summary(db: Session, month: str | None = None) -> dict:
    stmt = select(Expense.category, Expense.trip_match_status)
    if month:
        stmt = stmt.join(ExpenseReport, Expense.expense_report_id == ExpenseReport.id).where(
            ExpenseReport.report_month == month
        )
    rows = db.execute(stmt).all()

    counts: dict[str, int] = defaultdict(int)
    total_batta = 0
    for category, trip_match_status in rows:
        if normalize_category(category) != "BATTA":
            continue
        total_batta += 1
        counts[trip_match_status] += 1

    matched = counts.get("MATCHED", 0)
    return {
        "total_batta_expenses": total_batta,
        "matched_batta_expenses": matched,
        "unmatched_batta_expenses": total_batta - matched,
        "by_status": [{"status": s, "count": counts[s]} for s in TRIP_MATCH_STATUSES if s in counts]
        + [{"status": s, "count": c} for s, c in counts.items() if s not in TRIP_MATCH_STATUSES],
    }


def trip_analytics(db: Session) -> dict:
    trips = db.execute(select(TripReport).where(TripReport.is_embedded.is_(False))).scalars().all()

    by_month: dict[str, int] = defaultdict(int)
    by_destination: dict[str, int] = defaultdict(int)
    batta_linked = 0
    trip_ids = [t.id for t in trips]
    linked_trip_ids: set[int] = set()
    if trip_ids:
        linked_trip_ids = {
            row[0]
            for row in db.execute(
                select(Expense.trip_report_id).where(Expense.trip_report_id.in_(trip_ids)).distinct()
            ).all()
        }

    for t in trips:
        if t.report_month:
            by_month[t.report_month] += 1
        if t.destination:
            by_destination[t.destination] += 1
        if t.id in linked_trip_ids:
            batta_linked += 1

    months = _sorted_months(set(by_month))
    return {
        "total_trips": len(trips),
        "batta_linked_trips": batta_linked,
        "by_month": [{"month": m, "month_label": _month_label(m), "trip_count": by_month[m]} for m in months],
        "by_destination": sorted(
            ({"destination": d, "trip_count": c} for d, c in by_destination.items()),
            key=lambda r: r["trip_count"],
            reverse=True,
        ),
    }


def policy_limit_analysis(db: Session, month: str | None = None) -> dict:
    stmt = select(ValidationResult.result, func.count(ValidationResult.id)).where(
        ValidationResult.check_type == "Policy Limit Check"
    )
    if month:
        stmt = stmt.join(ExpenseReport, ValidationResult.expense_report_id == ExpenseReport.id).where(
            ExpenseReport.report_month == month
        )
    stmt = stmt.group_by(ValidationResult.result)
    rows = dict(db.execute(stmt).all())

    over_limit_stmt = select(func.coalesce(func.sum(ValidationResult.claimed_amount), 0)).where(
        ValidationResult.check_type == "Policy Limit Check", ValidationResult.result == "EXCEPTION"
    )
    if month:
        over_limit_stmt = over_limit_stmt.join(
            ExpenseReport, ValidationResult.expense_report_id == ExpenseReport.id
        ).where(ExpenseReport.report_month == month)
    over_limit_claimed = db.execute(over_limit_stmt).scalar() or 0

    return {
        # NOT_APPLICABLE = ACTUAL-basis categories, which never have a fixed
        # numeric limit - counted separately, never treated as "evaluated
        # against a limit".
        "evaluated_fixed_limit": rows.get("PASS", 0) + rows.get("EXCEPTION", 0),
        "passed_within_limit": rows.get("PASS", 0),
        "over_limit": rows.get("EXCEPTION", 0),
        "review_required": rows.get("REVIEW_REQUIRED", 0),
        "not_applicable_actual_basis": rows.get("NOT_APPLICABLE", 0),
        "over_limit_claimed_amount": round(float(over_limit_claimed), 2),
    }


def recent_uploads(db: Session, limit: int = 10) -> list[dict]:
    ers = db.execute(select(ExpenseReport).order_by(ExpenseReport.uploaded_at.desc()).limit(limit)).scalars().all()
    trips = (
        db.execute(
            select(TripReport)
            .where(TripReport.is_embedded.is_(False))
            .order_by(TripReport.uploaded_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    items = [
        {
            "document_id": f"expense:{er.id}",
            "filename": er.original_filename or er.filename,
            "person_name": er.employee_name,
            "file_type": "Expense",
            "report_month_label": er.report_month_label,
            "status": er.overall_status or er.extraction_status,
            "uploaded_at": er.uploaded_at,
        }
        for er in ers
    ] + [
        {
            "document_id": f"trip:{t.id}",
            "filename": t.original_filename or t.trip_id,
            "person_name": t.employee_name,
            "file_type": "Trip",
            "report_month_label": t.report_month,
            "status": t.validation_status or t.extraction_status,
            "uploaded_at": t.uploaded_at,
        }
        for t in trips
    ]
    items.sort(key=lambda i: i["uploaded_at"], reverse=True)
    return items[:limit]
