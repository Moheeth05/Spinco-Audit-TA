"""Shared expense-category normalization and category<->bill-evidence
mapping, used by both the Phase 4 bill matcher (as one of several scoring
signals) and the Phase 5 category-consistency validation check (as a
stricter pass/fail/review decision). One source of truth so the two never
drift apart.

Real extracted category text is sometimes noisy - a trailing occurrence
marker like "BATTA [1]" or a redundant repeated parenthetical like
"LODGING (LODGING)" - both observed in real sample documents. Normalizing
strips only these cosmetic patterns; it never maps one real category onto
a different one.
"""

import re

from app.services.extraction.money import parse_amount

# The full, authoritative 29-category Policy Master list (verbatim names,
# matching app/seed.py's POLICY_CATEGORIES - kept as a separate constant here
# rather than imported from app.seed because this module is the designated
# "one source of truth" for category-related matching/normalization logic
# used by the extraction layer, which must not import the DB-seeding module;
# if a category is ever added/renamed in Policy Master, update both lists
# together). Used by expense_report_parser.py to recognize "this line names
# a real category" by membership rather than by enumerating every possible
# non-category label line a document might print ahead of it (Merchant,
# Ref#, Expense Location, PNR Number, Attendees, ... - an open-ended,
# un-enumerable set; a fixed 29-name category list is the one thing that
# actually is closed and known).
POLICY_MASTER_CATEGORIES = frozenset(
    {
        "AIRFARE",
        "BATTA",
        "CAB CHARGES",
        "CALIBRATION CHARGE",
        "COMPANY CAR MAINTENANCE",
        "COMPANY CAR PETROL",
        "COURIER CHARGES",
        "INTERNET CHARGES",
        "LOCAL CONVEYANCE",
        "LODGING",
        "MARKETING - SEMINAR EXPS",
        "MARKETING EXPENSES - FOOD",
        "MARKETING EXPENSES - OTHERS",
        "MEMBERSHIP & SUBSCRIPTION FEE",
        "OFFICE EXPENSES - DRIVER SALARY",
        "OWN CAR PETROL",
        "PRINTING & STATIONERY",
        "RATES & TAXES",
        "REPAIRS - COMPUTERS",
        "SERVICE PURCHASES/SERVICE CHARGE",
        "STAFF WELFARE",
        "TELEPHONE ACCESSORIES",
        "TENDER FEE",
        "TOLL TAX CHARGES",
        "TRAIN / BUS FARE",
        "TRANSPORT CHARGES",
        "TRAVEL-OTHERS",
        "TWO WHEELER MAINTENANCE",
        "TWO WHEELER PETROL",
    }
)


def is_policy_master_category(text: str | None) -> bool:
    """Loose membership check (via the same cosmetic-noise-tolerant
    normalize_category used everywhere else) - True only for text that
    actually names one of the 29 real categories, never a guess."""
    return normalize_category(text) in POLICY_MASTER_CATEGORIES


CATEGORY_TO_BILL_LABEL = {
    "OWN CAR PETROL": "Fuel",
    "COMPANY CAR PETROL": "Fuel",
    "TWO WHEELER PETROL": "Fuel",
    "AIRFARE": "Flight",
    "LODGING": "Lodging",
    "CAB CHARGES": "Cab/Taxi",
    "LOCAL CONVEYANCE": "Cab/Taxi",
    "STAFF WELFARE": "Food",
    "MARKETING EXPENSES - FOOD": "Food",
    "COURIER CHARGES": "Courier",
    "PRINTING & STATIONERY": "Printing & Stationery",
    "TRAIN / BUS FARE": "Train",
    # Was "Bus" - research (category_vocabulary.json) confirmed this collapsed
    # TRANSPORT CHARGES onto the exact same bare `\bBUS\b` signal TRAIN / BUS
    # FARE already owns for passenger bus tickets, which is the wrong
    # evidence for this category: in Indian corporate accounting "Transport
    # Charges" is goods/freight movement (consignment note, GTA, lorry hire),
    # never a passenger fare. Given its own, distinct bill-evidence label.
    "TRANSPORT CHARGES": "Freight/Transport",
    # The 12 categories below had NO bill-evidence mapping at all before this
    # research (seed.py confirms all require a document: DA/YES) - every one
    # of them was structurally unable to ever PASS or EXCEPTION the Category
    # Consistency Check, regardless of the matched bill's content. See
    # category_vocabulary.json for the sourced research behind each label.
    "TOLL TAX CHARGES": "Toll",
    "CALIBRATION CHARGE": "Calibration",
    "INTERNET CHARGES": "Internet",
    "TELEPHONE ACCESSORIES": "Phone Accessories",
    "REPAIRS - COMPUTERS": "Computer Repair",
    "RATES & TAXES": "Statutory Tax",
    "MEMBERSHIP & SUBSCRIPTION FEE": "Membership",
    "TENDER FEE": "Tender Fee",
    "TRAVEL-OTHERS": "Travel Miscellaneous",
    "MARKETING - SEMINAR EXPS": "Seminar/Conference",
    "MARKETING EXPENSES - OTHERS": "Marketing Collateral",
    # Deliberately last / weakest - see bill_parser.py's CATEGORY_KEYWORDS
    # module docstring: "Service Purchase" is checked only after every other
    # category's own, more specific pattern has already failed to match.
    # Research (category_vocabulary.json) found no vocabulary that
    # distinguishes this genuine catch-all from half the other categories on
    # this list, and explicitly recommended a fallback-only, REVIEW_REQUIRED-
    # leaning design rather than confident automated matching.
    "SERVICE PURCHASES/SERVICE CHARGE": "Service Purchase",
}

_BRACKET_SUFFIX_RE = re.compile(r"\s*\[\d+\]\s*$")
_TRAILING_PAREN_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")
_WHITESPACE_RE = re.compile(r"\s+")

# Equivalent/shorthand labels for an existing Policy Master category -
# confirmed real (an expense row reading "CONVEYANCE" failed to resolve
# against the Policy Master's own "LOCAL CONVEYANCE" entry, producing a
# false POLICY NOT FOUND / REVIEW REQUIRED for an otherwise ordinary
# expense). Deliberately a small, explicit, hand-verified table - never
# fuzzy/similarity matching, which risks silently merging two genuinely
# different categories. Every value here must be an exact, existing entry in
# POLICY_MASTER_CATEGORIES; this table only ever resolves a shorter/looser
# real-world label onto the category it already, unambiguously means - it
# never invents a new category or maps unrelated ones together.
CATEGORY_ALIASES: dict[str, str] = {
    "CONVEYANCE": "LOCAL CONVEYANCE",
}
assert all(v in POLICY_MASTER_CATEGORIES for v in CATEGORY_ALIASES.values()), (
    "CATEGORY_ALIASES must only resolve to real, existing Policy Master categories"
)


def normalize_category(category: str | None) -> str | None:
    if not category:
        return None
    text = _BRACKET_SUFFIX_RE.sub("", category).strip()
    m = _TRAILING_PAREN_RE.match(text)
    if m and m.group(1).strip().upper() == m.group(2).strip().upper():
        text = m.group(1).strip()
    text = _WHITESPACE_RE.sub(" ", text).strip().upper()
    if not text:
        return None
    return CATEGORY_ALIASES.get(text, text)


def expected_bill_label(expense_category: str | None) -> str | None:
    return CATEGORY_TO_BILL_LABEL.get(normalize_category(expense_category))


def category_compatible(expense_category: str | None, bill_category_guess: str | None) -> bool:
    """Lenient compatibility check used only as one signal among several in
    bill matching - unknown categories are simply not a positive signal
    (False), never a rejection of the candidate bill."""
    if not bill_category_guess:
        return False
    expected = expected_bill_label(expense_category)
    if expected is None:
        return False
    if bill_category_guess == expected:
        return True
    if expected == "Train" and bill_category_guess == "Bus":
        return True
    if expected == "Bus" and bill_category_guess == "Train":
        return True
    return False


# Multi-service travel-booking aggregators - confirmed real case: a Cleartrip
# e-ticket for an Air India Express flight, and Cleartrip also books hotels
# and ground transport under the same brand name. "Cleartrip" (or any of
# these) therefore NEVER identifies a service type by itself - see
# bill_parser.py's CATEGORY_KEYWORDS, which already classifies Flight/
# Lodging/Cab-Taxi evidence from document CONTENT (airline names, hotel/
# check-in terms, cab/pickup terms), never from this vendor-name list. This
# list exists only so check_category_consistency can tell "this document's
# content genuinely didn't establish a service type, AND it's from a vendor
# known to offer several" (ambiguous - REVIEW REQUIRED) apart from "this
# document's content didn't establish a service type, but nothing about the
# vendor itself suggests it could be several different things" (the existing,
# narrower HIGH-confidence-match softening). Deliberately a short, explicit,
# hand-verified list of known OTAs (Online Travel Agents) - never a guess
# from an unfamiliar vendor name.
_TRAVEL_AGGREGATOR_RE = re.compile(
    r"\b(CLEARTRIP|MAKEMYTRIP|MAKE\s*MY\s*TRIP|YATRA|GOIBIBO|EASEMYTRIP|EASE\s*MY\s*TRIP|IXIGO|TRAVELGURU|BOOKING\.COM|EXPEDIA)\b",
    re.IGNORECASE,
)


def is_multi_service_aggregator(text: str | None) -> bool:
    """True if `text` (a bill's vendor field and/or raw OCR text) names a
    known multi-service travel-booking aggregator. Content-based category
    classification (bill_parser.py) is never affected by this - it only
    informs how check_category_consistency treats a document whose content
    did NOT establish any service type at all."""
    return bool(_TRAVEL_AGGREGATOR_RE.search(text or ""))


_CATERING_RE = re.compile(r"\bcatering\b", re.IGNORECASE)
_AMOUNT_NEAR_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\b", re.IGNORECASE)


def mentions_catering(raw_text: str | None) -> bool:
    return bool(_CATERING_RE.search(raw_text or ""))


def detect_catering_amount(raw_text: str | None) -> float | None:
    """The catering charge printed on a travel-ticket bill, if any - shared
    by validation/checks.py's Catering Check (the auditor-facing exception)
    and bill_matcher.py's scoring (so a claimed amount that legitimately
    excludes catering isn't treated as a mismatch against the bill's
    all-inclusive total - see Part 17: catering must not break an otherwise-
    correct bill match). One detector, reused everywhere, so the two can
    never disagree on whether/how much catering a bill includes.

    "Catering" can appear more than once (e.g. an IRCTC ticket's passenger
    table has a "Catering" preference COLUMN HEADER with no amount nearby,
    and the actual "Catering Charges (Incl. of GST) ... 368.00" line
    appears later) - the first occurrence with a nearby amount wins, not
    just the first occurrence of the word.
    """
    text = raw_text or ""
    for match in _CATERING_RE.finditer(text):
        window = text[match.end() : match.end() + 40]
        amount_match = _AMOUNT_NEAR_RE.search(window)
        if amount_match:
            return parse_amount(amount_match.group(0))
    return None
