"""An intercity bus ticket must be read as BUS evidence, not as Fuel.

Confirmed defect: a genuine bus ticket (operator, route, AC sleeper, boarding point, passenger, seat,
ticket/booking id, fare) claimed under TRAIN / BUS FARE raised "reads as Fuel evidence". The cause was
bill_parser._find_category: it returned the FIRST CATEGORY_KEYWORDS pattern that matched, and the Fuel
patterns (PETROL, FUEL, INDIAN OIL, BPCL ...) come before the bare BUS pattern. A boarding-point address
such as "Opp. HP Petrol Pump" was therefore enough. The fix recognises a bus ticket from several independent
indicators first (and never from the word "bus" alone) and leaves genuine fuel-dispenser evidence as Fuel.

All text below is SYNTHETIC: invented operator, passenger, ids and addresses; no real ticket data.
"""

import tempfile
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models.bill import Bill
from app.models.expense_report import Expense, ExpenseReport
from app.models.validation import ExceptionRecord
from app.services import category_map
from app.services.exception_engine import evaluate_report
from app.services.extraction import bill_parser
from app.services.extraction.bill_parser import CATEGORY_KEYWORDS, parse_bill_text
from app.services.validation import checks
from app.services.validation.pipeline import validate_expense_report

BUS_TICKET = """Bus Ticket Confirmation
Bus Operator: Verma Travels (synthetic)
From: Ahmedabad
To: Bhopal
Bus Type: AC Sleeper (2+1)
Journey Date: 14/08/2026
Boarding Date & Time: 14/08/2026 20:30
Boarding Point: Shahibaug Underbridge
Address: Opp. HP Petrol Pump, Shahibaug Road, Ahmedabad
Dropping Point: Habibganj Bus Stand, Bhopal
Passenger: TEST PASSENGER, 34, Male
Seat No: L7
Ticket No: TK123456789
Booking ID: BK987654
Total Fare: Rs.1,064.00
"""

AC_SLEEPER_TICKET = """E-Ticket
Bus Service: Sharma Tours
Route: Indore - Nagpur
A/C Sleeper
Pick-up Point: Vijay Nagar Square (Near Indian Oil Pump)
Drop Point: Sitabuldi
Passengers: TEST PASSENGER
Seat Number: 12
Booking Reference: BKR55501
Date: 21/08/2026
Fare Rs. 845.00
"""

DIFFERENT_FORMAT_TICKET = """BUS TICKET - SYNTHETIC
OPERATED BY GSRTC
VOLVO MULTI-AXLE SEMI SLEEPER
BOARDING POINT : CENTRAL BUS STATION (BPCL FUEL STATION OPPOSITE)
DROPPING POINT : MAIN BUS STAND
SEAT 14
PNR 8899001122
TOTAL FARE INR 520.00
DATE 18/08/2026
"""

TRAIN_TICKET = """IRCTC e-Ticket (synthetic)
Train No/Name: 12345 TEST EXPRESS
Class: Sleeper (SL)
Boarding Station: Nagpur Jn   Destination: Pune Jn
Passenger Details: TEST PASSENGER, 34, M
PNR: 4455667788
Seat/Berth: S4/32
Date of Journey: 21/08/2026
Ticket Fare Rs. 845.00
"""

FUEL_RECEIPT = """INDIAN OIL CORPORATION LTD (synthetic dealer)
Fuel Station Receipt
Nozzle No: 02
Product: PETROL
Rate/Ltr: 100.00
Volume (Ltr): 20.00
Amount Rs.2,000.00
Date: 18/08/2026
"""

# The same ticket as an operator that prints "Boarding Time" / "Baggage": those are Flight keywords too.
BUS_TICKET_BOARDING_TIME_WORDING = BUS_TICKET.replace("Boarding Date & Time:", "Boarding Time:") + "Baggage: 1 bag allowed\n"

# A bus ticket that also prints the word "fuel" in unrelated text (fuel surcharge note, pump landmark).
BUS_TICKET_WITH_FUEL_WORDS = BUS_TICKET + "Fare includes fuel surcharge. Landmark: Diesel pump junction.\n"

# Mentions of "bus" with no ticket evidence at all.
BUS_WORD_ONLY = """Shuttle Invoice (synthetic)
Hotel airport shuttle bus transfer arranged for guest.
Amount Rs.500.00
"""
PETROL_PUMP_NAMED_BUS_STAND = """Bus Stand Petrol Pump (synthetic)
Fuel Receipt
Nozzle No: 01
Product: DIESEL
Rate/Ltr: 90.00
Volume (Ltr): 10.00
Amount Rs.900.00
"""
SEAT_AND_TICKET_NO_BUS_IDENTITY = """Concert Entry Pass (synthetic)
Passenger: TEST PASSENGER
Seat No: A12
Ticket No: 5566
Amount Rs.700.00
"""


def _legacy_first_match(text):
    """The pre-fix behaviour: the first CATEGORY_KEYWORDS pattern that matches."""
    for pattern, label in CATEGORY_KEYWORDS:
        if pattern.search(text):
            return label
    return None


# ---------- the mechanism of the defect, shown on synthetic text ----------


def test_the_legacy_first_match_loop_reads_the_synthetic_bus_ticket_as_fuel():
    """Documents why the defect happened: with the ticket's own address wording, the Fuel pattern wins the
    first-match loop. (This is NOT what the application does any more - see the tests below.)"""
    assert _legacy_first_match(BUS_TICKET) == "Fuel"  # "Opp. HP Petrol Pump" in the boarding address
    assert _legacy_first_match(BUS_TICKET_WITH_FUEL_WORDS) == "Fuel"
    assert _legacy_first_match(AC_SLEEPER_TICKET) == "Fuel"  # "Near Indian Oil Pump"
    assert _legacy_first_match(DIFFERENT_FORMAT_TICKET) == "Fuel"  # "BPCL FUEL STATION OPPOSITE" in the boarding point
    assert _legacy_first_match(BUS_TICKET_BOARDING_TIME_WORDING) == "Flight"  # second pathway: "Boarding Time" / "Baggage" are Flight keywords, checked first


# ---------- 1-3: bus tickets are Bus ----------


@pytest.mark.parametrize("text", [BUS_TICKET, AC_SLEEPER_TICKET, DIFFERENT_FORMAT_TICKET, BUS_TICKET_WITH_FUEL_WORDS, BUS_TICKET_BOARDING_TIME_WORDING],
                         ids=["complete", "ac-sleeper", "different-format", "with-unrelated-fuel-words", "boarding-time-and-baggage-wording"])
def test_bus_tickets_are_classified_as_bus_not_fuel(text):
    assert parse_bill_text(text).category_guess == "Bus"


def test_the_complete_bus_ticket_keeps_its_amount_and_a_bus_evidence_snippet():
    parsed = parse_bill_text(BUS_TICKET)
    assert parsed.category_guess == "Bus"
    assert parsed.amount == 1064.0
    assert parsed.description == "Bus Operator"


# ---------- 4-5: train ticket and genuine fuel receipt keep their own labels ----------


def test_train_ticket_is_still_train():
    assert parse_bill_text(TRAIN_TICKET).category_guess == "Train"


def test_genuine_fuel_receipt_is_still_fuel():
    assert parse_bill_text(FUEL_RECEIPT).category_guess == "Fuel"


def test_fuel_dispenser_evidence_is_never_overruled_even_on_a_bus_stand_pump():
    assert parse_bill_text(PETROL_PUMP_NAMED_BUS_STAND).category_guess == "Fuel"


def test_fuel_dispenser_evidence_blocks_the_bus_rule_even_if_ticket_words_are_present():
    mixed_page = BUS_TICKET + FUEL_RECEIPT  # pathological single page: dispenser fields present
    assert bill_parser._bus_ticket_evidence(mixed_page) is None
    assert parse_bill_text(mixed_page).category_guess == "Fuel"


# ---------- 7: the word "bus" or ticket-ish wording alone is not bus-ticket evidence ----------


def test_the_word_bus_alone_is_not_bus_ticket_evidence():
    assert bill_parser._bus_ticket_evidence(BUS_WORD_ONLY) is None
    assert bill_parser._bus_ticket_evidence("Bus") is None
    assert bill_parser._bus_ticket_evidence("Bus Operator") is None  # one indicator is never enough


def test_seat_ticket_and_passenger_wording_without_a_bus_identity_is_not_a_bus_ticket():
    assert bill_parser._bus_ticket_evidence(SEAT_AND_TICKET_NO_BUS_IDENTITY) is None
    assert parse_bill_text(SEAT_AND_TICKET_NO_BUS_IDENTITY).category_guess != "Bus"


def test_a_single_bus_indicator_is_never_enough():
    for text in ("AC Sleeper\nTotal Rs.100.00", "Bus Operator: X Travels\nTotal Rs.100.00",
                 "Boarding Point: Central\nPassenger: TEST\nTotal Rs.100.00"):  # points + passenger: no operator / bus type
        assert bill_parser._bus_ticket_evidence(text) is None, text


# ---------- sparse and OCR-poor tickets (the sparse tier) ----------

SPARSE_TICKETS = {
    "type+seat+pump-landmark": "AC Sleeper\nSeat 12\nNear HP Petrol Pump\nFare Rs.800.00",
    "operator+seat+pump-landmark": "Bus Operator: Sharma Tours\nSeat No: 14\nOpp. Indian Oil Pump\nFare Rs.500.00",
    "operator+type": "Bus Operator: X Travels\nAC Sleeper\nTotal Rs.100.00",
    "ocr-garbled-labels+pump-landmark": "Bus Operat0r Verma Travels\nBoarding Poinl Shahibaug Opp HP Petrol Pump\nSeat No L7\nFare Rs.900.00",
}


@pytest.mark.parametrize("text", list(SPARSE_TICKETS.values()), ids=list(SPARSE_TICKETS))
def test_sparse_and_ocr_poor_bus_tickets_are_still_bus(text):
    assert parse_bill_text(text).category_guess == "Bus"


def test_the_legacy_loop_misread_the_sparse_tickets_with_a_pump_landmark():
    assert _legacy_first_match(SPARSE_TICKETS["type+seat+pump-landmark"]) == "Fuel"
    assert _legacy_first_match(SPARSE_TICKETS["operator+seat+pump-landmark"]) == "Fuel"


def test_a_sparse_train_document_is_not_read_as_bus():
    text = "AC Sleeper\nIRCTC e-Ticket\nTrain No 12345\nPassenger: TEST\nSeat/Berth: S4/32\nFare Rs.845.00"
    assert bill_parser._bus_ticket_evidence(text) is None
    assert parse_bill_text(text).category_guess == "Train"


@pytest.mark.xfail(strict=True, reason="KNOWN LIMITATION: a ticket whose operator/bus-type labels are missing or too garbled to read, with only "
                   "boarding-point + passenger wording and a petrol-pump landmark, still falls back to the first-match order (Fuel). It is reported "
                   "as a category mismatch for the auditor to review, never silently passed.")
def test_limitation_ticket_with_no_readable_operator_or_type_and_a_pump_landmark():
    text = "Boarding Point: Vijay Nagar (Near BPCL Petrol Pump)\nDropping Point: Sitabuldi\nPassenger: TEST\nFare Rs.500.00"
    assert parse_bill_text(text).category_guess == "Bus"


# ---------- found by the baseline-vs-current fuzz: label spans, train/flight identity, landmarks ----------


def test_a_bus_label_never_runs_across_a_line_break():
    # "Bus" ends one line and "Service Charges" starts the next: that is not the label "Bus Service".
    for text in ("Opp. Indian Oil Pump\nCourier DTDC\nBus\nService Charges\nSeat 14", "Restaurant meal\nBus\nService Charges\nSeat No: L7"):
        assert bill_parser._bus_ticket_evidence(text) is None, text
        assert parse_bill_text(text).category_guess != "Bus", text


@pytest.mark.parametrize("text", [
    "Bus Service: Sharma Tours\nDropping Point: Main Stand\nPassenger: TEST\nIRCTC\nSeat No: L7",
    "Boarding Point: Central\nTrain No 12345\nAC Sleeper (2+1)\nGSRTC\nSeat 3",
    "Bus Operator: X Travels\nBoarding Point: Y\nPassenger: T\nFlight No IX 1234\nSeat 5",
], ids=["irctc", "train-no", "flight-no"])
def test_a_page_that_prints_its_own_train_or_flight_identity_is_never_relabelled_bus(text):
    assert bill_parser._bus_ticket_evidence(text) is None


def test_a_bus_ticket_naming_a_railway_station_or_airline_office_as_a_landmark_is_still_bus():
    text = BUS_TICKET.replace("Opp. HP Petrol Pump", "Near Railway Station, Opp. Air India Airlines office")
    assert parse_bill_text(text).category_guess == "Bus"


def test_the_unreadable_label_limitation_is_never_silently_passed(client):
    """Phase-6 behaviour for the strict-xfail above: with too little bus evidence the old first-match order still
    reads the page as Fuel, and the claim is REPORTED as a category mismatch for the auditor - it is never passed."""
    ticket = ("Boarding Point: Vijay Nagar (Near BPCL Petrol Pump)\nDropping Point: Sitabuldi\nPassenger: TEST\n"
              "Date: 14/08/2026\nFare Rs.500.00")
    bills, exps, exceptions, exp_ids = _run_pdf_pipeline([REPORT_PAGE, ticket], [(1, date(2026, 8, 14), "TRAIN / BUS FARE", 500.0)])
    assert bills[2].category == "Fuel"
    assert ("CATEGORY MISMATCH", exp_ids[1]) in exceptions


# ---------- the weak bare-"bus" fallback ----------


def test_the_bare_word_bus_alone_is_no_longer_any_category():
    assert parse_bill_text("BUS\nAmount Rs.250.00").category_guess is None
    assert parse_bill_text("Bus Stand Parking Receipt\nVehicle KA01\nAmount Rs.30.00").category_guess is None


def test_an_unrelated_invoice_that_mentions_a_bus_is_not_bus_evidence():
    text = "Service Charges invoice\nFleet tracker project for the bus depot\nAmount Rs.3,000.00"
    assert parse_bill_text(text).category_guess == "Service Purchase"  # the legacy bare-BUS rule called this Bus


def test_electrical_bus_bar_is_never_bus_evidence():
    assert parse_bill_text("Copper Bus Bar 200A\nAmount Rs.5,400.00").category_guess != "Bus"
    assert parse_bill_text("Bus Duct fittings\nAmount Rs.900.00").category_guess != "Bus"


def test_a_plain_bus_fare_slip_with_ticket_wording_is_still_bus():
    assert parse_bill_text("Bus ticket\nFare Rs.250.00").category_guess == "Bus"
    assert parse_bill_text("Bus fare\nAmount Rs.120.00").category_guess == "Bus"


def test_a_petrol_receipt_naming_a_bus_stand_stays_fuel_even_without_dispenser_fields():
    assert parse_bill_text("Sri Balaji Petrol Pump, Near Bus Stand\nSale Rs.1,200.00").category_guess == "Fuel"


def test_a_flight_ticket_with_boarding_wording_is_not_read_as_bus():
    text = "Air India Express\nFlight: IX 1234\nPNR: ABCDEF\nPassenger: TEST\nSeat No: 12A\nBoarding Pass\nBoarding Time 10:15\nTotal Fare: Rs.4,797.00"
    assert bill_parser._bus_ticket_evidence(text) is None
    assert parse_bill_text(text).category_guess == "Flight"


def test_a_cab_receipt_with_pickup_and_drop_points_is_not_read_as_bus():
    text = "Outstation Cab: Sedan\nPick-up Point: Airport\nDrop Point: Office Tower\nPassenger: TEST\nBooking ID: C1\nFare: Rs.850.00"
    assert bill_parser._bus_ticket_evidence(text) is None
    assert parse_bill_text(text).category_guess == "Cab/Taxi"


def test_a_hotel_invoice_mentioning_a_bus_transfer_stays_lodging():
    text = "Hotel Residency (synthetic)\nCheck-in: 12 Aug 2026\nGuest Name: TEST\nAirport bus transfer included\nAmount Rs.3,000.00"
    assert parse_bill_text(text).category_guess == "Lodging"


# ---------- category compatibility (validation layer) ----------


class _Bill:
    def __init__(self, category_guess, page_number=49):
        self.category_guess = category_guess
        self.vendor = None
        self.raw_text = None
        self.page_number = page_number


@pytest.mark.parametrize("guess", ["Bus", "Train"])
def test_train_bus_fare_accepts_both_bus_and_train_evidence(guess):
    result = checks.check_category_consistency(category="TRAIN / BUS FARE", bill=_Bill(guess), bill_match_confidence="HIGH")
    assert result.result == checks.PASS
    assert category_map.category_compatible("TRAIN / BUS FARE", guess) is True


def test_fuel_evidence_for_a_bus_fare_claim_is_a_genuine_mismatch():
    result = checks.check_category_consistency(category="TRAIN / BUS FARE", bill=_Bill("Fuel", page_number=49), bill_match_confidence="HIGH")
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "CATEGORY MISMATCH"
    assert "page 49 reads as Fuel evidence" in result.remarks
    assert category_map.category_compatible("TRAIN / BUS FARE", "Fuel") is False


def test_fuel_claims_still_expect_fuel_and_taxi_or_flight_evidence_is_still_a_mismatch_for_bus_fare():
    assert checks.check_category_consistency(category="OWN CAR PETROL", bill=_Bill("Fuel"), bill_match_confidence="HIGH").result == checks.PASS
    assert checks.check_category_consistency(category="OWN CAR PETROL", bill=_Bill("Bus"), bill_match_confidence="HIGH").result == checks.EXCEPTION
    assert checks.check_category_consistency(category="TRAIN / BUS FARE", bill=_Bill("Cab/Taxi"), bill_match_confidence="HIGH").result == checks.EXCEPTION
    assert checks.check_category_consistency(category="TRAIN / BUS FARE", bill=_Bill("Flight"), bill_match_confidence="HIGH").result == checks.EXCEPTION


def test_end_to_end_parse_then_validate_for_the_synthetic_bus_ticket():
    guess = parse_bill_text(BUS_TICKET).category_guess
    assert checks.check_category_consistency(category="TRAIN / BUS FARE", bill=_Bill(guess), bill_match_confidence="HIGH").result == checks.PASS


# ---------- 8-10: whole pipeline on a multi-page synthetic PDF ----------


def _run_pdf_pipeline(pages, expenses):
    """pages: list of text, page 1 is the report's own content page (bills start on page 2).
    expenses: list of (s_no, date, category, amount). Returns (bills by page, expenses by s_no, exception types)."""
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
                               extraction_status="EXTRACTED", total_claimed=sum(e[3] for e in expenses), er_id="ER-BUS-SYNTH")
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
        recs = db.query(ExceptionRecord).filter(ExceptionRecord.expense_report_id == report.id).all()
        out_bills = {p: SimpleNamespace(id=b.id, category=b.category_guess, amount=float(b.amount) if b.amount is not None else None) for p, b in bills.items()}
        out_exps = {s: SimpleNamespace(bill_id=e.bill_id, status=e.bill_match_status) for s, e in exps.items()}
        return out_bills, out_exps, [(r.exception_type, r.expense_id) for r in recs], {s: e.id for s, e in exps.items()}
    finally:
        db.close()


REPORT_PAGE = "SYNTHETIC EXPENSE REPORT ER-BUS-SYNTH\nSubmitted By TEST EMPLOYEE"


def test_mixed_pdf_bus_fuel_and_train_keep_their_own_identity_and_page(client):
    bills, exps, exceptions, exp_ids = _run_pdf_pipeline(
        [REPORT_PAGE, BUS_TICKET, FUEL_RECEIPT, TRAIN_TICKET],
        [(1, date(2026, 8, 14), "TRAIN / BUS FARE", 1064.0), (2, date(2026, 8, 18), "OWN CAR PETROL", 2000.0), (3, date(2026, 8, 21), "TRAIN / BUS FARE", 845.0)],
    )
    # each page became its own bill, with its own evidence type and its own page number
    assert {p: b.category for p, b in bills.items()} == {2: "Bus", 3: "Fuel", 4: "Train"}
    assert {p: b.amount for p, b in bills.items()} == {2: 1064.0, 3: 2000.0, 4: 845.0}
    # every expense is matched to ITS OWN bill (no bill reused for another expense)
    assert exps[1].bill_id == bills[2].id and exps[2].bill_id == bills[3].id and exps[3].bill_id == bills[4].id
    assert len({e.bill_id for e in exps.values()}) == 3
    # no category mismatch anywhere: bus -> TRAIN / BUS FARE, fuel -> OWN CAR PETROL, train -> TRAIN / BUS FARE
    assert "CATEGORY MISMATCH" not in [t for t, _ in exceptions]


def test_fuel_receipt_submitted_for_a_bus_fare_expense_is_a_genuine_mismatch(client):
    bills, exps, exceptions, exp_ids = _run_pdf_pipeline(
        [REPORT_PAGE, FUEL_RECEIPT],
        [(1, date(2026, 8, 18), "TRAIN / BUS FARE", 2000.0)],
    )
    assert bills[2].category == "Fuel"
    assert ("CATEGORY MISMATCH", exp_ids[1]) in exceptions


def test_bus_ticket_for_a_fuel_expense_is_still_a_genuine_mismatch(client):
    bills, exps, exceptions, exp_ids = _run_pdf_pipeline(
        [REPORT_PAGE, BUS_TICKET],
        [(1, date(2026, 8, 14), "OWN CAR PETROL", 1064.0)],
    )
    assert bills[2].category == "Bus"
    assert ("CATEGORY MISMATCH", exp_ids[1]) in exceptions
