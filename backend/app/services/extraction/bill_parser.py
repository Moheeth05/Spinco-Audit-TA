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

from app.services.extraction import nvidia_fallback
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
    # NOZZLE/NOZL: confirmed on every real fuel-pump receipt in the
    # reference corpus (5/5) as a standalone fuel-dispenser line-item term
    # ("Nozzle No. :01") - a reliable second signal for pumps whose OCR text
    # garbles PETROL/DIESEL/FUEL itself (e.g. "Fue]", "Diese l" with an
    # inserted space) but still renders "Nozl"/"Nozzle" legibly.
    (re.compile(r"\b(PETROL|DIESEL|FUEL|AUTOFUEL|NOZZLE|NOZL)\b", re.IGNORECASE), "Fuel"),
    (re.compile(r"\b(HOTEL|LODGING|ROOM CHARGE|ACCOMMODATION)\b", re.IGNORECASE), "Lodging"),
    # A cab/ride-hailing bill rarely prints the literal word "cab"/"taxi" - it
    # prints what the service actually is (a ride fare, a passenger transport
    # service). These are the terms real ride-receipts use (see the
    # ER-41115 CAB CHARGES bill: "Ride fare", "Passengers Transport
    # Services") - never require the Policy Master category's own wording to
    # appear verbatim on the bill.
    (re.compile(r"\b(UBER|OLA|CAB|TAXI|RIDE FARE|RIDE|PASSENGERS?\s+TRANSPORT(?:\s+SERVICES?)?)\b", re.IGNORECASE), "Cab/Taxi"),
    (re.compile(r"\b(FOOD|RESTAURANT|MEAL|DINE|MESS|CAFE|BAR)\b", re.IGNORECASE), "Food"),
    (re.compile(r"\b(COURIER|DTDC|AWB)\b", re.IGNORECASE), "Courier"),
    (re.compile(r"\b(PRINTING|STATIONERY|STATIONARY)\b", re.IGNORECASE), "Printing & Stationery"),
    (re.compile(r"\b(TRAIN|RAILWAY|IRCTC|PNR)\b", re.IGNORECASE), "Train"),
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


def _find_date(text: str):
    lines = text.split("\n")
    best = None  # (priority, date) - highest priority wins
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(excluded in lower for excluded in DATE_EXCLUDE_LABELS):
            continue
        priority = max((p for p, label in DATE_LABELS if label in lower), default=None)
        if priority is None:
            continue
        dates = find_all_dates(line)
        if not dates and i + 1 < len(lines):
            dates = find_all_dates(lines[i + 1])
        if dates and (best is None or priority > best[0]):
            best = (priority, dates[0])
    if best is not None:
        return best[1], "HIGH", []

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

    # Optional last-resort supplementary pass (Part 8/33) - only attempted
    # when local extraction left the two signals bill_matcher.py weighs most
    # heavily (amount, date) genuinely missing, and only when
    # SPINCO_NVIDIA_API_KEY is configured; a no-op otherwise (see
    # nvidia_fallback.py's module docstring for the full pipeline
    # position/rules - it only fills gaps, never overrides a value already
    # extracted deterministically, and every field it returns still passes
    # through the same numeric/date parsers as everything else).
    if (bill.amount is None or bill.bill_date is None) and nvidia_fallback.is_available():
        fallback = nvidia_fallback.extract_structured_fields(text)
        if fallback is not None:
            if bill.amount is None and fallback.amount is not None:
                bill.amount = fallback.amount.value
                amount_confidence = fallback.amount.confidence
                bill.warnings.append(f"Amount supplied by the optional NVIDIA extraction fallback ({fallback.amount.confidence} confidence).")
            if bill.bill_date is None and fallback.date is not None:
                bill.bill_date = fallback.date.value
                date_confidence = fallback.date.confidence
                bill.warnings.append(f"Date supplied by the optional NVIDIA extraction fallback ({fallback.date.confidence} confidence).")
            if bill.vendor is None and fallback.vendor is not None:
                bill.vendor = fallback.vendor.value
                bill.warnings.append("Vendor supplied by the optional NVIDIA extraction fallback.")
            if bill.category_guess is None and fallback.category_evidence is not None:
                bill.description = fallback.category_evidence.value

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
