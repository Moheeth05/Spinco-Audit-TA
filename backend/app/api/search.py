"""App-wide search - one endpoint the top header's search box calls with
whatever partial text the user has typed. Every result is real data drawn
directly from the same tables the rest of the app reads (no separate search
index, nothing pre-baked), and every result carries enough information for
the frontend to navigate straight to it.

Deliberately keeps merchant and category as separate result kinds (never
merged into one "text matched somewhere" bucket) so a search for a vendor
name is never presented as if it matched a Policy Master category, or vice
versa - see models/expense_report.py's Expense.merchant for why these two
fields must stay distinct everywhere in the system.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.expense_report import Expense, ExpenseReport
from app.models.policy import PolicyMaster
from app.models.trip import TripReport
from app.models.user import User
from app.models.validation import ExceptionRecord
from app.schemas.search import SearchResponse, SearchResultItem
from app.services.permissions import VIEW_DOCUMENTS, require_permission

router = APIRouter(prefix="/api/search", tags=["search"])
_view = Depends(require_permission(VIEW_DOCUMENTS))

MIN_QUERY_LENGTH = 2
PER_KIND_LIMIT = 8


@router.get("", response_model=SearchResponse)
def search(q: str = Query(default="", min_length=0), db: Session = Depends(get_db), _user: User = _view) -> SearchResponse:
    query = q.strip()
    if len(query) < MIN_QUERY_LENGTH:
        return SearchResponse(query=query, results=[])

    like = f"%{query}%"
    results: list[SearchResultItem] = []

    reports = db.execute(
        select(ExpenseReport)
        .where(
            ExpenseReport.er_id.ilike(like)
            | ExpenseReport.original_filename.ilike(like)
            | ExpenseReport.employee_name.ilike(like)
            | ExpenseReport.employee_code.ilike(like)
        )
        .order_by(ExpenseReport.id.desc())
        .limit(PER_KIND_LIMIT)
    ).scalars().all()
    for r in reports:
        matched = "er_id" if r.er_id and query.lower() in r.er_id.lower() else (
            "employee_name" if r.employee_name and query.lower() in r.employee_name.lower() else (
                "employee_code" if r.employee_code and query.lower() in r.employee_code.lower() else "filename"
            )
        )
        results.append(
            SearchResultItem(
                kind="EXPENSE_REPORT",
                title=r.er_id or r.original_filename,
                subtitle=f"{r.employee_name or 'Unknown employee'} - {r.original_filename}",
                document_id=f"expense:{r.id}",
                path=None,
                matched_field=matched,
            )
        )

    trips = db.execute(
        select(TripReport)
        .where(
            TripReport.is_embedded.is_(False),
            TripReport.trip_id.ilike(like)
            | TripReport.original_filename.ilike(like)
            | TripReport.employee_name.ilike(like)
            | TripReport.employee_code.ilike(like),
        )
        .order_by(TripReport.id.desc())
        .limit(PER_KIND_LIMIT)
    ).scalars().all()
    for t in trips:
        matched = "trip_id" if t.trip_id and query.lower() in t.trip_id.lower() else (
            "employee_name" if t.employee_name and query.lower() in t.employee_name.lower() else (
                "employee_code" if t.employee_code and query.lower() in t.employee_code.lower() else "filename"
            )
        )
        results.append(
            SearchResultItem(
                kind="TRIP_REPORT",
                title=t.trip_id or (t.original_filename or "Trip Report"),
                subtitle=f"{t.employee_name or 'Unknown employee'} - {t.original_filename or ''}",
                document_id=f"trip:{t.id}",
                path=None,
                matched_field=matched,
            )
        )

    policies = db.execute(
        select(PolicyMaster).where(PolicyMaster.category.ilike(like)).order_by(PolicyMaster.category).limit(PER_KIND_LIMIT)
    ).scalars().all()
    for p in policies:
        results.append(
            SearchResultItem(
                kind="POLICY_CATEGORY",
                title=p.category,
                subtitle=f"{p.reimbursement_basis} - document {p.document_required}",
                document_id=None,
                path="/policy-master",
                matched_field="category",
            )
        )

    # Merchant/vendor matches - each points back at the expense report the
    # matching expense line lives on, never at the category, so a vendor-name
    # search can never be mistaken for a category-name search in the results.
    merchant_rows = db.execute(
        select(Expense.merchant, Expense.expense_report_id)
        .where(Expense.merchant.ilike(like))
        .distinct()
        .limit(PER_KIND_LIMIT)
    ).all()
    seen_merchant_reports: set[int] = set()
    for merchant, report_id in merchant_rows:
        if report_id in seen_merchant_reports:
            continue
        seen_merchant_reports.add(report_id)
        report = db.get(ExpenseReport, report_id)
        if report is None:
            continue
        results.append(
            SearchResultItem(
                kind="MERCHANT",
                title=merchant,
                subtitle=f"Merchant on {report.er_id or report.original_filename}",
                document_id=f"expense:{report.id}",
                path=None,
                matched_field="merchant",
            )
        )

    exception_rows = db.execute(
        select(ExceptionRecord.exception_type, ExceptionRecord.expense_report_id)
        .where(ExceptionRecord.exception_type.ilike(like))
        .distinct()
        .limit(PER_KIND_LIMIT)
    ).all()
    seen_exception_reports: set[tuple[str, int]] = set()
    for exception_type, report_id in exception_rows:
        key = (exception_type, report_id)
        if key in seen_exception_reports:
            continue
        seen_exception_reports.add(key)
        report = db.get(ExpenseReport, report_id)
        if report is None:
            continue
        results.append(
            SearchResultItem(
                kind="EXCEPTION_TYPE",
                title=exception_type,
                subtitle=f"On {report.er_id or report.original_filename}",
                document_id=f"expense:{report.id}",
                path=None,
                matched_field="exception_type",
            )
        )

    return SearchResponse(query=query, results=results)
