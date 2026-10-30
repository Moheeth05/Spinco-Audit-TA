"""Room-service / food and catering evidence on bills longer than the 2,000-
character `raw_text` cap.

`Bill.raw_text` stays capped at 2,000 characters, but a charge printed after
that point (a hotel folio's ROOM SERVICE row, an IRCTC "Catering Charges" line)
must still be found. The parser therefore reads the FULL bill text once and
persists the structured evidence:

    room_service_food_amount / room_service_food_mentioned   (new)
    catering_amount (existing column) / catering_mentioned   (new flag)

Everything below is SYNTHETIC; no real guest, invoice or receipt data.
"""

import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from sqlalchemy import create_engine, inspect, text

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ExceptionRecord
from app.services import db_migrations
from app.services.exception_engine import evaluate_report
from app.services.extraction import bill_matcher, bill_parser, bill_pipeline
from app.services.extraction.text_extraction import PyMuPDFTextExtractor
from app.services.validation import checks
from app.services.validation.pipeline import validate_expense_report

RAW_TEXT_CAP = 2000

# Neutral filler: no amounts, no dates, no food/room-service/catering/breakfast words.
_PAD_LINE = "Hotel policy note: checkout time is twelve noon and late checkout is subject to availability."


def _padding(min_chars: int = 2300) -> str:
    lines: list[str] = []
    while sum(len(line) + 1 for line in lines) < min_chars:
        lines.append(_PAD_LINE)
    return "\n".join(lines)


def _long_folio(middle: str, total: str = "Total 5,457.00") -> str:
    """Room charges first, then >2,000 chars of filler, THEN `middle`, then the total."""
    text_ = "\n".join(
        [
            "Aurangabad Gymkhana Club",
            "Tax Invoice No: 2526/2923",
            "Date: 24/09/2026",
            "Check-in 22/09/2026 Check-out 24/09/2026",
            "Room Charges 2,550.00",
            "Room Charges 2,550.00",
            _padding(),
            middle,
            total,
        ]
    )
    assert text_.index(middle) > RAW_TEXT_CAP, "fixture must put the charge beyond the raw_text cap"
    return text_


LONG_ROOM_SERVICE = _long_folio("ROOM SERVICE Restaurant Charges RS26/3238 357.00")
LONG_FOODING = _long_folio("FOODING 508.00", total="TOTAL 2308.00")
LONG_FOOD_NO_AMOUNT = _long_folio("ROOM SERVICE Restaurant Charges RS26/3238")
LONG_BREAKFAST_PLAN = _long_folio("Rate Type: Breakfast", total="Total 5,100.00")
LONG_CATERING = _long_folio("Catering Charges (Incl. of GST) 368.00", total="Total Fare 2,368.00")
LONG_CATERING_NO_AMOUNT = _long_folio("Catering Charges (Incl. of GST) included", total="Total Fare 2,368.00")


def _bill(**kwargs):
    defaults = dict(page_number=4, page_number_end=None, amount=5457.0, bill_date=date(2026, 9, 24), category_guess="Lodging", raw_text="")
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------- 1. the parser reads the FULL text and keeps raw_text capped ----------


def test_parser_finds_room_service_charge_beyond_the_raw_text_cap():
    parsed = bill_parser.parse_bill_text(LONG_ROOM_SERVICE)
    assert len(parsed.raw_text) == RAW_TEXT_CAP
    assert "ROOM SERVICE" not in parsed.raw_text  # truly beyond the cap
    assert parsed.room_service_food_amount == 357.0
    assert parsed.room_service_food_mentioned is True


def test_parser_finds_fooding_charge_beyond_the_raw_text_cap():
    parsed = bill_parser.parse_bill_text(LONG_FOODING)
    assert len(parsed.raw_text) == RAW_TEXT_CAP
    assert parsed.room_service_food_amount == 508.0
    assert parsed.room_service_food_mentioned is True


def test_parser_records_a_mention_without_inventing_an_unreadable_amount():
    parsed = bill_parser.parse_bill_text(LONG_FOOD_NO_AMOUNT)
    assert parsed.room_service_food_amount is None
    assert parsed.room_service_food_mentioned is True


def test_parser_finds_catering_charge_beyond_the_raw_text_cap():
    parsed = bill_parser.parse_bill_text(LONG_CATERING)
    assert len(parsed.raw_text) == RAW_TEXT_CAP
    assert "Catering" not in parsed.raw_text
    assert parsed.catering_amount == 368.0
    assert parsed.catering_mentioned is True


def test_parser_records_a_catering_mention_without_inventing_an_amount():
    parsed = bill_parser.parse_bill_text(LONG_CATERING_NO_AMOUNT)
    assert parsed.catering_amount is None
    assert parsed.catering_mentioned is True


def test_parser_records_no_food_evidence_for_a_breakfast_rate_plan():
    parsed = bill_parser.parse_bill_text(LONG_BREAKFAST_PLAN)
    assert parsed.room_service_food_amount is None
    assert parsed.room_service_food_mentioned is False
    assert parsed.catering_amount is None
    assert parsed.catering_mentioned is False


def test_parser_never_reads_fssai_or_hotel_header_text_as_food():
    folio = _long_folio("FSSAI Food Safety and Standards Authority of India Lic 11521999000123\nHotel Food Court Road", total="Total 5,100.00")
    parsed = bill_parser.parse_bill_text(folio)
    assert parsed.room_service_food_amount is None
    assert parsed.room_service_food_mentioned is False


# ---------- 2. validation uses the stored evidence when raw_text no longer has it ----------


def _stored_room_service(amount=357.0, mentioned=True, **kwargs):
    return _bill(raw_text="Aurangabad Gymkhana Club\nRoom Charges 2,550.00", room_service_food_amount=amount, room_service_food_mentioned=mentioned, **kwargs)


def test_lodging_review_is_raised_from_stored_evidence():
    result = checks.check_room_service_food(category="LODGING", bill=_stored_room_service(), claimed_amount=5100.0)
    assert result.result == checks.REVIEW_REQUIRED
    assert "Rs.357.00" in result.remarks


def test_lodging_amount_matches_once_stored_room_service_is_excluded():
    result = checks.check_bill_amount(claimed_amount=5100.0, bill=_stored_room_service(), category="LODGING")
    assert result.result == checks.PASS
    assert "room service / food" in result.remarks


def test_stored_mention_without_amount_is_review_required_and_never_excludes_anything():
    bill = _stored_room_service(amount=None, mentioned=True)
    review = checks.check_room_service_food(category="LODGING", bill=bill, claimed_amount=5100.0)
    assert review.result == checks.REVIEW_REQUIRED
    assert "could not be determined" in review.remarks
    amount = checks.check_bill_amount(claimed_amount=5100.0, bill=bill, category="LODGING")
    assert amount.result == checks.EXCEPTION and amount.exception_type == "AMOUNT MISMATCH"


@pytest.mark.parametrize("category", [None, "STAFF WELFARE", "MARKETING EXPENSES - FOOD", "TRAIN / BUS FARE", "CAB CHARGES"])
def test_stored_food_evidence_is_never_used_outside_lodging(category):
    bill = _stored_room_service()
    assert checks.check_room_service_food(category=category, bill=bill, claimed_amount=5100.0).result == checks.NOT_APPLICABLE
    amount = checks.check_bill_amount(claimed_amount=5100.0, bill=bill, category=category)
    assert amount.result == checks.EXCEPTION and amount.exception_type == "AMOUNT MISMATCH"


def test_stored_no_evidence_means_no_review():
    bill = _bill(raw_text="Room Charges 2,550.00\nRate Type: Breakfast", amount=5100.0, room_service_food_amount=None, room_service_food_mentioned=False)
    assert checks.check_room_service_food(category="LODGING", bill=bill, claimed_amount=5100.0).result == checks.PASS


# ---------- 3. catering uses the stored full-text evidence too ----------


def _stored_catering(amount=368.0, mentioned=True, raw_text="IRCTC e-ticket\nTicket fare 2,000.00", **kwargs):
    return _bill(category_guess="Train", amount=2368.0, raw_text=raw_text, catering_amount=amount, catering_mentioned=mentioned, **kwargs)


def test_catering_review_is_raised_from_stored_evidence():
    result = checks.check_catering(category="TRAIN / BUS FARE", bill=_stored_catering(), claimed_amount=2000.0)
    assert result.result == checks.REVIEW_REQUIRED
    assert "Rs.368.00" in result.remarks


def test_catering_amount_matches_once_stored_catering_is_excluded():
    result = checks.check_bill_amount(claimed_amount=2000.0, bill=_stored_catering(), category="TRAIN / BUS FARE")
    assert result.result == checks.PASS
    assert "catering" in result.remarks


def test_stored_catering_mention_without_amount_is_review_required():
    bill = _stored_catering(amount=None, mentioned=True)
    result = checks.check_catering(category="TRAIN / BUS FARE", bill=bill, claimed_amount=2000.0)
    assert result.result == checks.REVIEW_REQUIRED
    assert "could not be determined" in result.remarks


def test_stored_full_text_catering_amount_beats_a_number_cut_in_half_at_the_cap():
    # raw_text hit the cap mid-number ("...Rs.3"): it is only a prefix, so the full-text reading (368.00) is the real one.
    capped = ("Ticket fare 2,000.00\n" + "x" * RAW_TEXT_CAP)[: RAW_TEXT_CAP - len("\nCatering Charges Rs.3")] + "\nCatering Charges Rs.3"
    assert len(capped) == RAW_TEXT_CAP
    bill = _stored_catering(raw_text=capped, amount=368.0)
    result = checks.check_catering(category="TRAIN / BUS FARE", bill=bill, claimed_amount=2000.0)
    assert "Rs.368.00" in result.remarks and "Rs.3.00" not in result.remarks


def test_below_the_cap_raw_text_is_complete_so_its_own_reading_still_wins_over_a_stored_amount():
    # Existing precedence, unchanged (also pinned by test_document_classifier): raw_text regex first, stored amount second.
    regex_wins = _stored_catering(amount=999.0, raw_text="Catering charges Rs.150.00")
    assert "Rs.150.00" in checks.check_catering(category="TRAIN / BUS FARE", bill=regex_wins).remarks
    stored_fallback = _stored_catering(amount=250.0, raw_text="Ticket fare 2,000.00")
    assert "Rs.250.00" in checks.check_catering(category="TRAIN / BUS FARE", bill=stored_fallback).remarks


# ---------- 4. records with no stored evidence use the existing raw_text fallback ----------


def test_legacy_bill_without_new_attributes_uses_raw_text_for_room_service():
    legacy = SimpleNamespace(page_number=4, page_number_end=None, amount=5457.0, bill_date=date(2026, 9, 24), category_guess="Lodging",
                             raw_text="Room Charges 2,550.00\nROOM SERVICE 357.00\nTotal 5,457.00")
    assert checks.check_room_service_food(category="LODGING", bill=legacy, claimed_amount=5100.0).result == checks.REVIEW_REQUIRED
    assert checks.check_bill_amount(claimed_amount=5100.0, bill=legacy, category="LODGING").result == checks.PASS


def test_legacy_bill_without_new_attributes_uses_raw_text_for_catering():
    legacy = SimpleNamespace(page_number=4, page_number_end=None, amount=2368.0, bill_date=date(2026, 9, 24), category_guess="Train",
                             raw_text="Ticket fare 2,000.00\nCatering Charges 368.00\nTotal 2,368.00")
    assert checks.check_catering(category="TRAIN / BUS FARE", bill=legacy, claimed_amount=2000.0).result == checks.REVIEW_REQUIRED
    assert checks.check_bill_amount(claimed_amount=2000.0, bill=legacy, category="TRAIN / BUS FARE").result == checks.PASS


def test_legacy_bill_with_null_stored_columns_uses_raw_text():
    row = _bill(raw_text="ROOM SERVICE 357.00", room_service_food_amount=None, room_service_food_mentioned=None, catering_amount=None, catering_mentioned=None)
    assert checks.check_room_service_food(category="LODGING", bill=row).result == checks.REVIEW_REQUIRED


# ---------- 5. matcher uses the stored evidence ----------


def _expense(category, claimed):
    return {"claimed_amount": claimed, "expense_date": date(2026, 9, 24), "category": category, "expense_details": None}


def _bill_dict(raw_text, amount, **extra):
    return {"id": 1, "amount": amount, "bill_date": None, "vendor": None, "description": None, "category_guess": None, "bill_number": None, "raw_text": raw_text, **extra}


def test_matcher_credits_the_amount_from_stored_room_service_evidence_for_lodging_only():
    bill = _bill_dict("Room Charges 2,550.00", 5457.0, room_service_food_amount=357.0, room_service_food_mentioned=True)
    score, reasons = bill_matcher.score_pair(_expense("LODGING", 5100.0), bill)
    assert score >= 40 and any("room service / food is excluded" in r for r in reasons)
    other_score, other_reasons = bill_matcher.score_pair(_expense("STAFF WELFARE", 5100.0), bill)
    assert any("negative signal" in r for r in other_reasons) and other_score < 0
    assert bill_matcher._amount_supported(_expense("LODGING", 5100.0), bill)
    assert not bill_matcher._amount_supported(_expense("STAFF WELFARE", 5100.0), bill)


def test_matcher_credits_the_amount_from_stored_catering_evidence():
    bill = _bill_dict("Ticket fare 2,000.00", 2368.0, catering_amount=368.0, catering_mentioned=True)
    score, reasons = bill_matcher.score_pair(_expense("TRAIN / BUS FARE", 2000.0), bill)
    assert score >= 40 and any("catering is excluded" in r for r in reasons)
    assert bill_matcher._amount_supported(_expense("TRAIN / BUS FARE", 2000.0), bill)


def test_matcher_still_works_for_a_bill_dict_with_no_stored_evidence_keys():
    bill = _bill_dict("ROOM SERVICE 357.00", 5457.0)  # no new keys at all (an old-style dict)
    assert bill_matcher._amount_supported(_expense("LODGING", 5100.0), bill)
    assert not bill_matcher._amount_supported(_expense("LODGING", 4000.0), bill)


# ---------- 6. end to end through bill_pipeline: persisted, then validated ----------


def _pdf_with_text(tmp_path: Path, text_: str) -> Path:
    pdf_path = tmp_path / f"{uuid.uuid4()}.pdf"
    doc = fitz.open()
    page = doc.new_page()
    y = 20
    for line in text_.split("\n"):
        page.insert_text((24, y), line, fontsize=7)
        y += 8.5
    doc.save(pdf_path)
    doc.close()
    return pdf_path


def _run_pipeline(tmp_path, folio, *, category, claimed, total_claimed=None):
    pdf_path = _pdf_with_text(tmp_path, folio)
    db = SessionLocal()
    try:
        report = ExpenseReport(
            filename="t.pdf", original_filename="t.pdf", file_hash=f"hash-{uuid.uuid4()}", file_size=100, source_path=str(pdf_path),
            extraction_status="EXTRACTED", total_claimed=total_claimed if total_claimed is not None else claimed, er_id="ER-LONG-EVIDENCE",
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        db.add(Expense(expense_report_id=report.id, s_no=1, expense_date=date(2026, 9, 24), category=category, claimed_amount=claimed,
                       bill_match_status="NO_MATCH", trip_match_status="NOT_PROCESSED"))
        db.commit()
        bill_pipeline.extract_and_match_bills(db, report, PyMuPDFTextExtractor(), content_last_page=0)
        validate_expense_report(db, report.id)
        evaluate_report(db, report.id)
        bill = db.query(Bill).filter(Bill.expense_report_id == report.id).one()
        expense = db.query(Expense).filter(Expense.expense_report_id == report.id).one()
        types =[r.exception_type for r in db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report.id)]
        return {
            "raw_len": len(bill.raw_text or ""), "raw_text": bill.raw_text or "", "amount": float(bill.amount) if bill.amount is not None else None,
            "rs_amount": bill.room_service_food_amount, "rs_mentioned": bill.room_service_food_mentioned,
            "cat_amount": float(bill.catering_amount) if bill.catering_amount is not None else None, "cat_mentioned": bill.catering_mentioned,
            "types": types, "match_status": expense.bill_match_status, "match_reasons": expense.bill_match_reasons or "",
        }
    finally:
        db.close()


def test_pipeline_persists_and_flags_room_service_beyond_the_cap(client, tmp_path):
    out = _run_pipeline(tmp_path, LONG_ROOM_SERVICE, category="LODGING", claimed=5100.0)
    assert out["raw_len"] <= RAW_TEXT_CAP and "ROOM SERVICE" not in out["raw_text"]
    assert out["amount"] == 5457.0
    assert out["rs_amount"] == 357.0 and out["rs_mentioned"] is True
    assert "ROOM SERVICE / FOOD INCLUDED" in out["types"]
    assert "AMOUNT MISMATCH" not in out["types"]


def test_pipeline_breakfast_rate_plan_raises_nothing(client, tmp_path):
    out = _run_pipeline(tmp_path, LONG_BREAKFAST_PLAN, category="LODGING", claimed=5100.0)
    assert not out["rs_mentioned"] and out["rs_amount"] is None
    assert "ROOM SERVICE / FOOD INCLUDED" not in out["types"]
    assert "AMOUNT MISMATCH" not in out["types"]


def test_pipeline_food_on_a_non_lodging_claim_is_never_excluded(client, tmp_path):
    out = _run_pipeline(tmp_path, LONG_ROOM_SERVICE, category="STAFF WELFARE", claimed=5100.0)
    assert out["rs_amount"] == 357.0  # evidence is stored regardless of category ...
    assert "ROOM SERVICE / FOOD INCLUDED" not in out["types"]  # ... but only LODGING ever uses it
    # The 357.00 is never subtracted for a non-LODGING claim: no "excluded" amount credit,
    # the 5,457.00 folio does not support the 5,100.00 claim, and the claim is left unsupported.
    assert "excluded" not in out["match_reasons"]
    assert out["match_status"] != "MATCHED"
    assert "MISSING BILL" in out["types"]


def test_pipeline_persists_and_flags_catering_beyond_the_cap(client, tmp_path):
    out = _run_pipeline(tmp_path, LONG_CATERING, category="TRAIN / BUS FARE", claimed=2000.0)
    assert out["raw_len"] <= RAW_TEXT_CAP and "Catering" not in out["raw_text"]
    assert out["cat_amount"] == 368.0 and out["cat_mentioned"] is True
    assert "CATERING INCLUDED" in out["types"]
    assert "AMOUNT MISMATCH" not in out["types"]


# ---------- 7. schema: fresh database and existing-database migration ----------


NEW_BILL_COLUMNS = {"room_service_food_amount", "room_service_food_mentioned", "catering_mentioned"}


def test_fresh_database_schema_has_the_new_columns():
    from app.database import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    columns = {c["name"] for c in inspect(engine).get_columns("bills")}
    assert NEW_BILL_COLUMNS <= columns


def test_existing_database_is_migrated_additively_and_idempotently(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:  # a pre-feature bills table, with one real-looking row already in it
        conn.execute(text("CREATE TABLE bills (id INTEGER PRIMARY KEY, expense_report_id INTEGER, raw_text VARCHAR(2000), catering_amount NUMERIC(12, 2))"))
        conn.execute(text("INSERT INTO bills (id, expense_report_id, raw_text, catering_amount) VALUES (1, 7, 'kept as is', 150.00)"))
    db_migrations.run_light_migrations(engine)
    db_migrations.run_light_migrations(engine)  # second run must be a no-op
    columns = {c["name"] for c in inspect(engine).get_columns("bills")}
    assert NEW_BILL_COLUMNS <= columns
    with engine.connect() as conn:
        row = conn.execute(text("SELECT raw_text, catering_amount, room_service_food_amount, room_service_food_mentioned, catering_mentioned FROM bills WHERE id = 1")).one()
    assert row[0] == "kept as is" and float(row[1]) == 150.0
    assert row[2] is None and row[3] is None and row[4] is None  # old rows stay NULL -> raw_text fallback applies
