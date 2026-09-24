"""Bill extraction and expense-to-bill matching for one Expense Report,
run automatically right after Phase 3's header/expense-line extraction
succeeds. Reuses the same OCR/text-extraction infrastructure - see
text_extraction.py:bill_pages_from() for the page range this covers
(everything Phase 3's content_pages() deliberately left untouched).
"""

from pathlib import Path

from sqlalchemy.orm import Session

from app.models.bill import Bill
from app.models.bill_match import BillMatchCandidate
from app.models.expense_report import Expense, ExpenseReport
from app.services.category_map import normalize_category
from app.services.extraction import bill_matcher, bill_parser
from app.services.extraction.text_extraction import PyMuPDFTextExtractor


def _is_batta(category: str | None) -> bool:
    return normalize_category(category) == "BATTA"


def extract_and_match_bills(
    db: Session, row: ExpenseReport, text_extractor: PyMuPDFTextExtractor, content_last_page: int
) -> list[str]:
    """Replaces this report's bills and match results wholesale, then returns
    a short list of warning strings for the caller to fold into the report's
    own extraction_notes."""
    warnings: list[str] = []
    pdf_path = Path(row.source_path)

    bill_pages = text_extractor.bill_pages_from(pdf_path, content_last_page + 1)

    db.query(Bill).filter(Bill.expense_report_id == row.id).delete()
    db.commit()

    bills: list[Bill] = []
    for page in bill_pages:
        if not bill_parser.has_bill_evidence(page["text"]):
            continue
        segments = bill_parser.split_bill_segments(page["text"])
        is_multi = len(segments) > 1
        for segment in segments:
            parsed = bill_parser.parse_bill_text(segment)
            if not bill_parser.parsed_has_evidence(parsed, segment):
                continue
            if is_multi:
                parsed.status = "REVIEW_REQUIRED"
                parsed.warnings.append(
                    "This page appears to contain multiple bills; automatic separation may be unreliable."
                )
            bill = Bill(
                expense_report_id=row.id,
                page_number=page["page_number"],
                vendor=parsed.vendor,
                bill_number=parsed.bill_number,
                bill_date=parsed.bill_date,
                amount=parsed.amount,
                gstin=parsed.gstin,
                description=parsed.description,
                category_guess=parsed.category_guess,
                raw_text=parsed.raw_text,
                extraction_status=parsed.status,
                extraction_confidence=parsed.confidence,
                extraction_notes="; ".join(parsed.warnings) if parsed.warnings else None,
            )
            db.add(bill)
            bills.append(bill)
    db.commit()
    for bill in bills:
        db.refresh(bill)

    bill_dicts = [
        {
            "id": b.id,
            "amount": float(b.amount) if b.amount is not None else None,
            "bill_date": b.bill_date,
            "vendor": b.vendor,
            "description": b.description,
            "category_guess": b.category_guess,
            "bill_number": b.bill_number,
            "page_number": b.page_number,
            "raw_text": b.raw_text,
        }
        for b in bills
    ]
    bills_by_id = {b.id: b for b in bills}
    first_bill_page = min((b.page_number for b in bills if b.page_number is not None), default=None)

    expenses = db.query(Expense).filter(Expense.expense_report_id == row.id).order_by(Expense.s_no).all()
    if expenses:
        db.query(BillMatchCandidate).filter(
            BillMatchCandidate.expense_id.in_([e.id for e in expenses])
        ).delete(synchronize_session=False)

    for rank, expense in enumerate(expenses, start=1):
        if _is_batta(expense.category):
            # BATTA (daily allowance) has no supporting-document requirement
            # at all (Policy Master's document_required is DNA for BATTA) -
            # it is validated against Trip Report evidence only (see
            # batta_pipeline.py), never against a bill. Scanning bill
            # candidates for it anyway is worse than unnecessary: a BATTA
            # claim can incidentally score against some unrelated travel/cab
            # bill filed the same day, which used to surface as confusing
            # "no bill found"/"ambiguous match" review noise on an expense
            # that was never supposed to have a bill in the first place.
            expense.bill_match_status = "NOT_APPLICABLE"
            expense.bill_match_score = None
            expense.bill_match_confidence = None
            expense.bill_match_reasons = "BATTA has no supporting-document requirement; validated against Trip Report evidence instead."
            expense.bill_found = False
            expense.bill_id = None
            expense.bill_amount = None
            expense.bill_date = None
            continue

        expense_dict = {
            "claimed_amount": float(expense.claimed_amount) if expense.claimed_amount is not None else None,
            "expense_date": expense.expense_date,
            "category": expense.category,
            "expense_details": expense.expense_details,
            "expense_rank": rank,
        }
        outcome = bill_matcher.match_expense(expense_dict, bill_dicts, first_bill_page=first_bill_page)

        expense.bill_match_status = outcome.status
        expense.bill_match_score = outcome.score
        expense.bill_match_confidence = outcome.confidence
        expense.bill_match_reasons = "; ".join(outcome.reasons) if outcome.reasons else None

        if outcome.status == "MATCHED" and outcome.matched_bill_id is not None:
            matched_bill = bills_by_id[outcome.matched_bill_id]
            expense.bill_found = True
            expense.bill_id = matched_bill.id
            expense.bill_amount = matched_bill.amount
            expense.bill_date = matched_bill.bill_date
            matched_bill.match_status = "MATCHED"
            matched_bill.matched_expense_id = expense.id
        else:
            expense.bill_found = False
            expense.bill_id = None
            expense.bill_amount = None
            expense.bill_date = None

        for rank, candidate in enumerate(outcome.candidates, start=1):
            db.add(
                BillMatchCandidate(
                    expense_id=expense.id,
                    bill_id=candidate.bill_id,
                    rank=rank,
                    score=candidate.score,
                    reasons="; ".join(candidate.reasons) if candidate.reasons else None,
                )
            )
    db.commit()

    review_bills = sum(1 for b in bills if b.extraction_status == "REVIEW_REQUIRED")
    if review_bills:
        warnings.append(f"{review_bills} bill(s) require review (uncertain extraction).")
    multiple = sum(1 for e in expenses if e.bill_match_status == "MULTIPLE_CANDIDATES")
    if multiple:
        warnings.append(f"{multiple} expense(s) have multiple candidate bills requiring review.")

    return warnings
