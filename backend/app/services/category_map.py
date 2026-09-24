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

CATEGORY_TO_BILL_LABEL = {
    "OWN CAR PETROL": "Fuel",
    "COMPANY CAR PETROL": "Fuel",
    "TWO WHEELER PETROL": "Fuel",
    "LODGING": "Lodging",
    "CAB CHARGES": "Cab/Taxi",
    "LOCAL CONVEYANCE": "Cab/Taxi",
    "STAFF WELFARE": "Food",
    "MARKETING EXPENSES - FOOD": "Food",
    "COURIER CHARGES": "Courier",
    "PRINTING & STATIONERY": "Printing & Stationery",
    "TRAIN / BUS FARE": "Train",
    "TRANSPORT CHARGES": "Bus",
}

_BRACKET_SUFFIX_RE = re.compile(r"\s*\[\d+\]\s*$")
_TRAILING_PAREN_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")


def normalize_category(category: str | None) -> str | None:
    if not category:
        return None
    text = _BRACKET_SUFFIX_RE.sub("", category).strip()
    m = _TRAILING_PAREN_RE.match(text)
    if m and m.group(1).strip().upper() == m.group(2).strip().upper():
        text = m.group(1).strip()
    return text.strip().upper() or None


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
