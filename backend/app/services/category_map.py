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
from dataclasses import dataclass
from decimal import Decimal

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
# A catering AMOUNT: a currency-prefixed figure or a figure with two decimals. A
# currency prefix glued to a bare integer ("RS26/3238", "Rs26") is a voucher /
# reference, never an amount: an integer needs "Rs." / "Rs " / "INR " / "₹", and
# only a decimal figure may follow a prefix with no space ("Rs368.00").
_CATERING_AMOUNT = (
    r"(?:₹|\bRs\.?|\bINR)\s*[\d,]+\.\d{1,2}\b"
    r"|(?:₹|\bRs\.|\bRs\s|\bINR\s)\s*[\d,]+"
    r"|\b\d[\d,]*\.\d{2}\b"
)
# What may sit between the word "catering" and its amount on the SAME line: only
# punctuation / brackets and qualifier words - never another column or row name.
#   accepted words: charge(s), service, incl(uding|usive), of, gst, tax(es), amount,
#                   applicable, fee(s), cost, bill, total
# "fee / cost / bill / total" name the catering charge itself when they directly follow
# the label ("Catering Fee 150.00", "Catering Total 368.00"); anything else in between
# ("Option", "Fare", "Convenience", "Room Rent", "Veg Meal", "No") means the amount
# belongs to something else, so none is taken. An extra word that is genuinely part
# of a catering line therefore fails SAFE: a mention with no amount, still reviewed.
_CATERING_SAME_LINE_RE = re.compile(
    r"(?:[\s:.\-–()/,]"
    r"|\b(?:charges?|service|incl(?:uding|usive)?|of|gst|taxes?|amount|applicable|fees?|cost|bill|total)\b)*?"
    r"(" + _CATERING_AMOUNT + r")",
    re.IGNORECASE,
)
_CATERING_SAME_LINE_WINDOW = 40
_CATERING_LINES_AHEAD = 3
# A line that only qualifies the label ("(Incl. of GST)") and may sit between a
# catering label and its amount when the text was extracted column by column.
_CATERING_QUALIFIER_LINE_RE = re.compile(
    r"^[\W_]*(?:(?:incl(?:uding|usive)?|of|gst|taxes?|charges?|service|amount|applicable|rs|inr)\b[\W_]*)*$",
    re.IGNORECASE,
)


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
    appears later) - the first occurrence with a linked amount wins, not
    just the first occurrence of the word.

    The amount must be LINKED to the label, never merely nearby: it is read from
    the same line when it follows the label through qualifier words / punctuation
    only ("Catering Charges (Incl. of GST): Rs. 368.00", "Catering Fee 150.00" -
    but not "Catering Service Option Fare 2,412.00"), or - for text extracted
    column by column - from the next line when that line is nothing but an amount
    (blank / "(Incl. of GST)"-style qualifier lines may sit in between).
    Anything else is a mention without an amount (the review is still raised,
    nothing is excluded): a printed "CATERING SERVICE" header followed by
    "ROOM RENT 1607.00" has no catering amount, a voucher such as "RS26/3238"
    is never an amount, and an amount that heads a column of amounts
    ("1607.00 / 508.00 / 300.00" under a list of labels) cannot be tied to
    this label.
    """
    text = raw_text or ""
    for match in _CATERING_RE.finditer(text):
        amount = _catering_amount_after(text, match)
        if amount is not None:
            return amount
    return None


def _catering_amount_after(text: str, match) -> float | None:
    line_end = text.find("\n", match.end())
    line_end = len(text) if line_end == -1 else line_end
    # Anchored: the amount must follow the label through qualifier words / punctuation only.
    same_line = _CATERING_SAME_LINE_RE.match(text[match.end() : min(line_end, match.end() + _CATERING_SAME_LINE_WINDOW)])
    if same_line:
        return parse_amount(same_line.group(1))
    position = line_end + 1
    for _ in range(_CATERING_LINES_AHEAD):
        if position > len(text):
            break
        end = text.find("\n", position)
        end = len(text) if end == -1 else end
        line = text[position:end].strip()
        if line:
            if _AMOUNT_ONLY_LINE_RE.match(line):
                return None if _amount_only_line_follows(text, end + 1) else parse_amount(line)
            if not _CATERING_QUALIFIER_LINE_RE.match(line):
                return None  # another labelled row: its amount is not this label's
        position = end + 1
    return None


def _amount_only_line_follows(text: str, position: int) -> bool:
    """True when the next non-blank line is also just an amount - i.e. the amount
    just read is the head of a column of amounts, which cannot be tied to one label."""
    for _ in range(_CATERING_LINES_AHEAD):
        if position > len(text):
            return False
        end = text.find("\n", position)
        end = len(text) if end == -1 else end
        line = text[position:end].strip()
        if line:
            return bool(_AMOUNT_ONLY_LINE_RE.match(line))
        position = end + 1
    return False


# Lodging room-service / food charges - the LODGING counterpart of catering
# above (same "REVIEW REQUIRED, never an automatic exception" treatment, same
# "excluded from the amount comparison" treatment), shared by validation/
# checks.py and bill_matcher.py so they can never disagree on whether/how much
# a hotel folio includes.
#
# STRONG terms name a food/room-service charge on their own, so merely seeing
# one is enough to put the bill in front of the auditor. "Fooding" is the
# term printed on small-hotel handwritten bills. Deliberately NOT included:
# "breakfast" (a folio's "Rate Type: Breakfast" is only the tariff plan the
# room rate already includes, not a separate charge) and "catering service"
# (a printed row header on some hotel forms, not a charge).
_ROOM_SERVICE_FOOD_STRONG_RE = re.compile(
    r"\broom\s*service\b"
    r"|\bin[\s-]?room\s+dining\b"
    r"|\bfooding\b"
    r"|\bfood\s*(?:&|and)\s*beverages?\b"
    r"|\bF\s*&\s*B\b"
    r"|\brestaurant\s+charges?\b"
    r"|\bfood\s+(?:charges?|bill|items?)\b",
    re.IGNORECASE,
)
# A bare "food" counts only when a charge amount sits on the same line (never
# FSSAI's "Food Safety and Standards Authority of India" licence line).
_ROOM_SERVICE_FOOD_WEAK_RE = re.compile(
    r"\bfood\b(?!\s*(?:safety|licen[sc]e|standards?|authority))", re.IGNORECASE
)
# Like _CATERING_AMOUNT_RE, a folio voucher reference such as "RS26/3238"
# must never be read as "Rs 26".
_LINE_AMOUNT_RE = re.compile(
    r"(?:₹|\bRs\.|\bRs\s|\bINR\s?)\s*[\d,]+(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\b", re.IGNORECASE
)
# A line that is nothing but an amount ("368.00", "₹ 368.00", "₹<NBSP>368.00",
# "Rs. 368.00", "INR 368.00"). Shared by the catering and room-service/food
# next-line readers; a symbol may be followed by any whitespace (\s covers a
# non-breaking space), and a bare integer is deliberately NOT an amount.
_AMOUNT_ONLY_LINE_RE = re.compile(r"^\s*(?:(?:₹|Rs\.?|INR)\s*)?\d[\d,]*\.\d{2}\s*$", re.IGNORECASE)

# A MENTION-ONLY hit (a food term with no readable amount) counts as a charge
# only when its line looks like a charge ROW. A hotel's descriptive prose
# ("Room service is available 24 hours", "Food & Beverage outlets are open from
# 7 am to 11 pm", "...included in the tariff") says nothing about this bill's
# charges, and the full-text scan now sees a folio's footer terms. A line is
# prose when it is long, ends like a sentence, or contains a descriptive cue
# word / a clock time. This is NOT a keyword blacklist over the whole bill: it
# is consulted only for a term that has no amount on its line, so a row such as
# "ROOM SERVICE 357.00" is never affected, and a genuine row whose amount is
# unreadable ("ROOM SERVICE Restaurant Charges RS26/3238", "FOODING",
# "Outlet: Room Service") has none of these cues and keeps its mention. Words
# that also occur in real folio rows or column headers ("outlet", "extra",
# "guest") are deliberately NOT cues.
_PROSE_MAX_WORDS = 8
_PROSE_CUE_RE = re.compile(
    r"\b(?:is|are|was|were|will|shall|may|can|please|available|availability|open|closed|timings?|hours?|hrs?|"
    r"daily|offered|offers|served|serves|provided|provides|between|until|till|from|included|includes|"
    r"including|inclusive|complimentary|free|subject|payable|charged|directly|requested|advised)\b"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[A-Za-z]\.$")


def _line_around(text: str, match) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    return text[start : len(text) if end == -1 else end].strip()


def _is_descriptive_prose(line: str) -> bool:
    return (
        len(line.split()) > _PROSE_MAX_WORDS
        or bool(_SENTENCE_END_RE.search(line))
        or bool(_PROSE_CUE_RE.search(line))
    )


def _room_service_food_matches(text: str):
    """Every (match, is_strong) occurrence in text order."""
    hits = [(m, True) for m in _ROOM_SERVICE_FOOD_STRONG_RE.finditer(text)]
    hits += [(m, False) for m in _ROOM_SERVICE_FOOD_WEAK_RE.finditer(text)]
    return sorted(hits, key=lambda hit: hit[0].start())


def _room_service_food_line_amount(text: str, match) -> float | None:
    """The charge printed on the same line as the term (or, for a column-
    extracted folio, alone on the very next line). Never reaches across
    several lines - an unrelated row's amount must not be mistaken for it.

    A next-line amount that is immediately followed by ANOTHER amount-only line
    is the head of a column of amounts ("ROOM RENT / FOODING / 1607.00 / 508.00"
    after a list of labels): which amount belongs to which label cannot be told,
    so none is taken (the mention is kept, nothing is excluded). A label that
    alternates with its amount ("FOODING / 508.00 / TOTAL ...") is unaffected."""
    line_end = text.find("\n", match.end())
    line_end = len(text) if line_end == -1 else line_end
    amount_match = _LINE_AMOUNT_RE.search(text[match.end() : min(line_end, match.end() + 120)])
    if amount_match:
        amount = parse_amount(amount_match.group(0))
    else:
        next_end = text.find("\n", line_end + 1)
        next_end = len(text) if next_end == -1 else next_end
        next_line = text[line_end + 1 : next_end]
        if _AMOUNT_ONLY_LINE_RE.match(next_line) and not _amount_only_line_follows(text, next_end + 1):
            amount = parse_amount(next_line.strip())
        else:
            amount = None
    return amount if amount else None  # None and 0.00 both mean "no usable amount"



# --- Several charges on one bill -------------------------------------------
# A folio can carry several room-service / food rows (one per order), a printed
# total for them, and tax rows that name the same terms. analyze_room_service_
# food() therefore works ROW BY ROW:
#   * one row per LINE - a line naming several food terms ("ROOM SERVICE
#     Restaurant Charges RS26/3238 357.00") is one charge, never two;
#   * each amount-bearing row is a DETAIL, a SUMMARY (a total/subtotal row) or a
#     TAX row (GST / CGST / SGST / IGST / service charge / ... - never a food
#     charge, so never added);
#   * identical rows are de-duplicated conservatively (see below);
#   * the rows are then reconciled:
#         details only                      -> their sum
#         exactly one distinct summary only -> that summary
#         details + a summary that agrees   -> the detail sum (the summary is never
#                                              added on top of its own details)
#         anything ambiguous                -> amount None, mention still True
# "Ambiguous" means: several different summary values; details and a summary that
# disagree; more than _MAX_ROOM_SERVICE_FOOD_ROWS candidate rows; or identical
# rows that carry only a date (a repeated page, or two orders on one day -
# unknowable). An unresolved amount is never guessed and never excluded, and the
# review flag (`mentioned`) never depends on the amount reconciling.
_MAX_ROOM_SERVICE_FOOD_ROWS = 8
_TAX_MARKER_RE = re.compile(
    r"\b(?:tax(?:es)?|gst|cgst|sgst|igst|utgst|vat|cess|surcharge|levy|service\s*charges?)\b", re.IGNORECASE
)
_SUMMARY_MARKER_RE = re.compile(
    r"\b(?:sub\s*-?\s*total|total|grand|net|overall|summary|aggregate|balance|payable|due)\b", re.IGNORECASE
)
_DATE_TOKEN_RE = re.compile(
    r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b"
    r"|\b\d{1,2}[\s\-]?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[\s\-,]*\d{2,4}\b",
    re.IGNORECASE,
)
# A voucher / order / KOT reference ("RS26/3238", "KOT 4417" is a word + number,
# "RS26" is letters + digits). Dates are removed first so "24/09/2026" is not one.
_REF_TOKEN_RE = re.compile(r"\b[A-Za-z]+[\-/]?\d+[A-Za-z0-9/\-]*\b|\b\d+/\d+[A-Za-z0-9/\-]*\b")


@dataclass(frozen=True)
class RoomServiceFoodEvidence:
    amount: float | None  # reconciled charge, or None (no amount, or unresolved)
    mentioned: bool  # a room-service / food charge is on the bill (independent of `amount`)
    charges: tuple[float, ...] = ()  # the reconciled charge rows (for the audit note)
    basis: str = "none"  # none | detail | summary | detail+summary | unresolved
    note: str | None = None  # audit note, only for several charges or an unresolved result


def _money(value: Decimal) -> str:
    return f"Rs.{value:,.2f}"


def _row_kind(line: str) -> str:
    """DETAIL / SUMMARY / TAX for an amount-bearing row. The food terms are
    removed first, so "Room Service Charge 357.00" (a charge row) is not mistaken
    for a "service charge" tax row, while "Service Charge on Room Service 35.70" is."""
    stripped = _ROOM_SERVICE_FOOD_WEAK_RE.sub(" ", _ROOM_SERVICE_FOOD_STRONG_RE.sub(" ", line))
    if _TAX_MARKER_RE.search(stripped):
        return "tax"
    if _SUMMARY_MARKER_RE.search(stripped):
        return "summary"
    return "detail"


def analyze_room_service_food(raw_text: str | None) -> RoomServiceFoodEvidence:
    text = raw_text or ""
    lines: dict[int, list] = {}  # line start -> [(match, strong), ...] in text order
    for match, strong in _room_service_food_matches(text):
        lines.setdefault(text.rfind("\n", 0, match.start()) + 1, []).append((match, strong))

    mentioned = False
    details: list[tuple[str, Decimal, bool, bool]] = []  # (key, amount, has_ref, has_date) per row
    summaries: dict[str, Decimal] = {}
    for hits in lines.values():
        first_match = hits[0][0]
        line = _line_around(text, first_match)
        amount = _room_service_food_line_amount(text, first_match)
        if amount is None:
            # A mention-only row: counts as the charge being on the bill (never as an amount).
            if any(strong for _m, strong in hits) and not _is_descriptive_prose(line):
                mentioned = True
            continue
        mentioned = True
        kind = _row_kind(line)
        if kind == "tax":
            continue
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
        # The amount is part of the row's identity: with the amount on the NEXT line the label
        # line alone ("ROOM SERVICE") is identical for different charges and must not collapse them.
        key = " ".join(line.lower().split()) + f" | {value}"
        if kind == "summary":
            summaries[key] = value
        else:
            dateless = _DATE_TOKEN_RE.sub(" ", line)
            details.append((key, value, bool(_REF_TOKEN_RE.search(dateless)), bool(_DATE_TOKEN_RE.search(line))))

    # De-duplicate identical detail rows. The same voucher / reference twice is one
    # transaction; identical rows with no token at all are one repeated line; identical
    # rows that carry ONLY a date are unknowable (repeat or two orders) -> unresolved.
    kept: dict[str, Decimal] = {}
    ambiguous_repeat = False
    seen: dict[str, int] = {}
    for key, value, has_ref, has_date in details:
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1 and has_date and not has_ref:
            ambiguous_repeat = True
        kept.setdefault(key, value)
    detail_values = list(kept.values())
    summary_values = sorted(set(summaries.values()))

    if not detail_values and not summary_values:
        return RoomServiceFoodEvidence(amount=None, mentioned=mentioned)

    reason = None
    if ambiguous_repeat:
        reason = "identical rows that may be separate orders"
    elif len(detail_values) + len(summaries) > _MAX_ROOM_SERVICE_FOOD_ROWS:
        reason = f"more than {_MAX_ROOM_SERVICE_FOOD_ROWS} charge rows"
    elif len(summary_values) > 1:
        reason = "several different totals"
    elif detail_values and summary_values and abs(sum(detail_values) - summary_values[0]) > Decimal("0.01"):
        reason = "the listed charges and the printed total disagree"
    if reason:
        return RoomServiceFoodEvidence(
            amount=None, mentioned=mentioned, basis="unresolved",
            note=f"Room service/food charges could not be reconciled ({reason}); no amount was used.",
        )

    if detail_values:
        total = sum(detail_values)
        basis = "detail+summary" if summary_values else "detail"
        charges = tuple(float(v) for v in detail_values)
    else:
        total, basis, charges = summary_values[0], "summary", (float(summary_values[0]),)
    note = None
    if len(detail_values) > 1:
        note = (
            f"Room service/food: {len(detail_values)} charges ({' + '.join(_money(v) for v in detail_values)}) "
            f"totalling {_money(total)}."
            + (" Agrees with the printed total." if summary_values else "")
        )
    return RoomServiceFoodEvidence(amount=float(total), mentioned=mentioned, charges=charges, basis=basis, note=note)


def detect_room_service_food_amount(raw_text: str | None) -> float | None:
    """The reconciled room-service / food charge on a lodging bill; None when
    there is none, or when it cannot be reconciled with confidence (never a
    guess). See analyze_room_service_food()."""
    return analyze_room_service_food(raw_text).amount


def mentions_room_service_food(raw_text: str | None) -> bool:
    """Room service / food is on the bill, whether or not its amount could be
    read or reconciled: a term with a charge on its line, or - when no amount is
    readable - a strong term on a line that looks like a charge row rather than
    descriptive prose (see _is_descriptive_prose). A bare "food" needs an amount."""
    return analyze_room_service_food(raw_text).mentioned


# Bill.raw_text is capped at this many characters (bill_parser.parse_bill_text,
# bill_pipeline._bill_from_classification, models/bill.py's String(2000)).
BILL_RAW_TEXT_CAP = 2000

# --- Stored full-text evidence -------------------------------------------
# Bill.raw_text is capped at 2,000 characters, but a charge can be printed
# after that point. bill_parser.parse_bill_text therefore runs the detectors
# above on the FULL bill text once and persists the result on the Bill row
# (catering_amount / catering_mentioned / room_service_food_amount /
# room_service_food_mentioned). The resolvers below are the one place that
# decides which source a consumer uses: the stored evidence when there is
# some, otherwise the detectors on raw_text - so a row with NULL columns
# (extracted before these columns existed) behaves exactly as it always did.
# A stored flag can only ever ADD a mention; nothing here guesses an amount.


def _stored_positive_amount(value) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def resolve_catering_amount(raw_text: str | None, stored_amount=None) -> float | None:
    """Which catering amount to use. While raw_text is shorter than the cap it
    IS the whole bill text, so the existing order is untouched: the raw_text
    detector first, the stored amount (the classifier's own reading, or the
    full-text reading) only as the fallback. Once raw_text has hit the cap it
    is just a prefix - the detector could miss the charge or read a number cut
    in half at the cap - so the amount read from the full text at extraction
    time and stored on the Bill wins."""
    stored = _stored_positive_amount(stored_amount)
    from_text = detect_catering_amount(raw_text)
    if len(raw_text or "") >= BILL_RAW_TEXT_CAP and stored is not None:
        return stored
    return from_text if from_text is not None else stored


def resolve_catering_mentioned(raw_text: str | None, stored_flag=None, amount: float | None = None) -> bool:
    return amount is not None or bool(stored_flag) or mentions_catering(raw_text)


def resolve_room_service_food_amount(raw_text: str | None, stored_amount=None, stored_flag=None) -> float | None:
    """The room-service / food amount to use. The stored mention flag is NULL
    only on a row extracted before full-text evidence existed: that legacy row
    falls back to the (aggregating) detector on raw_text, exactly as before. A
    row that carries the flag - True OR False - was analysed on its FULL text, so
    its stored amount is final: None there means "no charge" or "could not be
    reconciled", and must never be replaced by a clean-looking row that happens
    to sit in the 2,000-character prefix (that would grant an exclusion the full
    text ruled out)."""
    stored = _stored_positive_amount(stored_amount)
    if stored_flag is not None:
        return stored
    return stored if stored is not None else detect_room_service_food_amount(raw_text)


def resolve_room_service_food_mentioned(raw_text: str | None, stored_flag=None, amount: float | None = None) -> bool:
    if stored_flag is not None:  # analysed on the full text: the stored flag (and any amount) is the answer
        return bool(stored_flag) or amount is not None
    return amount is not None or mentions_room_service_food(raw_text)
