"""TASK: Correct Cleartrip and other travel-aggregator category classification.

Real case: a LODGING claim for Rs.4,797 was matched to a Cleartrip e-ticket
for an Air India Express flight (Kolkata -> Bagdogra) and flagged CATEGORY
MISMATCH. Investigation (see bill_parser.py's CATEGORY_KEYWORDS "Flight"
pattern, already content-based: airline names + boarding-pass terms, never
the vendor name "Cleartrip") confirmed the extraction layer already
classifies by document CONTENT correctly - a flight ticket genuinely
contradicts a LODGING claim, so that specific mismatch is correct and must
be preserved (required test below).

The real gap was at validation time: when a bill's content gives NO
category evidence at all (bill.category_guess is None) AND the bill is from
a known multi-service aggregator (Cleartrip, MakeMyTrip, etc.), the existing
"HIGH-confidence match -> PASS" softening (added for a non-aggregator UPI
payment case) would silently auto-pass an expense whose actual service type
is genuinely unknown - exactly "automatically pass an expense simply
because the vendor is a travel aggregator", which this task explicitly
prohibits. See category_map.py::is_multi_service_aggregator and
checks.py::check_category_consistency for the fix.
"""

from app.services.category_map import POLICY_MASTER_CATEGORIES, is_multi_service_aggregator
from app.services.extraction.bill_parser import parse_bill_text
from app.services.validation import checks

CLEARTRIP_FLIGHT_TEXT = """Cleartrip
E-Ticket / Itinerary
Air India Express
Flight: IX 1234
PNR: ABCDEF
From: Kolkata (CCU)  To: Bagdogra (IXB)
Departure: 10:15  Arrival: 11:30
Passenger: Mr. Rajesh Kumar
Boarding Pass
Total Fare: Rs.4,797.00
Baggage: 15 Kg
"""

CLEARTRIP_HOTEL_TEXT = """Cleartrip
Booking Confirmation
Hotel Name: Grand Residency
Check-in: 12 Aug 2026  Check-out: 13 Aug 2026
Guest Name: Mr. Rajesh Kumar
Room Type: Deluxe Room
Amount Paid: Rs.3,200.00
"""

CLEARTRIP_CAB_TEXT = """Cleartrip
Cab Booking Confirmation
Outstation Cab: Sedan
Pickup: Kolkata Airport
Drop: City Center
Fare: Rs.850.00
"""

CLEARTRIP_AMBIGUOUS_TEXT = """Cleartrip
Booking Reference: CT12345678
Amount Paid: Rs.1,500.00
Payment Successful
"""


# ---------- Extraction layer: content-based, not vendor-name-based ----------


def test_cleartrip_flight_ticket_classifies_as_flight_from_document_content():
    bill = parse_bill_text(CLEARTRIP_FLIGHT_TEXT)
    assert bill.category_guess == "Flight"
    assert bill.amount == 4797.0


def test_cleartrip_hotel_confirmation_classifies_as_lodging_from_document_content():
    bill = parse_bill_text(CLEARTRIP_HOTEL_TEXT)
    assert bill.category_guess == "Lodging"


def test_cleartrip_cab_booking_classifies_as_cab_taxi_from_document_content():
    bill = parse_bill_text(CLEARTRIP_CAB_TEXT)
    assert bill.category_guess == "Cab/Taxi"


def test_cleartrip_document_with_insufficient_details_has_no_category_guess():
    """Confirms the extraction layer does NOT guess a category from the
    vendor name alone when the document content is insufficient - it must
    stay None, not default to any single category."""
    bill = parse_bill_text(CLEARTRIP_AMBIGUOUS_TEXT)
    assert bill.category_guess is None


# ---------- is_multi_service_aggregator ----------


def test_is_multi_service_aggregator_recognizes_known_otas():
    assert is_multi_service_aggregator("Cleartrip") is True
    assert is_multi_service_aggregator("MakeMyTrip") is True
    assert is_multi_service_aggregator("Make My Trip GQ") is True
    assert is_multi_service_aggregator(CLEARTRIP_AMBIGUOUS_TEXT) is True


def test_is_multi_service_aggregator_false_for_ordinary_vendors():
    assert is_multi_service_aggregator("Air India Express") is False
    assert is_multi_service_aggregator("Hotel Ayush") is False
    assert is_multi_service_aggregator(None) is False
    assert is_multi_service_aggregator("") is False


# ---------- Validation layer ----------


class _Bill:
    def __init__(self, category_guess, vendor=None, raw_text=None, page_number=1):
        self.category_guess = category_guess
        self.vendor = vendor
        self.raw_text = raw_text
        self.page_number = page_number


def test_cleartrip_flight_ticket_vs_lodging_claim_is_a_genuine_category_mismatch():
    """Required test: Cleartrip flight ticket versus LODGING claim must
    detect a flight-related category conflict - the aggregator exception
    below only applies when the bill's content gives NO category evidence
    at all, never when it gives CONFLICTING evidence."""
    bill = _Bill(category_guess="Flight", vendor="Cleartrip", page_number=3)
    result = checks.check_category_consistency(category="LODGING", bill=bill, bill_match_confidence="HIGH")
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "CATEGORY MISMATCH"


def test_cleartrip_hotel_confirmation_vs_lodging_claim_does_not_mismatch():
    """Required test: do not flag a mismatch solely because the vendor is
    Cleartrip - real hotel-confirmation evidence must PASS against a
    LODGING claim exactly like any other hotel bill."""
    bill = _Bill(category_guess="Lodging", vendor="Cleartrip", page_number=4)
    result = checks.check_category_consistency(category="LODGING", bill=bill)
    assert result.result == checks.PASS


def test_cleartrip_cab_booking_vs_cab_charges_claim_classifies_correctly():
    """Required test: Cleartrip ground-transport document versus the
    applicable existing transport category - classified from document
    evidence, resolving to an existing Policy Master category (CAB
    CHARGES), never a new one."""
    assert "CAB CHARGES" in POLICY_MASTER_CATEGORIES
    bill = _Bill(category_guess="Cab/Taxi", vendor="Cleartrip", page_number=5)
    result = checks.check_category_consistency(category="CAB CHARGES", bill=bill)
    assert result.result == checks.PASS


def test_cleartrip_ambiguous_document_is_review_required_not_mismatch_or_autopass():
    """Required test: a Cleartrip document with insufficient details must be
    REVIEW REQUIRED with a specific explanation - never silently PASSed
    (even at HIGH match confidence - the existing softening for an ordinary
    vendor with no category evidence must NOT apply to a known aggregator)
    and never automatically raised as a definitive mismatch either."""
    bill = _Bill(category_guess=None, vendor="Cleartrip", raw_text=CLEARTRIP_AMBIGUOUS_TEXT, page_number=6)
    result = checks.check_category_consistency(category="LODGING", bill=bill, bill_match_confidence="HIGH")
    assert result.result == checks.REVIEW_REQUIRED
    assert "aggregator" in result.details.lower()


def test_cleartrip_ambiguous_document_review_required_regardless_of_confidence():
    bill = _Bill(category_guess=None, vendor="Cleartrip", page_number=6)
    for confidence in ("HIGH", "MEDIUM", "LOW", None):
        result = checks.check_category_consistency(category="CAB CHARGES", bill=bill, bill_match_confidence=confidence)
        assert result.result == checks.REVIEW_REQUIRED, f"confidence={confidence}"


def test_direct_airline_ticket_vs_lodging_claim_retains_genuine_mismatch():
    """Required test: a direct (non-aggregator) airline ticket versus a
    lodging claim must still raise a genuine CATEGORY MISMATCH -
    unaffected by any of the aggregator-specific handling above."""
    bill = _Bill(category_guess="Flight", vendor="Air India Express", page_number=7)
    result = checks.check_category_consistency(category="LODGING", bill=bill, bill_match_confidence="HIGH")
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "CATEGORY MISMATCH"


def test_non_aggregator_ambiguous_bill_keeps_existing_high_confidence_softening():
    """Regression guard: the aggregator-specific REVIEW_REQUIRED carve-out
    must not affect an ordinary (non-aggregator) vendor with no category
    evidence - that case keeps the existing HIGH-confidence PASS behaviour,
    unchanged from before this task."""
    bill = _Bill(category_guess=None, vendor="Bhagabat Sahu", page_number=13)
    result = checks.check_category_consistency(category="CAB CHARGES", bill=bill, bill_match_confidence="HIGH")
    assert result.result == checks.PASS


def test_no_new_policy_categories_created_by_aggregator_handling():
    """Sanity check: the 29-category Policy Master list is unchanged by this
    task - aggregator handling only ever resolves to an EXISTING category
    via the expense's own claimed category, never invents one."""
    assert len(POLICY_MASTER_CATEGORIES) == 29
    assert "LODGING" in POLICY_MASTER_CATEGORIES
    assert "AIRFARE" in POLICY_MASTER_CATEGORIES
    assert "CAB CHARGES" in POLICY_MASTER_CATEGORIES


# ---------- Real case regression: Rs.4,797 Air India Express flight, 19 Aug
# 2026 (ER-41115.pdf, pages 2/25) ----------
#
# Traced the full data flow against the real source PDF: the Expense Summary
# table's own claimed-category text for this row (page 2, row 15) reads
# "LODGING (LODGING)" verbatim, with expense_details "Air ticket from CCU to
# Bagdogra" - a genuine human data-entry error in the original document (row
# 8 immediately above it is a real lodging claim reading the same
# "LODGING (LODGING)" text; row 15 appears to have been copy-pasted from it
# without updating the category). This is NOT an extraction, normalization,
# or alias bug anywhere in this codebase - normalize_category() already
# collapses "LODGING (LODGING)" to "LODGING" correctly (the existing
# repeated-parenthetical cosmetic rule), find_policy() resolves it to the
# real, active LODGING policy row, and the matched bill's own content
# (Cleartrip e-ticket, "Air India Express", flight terms) is correctly
# classified as Flight - so CATEGORY MISMATCH is the correct, honest verdict
# and must be preserved exactly as it already is (see module docstring:
# "that specific mismatch is correct and must be preserved").
#
# Kept here as a permanent regression anchored to a real reported case: if
# this specific example ever stops raising CATEGORY MISMATCH, something
# genuinely regressed (over-suppression of a real violation), not a bug fix.

REAL_AIR_INDIA_EXPRESS_TICKET_TEXT = """Trip ID : 260818517608
Baggage (per Adult/Child) - Check-in: 15KG (1 piece), Cabin: 7KG (1 piece)
Seats - 13A
Mr Dipak Kumar Pal
3,333
299
45
1,545
-425
4,797
Kolkata to Bagdogra
Wed, 19 Aug 2026
Air India Express
IX - 1248
Fare type: CLASSIC
CCU 07:45
09:05 IXB
Wed, 19 Aug 2026
TRAVELLERS
AIRLINE PNR
TICKET NO.
T5G2KC
T5G2KC
FARE BREAKUP
Base Fare: Rs. 3,333
Airline Fuel Charge: Rs. 299
Cute Fee: Rs. 45
Other charges: Rs. 1,545
Discounts And Cashbacks: Rs. -425
Total Fare: Rs. 4,797
Air India Express helpline 044 24301930
"""


def test_real_air_india_express_ticket_extracts_as_flight_evidence():
    """The real bill content (not a synthetic approximation) must classify
    as Flight evidence with the correct amount, exactly as the earlier
    investigation (see module docstring) found."""
    bill = parse_bill_text(REAL_AIR_INDIA_EXPRESS_TICKET_TEXT)
    assert bill.category_guess == "Flight"
    assert bill.amount == 4797.0


def test_real_case_lodging_claim_against_real_flight_ticket_is_genuine_mismatch():
    """The reported case exactly as it reads in the real document: claimed
    category is the verbatim raw text "LODGING (LODGING)" from the source
    PDF (not pre-normalized) - this must still resolve correctly and still
    raise CATEGORY MISMATCH, since the claim genuinely is LODGING and the
    evidence genuinely is a flight ticket."""
    bill = _Bill(category_guess="Flight", vendor="Air India Express", page_number=25)
    result = checks.check_category_consistency(category="LODGING (LODGING)", bill=bill, bill_match_confidence="HIGH")
    assert result.result == checks.EXCEPTION
    assert result.exception_type == "CATEGORY MISMATCH"


def test_real_flight_ticket_claimed_under_airfare_does_not_false_mismatch():
    """If this same real flight ticket had instead been claimed under the
    existing, configured AIRFARE policy category (the category it should
    actually have been claimed under), there must be no false CATEGORY
    MISMATCH - this is what the current claim SHOULD have been, and
    confirms the fix target isn't "suppress the mismatch" but "the mismatch
    is correct for what was actually claimed"."""
    bill = _Bill(category_guess="Flight", vendor="Air India Express", page_number=25)
    result = checks.check_category_consistency(category="AIRFARE", bill=bill, bill_match_confidence="HIGH")
    assert result.result == checks.PASS
