"""Safe amount parsing for ₹ / Rs / INR formatted values.

Never guesses: if the text doesn't contain a recognizable number, parse_amount
returns None rather than a fabricated figure.
"""

import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

_CURRENCY_PREFIX = re.compile(r"^\s*(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"-?\(?\d[\d,]*(?:\.\d+)?\)?")


def parse_amount(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = _CURRENCY_PREFIX.sub("", text.strip())
    match = _NUMBER.search(cleaned)
    if not match:
        return None
    token = match.group(0)
    is_negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "")
    try:
        value = float(token)
    except ValueError:
        return None
    return -value if is_negative else value


_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALE_WORDS = {"thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000, "crore": 10_000_000, "crores": 10_000_000}
# "Rupees Five Hundred Only" (amount-in-words printed under a figure, typical of
# UPI/bank transfer screenshots) - either "Rupees <words> Only" or "<words> Rupees Only".
_WORDS_AFTER_RUPEES = re.compile(r"\brupees?\b[\s:\-]*((?:[A-Za-z][A-Za-z\-]*\s+){1,14}?)only\b", re.IGNORECASE)
_WORDS_BEFORE_RUPEES = re.compile(r"\b((?:[A-Za-z][A-Za-z\-]*\s+){1,14}?)rupees?\s+only\b", re.IGNORECASE)


def _words_to_number(words: str) -> int | None:
    """Whole-rupee value of an English/Indian number phrase, or None if any
    word is not a recognised number word (never guesses a partial value)."""
    total = current = 0
    seen_number_word = False
    for raw in re.split(r"[\s\-]+", words.strip().lower()):
        if not raw or raw == "and":
            continue
        if raw in _NUMBER_WORDS:
            current += _NUMBER_WORDS[raw]
        elif raw == "hundred":
            current = (current or 1) * 100
        elif raw in _SCALE_WORDS:
            total += (current or 1) * _SCALE_WORDS[raw]
            current = 0
        else:
            return None
        seen_number_word = True
    return total + current if seen_number_word else None


def parse_amount_in_words(text: str | None) -> float | None:
    """The amount spelled out in words ("Rupees One Thousand Four Hundred Sixty
    Only" -> 1460.0), or None when there is no such line, it is not made
    purely of number words, it involves paise (ambiguous), or the text holds
    more than one such line that disagree. Independent of the printed figure,
    so it can corroborate or contradict an OCR-read amount."""
    if not text:
        return None
    values: set[int] = set()
    for pattern in (_WORDS_AFTER_RUPEES, _WORDS_BEFORE_RUPEES):
        for match in pattern.finditer(text):
            phrase = match.group(1)
            if re.search(r"\bpais[ae]\b", phrase, re.IGNORECASE):
                return None
            value = _words_to_number(phrase)
            if value is not None and value > 0:
                values.add(value)
    return float(values.pop()) if len(values) == 1 else None


def amounts_match(claimed, bill_amount) -> bool:
    """Whether a claimed amount is a valid match for a bill amount.

    An employee commonly enters a bill's total rounded/truncated to a whole
    rupee rather than the bill's own exact decimal figure - and does so in
    EITHER direction (truncating down or rounding up), not just the nearer
    of the two. For a bill amount with decimals, both neighbouring whole
    rupees are therefore valid: Bill Rs.4,499.77 accepts a claimed Rs.4,499
    OR Rs.4,500 - but nothing else (Rs.4,498 and Rs.4,501 are still
    mismatches). This is deliberately NOT ordinary single-direction
    rounding (`round(4499.77) == 4500` would reject the equally-valid
    Rs.4,499). For an already-whole-rupee bill amount, floor and ceiling
    coincide, so only that exact value is accepted - every other whole-rupee
    figure remains a mismatch.

    Accepts/returns plain numbers (int/float/Decimal/str) - internally
    everything is compared as Decimal to avoid float representation
    artifacts (e.g. 4499.77 stored as 4499.7699999999995).
    """
    if claimed is None or bill_amount is None:
        return False
    claimed_d = Decimal(str(claimed))
    bill_d = Decimal(str(bill_amount))
    if claimed_d == bill_d:
        return True
    floor_d = bill_d.to_integral_value(rounding=ROUND_FLOOR)
    ceil_d = bill_d.to_integral_value(rounding=ROUND_CEILING)
    return claimed_d in (floor_d, ceil_d)
