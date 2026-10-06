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
