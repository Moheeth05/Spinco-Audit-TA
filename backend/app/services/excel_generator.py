"""Phase 8 - deterministic consolidated Excel workbook generator.

Generates ONE workbook (never one file per PDF/month/employee) that
represents the CURRENT database state - the database remains the single
source of truth; this module only reads it and formats it as a report.
It never recalculates a policy limit, a bill match, a trip match, or a
report status independently - every value here is read straight from the
rows Phases 1-7 already computed (PolicyMaster, bill matching, Phase 5
validation, Phase 6 trip matching, Phase 7 exception engine).

Regeneration is always a full rebuild from the database (per-sheet row
lists are computed once from a handful of batch queries, never per-row
queries), not an incremental edit of the previous file - this is what
guarantees regenerating twice, or after a revalidation that changes
nothing, produces byte-identical row content and never duplicates or
drifts. See services/exception_engine.py and services/validation/pipeline.py
for the checks/statuses this module only reads.
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy.orm import Session

from app.models.bill import Bill
from app.models.employee import Employee
from app.models.excel import ExcelReport
from app.models.expense_report import Expense, ExpenseReport
from app.models.trip import Trip, TripReport
from app.models.validation import ExceptionRecord, ValidationResult
from app.services.category_map import normalize_category
from app.services.storage import EXCEL_DIR

logger = logging.getLogger(__name__)

WORKBOOK_FILENAME = "Spinco_TA_Audit_Consolidated.xlsx"

SHEET_EXPENSE_REPORTS = "Expense Reports"
SHEET_TRIP_REPORTS = "Trip Reports"
SHEET_EXPENSES = "Expenses"
SHEET_EXCEPTIONS = "Exceptions"
# "Batta / Trip Matching" isn't a legal Excel sheet name - "/" is one of the
# characters Excel (and openpyxl) forbid in a sheet title - so this is the
# safe equivalent the spec explicitly allows ("unless Excel's character
# limitations require a safe equivalent").
SHEET_BATTA_TRIP = "Batta-Trip Matching"
SHEET_SUMMARY = "Summary"

SHEET_NAMES = [SHEET_EXPENSE_REPORTS, SHEET_TRIP_REPORTS, SHEET_EXPENSES, SHEET_EXCEPTIONS, SHEET_BATTA_TRIP, SHEET_SUMMARY]

_HEADER_FILL = PatternFill("solid", fgColor="1E3A5F")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=10, name="Calibri")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_BODY_FONT = Font(size=10, name="Calibri")
_BODY_ALIGN = Alignment(vertical="top")
_WRAP_ALIGN = Alignment(vertical="top", wrap_text=True)
_THIN_BOTTOM = Border(bottom=Side(style="thin", color="D9DEE4"))
_CURRENCY_FORMAT = '"Rs."#,##0.00'
_DATE_FORMAT = "DD/MM/YYYY"
_DATETIME_FORMAT = "DD/MM/YYYY HH:MM"


@dataclass
class Column:
    header: str
    width: float = 14
    fmt: str | None = None  # None | "currency" | "date" | "datetime" | "wrap"


def _amt(value) -> float | None:
    return None if value is None else float(value)


def _write_sheet(wb: Workbook, title: str, columns: list[Column], rows: list[list]) -> Worksheet:
    ws = wb.create_sheet(title)
    for col_idx, col in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col.header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _HEADER_ALIGN
        ws.column_dimensions[get_column_letter(col_idx)].width = col.width

    for row_idx, row_values in enumerate(rows, start=2):
        for col_idx, (col, value) in enumerate(zip(columns, row_values), start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = _BODY_FONT
            cell.border = _THIN_BOTTOM
            if col.fmt == "currency":
                cell.number_format = _CURRENCY_FORMAT
                cell.alignment = Alignment(vertical="top", horizontal="right")
            elif col.fmt == "date":
                cell.number_format = _DATE_FORMAT
                cell.alignment = _BODY_ALIGN
            elif col.fmt == "datetime":
                cell.number_format = _DATETIME_FORMAT
                cell.alignment = _BODY_ALIGN
            elif col.fmt == "wrap":
                cell.alignment = _WRAP_ALIGN
            else:
                cell.alignment = _BODY_ALIGN

    ws.freeze_panes = "A2"
    last_col_letter = get_column_letter(len(columns))
    last_row = max(len(rows) + 1, 1)
    ws.auto_filter.ref = f"A1:{last_col_letter}{last_row}"
    ws.row_dimensions[1].height = 32
    return ws


class _DataSet:
    """Every table this workbook needs, fetched once via a handful of batch
    queries (never per-row) and cross-indexed in memory - every sheet
    builder below reads from here, so no sheet issues its own DB queries."""

    def __init__(self, db: Session):
        self.reports = db.query(ExpenseReport).order_by(ExpenseReport.id).all()
        self.expenses = db.query(Expense).order_by(Expense.id).all()
        self.trips = db.query(TripReport).order_by(TripReport.id).all()
        self.legs = db.query(Trip).order_by(Trip.id).all()
        self.bills = db.query(Bill).order_by(Bill.id).all()
        self.exceptions = db.query(ExceptionRecord).order_by(ExceptionRecord.id).all()
        self.validation_results = db.query(ValidationResult).all()
        self.employees = db.query(Employee).all()

        self.reports_by_id = {r.id: r for r in self.reports}
        self.expenses_by_id = {e.id: e for e in self.expenses}
        self.trips_by_id = {t.id: t for t in self.trips}
        self.bills_by_id = {b.id: b for b in self.bills}
        self.employees_by_id = {e.id: e for e in self.employees}
        self.vr_by_id = {v.id: v for v in self.validation_results}

        self.legs_by_trip: dict[int, list[Trip]] = defaultdict(list)
        for leg in self.legs:
            self.legs_by_trip[leg.trip_report_id].append(leg)

        self.exceptions_by_expense: dict[int | None, list[ExceptionRecord]] = defaultdict(list)
        for x in self.exceptions:
            self.exceptions_by_expense[x.expense_id].append(x)

        self.passed_sum_by_report: dict[int, float] = defaultdict(float)
        for e in self.expenses:
            if e.validation_status == "PASS" and e.claimed_amount is not None:
                self.passed_sum_by_report[e.expense_report_id] += float(e.claimed_amount)

        self.last_touched_by_report: dict[int, datetime] = {}
        for v in self.validation_results:
            if v.updated_at is None:
                continue
            current = self.last_touched_by_report.get(v.expense_report_id)
            if current is None or v.updated_at > current:
                self.last_touched_by_report[v.expense_report_id] = v.updated_at

        self.policy_check_by_expense: dict[int, str] = {
            v.expense_id: v.result for v in self.validation_results if v.check_type == "Policy Limit Check"
        }

    def employee_email(self, employee_id: int | None) -> str | None:
        if not employee_id:
            return None
        emp = self.employees_by_id.get(employee_id)
        return emp.email if emp else None

    def trip_source_pdf(self, trip: TripReport) -> str | None:
        if trip.is_embedded:
            parent = self.reports_by_id.get(trip.source_expense_report_id)
            return parent.original_filename if parent else None
        return trip.original_filename

    def trip_source_page(self, trip: TripReport) -> int | None:
        return trip.embedded_source_page if trip.is_embedded else None


def _build_expense_reports_sheet(ds: _DataSet) -> tuple[list[Column], list[list]]:
    columns = [
        Column("Report ID", 10),
        Column("Report Number", 16),
        Column("Employee Name", 24),
        Column("Employee ID", 14),
        Column("Employee Email", 26),
        Column("Report Month", 14),
        Column("Report Date", 14, "date"),
        Column("Submitted Date", 14, "date"),
        Column("Department", 18),
        Column("Location", 18),
        Column("Total Claimed", 16, "currency"),
        Column("Total Validated", 16, "currency"),
        Column("Expense Count", 12),
        Column("Passed Count", 12),
        Column("Exception Count", 12),
        Column("Review Required Count", 16),
        Column("Overall Status", 18),
        Column("Extraction Status", 16),
        Column("Source PDF", 32),
        Column("Source Pages", 12),
        Column("Created At", 18, "datetime"),
        Column("Updated At", 18, "datetime"),
    ]
    rows = []
    for r in ds.reports:
        report_date = None
        if r.report_month:
            try:
                year, month = (int(p) for p in r.report_month.split("-"))
                report_date = date(year, month, 1)
            except (ValueError, TypeError):
                report_date = None
        rows.append(
            [
                r.id,
                r.er_id,
                r.employee_name,
                r.employee_code,
                ds.employee_email(r.employee_id),
                r.report_month_label,
                report_date,
                r.submitted_on,
                r.department,
                r.location,
                _amt(r.total_claimed),
                round(ds.passed_sum_by_report.get(r.id, 0.0), 2),
                r.total_expenses,
                r.verified_expense_count,
                r.exception_count,
                r.review_required_count,
                r.overall_status,
                r.extraction_status,
                r.original_filename,
                r.page_count,
                r.uploaded_at,
                ds.last_touched_by_report.get(r.id, r.uploaded_at),
            ]
        )
    return columns, rows


def _build_trip_reports_sheet(ds: _DataSet) -> tuple[list[Column], list[list]]:
    columns = [
        Column("Trip ID", 10),
        Column("Trip Number", 16),
        Column("Source Type", 22),
        Column("Employee Name", 24),
        Column("Employee ID", 14),
        Column("Employee Email", 26),
        Column("Destination", 20),
        Column("City", 20),
        Column("Country", 14),
        Column("Domestic / International", 18),
        Column("Purpose", 34, "wrap"),
        Column("Trip Start Date", 16, "date"),
        Column("Trip End Date", 16, "date"),
        Column("Trip Days", 10),
        Column("Submission Date", 16, "date"),
        Column("Approval Status", 16),
        Column("Source PDF", 32),
        Column("Source Pages", 12),
        Column("Extraction Status", 16),
        Column("Created At", 18, "datetime"),
        Column("Updated At", 18, "datetime"),
    ]
    rows = []
    for t in ds.trips:
        days = None
        if t.start_date and t.end_date:
            days = (t.end_date - t.start_date).days + 1
        rows.append(
            [
                t.id,
                t.trip_id,
                "Embedded in Expense Report" if t.is_embedded else "Standalone Trip Report",
                t.employee_name,
                t.employee_code,
                ds.employee_email(t.employee_id),
                t.destination,
                t.destination,  # no distinct "city" field exists - same real value, not invented
                None,  # no Country field anywhere in the data model
                None,  # no Domestic/International field anywhere in the data model
                t.purpose,
                t.start_date,
                t.end_date,
                days,
                None,  # TripReport has no persisted submission date column
                t.approval_status,
                ds.trip_source_pdf(t),
                ds.trip_source_page(t),
                t.extraction_status,
                t.uploaded_at,
                None,  # TripReport has no updated_at column
            ]
        )
    return columns, rows


def _build_expenses_sheet(ds: _DataSet) -> tuple[list[Column], list[list]]:
    columns = [
        Column("Expense ID", 10),
        Column("Report ID", 10),
        Column("Employee Name", 24),
        Column("Employee ID", 14),
        Column("Expense Date", 14, "date"),
        Column("Category", 22),
        Column("Merchant", 22),
        Column("Description", 34, "wrap"),
        Column("Claimed Amount", 15, "currency"),
        Column("Policy Limit", 15, "currency"),
        Column("Policy Basis", 12),
        Column("Policy Status", 16),
        Column("Bill Match Status", 18),
        Column("Bill Amount", 15, "currency"),
        Column("Bill Date", 14, "date"),
        Column("Bill Vendor", 22),
        Column("Bill Number", 16),
        Column("Bill GSTIN", 18),
        Column("Trip Match Status", 18),
        Column("Trip Number", 14),
        Column("Trip Start Date", 14, "date"),
        Column("Trip End Date", 14, "date"),
        Column("Validation Status", 16),
        Column("Exception Count", 12),
        Column("Review Required Count", 16),
        Column("Source PDF", 32),
        Column("Source Page", 12),
    ]
    rows = []
    for e in ds.expenses:
        report = ds.reports_by_id.get(e.expense_report_id)
        bill = ds.bills_by_id.get(e.bill_id) if e.bill_id else None
        trip = ds.trips_by_id.get(e.trip_report_id) if e.trip_report_id else None
        exc_rows = ds.exceptions_by_expense.get(e.id, [])
        rows.append(
            [
                e.id,
                e.expense_report_id,
                report.employee_name if report else None,
                report.employee_code if report else None,
                e.expense_date,
                e.category,
                e.merchant,
                e.expense_details,
                _amt(e.claimed_amount),
                _amt(e.policy_limit),
                e.policy_basis,
                ds.policy_check_by_expense.get(e.id),
                e.bill_match_status,
                _amt(e.bill_amount),
                e.bill_date,
                bill.vendor if bill else None,
                bill.bill_number if bill else None,
                bill.gstin if bill else None,
                e.trip_match_status,
                trip.trip_id if trip else None,
                trip.start_date if trip else None,
                trip.end_date if trip else None,
                e.validation_status,
                sum(1 for x in exc_rows if x.severity == "EXCEPTION"),
                sum(1 for x in exc_rows if x.severity == "REVIEW REQUIRED"),
                report.original_filename if report else None,
                e.source_page,
            ]
        )
    return columns, rows


def _build_exceptions_sheet(ds: _DataSet) -> tuple[list[Column], list[list]]:
    columns = [
        Column("Exception ID", 10),
        Column("Report ID", 10),
        Column("Expense ID", 10),
        Column("Employee Name", 24),
        Column("Employee ID", 14),
        Column("Expense Date", 14, "date"),
        Column("Category", 20),
        Column("Exception Type", 24),
        Column("Severity", 16),
        Column("Status", 12),
        Column("Reason", 46, "wrap"),
        Column("Claimed Amount", 15, "currency"),
        Column("Bill Amount", 15, "currency"),
        Column("Policy Limit", 15, "currency"),
        Column("Trip Number", 14),
        Column("Trip Start Date", 14, "date"),
        Column("Trip End Date", 14, "date"),
        Column("Source PDF", 32),
        Column("Source Page", 12),
        Column("Bill ID", 10),
        Column("Trip Report ID", 12),
        Column("Validation Result ID", 14),
        Column("Created At", 18, "datetime"),
        Column("Updated At", 18, "datetime"),
    ]
    rows = []
    for x in ds.exceptions:
        report = ds.reports_by_id.get(x.expense_report_id)
        expense = ds.expenses_by_id.get(x.expense_id) if x.expense_id else None
        vr = ds.vr_by_id.get(x.validation_result_id) if x.validation_result_id else None
        trip = ds.trips_by_id.get(x.trip_report_id) if x.trip_report_id else None
        rows.append(
            [
                x.id,
                x.expense_report_id,
                x.expense_id,
                report.employee_name if report else None,
                report.employee_code if report else None,
                expense.expense_date if expense else None,
                expense.category if expense else None,
                x.exception_type,
                x.severity,
                x.status,
                x.reason,
                _amt(vr.claimed_amount) if vr else (_amt(expense.claimed_amount) if expense else None),
                _amt(vr.bill_amount) if vr else None,
                _amt(vr.policy_limit) if vr else None,
                trip.trip_id if trip else None,
                trip.start_date if trip else None,
                trip.end_date if trip else None,
                x.source_pdf,
                x.source_page,
                x.bill_id,
                x.trip_report_id,
                x.validation_result_id,
                x.created_at,
                x.updated_at,
            ]
        )
    return columns, rows


def _build_batta_trip_sheet(ds: _DataSet) -> tuple[list[Column], list[list]]:
    columns = [
        Column("Expense ID", 10),
        Column("Report ID", 10),
        Column("Employee Name", 24),
        Column("Employee ID", 14),
        Column("Expense Date", 14, "date"),
        Column("Claimed Amount", 15, "currency"),
        Column("Trip Match Status", 18),
        Column("Trip Match Confidence", 16),
        Column("Trip Match Score", 14),
        Column("Trip Number", 14),
        Column("Trip Employee", 24),
        Column("Trip Start Date", 14, "date"),
        Column("Trip End Date", 14, "date"),
        Column("Destination", 20),
        Column("Matching Evidence", 46, "wrap"),
        Column("Trip Source PDF", 32),
        Column("Trip Source Page", 14),
        Column("Trip Report ID", 12),
    ]
    rows = []
    for e in ds.expenses:
        if normalize_category(e.category) != "BATTA":
            continue
        report = ds.reports_by_id.get(e.expense_report_id)
        trip = ds.trips_by_id.get(e.trip_report_id) if e.trip_report_id else None
        rows.append(
            [
                e.id,
                e.expense_report_id,
                report.employee_name if report else None,
                report.employee_code if report else None,
                e.expense_date,
                _amt(e.claimed_amount),
                e.trip_match_status,
                e.trip_match_confidence,
                e.trip_match_score,
                trip.trip_id if trip else None,
                trip.employee_name if trip else None,
                trip.start_date if trip else None,
                trip.end_date if trip else None,
                trip.destination if trip else None,
                e.trip_match_reasons,
                ds.trip_source_pdf(trip) if trip else None,
                ds.trip_source_page(trip) if trip else None,
                e.trip_report_id,
            ]
        )
    return columns, rows


def _build_summary_sheet(ds: _DataSet) -> tuple[list[Column], list[list]]:
    columns = [Column("Metric", 40), Column("Value", 20)]
    rows: list[list] = []

    def add(label: str, value):
        rows.append([label, value])

    add("Total Expense Reports", len(ds.reports))
    add("Account Verified Reports", sum(1 for r in ds.reports if r.overall_status == "ACCOUNT VERIFIED"))
    add("Exception Reports", sum(1 for r in ds.reports if r.overall_status == "EXCEPTION"))
    add("Review Required Reports", sum(1 for r in ds.reports if r.overall_status == "REVIEW REQUIRED"))
    rows.append([])

    add("Total Expenses", len(ds.expenses))
    add("Passed Expenses", sum(1 for e in ds.expenses if e.validation_status == "PASS"))
    add("Exception Expenses", sum(1 for e in ds.expenses if e.validation_status == "EXCEPTION"))
    add("Review Required Expenses", sum(1 for e in ds.expenses if e.validation_status == "REVIEW_REQUIRED"))
    rows.append([])

    total_claimed = sum(float(r.total_claimed) for r in ds.reports if r.total_claimed is not None)
    add("Total Claimed Amount", round(total_claimed, 2))
    rows.append([])

    add("Total Exceptions", len(ds.exceptions))
    exception_type_counts: dict[str, int] = defaultdict(int)
    for x in ds.exceptions:
        exception_type_counts[x.exception_type] += 1
    for exc_type in sorted(exception_type_counts):
        add(f"  - {exc_type}", exception_type_counts[exc_type])
    rows.append([])

    add("Batta/Trip Match: MATCHED", 0)
    add("Batta/Trip Match: MULTIPLE_CANDIDATES", 0)
    add("Batta/Trip Match: NO_MATCH", 0)
    add("Batta/Trip Match: REVIEW_REQUIRED", 0)
    match_counts: dict[str, int] = defaultdict(int)
    for e in ds.expenses:
        if normalize_category(e.category) == "BATTA":
            match_counts[e.trip_match_status] += 1
    for i, status in enumerate(("MATCHED", "MULTIPLE_CANDIDATES", "NO_MATCH", "REVIEW_REQUIRED")):
        rows[-4 + i][1] = match_counts.get(status, 0)

    return columns, rows


def _write_summary_sheet(wb: Workbook, columns: list[Column], rows: list[list]) -> None:
    ws = _write_sheet(wb, SHEET_SUMMARY, columns, rows)
    for row_idx, row in enumerate(rows, start=2):
        if row and row[0] == "Total Claimed Amount":
            ws.cell(row=row_idx, column=2).number_format = _CURRENCY_FORMAT
        if row and str(row[0]).startswith("  - "):
            ws.cell(row=row_idx, column=1).alignment = Alignment(indent=1)


def generate_workbook(db: Session) -> ExcelReport:
    start = time.monotonic()
    logger.info("Workbook generation started")
    try:
        ds = _DataSet(db)

        wb = Workbook()
        wb.remove(wb.active)

        _write_sheet(wb, SHEET_EXPENSE_REPORTS, *_build_expense_reports_sheet(ds))
        _write_sheet(wb, SHEET_TRIP_REPORTS, *_build_trip_reports_sheet(ds))
        _write_sheet(wb, SHEET_EXPENSES, *_build_expenses_sheet(ds))
        _write_sheet(wb, SHEET_EXCEPTIONS, *_build_exceptions_sheet(ds))
        _write_sheet(wb, SHEET_BATTA_TRIP, *_build_batta_trip_sheet(ds))
        _write_summary_sheet(wb, *_build_summary_sheet(ds))

        EXCEL_DIR.mkdir(parents=True, exist_ok=True)
        path = EXCEL_DIR / WORKBOOK_FILENAME
        wb.save(path)

        record = db.query(ExcelReport).first()
        if record is None:
            record = ExcelReport(report_month="ALL", report_month_label="All Months")
            db.add(record)
        record.filename = WORKBOOK_FILENAME
        record.file_path = str(path)
        record.total_reports = len(ds.reports)
        record.total_expenses = len(ds.expenses)
        record.total_claimed = round(sum(float(r.total_claimed) for r in ds.reports if r.total_claimed is not None), 2)
        record.status = "UP_TO_DATE"
        record.last_updated = datetime.now(timezone.utc)
        db.commit()
        db.refresh(record)

        duration = time.monotonic() - start
        logger.info(
            "Workbook generation completed: %d reports, %d expenses, %d exceptions written in %.2fs",
            len(ds.reports),
            len(ds.expenses),
            len(ds.exceptions),
            duration,
        )
        return record
    except Exception:
        logger.exception("Workbook generation failed")
        raise
