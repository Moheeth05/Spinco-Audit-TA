"""Bill extraction and expense-to-bill matching for one Expense Report,
run automatically right after Phase 3's header/expense-line extraction
succeeds. Reuses the same OCR/text-extraction infrastructure - see
text_extraction.py:bill_pages_from() for the page range this covers
(everything Phase 3's content_pages() deliberately left untouched).
"""

from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.bill import Bill
from app.models.bill_match import BillMatchCandidate
from app.models.expense_report import Expense, ExpenseReport
from app.services import category_map
from app.services.category_map import normalize_category
from app.services.extraction import bill_grouping, bill_matcher, bill_parser, candidate_cleanup, dates, document_classifier
from app.services.extraction.text_extraction import PyMuPDFTextExtractor


def _is_batta(category: str | None) -> bool:
    return normalize_category(category) == "BATTA"


def _bill_from_classification(
    expense_report_id: int, page: dict, classification: "document_classifier.ClassificationResult"
) -> Bill:
    """Builds a Bill row from a document_classifier.py result - last-resort
    supporting evidence for a page bill_parser.py's own deterministic rules
    found nothing recognizable on at all. Always REVIEW_REQUIRED: this
    evidence is never confident enough to stand alone (see module docstring
    and bill_pipeline usage below), it only gives bill_matcher.py one more
    candidate row to score an expense against using the same amount/date/
    vendor signals every other bill is matched on."""
    field_confidence = "MEDIUM" if classification.type_confidence >= 0.5 else "LOW"
    notes = [
        f"Page classified as {classification.document_type} via NVIDIA vision/text fallback "
        f"(classifier confidence {classification.type_confidence:.2f}); no deterministic bill "
        f"evidence was found on this page, so this record requires manual review."
    ]
    if page.get("source") == "nvidia_ocr":
        notes.append(
            f"Page text recovered via NVIDIA Nemotron OCR v2 fallback "
            f"(local OCR confidence too low or empty; OCR confidence {page.get('ocr_confidence'):.2f})."
        )
    room_food = category_map.analyze_room_service_food(page["text"])
    if room_food.note:
        notes.append(room_food.note)
    return Bill(
        expense_report_id=expense_report_id,
        page_number=page["page_number"],
        vendor=classification.vendor.value if classification.vendor else None,
        bill_number=classification.invoice_number.value if classification.invoice_number else None,
        bill_date=classification.date.value if classification.date else None,
        amount=classification.total_amount.value if classification.total_amount else None,
        raw_text=page["text"][: category_map.BILL_RAW_TEXT_CAP],
        document_type=classification.document_type,
        document_type_confidence=classification.type_confidence,
        transaction_reference=classification.transaction_reference.value if classification.transaction_reference else None,
        # The classifier's own reading wins; otherwise what the deterministic
        # detector finds in the page's FULL text (raw_text above is capped).
        catering_amount=(
            classification.catering_amount.value
            if classification.catering_amount
            else category_map.detect_catering_amount(page["text"])
        ),
        catering_mentioned=category_map.mentions_catering(page["text"]),
        room_service_food_amount=room_food.amount,
        room_service_food_mentioned=room_food.mentioned,
        extraction_status="REVIEW_REQUIRED",
        extraction_confidence=field_confidence,
        extraction_notes="; ".join(notes),
    )


def _date_fields(reading: "dates.DateReading | None") -> dict:
    """Date provenance columns (see models/bill.py) for a bill's own reading."""
    if reading is None:
        return {}
    return {
        "bill_date_raw": reading.raw[:40],
        "bill_date_candidates": "|".join(c.isoformat() for c in reading.candidates)[:60] or None,
        "bill_date_method": reading.method,
        "bill_date_confidence": reading.confidence,
    }


def _note(bill: Bill, text: str) -> None:
    current = bill.extraction_notes
    bill.extraction_notes = (f"{current}; {text}" if current else text)[:1000]


def _apply_group_date_evidence(entries: list[tuple[Bill, str, "bill_parser.ParsedBill"]]) -> None:
    """Tier-3 date evidence (dates.apply_group_evidence): a receipt whose own
    text left day/month order open takes it from same-template receipts that
    PROVE the order. Updates the bill's date and provenance in place; a date no
    evidence can settle stays unresolved (bill_date NULL, both candidates kept)."""
    if len(entries) < 2:
        return
    texts = [text for _, text, _ in entries]
    own = [dates.read_dates(text) for text in texts]
    grouped = dates.apply_group_evidence(texts, own)
    for (bill, _, parsed), doc_readings in zip(entries, grouped):
        chosen = parsed.date_reading
        if chosen is None or not chosen.ambiguous:
            continue
        better = next((r for r in doc_readings if r.start == chosen.start and r.value is not None), None)
        if better is None:
            continue
        bill.bill_date = better.value
        for column, value in _date_fields(better).items():
            setattr(bill, column, value)
        _note(
            bill,
            f"Date '{better.raw}' read as {better.value.isoformat()} ({'month' if better.order == 'MDY' else 'day'}-first): "
            "other receipts of the same template in this report print dates in the same format and prove the order.",
        )


def _find_strong_duplicates(entries: list[tuple[Bill, str, "bill_parser.ParsedBill"]]) -> dict[int, tuple[Bill, str]]:
    """Bills that are the SAME underlying document or transaction as an earlier
    page: same transaction reference, same invoice (identifying number + vendor +
    amount + date), or a byte-for-byte identical screenshot (text equal after
    whitespace/case normalisation). Returns {id(duplicate bill): (canonical bill, reason)};
    the canonical copy is the lowest page. Two payments are NOT duplicates just
    because amount and date agree: different readable times or different
    references make them separate transactions."""
    by_key: dict[tuple, list[Bill]] = {}

    def add(key: tuple, bill: Bill) -> None:
        by_key.setdefault(key, []).append(bill)

    for bill, text, parsed in entries:
        if parsed.transaction_reference:
            add(("txn", parsed.transaction_reference), bill)
        if bill.bill_number and any(ch.isdigit() for ch in bill.bill_number) and bill.amount is not None and bill.bill_date is not None:
            add(("invoice", (bill.vendor or "").lower(), bill.bill_number.lower(), float(bill.amount), bill.bill_date), bill)
        flat = " ".join(text.lower().split())
        if len(flat) >= 60:
            add(("text", flat), bill)
    reasons = {"txn": "the same transaction reference", "invoice": "the same invoice (number, vendor, amount and date)", "text": "an identical screenshot/page"}
    found: dict[int, tuple[Bill, str]] = {}
    for key, members in by_key.items():
        distinct = list({id(b): b for b in members}.values())
        if len(distinct) < 2:
            continue
        distinct.sort(key=lambda b: (b.page_number or 0))
        canonical = distinct[0]
        for dup in distinct[1:]:
            if id(dup) not in found:
                found[id(dup)] = (canonical, reasons[key[0]])
    return found


def extract_and_match_bills(
    db: Session, row: ExpenseReport, text_extractor: PyMuPDFTextExtractor, content_last_page: int
) -> list[str]:
    """Replaces this report's bills and match results wholesale, then returns
    a short list of warning strings for the caller to fold into the report's
    own extraction_notes."""
    warnings: list[str] = []
    pdf_path = Path(row.source_path)

    bill_pages = text_extractor.bill_pages_from(pdf_path, content_last_page + 1)

    # Cleanup BEFORE delete, same transaction - see candidate_cleanup.py /
    # docs/ORPHAN_RECORD_INVESTIGATION.md. These Bill ids are about to stop
    # existing; any BillMatchCandidate row referencing them by bill_id would
    # otherwise be orphaned permanently.
    old_bill_ids = [b.id for b in db.query(Bill.id).filter(Bill.expense_report_id == row.id).all()]
    candidate_cleanup.delete_bill_match_candidates_for_bill_ids(db, old_bill_ids)
    db.query(Bill).filter(Bill.expense_report_id == row.id).delete()
    db.commit()

    bills: list[Bill] = []
    bill_texts: list[tuple[Bill, str, "bill_parser.ParsedBill"]] = []  # (bill, the exact text it was parsed from, its parse), for date/duplicate evidence
    # Conservative continuation-evidence grouping (bill_grouping.py) - a
    # no-op for every single-page bill (the entire real corpus, by
    # construction: each page becomes its own one-page "group" unless
    # genuine continuation evidence says otherwise). See bill_grouping.py's
    # module docstring for exactly what evidence triggers grouping.
    for group in bill_grouping.group_bill_pages(bill_pages):
        combined_text = "\n".join(p["text"] for p in group)
        if not bill_parser.has_bill_evidence(combined_text):
            continue
        is_grouped = len(group) > 1
        first_page, last_page = group[0], group[-1]
        segments = bill_parser.split_bill_segments(combined_text)
        is_multi = len(segments) > 1
        page_had_evidence = False
        for segment in segments:
            parsed = bill_parser.parse_bill_text(segment)
            if not bill_parser.parsed_has_evidence(parsed, segment):
                continue
            page_had_evidence = True
            if any(p.get("source") == "nvidia_ocr" for p in group):
                # Provenance/traceability (no schema change - this bill's
                # existing page_number column already anchors it to a
                # document+page for "View Source" navigation; this note
                # documents which text this page's fields were parsed from).
                parsed.warnings.append(
                    "Page text recovered via NVIDIA Nemotron OCR v2 fallback for at least one page in this group "
                    "(local OCR confidence too low or empty)."
                )
            if is_multi:
                parsed.status = "REVIEW_REQUIRED"
                parsed.warnings.append(
                    "This page appears to contain multiple bills; automatic separation may be unreliable."
                )
            if is_grouped:
                # Grouping is always a judgment call, never silently trusted
                # - every multi-page group is flagged for human review with
                # an explicit, honest reason, regardless of how confident its
                # own amount/date extraction otherwise looks.
                parsed.status = "REVIEW_REQUIRED"
                parsed.warnings.append(
                    f"Pages {first_page['page_number']}-{last_page['page_number']} were grouped as one logical "
                    "bill based on continuation evidence (a repeated invoice/bill number or an explicit "
                    "continuation marker) - verify this grouping is correct before relying on it."
                )
            bill = Bill(
                expense_report_id=row.id,
                page_number=first_page["page_number"],
                page_number_end=last_page["page_number"] if is_grouped else None,
                vendor=parsed.vendor,
                bill_number=parsed.bill_number,
                bill_date=parsed.bill_date,
                **_date_fields(parsed.date_reading),
                transaction_reference=parsed.transaction_reference,
                payment_status=parsed.payment_status,
                transaction_time=parsed.transaction_time,
                amount=parsed.amount,
                gstin=parsed.gstin,
                description=parsed.description,
                category_guess=parsed.category_guess,
                raw_text=parsed.raw_text,
                # Read from the FULL bill text, not the 2,000-char raw_text above.
                catering_amount=parsed.catering_amount,
                catering_mentioned=parsed.catering_mentioned,
                room_service_food_amount=parsed.room_service_food_amount,
                room_service_food_mentioned=parsed.room_service_food_mentioned,
                extraction_status=parsed.status,
                extraction_confidence=parsed.confidence,
                extraction_notes="; ".join(parsed.warnings) if parsed.warnings else None,
            )
            db.add(bill)
            bills.append(bill)
            bill_texts.append((bill, segment, parsed))

        if not page_had_evidence:
            # Last-resort fallback: bill_parser.py's own deterministic rules
            # found nothing on this group at all (not even a malformed
            # amount/GSTIN/bill-number), so give the optional NVIDIA
            # classifier one chance per page. Disabled by default and a
            # no-op unless configured (document_classifier.is_available()) -
            # when unavailable or the response doesn't validate, each page is
            # dropped exactly as it always was before this feature existed.
            for page in group:
                classification = document_classifier.classify_page(page["text"])
                if classification is not None and classification.document_type != "NOT_APPLICABLE":
                    bill = _bill_from_classification(row.id, page, classification)
                    db.add(bill)
                    bills.append(bill)
    # Report-level evidence for dates the receipt itself could not settle (see
    # dates.apply_group_evidence), then strong-duplicate detection. Both use only the
    # bills' own text - never an expense date.
    _apply_group_date_evidence(bill_texts)
    duplicates = _find_strong_duplicates(bill_texts)
    for bill in bills:
        found = duplicates.get(id(bill))
        if found:
            canonical, why = found
            _note(bill, f"Duplicate of the bill on page {canonical.page_number}: {why}. Matching uses only the first copy.")
    db.commit()
    for bill in bills:
        db.refresh(bill)

    # Strong duplicates are one document: only the first copy is offered to the
    # matcher, so one payment/invoice can never support two independent claims
    # merely because it was pasted twice (the Duplicate Bill Check then flags the
    # second claim that relies on it). The duplicate page stays visible, with a note.
    duplicate_ids = {b.id for b in bills if id(b) in duplicates}
    bill_dicts = [
        {
            "id": b.id,
            "amount": float(b.amount) if b.amount is not None else None,
            "bill_date": b.bill_date,
            "date_candidates": [date.fromisoformat(c) for c in (b.bill_date_candidates or "").split("|") if c],
            "vendor": b.vendor,
            "description": b.description,
            "category_guess": b.category_guess,
            "bill_number": b.bill_number,
            "page_number": b.page_number,
            "raw_text": b.raw_text,
            # Full-text charge evidence (see category_map.resolve_*).
            "catering_amount": float(b.catering_amount) if b.catering_amount is not None else None,
            "room_service_food_amount": float(b.room_service_food_amount) if b.room_service_food_amount is not None else None,
            "room_service_food_mentioned": b.room_service_food_mentioned,
            "payment_status": b.payment_status,
            "transaction_time": b.transaction_time,
            "transaction_reference": b.transaction_reference,
        }
        for b in bills
        if b.id not in duplicate_ids
    ]
    bills_by_id = {b.id: b for b in bills}
    # The "page position matches order" signal (bill_matcher.py) anchors on
    # the bill region's actual start page - deliberately excludes
    # classifier-derived bills here. A classifier fallback fires exactly
    # where bill_parser.py found NO evidence at all; if such a page happens
    # to sit earlier than every deterministically-extracted bill, using it as
    # the anchor would shift the expected page for EVERY other expense/bill
    # pairing on the report, silently perturbing matches that have nothing
    # to do with the ambiguous page itself. Falls back to including
    # classifier-derived bills only if there are no deterministic bills at
    # all (better than no position signal whatsoever in that rare case).
    first_bill_page = min(
        (b.page_number for b in bills if b.page_number is not None and b.document_type is None),
        default=None,
    )
    if first_bill_page is None:
        first_bill_page = min((b.page_number for b in bills if b.page_number is not None), default=None)

    expenses = db.query(Expense).filter(Expense.expense_report_id == row.id).order_by(Expense.s_no).all()
    if expenses:
        db.query(BillMatchCandidate).filter(
            BillMatchCandidate.expense_id.in_([e.id for e in expenses])
        ).delete(synchronize_session=False)

    # BATTA (daily allowance) has no supporting-document requirement at all
    # (Policy Master's document_required is DNA for BATTA) - it is validated
    # against Trip Report evidence only (see batta_pipeline.py), never against
    # a bill. Scanning bill candidates for it anyway is worse than
    # unnecessary: a BATTA claim can incidentally score against some
    # unrelated travel/cab bill filed the same day, which used to surface as
    # confusing "no bill found"/"ambiguous match" review noise on an expense
    # that was never supposed to have a bill in the first place. A None entry
    # tells bill_matcher.allocate_bills to skip it.
    expense_dicts: list[dict | None] = [
        None
        if _is_batta(expense.category)
        else {
            "s_no": expense.s_no,
            "claimed_amount": float(expense.claimed_amount) if expense.claimed_amount is not None else None,
            "expense_date": expense.expense_date,
            "category": expense.category,
            "expense_details": expense.expense_details,
            "expense_rank": rank,
        }
        for rank, expense in enumerate(expenses, start=1)
    ]

    # Whole-report allocation (see bill_matcher.allocate_bills): per-expense
    # matching, split-payment exclusion of already-claimed bills, and - new -
    # evidence-based reconciliation when one bill was confidently chosen by
    # more than one claim, so the claim the bill actually fits is not
    # penalised for merely being processed second (or having a higher S.No).
    outcomes = bill_matcher.allocate_bills(expense_dicts, bill_dicts, first_bill_page=first_bill_page)

    # Bill.matched_expense_id records ONE expense per bill: the best-fitting
    # confirmed claimant (highest match score), not simply the last one written.
    best_claimant_score: dict[int, float] = {}

    for expense, outcome in zip(expenses, outcomes):
        if outcome is None:
            expense.bill_match_status = "NOT_APPLICABLE"
            expense.bill_match_score = None
            expense.bill_match_confidence = None
            expense.bill_match_reasons = "BATTA has no supporting-document requirement; validated against Trip Report evidence instead."
            expense.bill_found = False
            expense.bill_id = None
            expense.bill_amount = None
            expense.bill_date = None
            expense.candidate_bill_id = None
            continue

        expense.bill_match_status = outcome.status
        expense.bill_match_score = outcome.score
        expense.bill_match_confidence = outcome.confidence
        expense.bill_match_reasons = "; ".join(outcome.reasons) if outcome.reasons else None
        expense.candidate_bill_id = outcome.candidate_bill_id

        if outcome.status == "MATCHED" and outcome.matched_bill_id is not None:
            matched_bill = bills_by_id[outcome.matched_bill_id]
            expense.bill_found = True
            expense.bill_id = matched_bill.id
            expense.bill_amount = matched_bill.amount
            expense.bill_date = matched_bill.bill_date
            matched_bill.match_status = "MATCHED"
            if matched_bill.id not in best_claimant_score or (outcome.score or 0) > best_claimant_score[matched_bill.id]:
                matched_bill.matched_expense_id = expense.id
                best_claimant_score[matched_bill.id] = outcome.score or 0
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
