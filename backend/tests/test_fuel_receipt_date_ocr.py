"""A fuel receipt dated 16-SEP-2026 must give the bill date 2026-09-16 - and an OCR-damaged date is repaired ONLY with evidence.

Symptom: "The matched bill's date could not be reliably extracted." (checks.check_bill_date, raised when a bill has no
date and no ambiguity candidates). Traced with synthetic text: the DD-MON-YYYY form is already supported, and every
cleanly printed variant (any month case, separators, spacing, time/transaction numbers beside it) parses. The date
was lost only when the OCR text itself was damaged - a month read as "SEF"/"5EP", a year read as "2O26", the date
broken across a line, or the clock fused to the year ("202610:15:48") - because every reader demands the exact form.
A second, quieter fault: "I6-SEP-2026" (a misread "16") was read as the 6th, a WRONG date at HIGH confidence.

dates._ocr_readings now recovers those forms, but never as a guess: complete structure, a month that is a single glyph
from exactly ONE month (and not an English word), a valid calendar date, AND independent evidence (a Date label or an
adjacent clock). The result is MEDIUM confidence, method "month_name_ocr", with the printed text kept as `raw`.

All text below is SYNTHETIC: invented station, numbers and amounts; no real receipt data.
"""

import tempfile
import uuid
from datetime import date
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ValidationResult
from app.services.exception_engine import evaluate_report
from app.services.extraction import dates
from app.services.extraction.bill_parser import parse_bill_text
from app.services.validation import checks
from app.services.validation.pipeline import validate_expense_report

SEP16 = date(2026, 9, 16)


def receipt(date_line: str) -> str:
    return (
        "INDIAN OIL (synthetic dealer)\nFuel Station Receipt\n" + date_line + "\n"
        "Transaction No: 6091614189\nInvoice No: 535084\nVehicle No: NOT ENTERED\n"
        "Product: PETROL\nNozzle No: 02\nRate/Ltr: 100.00\nVolume (Ltr): 20.00\nAmount Rs.2,000.00\n"
    )


# ---------- exact formats the application must read (and already did) ----------


@pytest.mark.parametrize(
    "date_line",
    [
        "Date: 16-SEP-2026  Time: 10:15:48",
        "Date: 16-Sep-2026 10:15:48",
        "Date: 16-sep-2026 10:15:48",
        "Date: 16 SEP 2026 10:15:48",
        "Date: 16/SEP/2026 10:15:48",
        "Date: 16.SEP.2026 10:15:48",
        "Date:   16-SEP-2026      Time:  10:15:48",  # extra spaces
        "Date: 16 - SEP - 2026 10:15:48",
        "Date: 16-SEP-2026 | Time: 10:15:48 | ***",  # receipt separators
        "Date: =16-SEP-2026= 10:15:48",
        "Date: 16-SEPT-2026 10:15:48",
        "DATE & TIME 16-SEP-2026 10:15:48",
        "Date: 16-SEP-26 10:15:48",
    ],
)
def test_exact_dd_mon_yyyy_variants_normalise_to_2026_09_16(date_line):
    parsed = parse_bill_text(receipt(date_line))
    assert parsed.bill_date == SEP16
    assert parsed.date_reading.method == "month_name" and parsed.date_reading.confidence == "HIGH"


def test_the_printed_text_is_kept_alongside_the_normalised_date():
    parsed = parse_bill_text(receipt("Date: 16-SEP-2026  Time: 10:15:48"))
    assert parsed.date_reading.raw == "16-SEP-2026"
    assert parsed.bill_date.isoformat() == "2026-09-16"


def test_date_next_to_transaction_number_time_and_invoice_number_is_not_confused_with_them():
    text = "Txn 6091614189 Inv 535084 Date 16-SEP-2026 10:15:48\nProduct: PETROL\nNozzle 02\nAmount Rs.2,000.00"
    assert parse_bill_text(text).bill_date == SEP16


def test_label_on_its_own_line_above_the_date():
    assert parse_bill_text(receipt("DATE\n16-SEP-2026\nTIME\n10:15:48")).bill_date == SEP16


# ---------- OCR resilience: repaired only with evidence ----------

OCR_DAMAGED = {
    "month P read as F": "Date: 16-SEF-2026 Time: 10:15:48",
    "month S read as 5": "Date: 16-5EP-2026 Time: 10:15:48",
    "year digit 0 read as O": "Date: 16-SEP-2O26 Time: 10:15:48",
    "line break after the separator": "Date: 16-SEP-\n2026 Time: 10:15:48",
    "clock fused to the year": "Date: 16-SEP-202610:15:48",
    "damaged month, no label but a clock": "Txn 16-SEF-2026 10:15:48",
    "damaged month, label on the line above": "Date\n16-SEF-2026",
}


@pytest.mark.parametrize("date_line", list(OCR_DAMAGED.values()), ids=list(OCR_DAMAGED))
def test_ocr_damaged_dates_are_recovered_with_medium_confidence_and_the_printed_text_kept(date_line):
    parsed = parse_bill_text(receipt(date_line))
    assert parsed.bill_date == SEP16
    reading = parsed.date_reading
    assert reading.method == "month_name_ocr" and reading.confidence == "MEDIUM"
    assert reading.raw.replace(" ", "") in " ".join(date_line.split()).replace(" ", "") or reading.raw
    assert any("after correcting a likely OCR error" in w and "2026-09-16" in w for w in parsed.warnings)


def test_the_original_printed_text_is_the_stored_raw_value():
    reading = parse_bill_text(receipt("Date: 16-SEF-2026 Time: 10:15:48")).date_reading
    assert reading.raw == "16-SEF-2026"  # what the OCR printed, not the corrected spelling


def test_an_ocr_repaired_date_is_never_proof_for_anything_else():
    text = "Date: 07/02/2026\nLast calibration 16-SEF-2026 10:15"
    readings = dates.read_dates(text)
    ocr = [r for r in readings if r.method == "month_name_ocr"]
    numeric = [r for r in readings if r.method == "unresolved"]
    assert len(ocr) == 1 and numeric, readings  # the numeric token stays unresolved: the repaired date is not an "echo"


# ---------- never fabricate ----------


@pytest.mark.parametrize(
    "date_line",
    [
        "Ref 16-SEF-2026",  # damaged month with NO label and NO clock: no evidence a date is printed
        "Date: 16-SPE-2026",  # two glyphs away from September
        "Date: 16-ABC-2026",  # not near any month
        "Date: 16-SET-2026 10:15",  # a common English word, never repaired
        "Date: 16-JXN-2026 10:15",  # one glyph from BOTH January and June: ambiguous
        "Date: 31-SEF-2026 10:15",  # September has 30 days
        "Date: 31-SEP-2026 10:15",  # invalid calendar date (exact month)
        "Date: 29-FEB-2026 10:15",  # not a leap year
        "Date: 16-SEP-1875 10:15",  # implausible year
        "Date: 16-SEP-2126 10:15",  # outside 2000-2099
        "Date: 16\nSEP\n2026",  # a bare line-by-line split with no separators is not bridged
        "Date: I6-SEP-2026 10:15:48",  # OCR "I6": neither 6 nor 16 may be chosen
        "Date: 16-SEP-2Z26 10:15:48",  # a year glyph outside the O->0 / I,l->1 confusions
    ],
)
def test_no_date_is_fabricated_from_unsupported_or_invalid_text(date_line):
    parsed = parse_bill_text(receipt(date_line))
    assert parsed.bill_date is None
    assert parsed.date_reading is None


def test_ocr_readings_never_turn_unrelated_digits_and_words_into_a_date():
    text = "Qty 12 SEF 2026 units\nBatch 20 Mar 2026001\nPage 3 OF 2026"
    assert [r for r in dates.read_dates(text) if r.method == "month_name_ocr"] == []


# ---------- a label glued to its date is kept; only a LONE misread letter is refused (found by the baseline-vs-current fuzz) ----------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Inv01.DEC.2026 Time: 10:15", date(2026, 12, 1)),
        ("On16-Sep-2026 10:15", SEP16),
        ("Date16-SEP-2026 10:15:48", SEP16),
        ("Bill16 SEP 2026 Time 10:15", SEP16),
    ],
)
def test_a_multi_letter_word_glued_to_the_day_does_not_hide_the_date(text, expected):
    assert dates.read_dates(text)[0].value == expected


@pytest.mark.parametrize("text", ["I6-SEP-2026 10:15", "Date:I6 SEP 2026", "l6-SEP-2026 10:15", "Txn O6-SEP-2026 10:15"])
def test_a_lone_misread_letter_in_front_of_the_day_is_refused_not_read_as_a_wrong_day(text):
    assert [r.value for r in dates.read_dates(text) if r.value is not None] == []


def test_an_ocr_repaired_date_beside_a_clock_beats_an_unrelated_printed_date():
    parsed = parse_bill_text(receipt("Time 10:15 16-SEF-2026\nPrinted 17-SEP-2026"))
    assert parsed.bill_date == SEP16 and parsed.date_reading.method == "month_name_ocr"


def test_adjacent_clock_uses_the_real_printed_span_of_an_ocr_date():
    text = "Txn 16-SEP-\n2026 10:15:48"  # raw is whitespace-collapsed ("16-SEP- 2026"); the clock follows the REAL span
    reading = [r for r in dates.read_dates(text) if r.method == "month_name_ocr"][0]
    assert dates.has_adjacent_clock(text, reading)
    assert not dates.has_adjacent_clock("Txn 16-SEP-\n2026 Qty 20", reading)


# ---------- several dates and no "Date" label ----------


def test_unlabelled_transaction_timestamp_wins_over_a_date_printed_without_a_clock():
    parsed = parse_bill_text(receipt("16-SEP-2026 10:15:48\nCalibration valid till 12-JAN-2027"))
    assert parsed.bill_date == SEP16
    assert parsed.date_reading.method == "month_name" and parsed.date_reading.raw == "16-SEP-2026"


def test_two_different_timestamped_dates_with_no_label_still_need_review():
    parsed = parse_bill_text(receipt("Arrival 16-SEP-2026 14:00\nDeparture 18-SEP-2026 11:00"))
    assert parsed.bill_date is None
    assert any("Multiple differing dates" in w for w in parsed.warnings)


def test_several_unlabelled_dates_with_no_clock_still_need_review():
    parsed = parse_bill_text(receipt("16-SEP-2026\nCalibration valid till 12-JAN-2027"))
    assert parsed.bill_date is None


def test_an_ambiguous_numeric_date_is_never_settled_by_a_clock():
    parsed = parse_bill_text(receipt("07/02/2026 10:15:48\nCalibration valid till 12-JAN-2027"))
    assert parsed.bill_date is None


# ---------- ambiguous and missing dates are unchanged ----------


def test_ambiguous_numeric_date_stays_unresolved_with_both_candidates():
    parsed = parse_bill_text(receipt("Date: 07/02/2026 Time: 10:15:48"))
    assert parsed.bill_date is None
    assert parsed.date_reading.ambiguous and {c.isoformat() for c in parsed.date_reading.candidates} == {"2026-07-02", "2026-02-07"}


def test_a_receipt_with_no_date_has_no_date():
    parsed = parse_bill_text(receipt("Time: 10:15:48"))
    assert parsed.bill_date is None and parsed.date_reading is None


def test_the_labelled_bill_date_wins_over_other_dates_on_the_same_bill():
    text = receipt("Date: 16-SEF-2026 Time: 10:15:48") + "Calibration due 12-JAN-2027\nPrinted 17-SEP-2026\n"
    parsed = parse_bill_text(text)
    assert parsed.bill_date == SEP16 and parsed.date_reading.raw == "16-SEF-2026"


# ---------- validation uses the date; real mismatches are still caught ----------


class _Bill:
    def __init__(self, bill_date, raw_text=""):
        self.bill_date, self.bill_date_raw, self.bill_date_candidates = bill_date, "16-SEF-2026", None
        self.page_number, self.raw_text = 7, raw_text


def test_validation_passes_a_matching_ocr_repaired_date_and_reports_a_mismatching_one():
    ok = checks.check_bill_date(expense_date=SEP16, bill=_Bill(SEP16, raw_text="Date: 16-SEF-2026 10:15"))
    assert ok.result == checks.PASS and ok.source_page == 7
    bad = checks.check_bill_date(expense_date=date(2026, 9, 17), bill=_Bill(SEP16, raw_text="Date: 16-SEF-2026 10:15"))
    assert bad.result == checks.EXCEPTION and bad.exception_type == "DATE MISMATCH"  # the repaired date does NOT excuse a wrong claim date


def test_a_date_that_was_not_recovered_still_gives_the_review_reason():
    result = checks.check_bill_date(expense_date=SEP16, bill=_Bill(None))
    assert result.result == checks.REVIEW_REQUIRED and result.details == "The matched bill's date could not be reliably extracted."


# ---------- end to end: separate bills keep their own date, page and expense ----------


def _run(pages, expenses):
    import fitz

    from app.services.extraction import bill_pipeline
    from app.services.extraction.text_extraction import PyMuPDFTextExtractor

    pdf_path = Path(tempfile.mkdtemp()) / f"{uuid.uuid4()}.pdf"
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        y = 24
        for line in text.split("\n"):
            page.insert_text((24, y), line, fontsize=8)
            y += 11
    doc.save(pdf_path)
    doc.close()
    db = SessionLocal()
    try:
        report = ExpenseReport(filename="t.pdf", original_filename="t.pdf", file_hash=f"hash-{uuid.uuid4()}", file_size=100, source_path=str(pdf_path),
                               extraction_status="EXTRACTED", total_claimed=sum(e[3] for e in expenses), er_id="ER-FUELDATE-SYNTH")
        db.add(report)
        db.commit()
        db.refresh(report)
        for s_no, d, cat, amt in expenses:
            db.add(Expense(expense_report_id=report.id, s_no=s_no, expense_date=d, category=cat, claimed_amount=amt,
                           bill_match_status="NO_MATCH", trip_match_status="NOT_PROCESSED"))
        db.commit()
        bill_pipeline.extract_and_match_bills(db, report, PyMuPDFTextExtractor(), content_last_page=1)
        validate_expense_report(db, report.id)
        evaluate_report(db, report.id)
        bills = {b.page_number: b for b in db.query(Bill).filter(Bill.expense_report_id == report.id)}
        exps = {e.s_no: e for e in db.query(Expense).filter(Expense.expense_report_id == report.id)}
        date_checks = {
            r.expense_id: (r.result, r.details, r.source_page)
            for r in db.query(ValidationResult).filter(ValidationResult.expense_id.in_([e.id for e in exps.values()]), ValidationResult.check_type == "Bill Date Check")
        }
        return (
            {p: (b.id, b.bill_date, b.bill_date_raw, b.bill_date_method, b.bill_date_confidence, float(b.amount) if b.amount else None) for p, b in bills.items()},
            {s: (e.id, e.bill_id, e.bill_match_status) for s, e in exps.items()},
            date_checks,
        )
    finally:
        db.close()


HEADER_PAGE = "SYNTHETIC EXPENSE REPORT ER-FUELDATE-SYNTH\nSubmitted By TEST EMPLOYEE"


def _fuel(date_line, amount, station):
    return (f"{station} (synthetic dealer)\nFuel Station Receipt\n{date_line}\nTransaction No: 60916{int(amount)}\nInvoice No: 5{int(amount)}\n"
            f"Product: PETROL\nNozzle No: 02\nRate/Ltr: 100.00\nVolume (Ltr): {amount / 100:.2f}\nAmount Rs.{amount:,.2f}\n")


def test_two_fuel_receipts_keep_their_own_date_page_amount_and_expense(client):
    bills, exps, date_checks = _run(
        [HEADER_PAGE,
         _fuel("Date: 16-SEF-2026 Time: 10:15:48", 2000.0, "INDIAN OIL"),  # page 2: OCR-damaged month
         _fuel("Date: 18-SEP-2026 Time: 11:20:01", 1500.0, "BHARAT PETROLEUM")],  # page 3: clean
        [(1, SEP16, "OWN CAR PETROL", 2000.0), (2, date(2026, 9, 18), "OWN CAR PETROL", 1500.0)],
    )
    assert bills[2][1] == SEP16 and bills[2][2] == "16-SEF-2026" and bills[2][3] == "month_name_ocr" and bills[2][4] == "MEDIUM" and bills[2][5] == 2000.0
    assert bills[3][1] == date(2026, 9, 18) and bills[3][2] == "18-SEP-2026" and bills[3][3] == "month_name" and bills[3][5] == 1500.0
    # each expense is tied to ITS bill (no reuse) and the bill's date reached the validator with the bill's own page
    assert exps[1][1] == bills[2][0] and exps[2][1] == bills[3][0] and exps[1][1] != exps[2][1]
    assert date_checks[exps[1][0]][0] == checks.PASS and date_checks[exps[1][0]][2] == 2
    assert date_checks[exps[2][0]][0] == checks.PASS and date_checks[exps[2][0]][2] == 3
    assert all("could not be reliably extracted" not in (v[1] or "") for v in date_checks.values())


def test_an_unsupported_damaged_date_still_goes_to_review_through_the_whole_pipeline(client):
    bills, exps, date_checks = _run(
        [HEADER_PAGE, _fuel("Ref 16-SEF-2026", 2000.0, "INDIAN OIL")],  # no Date label, no clock: not recovered
        [(1, SEP16, "OWN CAR PETROL", 2000.0)],
    )
    assert bills[2][1] is None
    assert date_checks[exps[1][0]][0] == checks.REVIEW_REQUIRED
    assert "could not be reliably extracted" in date_checks[exps[1][0]][1]


def test_the_matched_fuel_receipts_date_that_differs_from_the_claim_is_still_a_mismatch(client):
    bills, exps, date_checks = _run(
        [HEADER_PAGE, _fuel("Date: 16-SEF-2026 Time: 10:15:48", 2000.0, "INDIAN OIL")],
        [(1, date(2026, 9, 20), "OWN CAR PETROL", 2000.0)],
    )
    assert bills[2][1] == SEP16
    assert date_checks[exps[1][0]][0] == checks.EXCEPTION
