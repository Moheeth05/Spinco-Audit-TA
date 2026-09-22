"""Orchestrates PDF -> classification -> extraction -> persistence for a
single document. Used both right after a successful upload and by the
manual reprocess endpoint - the two call sites share this exact code path.
"""

import calendar
from collections import Counter
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.evidence import SourceEvidence
from app.models.expense_report import Expense, ExpenseReport
from app.models.logs import ProcessingLog
from app.models.trip import Trip, TripReport
from app.services.document_ref import EXPENSE, TRIP
from app.services.extraction import batta_pipeline, bill_pipeline, expense_report_parser, trip_report_parser
from app.services.extraction.classifier import UNKNOWN, classify_text
from app.services.extraction.shared import text_extractor as _text_extractor
from app.services.validation.pipeline import validate_expense_report

STAGE = "EXTRACTION"


def _month_label(report_month: str) -> str:
    year, month = report_month.split("-")
    return f"{calendar.month_name[int(month)]} {year}"


def _log(db: Session, *, expense_report_id: int | None, trip_report_id: int | None, filename: str, status: str, message: str):
    db.add(
        ProcessingLog(
            expense_report_id=expense_report_id,
            trip_report_id=trip_report_id,
            filename=filename,
            stage=STAGE,
            status=status,
            message=message,
        )
    )
    db.commit()


def _replace_evidence(db: Session, document_type: str, document_ref_id: int, evidence: list[tuple[str, int, str | None]]):
    db.query(SourceEvidence).filter(
        SourceEvidence.document_type == document_type, SourceEvidence.document_ref_id == document_ref_id
    ).delete()
    for field_name, page_number, text in evidence:
        if page_number is None:
            continue
        db.add(
            SourceEvidence(
                document_type=document_type,
                document_ref_id=document_ref_id,
                field_name=field_name,
                page_number=page_number,
                extracted_text=(text[:500] if text else None),
            )
        )
    db.commit()


def _find_or_create_employee(db: Session, name: str | None, code: str | None) -> Employee | None:
    if not name:
        return None
    query = db.query(Employee)
    employee = None
    if code:
        employee = query.filter(Employee.employee_code == code).first()
    if not employee:
        employee = query.filter(Employee.name == name).first()
    if not employee:
        employee = Employee(name=name, employee_code=code)
        db.add(employee)
        db.commit()
        db.refresh(employee)
    elif code and not employee.employee_code:
        employee.employee_code = code
        db.commit()
    return employee


def _extract_expense_report(db: Session, row: ExpenseReport) -> None:
    pdf_path = Path(row.source_path)
    pages = _text_extractor.content_pages(pdf_path)
    combined_text = "\n".join(p["text"] for p in pages)
    detected_type = classify_text(combined_text)

    header = expense_report_parser.extract_header(pages)
    lines, line_warnings = expense_report_parser.extract_expense_lines(pages)
    embedded = expense_report_parser.extract_embedded_trip_summary(pages)

    warnings = list(header["warnings"]) + line_warnings
    if detected_type == UNKNOWN:
        warnings.append("Document type could not be reliably determined from the source PDF.")
    elif detected_type == "TRIP_REPORT":
        warnings.append("Document content looks like a Trip Report, but this file was uploaded as an Expense Report.")

    if header["er_id"]:
        row.er_id = header["er_id"]
    if header["employee_name"]:
        row.employee_name = header["employee_name"]
        employee = _find_or_create_employee(db, header["employee_name"], header["employee_code"])
        if employee:
            row.employee_id = employee.id
    if header["employee_code"]:
        row.employee_code = header["employee_code"]
    if header["submitted_on"]:
        row.submitted_on = header["submitted_on"]
    if header["total_claimed"] is not None:
        row.total_claimed = header["total_claimed"]
    row.total_expenses = len(lines)

    month_counts = Counter(f"{d.year:04d}-{d.month:02d}" for d in (l["expense_date"] for l in lines) if d)
    if month_counts:
        report_month = month_counts.most_common(1)[0][0]
        row.report_month = report_month
        row.report_month_label = _month_label(report_month)
    else:
        row.report_month = None
        row.report_month_label = None
        warnings.append("Report month could not be reliably determined from the source document.")

    db.query(Expense).filter(Expense.expense_report_id == row.id).delete()
    for line in lines:
        db.add(
            Expense(
                expense_report_id=row.id,
                s_no=line["s_no"],
                expense_date=line["expense_date"],
                category=line["category"],
                expense_details=line["expense_details"],
                claimed_amount=line["claimed_amount"],
                source_page=line["source_page"],
                extraction_status=line["status"],
            )
        )
    db.commit()  # expenses need real ids before bill matching below can query/reference them

    # Prefer the report's own "REPORT SUMMARY" section as the content/bills
    # boundary - content_pages()'s blank-page heuristic over-includes when the
    # supporting bills also carry native text (e.g. emailed e-receipts).
    content_last_page = expense_report_parser.find_report_summary_page(pages)
    if content_last_page is None:
        content_last_page = pages[-1]["page_number"] if pages else 0
    warnings.extend(bill_pipeline.extract_and_match_bills(db, row, _text_extractor, content_last_page))

    existing_embedded = (
        db.query(TripReport)
        .filter(TripReport.is_embedded.is_(True), TripReport.source_expense_report_id == row.id)
        .first()
    )
    if embedded:
        embedded_trip = existing_embedded or TripReport(is_embedded=True, source_expense_report_id=row.id)
        embedded_trip.trip_id = embedded["trip_number"]
        embedded_trip.destination = embedded["destination"]
        embedded_trip.embedded_source_page = embedded["source_page"]
        embedded_trip.employee_name = row.employee_name
        embedded_trip.employee_code = row.employee_code
        embedded_trip.report_month = row.report_month
        embedded_trip.processing_status = "PROCESSED"
        if embedded["legs"]:
            embedded_trip.origin = embedded["legs"][0]["from_location"]
            embedded_trip.start_date = embedded["legs"][0]["from_date"]
            embedded_trip.end_date = embedded["legs"][-1]["to_date"]
        embedded_ok = bool(embedded["trip_number"]) or bool(embedded["legs"])
        embedded_trip.extraction_status = "EXTRACTED" if embedded_ok else "REVIEW_REQUIRED"
        embedded_trip.extraction_confidence = "MEDIUM" if embedded_ok else "LOW"
        embedded_trip.extraction_notes = None if embedded_ok else "Embedded trip summary section found but no usable fields could be extracted."
        if not existing_embedded:
            db.add(embedded_trip)
        db.commit()
        db.refresh(embedded_trip)

        db.query(Trip).filter(Trip.trip_report_id == embedded_trip.id).delete()
        for leg in embedded["legs"]:
            db.add(
                Trip(
                    trip_report_id=embedded_trip.id,
                    leg_no=leg["leg_no"],
                    from_location=leg["from_location"],
                    from_date=leg["from_date"],
                    to_location=leg["to_location"],
                    to_date=leg["to_date"],
                    description=leg["description"],
                )
            )
        db.commit()
    elif existing_embedded:
        db.delete(existing_embedded)
        db.commit()

    # Batta matching needs this report's own embedded trip (if any) to already
    # be committed above, and runs before validation so its results are ready
    # for the Batta-trip check inside validate_expense_report() below.
    batta_pipeline.match_batta_expenses(db, row)

    critical_ok = bool(row.er_id) and bool(row.employee_name) and row.total_claimed is not None and len(lines) > 0
    if not critical_ok:
        row.extraction_status = "REVIEW_REQUIRED"
        row.extraction_confidence = "LOW"
    elif warnings:
        row.extraction_status = "EXTRACTED"
        row.extraction_confidence = "MEDIUM"
    else:
        row.extraction_status = "EXTRACTED"
        row.extraction_confidence = "HIGH"
    row.extraction_notes = "; ".join(warnings) if warnings else None
    db.commit()

    evidence: list[tuple[str, int, str | None]] = [
        ("er_id", header["er_id_page"], header["er_id"]),
        ("employee_name", header["employee_name_page"], header["employee_name"]),
        ("employee_code", header["employee_code_page"], header["employee_code"]),
        ("submitted_on", header["submitted_on_page"], str(header["submitted_on"]) if header["submitted_on"] else None),
        ("total_claimed", header["total_claimed_page"], str(header["total_claimed"]) if header["total_claimed"] is not None else None),
    ]
    for line in lines:
        evidence.append((f"expense_line[{line['s_no']}]", line["source_page"], line["category"]))
        evidence.append((f"expense_line[{line['s_no']}].amount", line["amount_source_page"], str(line["claimed_amount"])))
    if embedded:
        evidence.append(("embedded_trip_summary", embedded["source_page"], embedded["trip_number"]))
    _replace_evidence(db, EXPENSE, row.id, evidence)

    # Policy validation runs on whatever was extracted, even a REVIEW_REQUIRED
    # extraction - isolated in its own try/except so a validation-engine problem
    # never retroactively marks an otherwise-successful extraction as FAILED.
    try:
        validate_expense_report(db, row.id)
    except Exception:  # noqa: BLE001
        pass


def _extract_trip_report(db: Session, row: TripReport) -> None:
    pdf_path = Path(row.source_path)
    pages = _text_extractor.content_pages(pdf_path)
    details = trip_report_parser.extract_trip_details(pages)

    warnings = list(details["warnings"])

    if details["trip_id"]:
        row.trip_id = details["trip_id"]
    if details["employee_name"]:
        row.employee_name = details["employee_name"]
        employee = _find_or_create_employee(db, details["employee_name"], details["employee_code"])
        if employee:
            row.employee_id = employee.id
    if details["employee_code"]:
        row.employee_code = details["employee_code"]
    row.destination = details["destination"]
    row.origin = details["origin"]
    row.start_date = details["start_date"]
    row.end_date = details["end_date"]
    row.purpose = details["purpose"]
    row.approver = details["approver"]
    row.approval_status = details["approval_status"]
    row.approval_date = details["approval_date"]

    if row.start_date:
        row.report_month = f"{row.start_date.year:04d}-{row.start_date.month:02d}"

    db.query(Trip).filter(Trip.trip_report_id == row.id).delete()
    for leg in details["legs"]:
        db.add(
            Trip(
                trip_report_id=row.id,
                leg_no=leg["leg_no"],
                from_location=leg["from_location"],
                from_date=leg["from_date"],
                to_location=leg["to_location"],
                to_date=leg["to_date"],
                description=leg["description"],
            )
        )
    db.commit()

    critical_ok = bool(row.trip_id) and bool(row.employee_name) and row.start_date is not None and row.end_date is not None
    if not critical_ok:
        row.extraction_status = "REVIEW_REQUIRED"
        row.extraction_confidence = "LOW"
    elif warnings:
        row.extraction_status = "EXTRACTED"
        row.extraction_confidence = "MEDIUM"
    else:
        row.extraction_status = "EXTRACTED"
        row.extraction_confidence = "HIGH"
    row.extraction_notes = "; ".join(warnings) if warnings else None
    db.commit()

    evidence: list[tuple[str, int, str | None]] = [
        ("trip_id", details["trip_id_page"], details["trip_id"]),
        ("destination", details["destination_page"], details["destination"]),
        ("employee_name", details["employee_name_page"], details["employee_name"]),
        ("submitted_on", details["submitted_on_page"], str(details["submitted_on"]) if details["submitted_on"] else None),
        ("travel_period", details["duration_page"], f"{details['start_date']} - {details['end_date']}"),
        ("purpose", details["purpose_page"], details["purpose"]),
        ("approver", details["approver_page"], details["approver"]),
    ]
    for leg in details["legs"]:
        evidence.append((f"leg[{leg['leg_no']}]", leg.get("source_page"), leg["from_location"]))
    _replace_evidence(db, TRIP, row.id, evidence)

    # A standalone Trip Report may be the missing evidence for a Batta claim
    # extracted earlier (no manual re-upload of the Expense Report required) -
    # re-run matching for every not-yet-MATCHED Batta expense in the system,
    # then re-run policy validation on exactly the reports that changed.
    touched_report_ids = batta_pipeline.reevaluate_unresolved_batta_for_trip(db, row)
    for report_id in touched_report_ids:
        try:
            validate_expense_report(db, report_id)
        except Exception:  # noqa: BLE001
            pass


def run_extraction(db: Session, kind: str, pk: int) -> None:
    model = ExpenseReport if kind == EXPENSE else TripReport
    row = db.get(model, pk)
    if row is None:
        raise ValueError(f"No {kind} document with id {pk}")

    filename = row.original_filename or row.filename or f"{kind}:{pk}"
    row.extraction_status = "PROCESSING"
    db.commit()
    _log(
        db,
        expense_report_id=row.id if kind == EXPENSE else None,
        trip_report_id=row.id if kind == TRIP else None,
        filename=filename,
        status="PROCESSING",
        message="Extraction started.",
    )

    try:
        if not row.source_path or not Path(row.source_path).exists():
            raise FileNotFoundError("Stored PDF file could not be found.")

        if kind == EXPENSE:
            _extract_expense_report(db, row)
        else:
            _extract_trip_report(db, row)

        _log(
            db,
            expense_report_id=row.id if kind == EXPENSE else None,
            trip_report_id=row.id if kind == TRIP else None,
            filename=filename,
            status="PROCESSED",
            message=f"Extraction completed: status={row.extraction_status}, confidence={row.extraction_confidence}.",
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad: one bad document must never break the batch
        row.extraction_status = "FAILED"
        row.extraction_confidence = None
        row.extraction_notes = "Extraction failed due to an internal error. See processing logs for detail."
        db.commit()
        _log(
            db,
            expense_report_id=row.id if kind == EXPENSE else None,
            trip_report_id=row.id if kind == TRIP else None,
            filename=filename,
            status="FAILED",
            message=f"{type(exc).__name__}: {exc}",
        )
