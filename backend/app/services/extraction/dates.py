"""Date parsing for the DD/MM/YYYY (and DD-MM-YYYY, seen on some supporting
bills) formats used throughout the real sample Expense/Trip Report text, plus
the textual month-name formats real supporting bills print (e.g. a ride
receipt's "Date: August 7, 2026", an IRCTC ticket's "22-Aug-2026"), plus the
2-digit-year formats confirmed on real fuel-pump/thermal-print receipts
(IndianOil's own "Date : 07/07/26", a Uber receipt's bare "8/19/26"). Returns
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
# ISO 8601 (YYYY-MM-DD) - confirmed on a real fuel-pump receipt ("DATE:
# 2006-09-23"). Unambiguous: the leading 4-digit year can never be confused
# with _DATE_PATTERN above (which starts with a 1-2 digit day/month), and
# ISO order is always year-month-day, never day-first - no DD/MM-style
# ambiguity to resolve. Hyphen-only, matching the standard's own separator;
# a slash-separated "2026/09/23" is rare enough in this domain's real
# documents to be deliberately left unsupported rather than guessed at.
_DATE_PATTERN_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
# A 2-digit year, confirmed on real fuel-pump/thermal receipts in the
# reference corpus ("Date : 07/07/26", "Date : 13/07/26", a bare "8/19/26"
# on a Uber receipt with no "Date:" label at all) - never confused with the
# _DATE_PATTERN above, since \b(\d{2})\b requires an EXACT 2-digit token
# (no internal word boundary exists inside a contiguous 4-digit "2026", so
# the two patterns can never both match the same year).
_DATE_PATTERN_SHORT_YEAR = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2})\b")
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


def could_represent_date(text: str | None, candidate: date) -> bool:
    """True if some numeric date-shaped token in `text`, under EITHER
    structurally valid day/month reading, equals `candidate` - never invents
    a date, only recognizes that already-present text could also represent a
    specific, already-known date written in the other day/month convention.

    Confirmed real case: a US-issued Uber receipt prints a bare "7/1/26"
    (meant as July 1, MM/DD) which this module's own DD/MM-first default
    (see find_all_dates above) reads as January 7 - producing a false DATE
    MISMATCH against an Expense Report date of July 1 extracted correctly
    elsewhere. Used only by validation/checks.py's check_bill_date, as a
    narrow reconciliation check against the expense's OWN already-extracted
    date - it never changes what gets stored as a bill's own parsed
    bill_date, and a genuine mismatch (no reading of any date in the text
    equals the candidate) is completely unaffected."""
    text = text or ""
    for match in _DATE_PATTERN.finditer(text):
        day, month, year = (int(g) for g in match.groups())
        for d, m in ((day, month), (month, day)):
            if _safe_date(year, m, d) == candidate:
                return True
    for match in _DATE_PATTERN_SHORT_YEAR.finditer(text):
        a, b, yy = (int(g) for g in match.groups())
        year = 2000 + yy
        for d, m in ((a, b), (b, a)):
            if _safe_date(year, m, d) == candidate:
                return True
    return False


def find_all_dates(text: str) -> list[date]:
    text = text or ""
    found: list[tuple[int, date]] = []  # (position, date) - kept in document order
    # Dates from the unambiguous 4-digit-year/month-name patterns below -
    # collected first so the short-year pattern (weaker: day/month order is
    # genuinely ambiguous for small numbers) can be reconciled against them.
    strong_dates: set[date] = set()

    for match in _DATE_PATTERN.finditer(text):
        day, month, year = (int(g) for g in match.groups())
        d = _safe_date(year, month, day)
        if d:
            found.append((match.start(), d))
            strong_dates.add(d)

    for match in _DATE_PATTERN_ISO.finditer(text):
        year, month, day = (int(g) for g in match.groups())
        d = _safe_date(year, month, day)
        if d:
            found.append((match.start(), d))
            strong_dates.add(d)

    for match in _MONTH_DAY_YEAR_RE.finditer(text):
        month = _MONTH_NAMES[match.group(1).lower()]
        day, year = int(match.group(2)), int(match.group(3))
        d = _safe_date(year, month, day)
        if d:
            found.append((match.start(), d))
            strong_dates.add(d)

    for match in _DAY_MONTH_YEAR_RE.finditer(text):
        day = int(match.group(1))
        month = _MONTH_NAMES[match.group(2).lower()]
        year = int(match.group(3))
        d = _safe_date(year, month, day)
        if d:
            found.append((match.start(), d))
            strong_dates.add(d)

    for match in _DATE_PATTERN_SHORT_YEAR.finditer(text):
        a, b, yy = (int(g) for g in match.groups())
        # This app's whole domain is current-era corporate travel expense
        # audits (the real corpus is entirely 2026) - never pre-2000 paper
        # receipts, so a 2-digit year is unambiguously 2000+yy here.
        year = 2000 + yy
        # DD/MM is this codebase's established convention (matches
        # _DATE_PATTERN above) - tried first and preferred whenever it's
        # valid, even if MM/DD would also happen to be valid (e.g. 07/07
        # is symmetric; 03/07 stays "3 July", never silently reinterpreted
        # as "March 7").
        primary = _safe_date(year, b, a)
        alt = _safe_date(year, a, b) if 1 <= a <= 12 else None
        if primary is None and alt is not None:
            # DD/MM is genuinely IMPOSSIBLE (b > 12, the second number can't
            # be a month) - a real, confirmed case on a Uber receipt's bare
            # "8/19/26" (day 19 can't be a month, so it can only be
            # August 19).
            found.append((match.start(), alt))
        elif primary is not None and primary not in strong_dates and alt is not None and alt in strong_dates:
            # DD/MM is technically valid but matches no other date already
            # found on this bill, while the alternate MM/DD reading exactly
            # corroborates one that was - a real, confirmed case: a Uber
            # receipt prints "Jul 10, 2026" at the top and, later, a bare
            # "7/10/26" next to the payment method/time ("Cash\n7/10/26
            # 8:50 PM") - a payment-timestamp echo of the SAME date in the
            # other order, not a second, different date. Only ever
            # reconciles toward a date already independently confirmed by a
            # stronger (4-digit-year/month-name) pattern elsewhere on the
            # same bill - never invents a date, never resolves a genuine
            # conflict between two dates neither of which is corroborated.
            found.append((match.start(), alt))
        elif primary is not None:
            found.append((match.start(), primary))

    found.sort(key=lambda item: item[0])
    return [d for _, d in found]
