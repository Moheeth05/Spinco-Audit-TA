"""Date parsing for the DD/MM/YYYY (and DD-MM-YYYY, seen on some supporting
bills) formats used throughout the real sample Expense/Trip Report text, plus
the textual month-name formats real supporting bills print (e.g. a ride
receipt's "Date: August 7, 2026", an IRCTC ticket's "22-Aug-2026"). Returns
None (never a guessed date) when the text doesn't match."""

import re
from datetime import date

_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))

_DATE_PATTERN = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
# "August 7, 2026" / "August 7 2026"
_MONTH_DAY_YEAR_RE = re.compile(rf"\b({_MONTH_ALT})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE)
# "7 August 2026" / "07-Aug-2026" / "22-Aug-2026 16:31:16 HRS"
_DAY_MONTH_YEAR_RE = re.compile(rf"\b(\d{{1,2}})[\s-]({_MONTH_ALT})[\s-](\d{{4}})\b", re.IGNORECASE)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(text: str | None) -> date | None:
    dates = find_all_dates(text)
    return dates[0] if dates else None


def find_all_dates(text: str) -> list[date]:
    text = text or ""
    found: list[tuple[int, date]] = []  # (position, date) - kept in document order

    for match in _DATE_PATTERN.finditer(text):
        day, month, year = (int(g) for g in match.groups())
        d = _safe_date(year, month, day)
        if d:
            found.append((match.start(), d))

    for match in _MONTH_DAY_YEAR_RE.finditer(text):
        month = _MONTH_NAMES[match.group(1).lower()]
        day, year = int(match.group(2)), int(match.group(3))
        d = _safe_date(year, month, day)
        if d:
            found.append((match.start(), d))

    for match in _DAY_MONTH_YEAR_RE.finditer(text):
        day = int(match.group(1))
        month = _MONTH_NAMES[match.group(2).lower()]
        year = int(match.group(3))
        d = _safe_date(year, month, day)
        if d:
            found.append((match.start(), d))

    found.sort(key=lambda item: item[0])
    return [d for _, d in found]
