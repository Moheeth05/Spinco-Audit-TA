"""Safe amount parsing for ₹ / Rs / INR formatted values.

Never guesses: if the text doesn't contain a recognizable number, parse_amount
returns None rather than a fabricated figure.
"""

import re

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
