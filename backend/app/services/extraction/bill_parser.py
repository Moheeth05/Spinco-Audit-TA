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

from app.services.extraction.dates import find_all_dates
from app.services.extraction.money import parse_amount

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b")

# (priority, keyword) - higher priority wins when multiple labeled amounts exist.
AMOUNT_LABELS = [
    (5, "grand total"),
    (5, "total amount"),
    (4, "net amount"),
    (4, "amount payable"),
    (4, "total payable"),
    (3, "total reimbursable"),
    (3, "amount"),
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

DATE_LABELS = ["bill date", "invoice date", "date", "dt", "txst", "txn date"]

VENDOR_SUFFIX_RE = re.compile(
    r"\b(LTD|LIMITED|PVT|CORP|CORPORATION|HOTEL|RESTAURANT|TRAVELS|TOUR|STORES|AGENC\w*|"
    r"PETROLEUM|EXPRESS|DEALER|CAFE|ENTERPRISE\w*|HARDWARE\w*|ELECTRICAL\w*|SERVICES?|"
    r"BAR|AUTOFUELS?|MESS)\b",
    re.IGNORECASE,
)

CATEGORY_KEYWORDS = [
    (re.compile(r"\b(PETROL|DIESEL|FUEL|AUTOFUEL)\b", re.IGNORECASE), "Fuel"),
    (re.compile(r"\b(HOTEL|LODGING|ROOM CHARGE|ACCOMMODATION)\b", re.IGNORECASE), "Lodging"),
    (re.compile(r"\b(UBER|CAB|TAXI|uber)\b", re.IGNORECASE), "Cab/Taxi"),
    (re.compile(r"\b(FOOD|RESTAURANT|MEAL|DINE|MESS|CAFE|BAR)\b", re.IGNORECASE), "Food"),
    (re.compile(r"\b(COURIER|DTDC|AWB)\b", re.IGNORECASE), "Courier"),
    (re.compile(r"\b(PRINTING|STATIONERY|STATIONARY)\b", re.IGNORECASE), "Printing & Stationery"),
    (re.compile(r"\b(TRAIN|RAILWAY)\b", re.IGNORECASE), "Train"),
    (re.compile(r"\bBUS\b", re.IGNORECASE), "Bus"),
    (re.compile(r"\b(ELECTRICAL|HARDWARE)\b", re.IGNORECASE), "Hardware/Electrical"),
]

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
    raw_text: str = ""
    confidence: str = "LOW"  # HIGH | MEDIUM | LOW
    status: str = "REVIEW_REQUIRED"  # EXTRACTED | REVIEW_REQUIRED
    warnings: list[str] = field(default_factory=list)


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


def _label_value_window(text: str, idx: int, label_len: int) -> str:
    """The text to search for this label occurrence's value: the rest of the
    label's own line first; if that line has nothing after the label, the
    single line that follows (covers "Total\\n₹2,329.65"-style receipts).
    Falls back to a short flat character window when the OCR pass produced
    no line breaks at all (some badly-skewed thermal-print scans collapse
    into one line), so a next-line lookup can't accidentally run past
    several unrelated following lines."""
    line_end = text.find("\n", idx)
    if line_end == -1:
        return text[idx : idx + label_len + _FLAT_WINDOW_CHARS]

    own_line_rest = text[idx + label_len : line_end]
    if _AMOUNT_TOKEN_RE.search(own_line_rest) or _LOOSE_NUMBER_RE.search(own_line_rest):
        return text[idx:line_end]

    next_line_end = text.find("\n", line_end + 1)
    next_line = text[line_end + 1 : next_line_end] if next_line_end != -1 else text[line_end + 1 :]
    return next_line


def _find_amount(text: str) -> tuple[float | None, str, list[str]]:
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

    # No labeled amount anywhere - only trust an unlabeled figure if it's at least
    # plausibly a bill total; small bare decimals are usually something else
    # entirely (e.g. a "4.98" star rating on a ride-hailing trip-details page).
    all_tokens = _AMOUNT_TOKEN_RE.findall(text)
    distinct = {a for a in (parse_amount(t) for t in all_tokens) if a is not None and a >= 10}
    if len(distinct) == 1:
        return next(iter(distinct)), "MEDIUM", warnings
    if len(distinct) > 1:
        warnings.append(f"Multiple unlabeled amounts found, none reliably the total: {sorted(distinct)}.")
        return None, "LOW", warnings
    return None, "LOW", warnings


def _find_date(text: str):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(label in lower for label in DATE_LABELS):
            dates = find_all_dates(line)
            if not dates and i + 1 < len(lines):
                dates = find_all_dates(lines[i + 1])
            if dates:
                return dates[0], "HIGH", []

    all_dates = find_all_dates(text)
    distinct = sorted(set(all_dates))
    if len(distinct) == 1:
        return distinct[0], "MEDIUM", []
    if len(distinct) > 1:
        return None, "LOW", [f"Multiple differing dates found with no clear bill-date label: {distinct}."]
    return None, "LOW", []


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
        if 3 <= len(s) <= 60 and re.search(r"[A-Za-z]{3,}", s) and not _AMOUNT_TOKEN_RE.fullmatch(s):
            return s, False
    return None, False


def _find_category(text: str) -> tuple[str | None, str | None]:
    for pattern, label in CATEGORY_KEYWORDS:
        m = pattern.search(text)
        if m:
            return label, m.group(0)
    return None, None


def parse_bill_text(text: str) -> ParsedBill:
    bill = ParsedBill(raw_text=text[:2000])

    amount, amount_confidence, amount_warnings = _find_amount(text)
    bill.amount = amount
    bill.warnings.extend(amount_warnings)

    bill_date, date_confidence, date_warnings = _find_date(text)
    bill.bill_date = bill_date
    bill.warnings.extend(date_warnings)

    gstin_match = GSTIN_RE.search(text.upper())
    bill.gstin = gstin_match.group(0) if gstin_match else None

    bill.bill_number = _find_bill_number(text)

    vendor, vendor_strong = _find_vendor(text)
    bill.vendor = vendor

    category_guess, snippet = _find_category(text)
    bill.category_guess = category_guess
    bill.description = snippet

    if bill.amount is None:
        bill.warnings.append("Bill amount could not be reliably determined.")
    if bill.bill_date is None:
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
