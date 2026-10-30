"""Extracts bill/receipt fields from a single supporting-document page's text
(native or OCR'd - see text_extraction.py:bill_pages_from). Reuses Phase 3's
money/date parsing rather than reimplementing it.

Never invents a value: amount/date/vendor are only set when the evidence is
reasonably clear, otherwise the field stays None and a warning is recorded so
the caller can mark the bill REVIEW_REQUIRED instead of guessing.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.category_map import (
    BILL_RAW_TEXT_CAP,
    analyze_room_service_food,
    detect_catering_amount,
    mentions_catering,
)
from app.services.extraction import deepseek_fallback, nvidia_fallback, payment_evidence
from app.services.extraction.dates import DateReading, has_adjacent_clock, read_dates
from app.services.extraction.money import parse_amount, parse_amount_in_words

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b")

# (priority, keyword) - higher priority wins when multiple labeled amounts exist.
AMOUNT_LABELS = [
    (5, "grand total"),
    (5, "total amount"),
    (4, "net amount"),
    (4, "amount payable"),
    (4, "paid amount"),
    (4, "total payable"),
    (4, "total fare"),
    (3, "total reimbursable"),
    (3, "amount"),
    # A bare "total" also matches inside an unrelated boilerplate line further
    # down the same page (e.g. an IRCTC ticket's GST breakdown ends with
    # "Total Tax:" underneath the real "Total Fare (all inclusive)" line) -
    # kept lower priority than the more specific "total fare"/"total
    # payable"/etc. above so that boilerplate can never tie with, or beat,
    # the real total.
    (3, "total"),
    (2, "sub total"),
    (2, "subtotal"),
]

# Word-boundary versions of the same labels - "total" must not match inside
# "QtyTotal" (a table column header some receipts render with no space).
_AMOUNT_LABEL_PATTERNS = [
    (priority, label, re.compile(r"\b" + re.escape(label) + r"\b", re.IGNORECASE)) for priority, label in AMOUNT_LABELS
]

BILL_NUMBER_LABELS = [
    "invoice no",
    "inv no",
    "inv.no",
    "bill no",
    "receipt no",
    "receipt",
    "txn id",
    "reference no",
    "ref no",
    "awb no",
    "memo#",
    "memo #",
]

# (priority, label) - higher wins when several labelled dates are found on
# the same bill (e.g. a train ticket has its own "Date:", a "Booking Date",
# and a "Start Date"). Every entry containing "date" as a substring would
# otherwise also match the generic "date" label - max()-by-priority in
# _find_date already handles that overlap correctly, no exclusion needed
# for these.
DATE_LABELS = [
    (4, "invoice date"),
    (4, "bill date"),
    (3, "date of travel"),
    (3, "journey date"),
    (3, "start date"),
    (2, "txn date"),
    (2, "booking date"),
    (1, "date"),
    (1, "dt"),
    (1, "txst"),
]
# A date CAN appear on these labelled lines, but it is never the bill's own
# transaction date - a "Due Date"/"Print Date"/"Departure Date" etc. must
# never be picked over (or instead of) the real one, so these lines are
# skipped entirely rather than being assigned any priority.
DATE_EXCLUDE_LABELS = [
    "due date", "payment date", "print date", "printed date", "generated date",
    "upload date", "departure date", "arrival date", "expiry date", "valid until",
    "valid upto", "validity date",
]

VENDOR_SUFFIX_RE = re.compile(
    r"\b(LTD|LIMITED|PVT|CORP|CORPORATION|HOTEL|RESTAURANT|TRAVELS|TOUR|STORES|AGENC\w*|"
    r"PETROLEUM|EXPRESS|DEALER|CAFE|ENTERPRISE\w*|HARDWARE\w*|ELECTRICAL\w*|SERVICES?|"
    r"BAR|AUTOFUELS?|MESS)\b",
    re.IGNORECASE,
)

CATEGORY_KEYWORDS = [
    # Checked before Fuel below: a flight ticket's own fare breakdown often
    # includes a line item literally named "Airline Fuel Charge" (confirmed
    # real case: a Cleartrip/Air India Express ticket), which must never be
    # read as fuel-pump evidence. Named airlines are the actual service
    # provider (never a booking platform like Cleartrip/MakeMyTrip, which is
    # a vendor, not the service itself - see module-level guidance on
    # keeping document type/service/vendor distinct) plus structural airport/
    # boarding terms that only appear on a real ticket or boarding pass.
    (
        re.compile(
            r"\b(AIR INDIA(?:\s+EXPRESS)?|INDIGO|SPICEJET|VISTARA|AKASA(?:\s+AIR)?|GO\s*FIRST|GOAIR|ALLIANCE AIR|"
            r"BOARDING PASS|BAGGAGE|CABIN BAGGAGE|BOARDING TIME|DEPARTURE GATE|"
            # Carrier-independent evidence, so an airline this list has never heard of is still a
            # flight: the words airline/airways, or a printed flight designator ("Flight SK 123",
            # "IX - 1248" after a Flight label).
            r"AIRLINES?|AIRWAYS|FLIGHT\s*(?:NO\.?|NUMBER)?\s*:?\s*[A-Z0-9]{2}\s*-?\s*\d{2,4})\b",
            re.IGNORECASE,
        ),
        "Flight",
    ),
    # NOZZLE/NOZL: confirmed on every real fuel-pump receipt in the
    # reference corpus (5/5) as a standalone fuel-dispenser line-item term
    # ("Nozzle No. :01") - a reliable second signal for pumps whose OCR text
    # garbles PETROL/DIESEL/FUEL itself (e.g. "Fue]", "Diese l" with an
    # inserted space) but still renders "Nozl"/"Nozzle" legibly. These
    # (plus PETROL/DIESEL/AUTOFUEL) are unambiguous on their own. The bare
    # word "FUEL" is not - confirmed real false positives: a flight ticket's
    # "Airline Fuel Charge" fare line, and a hotel bill whose own printed
    # address happens to read "...BESIDE KESAR KUNTI FUEL STATION" (a nearby
    # landmark, not a transaction) - excluded narrowly by context rather
    # than dropping "FUEL" entirely, which stays a reliable signal in every
    # other position.
    (re.compile(r"\b(PETROL|DIESEL|AUTOFUEL|NOZZLE|NOZL)\b", re.IGNORECASE), "Fuel"),
    (re.compile(r"(?<!AIRLINE )\bFUEL\b(?!\s+STATION)", re.IGNORECASE), "Fuel"),
    # Oil-company brand names (category_vocabulary.json, OWN/COMPANY CAR/TWO
    # WHEELER PETROL entry) - reliable standalone like the airline names
    # above, a dealer's own letterhead naming the oil company it resells for.
    (re.compile(r"\b(BHARAT PETROLEUM|HINDUSTAN PETROLEUM|INDIAN\s*OIL|IOCL|BPCL|HPCL|NAYARA|XTRAGREEN|\bHSD\b)\b", re.IGNORECASE), "Fuel"),
    # PETROLEUM alone (e.g. a dealer name like "UNNATI PETROLEUM" - the
    # confirmed real defect this research traced) is NOT used unguarded:
    # research flagged real collision risk against an unrelated company whose
    # trade name happens to end in "Petroleum". Only counted as fuel evidence
    # when a fuel-dispenser-specific field (nozzle/rate/volume/density/
    # product) also appears nearby on the same page.
    (re.compile(r"\bPETROLEUM\b(?=[\s\S]{0,300}\b(?:NOZZLE|NOZL|RATE|VOLUME|LTR|LITRE|DENSITY|PRODUCT)\b)", re.IGNORECASE), "Fuel"),
    # ROOM TARIFF/ROOM NO/GUEST NAME/CHECK-IN/CHECK-OUT: confirmed on a real
    # hotel tax invoice whose own printed name ("... Suites Inn & Spa") never
    # contains the word "hotel" at all - these front-desk/folio terms are
    # reliable, vendor-name-independent hotel evidence. FOLIO/NIGHTS STAYED
    # added per category_vocabulary.json's LODGING broadening entry, which
    # explicitly researched and then explicitly REJECTED adding bare hotel-
    # brand-suffix words (Inn/Suites/Resort/Residency/Grand/Regency/Palace) -
    # those collide with ordinary prose and "Grand Total" - so none of them
    # are added here.
    (
        re.compile(
            r"\b(HOTEL|LODGING|ROOM CHARGE|ACCOMMODATION|ROOM TARIFF|ROOM NO|GUEST NAME|CHECK-IN|CHECK-OUT|FOLIO|NIGHTS STAYED)\b",
            re.IGNORECASE,
        ),
        "Lodging",
    ),
    # A cab/ride-hailing bill rarely prints the literal word "cab"/"taxi" - it
    # prints what the service actually is (a ride fare, a passenger transport
    # service). These are the terms real ride-receipts use (see the
    # ER-41115 CAB CHARGES bill: "Ride fare", "Passengers Transport
    # Services") - never require the Policy Master category's own wording to
    # appear verbatim on the bill.
    (re.compile(r"\b(UBER|OLA|CAB|TAXI|RIDE FARE|RIDE|PASSENGERS?\s+TRANSPORT(?:\s+SERVICES?)?)\b", re.IGNORECASE), "Cab/Taxi"),
    (re.compile(r"\b(FOOD|RESTAURANT|MEAL|DINE|MESS|CAFE|BAR)\b", re.IGNORECASE), "Food"),
    (re.compile(r"\b(COURIER|DTDC|AWB)\b", re.IGNORECASE), "Courier"),
    # Checked before the bare "PRINTING" pattern below, same reason Flight is
    # checked before Fuel above: "Flex Banner Printing"/"Brochure Printing"/
    # "Flyer Printing" all contain the literal word "Printing", which must
    # not be claimed by ordinary office PRINTING & STATIONERY.
    (
        re.compile(
            r"\b(ADVERTISEMENT|ADVERTISING|PUBLICITY EXPENSES?|SPONSORSHIP|FLEX (?:BANNER|PRINTING)|HOARDING|"
            r"BRANDED MERCHANDISE|CORPORATE GIFT|CLIENT GIFT|BROCHURE PRINTING|FLYER PRINTING|SIGNAGE)\b",
            re.IGNORECASE,
        ),
        "Marketing Collateral",
    ),
    (re.compile(r"\b(PRINTING|STATIONERY|STATIONARY)\b", re.IGNORECASE), "Printing & Stationery"),
    # PNR is deliberately NOT a train keyword: airlines print a PNR too, and a bare "PNR" on a page
    # with no other evidence is not enough to call it a railway ticket.
    (re.compile(r"\b(TRAIN|RAILWAY|IRCTC)\b", re.IGNORECASE), "Train"),
    (re.compile(r"\bBUS\b(?!\s*-?\s*(?:BARS?|DUCTS?)\b)", re.IGNORECASE), "Bus"),  # weak: see _BUS_WORD_TRAVEL_CONTEXT_RE
    # Checked before the bare "HARDWARE" pattern below - "Hardware Repair"
    # contains the literal word "Hardware", which must not be claimed by the
    # pre-existing generic Hardware/Electrical label.
    (
        re.compile(
            r"\b(LAPTOP REPAIR|COMPUTER REPAIR|MOTHERBOARD REPAIR|SCREEN REPLACEMENT|HARDWARE REPAIR|"
            r"IT SERVICE CENTER|AUTHORIZED SERVICE CENTER|LAPTOP SERVICING|CHIP-LEVEL REPAIR|DIAGNOSTIC FEE|DIAGNOSIS FEE)\b",
            re.IGNORECASE,
        ),
        "Computer Repair",
    ),
    (re.compile(r"\b(ELECTRICAL|HARDWARE)\b", re.IGNORECASE), "Hardware/Electrical"),
    # TOLL TAX CHARGES - deliberately excludes "ROAD TAX" (an RTO vehicle tax,
    # not a toll - see category_vocabulary.json's exclusion note; that belongs
    # to the Statutory Tax pattern below instead).
    (re.compile(r"\b(TOLL PLAZA|TOLL TAX|TOLL FEE|FASTAG|NETC|\bNHAI\b)\b", re.IGNORECASE), "Toll"),
    # TRANSPORT CHARGES - goods/freight movement evidence only (see
    # category_map.py's comment on why this category no longer shares the
    # passenger-ticket "Bus" label). A passenger cab/bus/train ticket must
    # never match here.
    (
        re.compile(
            r"\b(CONSIGNMENT NOTE|GOODS TRANSPORT AGENCY|\bGTA\b|LORRY RECEIPT|\bLR NO\b|TRUCK HIRE|VEHICLE HIRE CHARGES|CARTAGE|FREIGHT)\b",
            re.IGNORECASE,
        ),
        "Freight/Transport",
    ),
    (re.compile(r"\b(CALIBRATION|NABL ACCREDITED|ISO[\s/]?IEC\s*17025|TRACEABILITY CERTIFICATE)\b", re.IGNORECASE), "Calibration"),
    # Phrase-anchored, not bare "INTERNET" - a bare word that common enough to
    # appear in unrelated boilerplate/footer text (e.g. "visit our website").
    (
        re.compile(
            r"\b(BROADBAND|WI-?FI|STATIC IP|LEASED LINE|FIBERNET|FIBRENET|JIO FIBER|AIRTEL XSTREAM|ACT FIBERNET|"
            r"HATHWAY|BSNL BROADBAND|CONNECTIVITY SERVICES|INTERNET (?:BILL|CHARGES|SERVICES?|CONNECTION))\b",
            re.IGNORECASE,
        ),
        "Internet",
    ),
    # TELEPHONE ACCESSORIES - products, not a mobile/telecom service bill.
    # "MOBILE "/"PHONE "/"DATA " prefixes on charger/cable keep this from
    # matching a laptop charger or an unrelated cable (see
    # category_vocabulary.json's exclusion note: a mobile recharge or
    # telecom service bill must never land here).
    (
        re.compile(
            r"\b(MOBILE CHARGER|PHONE CHARGER|DATA CABLE|USB CABLE|EARPHONES?|EARBUDS|HEADSET|HEADPHONES?|"
            r"POWER BANK|SCREEN GUARD|TEMPERED GLASS|PHONE CASE|MOBILE COVER|MOBILE STAND)\b",
            re.IGNORECASE,
        ),
        "Phone Accessories",
    ),
    # RATES & TAXES - a statutory/government levy, never a routine GST line
    # on an ordinary purchase invoice (see category_vocabulary.json's
    # exclusion note - a CGST/SGST line alone must never match here).
    (
        re.compile(
            r"\b(PROPERTY TAX|PROFESSIONAL TAX|MUNICIPAL (?:CORPORATION )?TAX|TRADE LICEN[CS]E|FACTORY LICEN[CS]E|"
            r"SHOP (?:AND|&) ESTABLISHMENT LICEN[CS]E|ROAD TAX)\b",
            re.IGNORECASE,
        ),
        "Statutory Tax",
    ),
    # Never bare "SUBSCRIPTION" - collides with a SaaS/software subscription
    # (INTERNET CHARGES/SERVICE PURCHASES territory, see exclusion note).
    (
        re.compile(
            r"\b(MEMBERSHIP FEE|ANNUAL MEMBERSHIP|MEMBERSHIP RENEWAL|\bICAI\b|\bICSI\b|\bIEEE\b|"
            r"CERTIFICATE OF PRACTICE|\bCOP FEE\b|ANNUAL FEE CIRCULAR|ASSOCIATION DUES)\b",
            re.IGNORECASE,
        ),
        "Membership",
    ),
    # Never bare "EMD"/"EARNEST MONEY DEPOSIT" - real tender notices list
    # that as a separate, usually-refundable line item, not an expense (see
    # category_vocabulary.json's exclusion note).
    (re.compile(r"\b(TENDER FEE|TENDER DOCUMENT|COST OF TENDER|BID DOCUMENT FEE)\b", re.IGNORECASE), "Tender Fee"),
    # Each anchored to an explicit "...FEE"/"...APPLICATION" etc. phrase -
    # bare VISA/PARKING/INSURANCE are all explicitly flagged as unsafe
    # standalone matches (VISA is the card-network brand on most POS
    # receipts; PARKING/INSURANCE are too generic) - see
    # category_vocabulary.json's TRAVEL-OTHERS exclusion note.
    (
        re.compile(
            r"\b(VISA FEE|VISA APPLICATION|TRAVEL INSURANCE|OVERSEAS MEDICLAIM|EXCESS BAGGAGE|BAGGAGE CHARGES|"
            r"AIRPORT PARKING|LOUNGE ACCESS|CURRENCY CONVERSION|DYNAMIC CURRENCY CONVERSION|\bDCC\b|\bFOREX\b)\b",
            re.IGNORECASE,
        ),
        "Travel Miscellaneous",
    ),
    # "REGISTRATION" only in event-specific phrase form (never bare) - a bare
    # word collides with GST/vehicle/company registration (see
    # category_vocabulary.json's exclusion note).
    (
        re.compile(
            r"\b(DELEGATE FEE|SEMINAR REGISTRATION|CONFERENCE REGISTRATION|WORKSHOP FEE|EXHIBITION STALL|"
            r"TRADE FAIR|SHELL SCHEME|STALL (?:RENT|CHARGES)|BOOTH CHARGES|SPACE RENT)\b",
            re.IGNORECASE,
        ),
        "Seminar/Conference",
    ),
    # SERVICE PURCHASES/SERVICE CHARGE - deliberately LAST. Research
    # (category_vocabulary.json) found this is a genuine catch-all sharing
    # SAC 9983 and generic "service"/"professional"/"consultancy" wording
    # with most of the categories above it, with no vocabulary that reliably
    # distinguishes it from them. Checked only once every more specific
    # category above has already failed to match, so a specific match never
    # loses to this one - exactly mirroring how _find_category itself
    # returns on the first match in list order.
    (
        re.compile(
            r"\b(SERVICE CHARGES?|SERVICE PURCHASES?|\bAMC\b|ANNUAL MAINTENANCE CONTRACT|PROFESSIONAL (?:CHARGES|FEES)|"
            r"CONSULTANCY (?:CHARGES|FEES))\b",
            re.IGNORECASE,
        ),
        "Service Purchase",
    ),
]

# Small, deliberately conservative set of category-defining words known to
# suffer real OCR corruption on thermal-print/scanned receipts (confirmed
# real case: "Nozzle" OCR'd with a stray leading character fused onto it with
# no word-boundary gap, "Diesel" OCR'd as "Diese l" with an inserted space -
# both defeat the strict \b-anchored patterns above even though the word is
# plainly present to a human reader). Only ever consulted as a fallback, once
# the strict pass above has already found nothing - never overrides a strict
# match, never widens what counts as a match for anything already strict-
# matchable. Kept to a handful of long (>=5 char), highly category-specific
# words for the same reason _FUZZY_AMOUNT_LABELS stays short and conservative
# - short/common words (e.g. "BUS", "MS", "INN") are never eligible here, the
# false-positive risk of fuzzy-matching them is too high.
_FUZZY_CATEGORY_WORDS = [
    ("NOZZLE", "Fuel"),
    ("PETROL", "Fuel"),
    ("DIESEL", "Fuel"),
    ("HOTEL", "Lodging"),
    ("COURIER", "Courier"),
    ("STATIONERY", "Printing & Stationery"),
]
_FUZZY_CATEGORY_WORD_RE = re.compile(r"[A-Za-z]{4,}")


def _find_category_fuzzy(text: str) -> tuple[str | None, str | None]:
    for m in _FUZZY_CATEGORY_WORD_RE.finditer(text):
        word = m.group(0).upper()
        for keyword, label in _FUZZY_CATEGORY_WORDS:
            if _edit_distance_le(word, keyword, 1):
                return label, m.group(0)
    return None, None

BILL_EVIDENCE_RE = re.compile(
    r"\b(RECEIPT|INVOICE|TAX INVOICE|BILL|GSTIN|THANK YOU|CASH BILL|RETAIL INVOICE)\b", re.IGNORECASE
)

_AMOUNT_TOKEN_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\b", re.IGNORECASE)
# OCR on thermal-print bills routinely mangles the currency marker and decimal
# point (e.g. "Amount. (Fis. J: 2000 Gti") while the integer digits survive -
# used only as a second-chance read on a line we already know is labeled
# "amount"/"total"/etc., never applied to unlabeled text.
_LOOSE_NUMBER_RE = re.compile(r"\d[\d,]{1,6}")


@dataclass
class ParsedBill:
    vendor: str | None = None
    bill_number: str | None = None
    bill_date: Any = None
    amount: float | None = None
    gstin: str | None = None
    description: str | None = None
    category_guess: str | None = None
    raw_text: str = ""  # capped at 2,000 characters
    # Catering / lodging room-service-food evidence read from the FULL bill text
    # (not the capped raw_text), persisted so a charge printed beyond the cap is
    # still found - see category_map.resolve_*. An unreadable amount stays None
    # while the "mentioned" flag still records that the charge is on the bill.
    catering_amount: float | None = None
    catering_mentioned: bool = False
    room_service_food_amount: float | None = None
    room_service_food_mentioned: bool = False
    confidence: str = "LOW"  # HIGH | MEDIUM | LOW
    status: str = "REVIEW_REQUIRED"  # EXTRACTED | REVIEW_REQUIRED
    warnings: list[str] = field(default_factory=list)
    # The date exactly as printed, every valid reading, and how (if at all) the
    # day/month order was established. bill_date is None whenever the order is
    # unresolved - the candidates stay available here for review.
    date_reading: "DateReading | None" = None
    # Payment-screenshot evidence (see payment_evidence.py); all None when the
    # page is not a transaction screenshot.
    is_payment: bool = False
    recipient: str | None = None  # payee named by the screenshot ("Paid to X"); preferred over a guessed vendor line
    payment_status: str | None = None  # COMPLETED | PENDING | FAILED
    transaction_time: str | None = None  # HH:MM, 24 h
    transaction_reference: str | None = None


def has_bill_evidence(text: str) -> bool:
    """Cheap pre-filter: is there enough raw text here to even attempt
    extraction, as opposed to a truly blank/unreadable page? The real
    decision - whether what was extracted is worth keeping as a bill
    record - happens after parsing, in parsed_has_evidence(), since some
    signals (e.g. a labeled amount) only surface once parsing has run."""
    return bool(text) and len(text.strip()) >= 5


def parsed_has_evidence(parsed: "ParsedBill", text: str) -> bool:
    """Post-parse decision: does this page contain enough to be worth a bill
    record, even a REVIEW_REQUIRED one? Guards against creating phantom bill
    rows for pages that turn out to have no recognizable content at all."""
    if parsed.amount is not None:
        return True
    if parsed.gstin or parsed.bill_number or parsed.category_guess:
        return True
    if BILL_EVIDENCE_RE.search(text):
        return True
    return False


_FLAT_WINDOW_CHARS = 40
# How many further *blank-separated* lines below a label to search for its
# value once the label's own line and the single immediate next line both
# came up empty - covers "Grand Total\n\n₹2,329.65"-style receipts where a
# label and its value are separated only by blank spacer lines. Deliberately
# does NOT bridge over a non-blank line: a real-corpus regression proved that
# doing so lets a bare column-header label (e.g. a table's "Amount" header
# above a "Description | Amount" row) reach through an intervening line-item
# label (e.g. "Ride fare (in INR)") and grab *that* field's own value instead
# of correctly finding nothing and falling through to this module's
# arithmetic-sum-of-components fallback. Bridging blank lines only keeps the
# legitimate spacer-gap case while never overshooting into another field.
_MAX_VALUE_LOOKAHEAD_LINES = 3


def _label_value_window(text: str, idx: int, label_len: int) -> str:
    """The text to search for this label occurrence's value: the rest of the
    label's own line first; if that line has nothing after the label, the
    single immediate next line, skipping over up to _MAX_VALUE_LOOKAHEAD_LINES
    blank spacer lines to reach it (covers "Total\\n\\n₹2,329.65"-style
    receipts). Stops at the first non-blank line, whatever it contains - it
    never keeps searching past one real line of content, so it can't
    accidentally run past that line into some other field's own value (see
    _MAX_VALUE_LOOKAHEAD_LINES for why). Falls back to a short flat character
    window when the text has no line breaks at all (some badly-skewed
    thermal-print scans collapse into one line)."""
    line_end = text.find("\n", idx)
    if line_end == -1:
        return text[idx : idx + label_len + _FLAT_WINDOW_CHARS]

    own_line_rest = text[idx + label_len : line_end]
    if _AMOUNT_TOKEN_RE.search(own_line_rest) or _LOOSE_NUMBER_RE.search(own_line_rest):
        return text[idx:line_end]

    search_from = line_end + 1
    for _ in range(_MAX_VALUE_LOOKAHEAD_LINES):
        next_end = text.find("\n", search_from)
        line = text[search_from:next_end] if next_end != -1 else text[search_from:]
        if line.strip():
            return line
        if next_end == -1:
            break
        search_from = next_end + 1
    return ""


_FUZZY_AMOUNT_LABELS = ("amount", "total")  # short, single-word, high-confidence labels only
_FUZZY_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def _edit_distance_le(a: str, b: str, max_dist: int) -> bool:
    """True if the Levenshtein distance between a and b is <= max_dist."""
    if abs(len(a) - len(b)) > max_dist:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1] <= max_dist


def _find_fuzzy_labelled_amount(text: str) -> float | None:
    for m in _FUZZY_WORD_RE.finditer(text):
        word = m.group(0).lower()
        for label in _FUZZY_AMOUNT_LABELS:
            if word == label or not _edit_distance_le(word, label, 1):
                continue
            window = _label_value_window(text, m.start(), len(m.group(0)))
            tokens = _AMOUNT_TOKEN_RE.findall(window)  # strict only - no loose-number fallback here
            if tokens:
                amt = parse_amount(tokens[0])
                if amt is not None and amt >= 10:
                    return amt
    return None


def _find_amount(text: str) -> tuple[float | None, str, list[str]]:
    """The bill's amount: the printed figure (_find_printed_amount),
    cross-checked against any amount spelled out in words on the same page."""
    amount, confidence, warnings = _find_printed_amount(text)
    return _reconcile_with_amount_in_words(text, amount, confidence, warnings)


def _reconcile_with_amount_in_words(
    text: str, amount: float | None, confidence: str, warnings: list[str]
) -> tuple[float | None, str, list[str]]:
    """Payment-app screenshots print the amount twice - as a figure and as
    "Rupees Five Hundred Only". OCR often reads the rupee glyph as a leading
    digit, turning "₹500" into "7500". The words are independent evidence,
    used only to check a figure that WAS read:

      * no printed figure at all -> unchanged (None). The words alone are not
        used to create an amount: that would silently turn every unreadable
        bill that happens to print a total in words into a confirmable one;
      * figure == words (to the rupee) -> unchanged;
      * figure is exactly one extra leading digit + the words' value
        ("7500" vs "500": the symbol was read as a digit) -> the words win,
        MEDIUM, with the discrepancy recorded;
      * any other disagreement -> keep the printed figure but drop to LOW so
        the bill needs review; never silently pick one of two conflicting
        readings."""
    words = parse_amount_in_words(text)
    if words is None or amount is None:
        return amount, confidence, warnings
    if abs(amount - words) < 1:
        return amount, confidence, warnings
    printed_digits, word_digits = f"{int(round(amount))}", f"{int(words)}"
    if float(amount).is_integer() and len(printed_digits) == len(word_digits) + 1 and printed_digits[1:] == word_digits and printed_digits[0] != "0":
        return (
            words,
            "MEDIUM",
            [
                *warnings,
                f"Printed amount read as Rs.{amount:,.0f} but the bill's amount in words is Rs.{words:,.0f}; "
                "the currency symbol was most likely read as a leading digit, so the amount in words was used.",
            ],
        )
    return (
        amount,
        "LOW",
        [
            *warnings,
            f"Printed amount Rs.{amount:,.2f} disagrees with the amount in words (Rs.{words:,.2f}); needs review.",
        ],
    )


def _find_printed_amount(text: str) -> tuple[float | None, str, list[str]]:
    warnings: list[str] = []
    labeled: dict[int, set[float]] = {}
    loose_used = False

    for priority, label, pattern in _AMOUNT_LABEL_PATTERNS:
        for m in pattern.finditer(text):
            idx = m.start()
            window = _label_value_window(text, idx, len(m.group(0)))
            tokens = _AMOUNT_TOKEN_RE.findall(window)
            if tokens:
                for tok in tokens:
                    amt = parse_amount(tok)
                    if amt is not None:
                        labeled.setdefault(priority, set()).add(amt)
            else:
                # Strict pattern found nothing in this window - try a looser read
                # before giving up, since OCR often keeps the digits but loses the
                # currency symbol/decimal point around them. Only the first loose
                # number right after the label is used, not every number in sight.
                loose_tokens = _LOOSE_NUMBER_RE.findall(window)
                if loose_tokens:
                    amt = parse_amount(loose_tokens[0])
                    if amt is not None and amt >= 10:
                        labeled.setdefault(priority, set()).add(amt)
                        loose_used = True

    if labeled:
        best_priority = max(labeled)
        candidates = labeled[best_priority]
        if len(candidates) == 1:
            confidence = "MEDIUM" if loose_used else "HIGH"
            if loose_used:
                warnings.append("Amount read via a loose fallback pattern - OCR likely obscured the currency/decimal marker.")
            return next(iter(candidates)), confidence, warnings
        warnings.append(f"Multiple differing amounts found at the same label priority: {sorted(candidates)}.")
        return None, "LOW", warnings

    # No EXACT amount label found anywhere - last resort before giving up on
    # a labelled read entirely: a known label whose OWN letters were
    # OCR-corrupted by a single dropped/substituted character (confirmed
    # real case: "Amount" -> "mount", the leading "A" misattributed to the
    # next word on a thermal-print receipt). Deliberately narrow: only a
    # short, high-value label set, only a single-character edit distance,
    # and only accepted when a STRICT (not loose) amount token - a real
    # currency/decimal-shaped value, not a bare loose number - sits in the
    # normal label/value window right after it. This never loosens what
    # counts as a valid VALUE, only how corrupted the LABEL text itself is
    # allowed to be.
    fuzzy_amt = _find_fuzzy_labelled_amount(text)
    if fuzzy_amt is not None:
        warnings.append("Amount label itself was OCR-corrupted (single character) but recovered via fuzzy label matching.")
        return fuzzy_amt, "MEDIUM", warnings

    # No labeled amount anywhere - only trust an unlabeled figure if it's at least
    # plausibly a bill total; small bare decimals are usually something else
    # entirely (e.g. a "4.98" star rating on a ride-hailing trip-details page).
    # Duplicates are kept (not deduped into a set) here specifically so the
    # arithmetic check below can see e.g. two identical CGST/SGST lines.
    all_tokens = _AMOUNT_TOKEN_RE.findall(text)
    amounts = [a for a in (parse_amount(t) for t in all_tokens) if a is not None and a >= 10]
    distinct = sorted(set(amounts))
    if len(distinct) == 1:
        return distinct[0], "MEDIUM", warnings
    if len(distinct) > 1:
        # A bill total is structurally never smaller than any line item or tax
        # component printed on the same page (subtotal + CGST/SGST = total,
        # base fare + reservation charge = total, etc.), so the largest figure
        # found is a plausible - if unlabeled - total.
        best = max(distinct)
        others = [a for a in amounts if a != best]
        # Stronger than "just the largest": if the other figures on the page
        # (a subtotal/ride-fare plus its CGST/SGST lines, etc.) sum exactly to
        # the largest one, that's direct arithmetic proof it's the real total,
        # not just a guess - e.g. Ride fare 520 + CGST 13 + SGST 13 = 546.
        if others and abs(sum(others) - best) < 0.02:
            warnings.append(
                f"Amount confirmed via arithmetic: components {sorted(others)} sum to {best}, "
                "which is used as the bill total."
            )
            return best, "MEDIUM", warnings
        warnings.append(
            f"Multiple unlabeled amounts found; using the largest ({best}) as a low-confidence "
            f"candidate total. Other amounts on the page: {sorted(set(others))}."
        )
        return best, "LOW", warnings
    return None, "LOW", warnings


def _find_date_reading(text: str, readings: "list[DateReading] | None" = None) -> "tuple[DateReading | None, str, list[str]]":
    """The bill's own date as a DateReading (raw string, candidates, method,
    confidence). `value` is None when the printed day/month order cannot be
    established from evidence (see dates.read_dates) - the candidates stay on
    the reading so they can be shown for review; a guess is never substituted."""
    readings = read_dates(text) if readings is None else readings
    lines = text.split("\n")
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line) + 1

    def in_line(i: int) -> "list[DateReading]":
        end = starts[i] + len(lines[i])
        return [r for r in readings if starts[i] <= r.start <= end]

    best = None  # (priority, reading) - highest priority wins
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(excluded in lower for excluded in DATE_EXCLUDE_LABELS):
            continue
        priority = max((p for p, label in DATE_LABELS if label in lower), default=None)
        if priority is None:
            continue
        found = [r for r in in_line(i) if r.method != "invalid"]
        if not found and i + 1 < len(lines):
            found = [r for r in in_line(i + 1) if r.method != "invalid"]
        if found and (best is None or priority > best[0]):
            best = (priority, found[0])
    if best is not None:
        reading = best[1]
        if reading.value is None:
            return reading, "LOW", [_ambiguous_date_warning(reading)]
        return reading, ("HIGH" if reading.confidence == "HIGH" else "MEDIUM"), _ocr_date_warnings(reading)

    usable = [r for r in readings if r.method != "invalid"]
    resolved = {r.value for r in usable if r.value is not None}
    unresolved = [r for r in usable if r.value is None]
    if len(resolved) == 1 and not unresolved:
        reading = next(r for r in usable if r.value is not None)
        return reading, "MEDIUM", _ocr_date_warnings(reading)
    if not resolved and unresolved and len({r.candidates for r in unresolved}) == 1:
        return unresolved[0], "LOW", [_ambiguous_date_warning(unresolved[0])]
    # Several different dates and no "Date" label (a fuel slip prints a transaction timestamp plus, say, a
    # calibration or print date): when exactly ONE distinct date sits beside a clock time it is the transaction
    # date. Two different timestamped dates (a hotel's arrival/departure) or none still go to review, and an
    # unresolved day/month order is never settled this way.
    if len(resolved) > 1 and not unresolved:
        stamped = [r for r in usable if r.value is not None and has_adjacent_clock(text, r)]
        if stamped and len({r.value for r in stamped}) == 1:
            return stamped[0], "MEDIUM", []
    if resolved or unresolved:
        shown = sorted(resolved) + [f"{r.raw} ({' or '.join(c.isoformat() for c in r.candidates)})" for r in unresolved]
        return None, "LOW", [f"Multiple differing dates found with no clear bill-date label: {shown}."]
    return None, "LOW", []


_HERO_AMOUNT_LINE = re.compile(r"^[^\w\s]{0,3}\s*(\d{1,7}(?:\.\d{1,2})?|\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?)\s*$")
_STATUS_BANNER = re.compile(r"^(?:payment\s+|transaction\s+)?(?:completed|successful(?:ly)?|success|paid(?:\s+successfully)?|pending|failed|processing)\b", re.IGNORECASE)


def _find_payment_hero_amount(text: str) -> float | None:
    """Payment apps (e.g. Google Pay) show the amount ALONE on a line directly above (or
    below) the status banner, with no "Amount"/"Total" label, and OCR often drops the
    rupee glyph. Accepted only for a transaction screenshot, only when that line
    is immediately adjacent to a status banner, and only when exactly one such figure
    exists - never from an arbitrary number on the page."""
    lines = [ln.strip() for ln in text.splitlines()]
    found: set[float] = set()
    for i, line in enumerate(lines):
        m = _HERO_AMOUNT_LINE.match(line)
        if not m:
            continue
        before = next((ln for ln in reversed(lines[:i]) if ln), "")
        after = next((ln for ln in lines[i + 1 :] if ln), "")
        if _STATUS_BANNER.match(after) or _STATUS_BANNER.match(before):
            value = parse_amount(m.group(1))
            if value is not None and value > 0:
                found.add(value)
    return found.pop() if len(found) == 1 else None


def _ocr_date_warnings(reading: "DateReading") -> list[str]:
    """An OCR-repaired date (dates.py, method month_name_ocr) is recorded with exactly what was printed, so an
    auditor can compare it with the document."""
    if reading.method != "month_name_ocr":
        return []
    return [
        f"Date printed as '{reading.raw}' was read as {reading.value.isoformat()} after correcting a likely OCR error; "
        "verify it against the document."
    ]


def _ambiguous_date_warning(reading: "DateReading") -> str:
    options = " or ".join(c.isoformat() for c in reading.candidates)
    return (
        f"Date '{reading.raw}' is ambiguous ({options}) and nothing in this report establishes its day/month order; "
        "left unresolved for review."
    )


def _find_date(text: str):
    reading, confidence, warnings = _find_date_reading(text)
    return (reading.value if reading else None), confidence, warnings

def _find_bill_number(text: str) -> str | None:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        lower = line.lower()
        for label in BILL_NUMBER_LABELS:
            if label in lower:
                after = line.split(":", 1)
                candidate = after[1].strip() if len(after) > 1 else ""
                if not candidate and i + 1 < len(lines):
                    candidate = lines[i + 1].strip()
                candidate = re.sub(r"[^A-Za-z0-9\-/]", "", candidate)
                if 3 <= len(candidate) <= 30:
                    return candidate
    return None


# Mirrors bill_grouping._CONTINUATION_MARKER_RE (duplicated rather than
# imported - bill_grouping.py imports bill_parser.py, not the reverse).
# Confirmed real defect this guards against: a genuine multi-page bill's
# continuation page, with no vendor of its own, fell through to the weak
# fallback below and had a line like "Continued from page 1" picked up as if
# it were a vendor name.
_CONTINUATION_MARKER_RE = re.compile(
    r"\b(CONTINUED|CONTD\.?|CONTINUATION)\b|\bPAGE\s+\d+\s+OF\s+\d+\b", re.IGNORECASE
)

# A UPI/payment-app "transaction successful" screenshot (confirmed real on
# several real pages: Google Pay/PhonePe money-transfer confirmations used as
# supporting evidence for cash-paid cab/local expenses) has no company name
# at all - just a bare field label, an amount-in-words line, or UI chrome
# describing the screen itself ("Paid Successfully", "Share", "Edit"). Left
# unexcluded, the weak vendor fallback below picked up whichever of these
# happened to be the first eligible-looking line - confirmed real defect:
# vendor extracted as the literal word "Amount" on multiple real bills.
# Narrow and line-content-based (never a payee-name denylist) so a genuine,
# unusual vendor name is never excluded by familiarity alone.
_GENERIC_NONVENDOR_LINE_RE = [
    re.compile(r"^(name|date|amount|total|merchant|vendor|to|from|paid to|sent to)\s*[:\-]?\s*$", re.IGNORECASE),
    re.compile(r"^rupees\b", re.IGNORECASE),  # amount-in-words, e.g. "Rupees One Thousand Five Hundred"
    re.compile(r"\bonly\s*$", re.IGNORECASE),  # amount-in-words tail, e.g. "Twenty Four Only" / bare "Only"
    re.compile(r"^[<>~]?\s*[\d,]{2,}\s*[&@#%]?\s*$"),  # a garbled OCR read of the amount figure itself, e.g. "1,224 @"
    re.compile(
        r"\b(money transfer|paid successfully|sent successfully|share help|payment details|transfer details|split this payment)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^payment for\b", re.IGNORECASE),
    re.compile(r"^\w+\s+edit$", re.IGNORECASE),  # a short UI category tag, e.g. "Travel Edit"
]


def _is_generic_nonvendor_line(s: str) -> bool:
    return any(pattern.search(s) for pattern in _GENERIC_NONVENDOR_LINE_RE)


def has_confident_vendor_line(text: str) -> bool:
    """Public (used by bill_grouping.py to decide whether a page carries
    strong enough evidence of being a NEW bill's own start): the same
    strict, line-end-anchored company-suffix match _find_vendor's first
    branch uses, exposed on its own so callers don't need a weaker, re-
    implemented check that would risk false-triggering on ordinary prose
    merely containing a suffix word (e.g. "Advisory services continued")."""
    for line in text.split("\n"):
        s = line.strip()
        if not (3 <= len(s) <= 80):
            continue
        last_words = " ".join(s.split()[-4:])
        if VENDOR_SUFFIX_RE.search(last_words):
            return True
    return False


def _find_vendor(text: str) -> tuple[str | None, bool]:
    for line in text.split("\n"):
        s = line.strip()
        if not (3 <= len(s) <= 80):
            continue
        # Require the suffix near the end of the line (typical for a company name
        # like "... PVT LTD" or "... HARDWARES") rather than matching anywhere,
        # which previously picked up disclaimer prose containing "services" etc.
        last_words = " ".join(s.split()[-4:])
        if VENDOR_SUFFIX_RE.search(last_words):
            return s, True
    for line in text.split("\n"):
        s = line.strip()
        if (
            3 <= len(s) <= 60
            and re.search(r"[A-Za-z]{3,}", s)
            and not _AMOUNT_TOKEN_RE.fullmatch(s)
            and not _CONTINUATION_MARKER_RE.search(s)
            and not _is_generic_nonvendor_line(s)
        ):
            return s, False
    return None, False


# An intercity bus ticket is recognised from SEVERAL independent indicators, never from the bare word
# "bus" (which CATEGORY_KEYWORDS below still reads only as a last-resort signal). The ticket's own
# boarding/dropping address often names a landmark ("Near HP Petrol Pump", "Opp. Indian Oil"), and its
# "Boarding time" line and baggage note match the Flight and Fuel keywords, which sit earlier in
# CATEGORY_KEYWORDS - a first-match-wins read of such a ticket called it Fuel (confirmed real defect:
# a genuine Ahmedabad-Bhopal AC sleeper ticket was reported as "Fuel evidence" for a TRAIN / BUS FARE claim).
# Every label below uses HORIZONTAL whitespace only ([ \t]): a label never runs across a line break, so "Bus" at
# the end of one line and "Service Charges" on the next is not the label "Bus Service".
_BUS_OPERATOR_RE = re.compile(
    r"\bBUS[ \t]*(?:OPERAT[O0]R|SERVICE|OPERATED[ \t]+BY|COMPANY|NO\.?|NUMBER)\b|\bCOACH[ \t]*(?:TYPE|NO\.?|NUMBER)\b|"
    r"\b(?:RED[ \t]*BUS|ABHI[ \t]*BUS|GSRTC|MSRTC|KSRTC|APSRTC|TSRTC|RSRTC|UPSRTC|HRTC|TNSTC)\b",
    re.IGNORECASE,
)
_BUS_TYPE_RE = re.compile(
    r"\b(?:NON[ \t-]*)?(?:A[ \t]*/[ \t]*C|AC)[ \t-]*(?:SEMI[ \t-]*)?(?:SLEEPER|SEATER)\b|\bSEMI[ \t-]*SLEEPER\b|\bMULTI[ \t-]*AXLE\b|"
    r"\bSLEEPER[ \t]*\([ \t]*\d[ \t]*\+[ \t]*\d[ \t]*\)",
    re.IGNORECASE,
)
_BUS_POINTS_RE = re.compile(
    # POIN[TLI1]: OCR routinely reads the final "t" of "Point" as l / I / 1 (kept to this one label on purpose)
    r"\b(?:BOARDING|PICK[ \t-]*UP)[ \t]*POIN[TLI1]\b|\b(?:DROPPING|DROP(?:[ \t-]*OFF)?)[ \t]*POIN[TLI1]\b", re.IGNORECASE
)
_BUS_SEAT_RE = re.compile(r"\bSEATS?[ \t]*(?:NO\.?|NOS\.?|NUMBERS?)?[ \t]*[:\-]?[ \t]*[A-Z]{0,2}\d{1,2}[A-Z]?\b", re.IGNORECASE)
_BUS_TICKET_ID_RE = re.compile(
    r"\b(?:TICKET|BOOKING|RESERVATION)[ \t]*(?:NO\.?|NUMBER|ID|REF(?:ERENCE)?)\b|\bPNR\b", re.IGNORECASE
)
_BUS_PASSENGER_RE = re.compile(r"\bPASSENGERS?\b", re.IGNORECASE)
# Fuel-DISPENSER transaction evidence (a pump receipt's own fields). When present it is never overruled
# by ticket-like wording, so a genuine fuel receipt stays Fuel.
_FUEL_TRANSACTION_RE = re.compile(
    r"\b(?:NOZZLE|NOZL|DENSITY)\b|\bRATE\s*(?:/|PER)\s*(?:LTR|LITRE|L)\b|\bVOLUME\s*\(?\s*(?:LTRS?|LITRES?|L)\b", re.IGNORECASE
)


# Train / flight documents that can share seat, ticket-id and passenger wording with a bus ticket.
# _RAIL_AIR_IDENTITY_RE is the explicit identity a train or flight document prints about ITSELF ("IRCTC",
# "Train No 12345", "Flight No / Flight: IX 1234"); a page carrying it is never re-labelled Bus, in either tier.
# _NON_BUS_TRANSPORT_RE is the broader wording (any "flight"/"airline"), consulted only by the sparse tier,
# because a full bus ticket may legitimately name a "Railway Station" or an airline office as a landmark.
_RAIL_AIR_IDENTITY_RE = re.compile(r"\bIRCTC\b|\bTRAIN[ \t]*(?:NO|NAME|NUMBER)\b|\bFLIGHT[ \t]*(?:NO|NUMBER)\b|\bFLIGHT[ \t]*:", re.IGNORECASE)
_NON_BUS_TRANSPORT_RE = re.compile(r"\bIRCTC\b|\bTRAIN[ \t]*(?:NO|NAME|NUMBER)\b|\bFLIGHT\b|\bAIRLINES?\b|\bAIRWAYS\b", re.IGNORECASE)


def _bus_ticket_evidence(text: str) -> str | None:
    """Evidence snippet when `text` is an intercity bus ticket, else None.

    Strong tier: at least TWO bus-specific indicators (a bus operator/service label, a bus-type descriptor such
    as "AC Sleeper", boarding/dropping point labels) AND at least THREE distinct indicator groups overall (the
    other groups being a seat number, a ticket/booking id, and passenger details).

    Sparse tier (an OCR-poor or abbreviated ticket): a bus operator/service label OR a bus-type descriptor,
    plus at least one more distinct group, on a page that is not a train or flight document. A flight, train
    or cab document can share seat/ticket/passenger/pick-up wording but has no bus operator or bus type, a
    lone "AC Sleeper" or "Bus Operator" is a single indicator, and a page that merely contains the word "bus"
    has none of these - none of those qualifies."""
    operator = _BUS_OPERATOR_RE.search(text)
    bus_type = _BUS_TYPE_RE.search(text)
    points = _BUS_POINTS_RE.search(text)
    bus_specific = sum(m is not None for m in (operator, bus_type, points))
    others = sum(rx.search(text) is not None for rx in (_BUS_SEAT_RE, _BUS_TICKET_ID_RE, _BUS_PASSENGER_RE))
    strong = bus_specific >= 2 and bus_specific + others >= 3
    sparse = (operator or bus_type) is not None and bus_specific + others >= 2 and not _NON_BUS_TRANSPORT_RE.search(text)
    if not (strong or sparse):
        return None
    if _FUEL_TRANSACTION_RE.search(text) or _RAIL_AIR_IDENTITY_RE.search(text):
        return None
    return (operator or bus_type or points).group(0)


# The bare word "bus" is only a WEAK signal. Left unguarded it classified an unrelated "Service Charges"
# invoice that mentions a bus route, and a bus-stand parking receipt, as Bus evidence - which then raised a
# false CATEGORY MISMATCH against the claimed category. It now counts only when the same page also carries
# travel-ticket wording, and never as part of "bus bar" / "bus duct" (electrical terms).
_BUS_WORD_TRAVEL_CONTEXT_RE = re.compile(
    r"\b(?:TICKETS?|FARE|PASSENGERS?|SEATS?|BOARDING|TRAVELS|TOURS|ROUTE|JOURNEY|COACH|DEPARTURE|DROPPING|OPERATOR|PNR)\b",
    re.IGNORECASE,
)


def _find_category(text: str) -> tuple[str | None, str | None]:
    bus_snippet = _bus_ticket_evidence(text)
    if bus_snippet is not None:
        return "Bus", bus_snippet
    for pattern, label in CATEGORY_KEYWORDS:
        m = pattern.search(text)
        if m:
            if label == "Bus" and not _BUS_WORD_TRAVEL_CONTEXT_RE.search(text):
                continue  # the bare word "bus" alone is not evidence of a bus ticket
            return label, m.group(0)
    return _find_category_fuzzy(text)


def parse_bill_text(text: str) -> ParsedBill:
    bill = ParsedBill(raw_text=text[:BILL_RAW_TEXT_CAP])
    # Full text, before the cap: the charge may be printed after character 2,000.
    bill.catering_amount = detect_catering_amount(text)
    bill.catering_mentioned = mentions_catering(text)
    room_food = analyze_room_service_food(text)  # one row per line, reconciled; amount None when unresolved
    bill.room_service_food_amount = room_food.amount
    bill.room_service_food_mentioned = room_food.mentioned
    if room_food.note:  # several charges, or an unresolved result - an audit note only (never affects status)
        bill.warnings.append(room_food.note)

    amount, amount_confidence, amount_warnings = _find_amount(text)
    bill.amount = amount
    bill.warnings.extend(amount_warnings)

    date_reading, date_confidence, date_warnings = _find_date_reading(text)
    bill.date_reading = date_reading
    bill.bill_date = date_reading.value if date_reading else None
    bill.warnings.extend(date_warnings)

    payment = payment_evidence.extract(text)
    if bill.amount is None and payment.is_payment:
        hero = _find_payment_hero_amount(text)
        if hero is not None:
            bill.amount = hero
            amount_confidence = "MEDIUM"
            bill.warnings.append(
                "Amount read from the standalone figure next to the payment status (no amount label or currency symbol was readable)."
            )
    bill.is_payment = payment.is_payment
    bill.payment_status = payment.status
    bill.recipient = payment.recipient
    bill.transaction_time = payment.time
    bill.transaction_reference = payment.reference
    if payment.status in ("PENDING", "FAILED"):
        bill.warnings.append(
            f"Payment screenshot shows the transaction as {payment.status}; it is not proof of a completed payment."
        )

    gstin_match = GSTIN_RE.search(text.upper())
    bill.gstin = gstin_match.group(0) if gstin_match else None

    bill.bill_number = _find_bill_number(text)

    vendor, vendor_strong = _find_vendor(text)
    if payment.recipient:
        # On a transaction screenshot the payee is the merchant evidence; an explicit "Paid to X" outranks the
        # generic first-line heuristic (which would pick a status banner or an amount line).
        vendor, vendor_strong = payment.recipient, True
    bill.vendor = vendor

    category_guess, snippet = _find_category(text)
    bill.category_guess = category_guess
    bill.description = snippet

    # Optional last-resort supplementary pass (Part 8/33) - only attempted
    # when local extraction left the two signals bill_matcher.py weighs most
    # heavily (amount, date) genuinely missing, and only when a structured-
    # extraction fallback provider is configured; a no-op otherwise (see
    # nvidia_fallback.py's module docstring for the full pipeline
    # position/rules - it only fills gaps, never overrides a value already
    # extracted deterministically, and every field it returns still passes
    # through the same numeric/date parsers as everything else).
    #
    # At most ONE provider is tried per bill, never both - this project's
    # intended split is NVIDIA for OCR (image-to-text, see nvidia_ocr.py)
    # and DeepSeek for this text-interpretation step, so DeepSeek is
    # preferred here when configured; nvidia_fallback.py remains available
    # as a fallback-of-the-fallback for a deployment that only has an NVIDIA
    # key configured (existing behaviour, unchanged for that case).
    if bill.amount is None or bill.bill_date is None:
        fallback = None
        provider_label = None
        if deepseek_fallback.is_available():
            fallback = deepseek_fallback.extract_structured_fields(text)
            provider_label = "DeepSeek"
        elif nvidia_fallback.is_available():
            fallback = nvidia_fallback.extract_structured_fields(text)
            provider_label = "NVIDIA"

        if fallback is not None:
            if bill.amount is None and fallback.amount is not None:
                bill.amount = fallback.amount.value
                amount_confidence = fallback.amount.confidence
                bill.warnings.append(f"Amount supplied by the optional {provider_label} extraction fallback ({fallback.amount.confidence} confidence).")
            if bill.bill_date is None and fallback.date is not None:
                bill.bill_date = fallback.date.value
                date_confidence = fallback.date.confidence
                bill.warnings.append(f"Date supplied by the optional {provider_label} extraction fallback ({fallback.date.confidence} confidence).")
            if bill.vendor is None and fallback.vendor is not None:
                bill.vendor = fallback.vendor.value
                bill.warnings.append(f"Vendor supplied by the optional {provider_label} extraction fallback.")
            if bill.category_guess is None and fallback.category_evidence is not None:
                bill.description = fallback.category_evidence.value

    if bill.amount is None:
        bill.warnings.append("Bill amount could not be reliably determined.")
    if bill.bill_date is None and not (bill.date_reading and bill.date_reading.ambiguous):
        bill.warnings.append("Bill date could not be reliably determined.")
    if bill.vendor is None:
        bill.warnings.append("Vendor could not be reliably determined.")

    strong_signals = sum(
        [
            bill.amount is not None and amount_confidence == "HIGH",
            bill.bill_date is not None and date_confidence == "HIGH",
            bool(bill.gstin),
            vendor_strong,
        ]
    )
    if bill.amount is not None and bill.bill_date is not None and strong_signals >= 2:
        bill.confidence = "HIGH"
        bill.status = "EXTRACTED"
    elif bill.amount is not None:
        bill.confidence = "MEDIUM" if amount_confidence != "LOW" else "LOW"
        bill.status = "EXTRACTED" if amount_confidence != "LOW" else "REVIEW_REQUIRED"
    else:
        bill.confidence = "LOW"
        bill.status = "REVIEW_REQUIRED"

    return bill


def split_bill_segments(text: str) -> list[str]:
    """Best-effort separation of multiple receipts sharing one page. Only
    splits when there is strong, unambiguous evidence (2+ distinct GSTINs) -
    otherwise returns the page as a single segment rather than inventing a
    boundary."""
    gstin_matches = list(GSTIN_RE.finditer(text.upper()))
    distinct_gstins = {m.group(0) for m in gstin_matches}
    if len(distinct_gstins) < 2:
        return [text]

    positions = sorted(m.start() for m in gstin_matches)
    boundaries = [0]
    for i in range(1, len(positions)):
        midpoint = (positions[i - 1] + positions[i]) // 2
        boundaries.append(midpoint)
    boundaries.append(len(text))

    segments = []
    for i in range(len(boundaries) - 1):
        segment = text[boundaries[i] : boundaries[i + 1]].strip()
        if segment:
            segments.append(segment)
    return segments or [text]
