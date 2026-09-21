"""Date parsing for the DD/MM/YYYY format used throughout the real sample
documents. Returns None (never a guessed date) when the text doesn't match."""

import re
from datetime import date

_DATE_PATTERN = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_all_dates(text: str) -> list[date]:
    results = []
    for match in _DATE_PATTERN.finditer(text or ""):
        day, month, year = (int(g) for g in match.groups())
        try:
            results.append(date(year, month, day))
        except ValueError:
            continue
    return results
