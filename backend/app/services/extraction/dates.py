"""Date parsing for the DD/MM/YYYY (and DD-MM-YYYY, seen on some supporting
bills) formats used throughout the real sample Expense/Trip Report text, plus
the textual month-name formats real supporting bills print (e.g. a ride
receipt's "Date: August 7, 2026", an IRCTC ticket's "22-Aug-2026"), plus the
2-digit-year formats confirmed on real fuel-pump/thermal-print receipts
(IndianOil's own "Date : 07/07/26", a Uber receipt's bare "8/19/26"). Returns
None (never a guessed date) when the text doesn't match."""

import re
from dataclasses import dataclass
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

_DATE_PATTERN = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")
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


# ----------------------------------------------------------------------------------------
# Evidence-based reader for SUPPORTING-DOCUMENT dates.
#
# find_all_dates() (below) assumes day-first for a numeric date and is kept for
# the Expense Report / Trip Report, whose own system prints DD/MM/YYYY. A bill
# or receipt is different: it comes from any issuer, and "07/02/2026" is 7 February
# on an Indian invoice and 2 July on a US-style receipt. read_dates() therefore
# never assumes - it records exactly what was printed, every structurally valid
# reading, and resolves the order ONLY from independent evidence in this order:
#
#   1. an explicit cue in the document ("MM/DD/YYYY", "DD-MM-YY", ...)       -> HIGH
#   2. the same document's own unambiguous dates in the same format (a
#      component > 12), or an unambiguous date it echoes                      -> MEDIUM
#   3. (apply_group_evidence) other documents from the same TEMPLATE with the
#      same printed format whose dates prove the order, none contradicting    -> MEDIUM
#
# Anything else stays UNRESOLVED: value is None, both candidates are kept, and no
# caller may award exact-date credit. The expense/claim date is never evidence.
# ----------------------------------------------------------------------------------------

# "/" and "-" separators take a 2- or 4-digit year; "." (14.06.2026) takes ONLY a 4-digit year, because a dotted
# 2-digit year collides with decimals and version numbers far too easily (see read_dates).
_NUM_TOKEN = re.compile(r"(?<![\d/.])(?<![\d/.]-)(\d{1,2})([/.-])(\d{1,2})\2(\d{4}|\d{2})(?!\d)")
_ISO_TOKEN = re.compile(r"(?<![\d/-])(\d{4})([-/])(\d{1,2})\2(\d{1,2})(?!\d)")  # 2026-06-14 and 2026/06/14 (year-first is never day-first)
# Month-name dates, any separators ("14 Jun 2026", "14/Jun/2026", "14-Jun-26", "14Jun26", "14 Jun'26", "Jun 14, 2026",
# "Jun-14-2026", "14th of June 2026"). A 2-digit year is refused when a clock minute follows ("14 Jun 12:30" is a
# time, not the year 2012) and the day/year must not be glued to other digits.
_SEP = r"[ \t/.,'\u2019-]*"  # horizontal only: a date never runs across a line break
_SEP1 = r"[ \t/.,'\u2019-]+"  # at least one separator: day and year are never fused ("Jul -293763")
_MDY_NAME_TOKEN = re.compile(rf"(?<![A-Za-z\d])({_MONTH_ALT})\.?{_SEP}(\d{{1,2}})(?:st|nd|rd|th)?{_SEP1}(\d{{4}})(?!\d)", re.IGNORECASE)
# A LONE letter directly in front of the day is refused: OCR "I6-SEP-2026" (a misread "16") must not be read as the
# 6th. A letter that ends a longer word is kept ("Inv01.DEC.2026", "On16 Sep 2026", "Date16-Sep-2026" - a label glued
# to its date), exactly as before.
_LONE_LETTER_BEFORE = r"(?<!(?<![A-Za-z])[A-Za-z])"
_DMY_NAME_TOKEN = re.compile(rf"(?<![\d/.]){_LONE_LETTER_BEFORE}(?<![\d/.]-)(\d{{1,2}})(?:st|nd|rd|th)?(?:[ \t]+of)?{_SEP}({_MONTH_ALT})\.?{_SEP}(\d{{4}}|\d{{2}})(?!\d|:\d)", re.IGNORECASE)
_CUE_MDY = re.compile(r"\b(?:MM|M)\s*[/-]\s*(?:DD|D)\s*[/-]\s*(?:YYYY|YY)\b", re.IGNORECASE)
_CUE_DMY = re.compile(r"\b(?:DD|D)\s*[/-]\s*(?:MM|M)\s*[/-]\s*(?:YYYY|YY)\b", re.IGNORECASE)
_CLOCK_AFTER = re.compile(r"^[\s,@T]*(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp]\.?[Mm]\.?)?")
_CLOCK_BEFORE = re.compile(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp]\.?[Mm]\.?)?[\s,@]*$")
_PLAUSIBLE_YEARS = range(2000, 2100)


@dataclass(frozen=True)
class DateReading:
    """One date as printed, with every valid reading and how (if at all) the
    day/month order was established."""

    raw: str
    start: int
    value: date | None  # None = unresolved or invalid; callers must not substitute a guess
    candidates: tuple[date, ...]
    method: str  # iso | month_name | numeric | cue | same_document | report_group | unresolved | invalid
    confidence: str | None  # HIGH | MEDIUM | None
    signature: tuple | None = None  # (separator, year digits, zero-padded, clock style) of a numeric token
    order: str | None = None  # DMY | MDY once known for a numeric token

    @property
    def ambiguous(self) -> bool:
        return self.value is None and len(self.candidates) > 1


def _clock_style(text: str, start: int, end: int) -> str:
    after = _CLOCK_AFTER.match(text[end : end + 18])
    before = _CLOCK_BEFORE.search(text[max(0, start - 18) : start])
    hit = after or before
    if hit is None:
        return "none"
    return "12h" if hit.group(3) else "24h"


def _numeric_reading(text: str, m: re.Match) -> DateReading:
    a_s, sep, b_s, y_s = m.group(1), m.group(2), m.group(3), m.group(4)
    a, b = int(a_s), int(b_s)
    year = int(y_s) if len(y_s) == 4 else 2000 + int(y_s)
    signature = (sep, len(y_s), len(a_s) == 2 and len(b_s) == 2, _clock_style(text, m.start(), m.end()))
    raw = m.group(0)
    if year not in _PLAUSIBLE_YEARS:
        return DateReading(raw, m.start(), None, (), "invalid", None, signature)
    dmy, mdy = _safe_date(year, b, a), _safe_date(year, a, b)
    if dmy is None and mdy is None:
        return DateReading(raw, m.start(), None, (), "invalid", None, signature)
    if dmy is not None and mdy is not None and dmy != mdy:
        return DateReading(raw, m.start(), None, (dmy, mdy), "unresolved", None, signature)
    only = dmy or mdy
    order = None if (dmy is not None and mdy is not None) else ("DMY" if dmy is not None else "MDY")
    return DateReading(raw, m.start(), only, (only,), "numeric", "HIGH", signature, order)


# ----------------------------------------------------------------------------------------
# OCR-damaged month-name dates ("16-SEF-2026", "16-5EP-2026", "16-SEP-2O26", "16-SEP-\n2026", "16-SEP-202610:15:48").
#
# Never a guess. A reading is produced ONLY when ALL of these hold:
#   * the printed STRUCTURE is complete: a real 1-2 digit day, separators, a month-like word, a 20xx year;
#   * the month word is repaired only to the ONE month it is a single glyph away from (digit-for-letter such as
#     5->S, or one substituted letter), is not a common English word, and is not itself already a valid month;
#   * the year is repaired only for the glyph confusions O->0 and I/l->1, and must stay within 2000-2099;
#   * the repaired calendar date is valid;
#   * independent evidence supports a date being printed there: a "Date"/"Dt" label on the same line (or the line
#     above, when the date starts its line) or an adjacent clock time;
#   * a line break is bridged only directly after a separator, and a fused clock only when "hh:mm" follows the year.
# The result is method "month_name_ocr" with MEDIUM confidence, `raw` keeps exactly what was printed, and it is
# never used as proof of anything else (not an "echo", not a "claimed date is printed elsewhere" match).
# ----------------------------------------------------------------------------------------
_OCR_SEP = r"(?:[ \t]*[-/.][ \t]*(?:\n[ \t]*)?|[ \t]+)"
_OCR_DMY_TOKEN = re.compile(
    rf"(?<![\d/.]){_LONE_LETTER_BEFORE}(?<![\d/.]-)(\d{{1,2}}){_OCR_SEP}([A-Za-z0-9]{{3,9}})\.?{_OCR_SEP}(2[0Oo][0-9OoIl]{{2}})(?=\d{{2}}:\d{{2}}|(?![A-Za-z\d]))"
)
_OCR_DIGIT_AS_LETTER = {"5": "S", "0": "O", "8": "B"}
_OCR_YEAR_GLYPHS = str.maketrans("OoIl", "0011")
_OCR_NOT_A_MONTH = frozenset(
    {"SET", "FEE", "FEW", "MAD", "MAT", "MAN", "MAP", "MAX", "MAS", "JAM", "JAR", "DEN", "DEE", "NOW", "NOT", "NOR", "OAT", "ACT", "APT", "OUT"}
)
_DATE_LABEL = re.compile(r"\b(?:date|dt|dated)\b", re.IGNORECASE)


def _ocr_month(token: str) -> int | None:
    """The one month `token` is a single glyph away from, else None (also None for an already-valid month)."""
    upper = token.upper()
    if upper.lower() in _MONTH_NAMES or upper in _OCR_NOT_A_MONTH or sum(c.isalpha() for c in upper) < 2:
        return None
    mapped = "".join(_OCR_DIGIT_AS_LETTER.get(c, c) for c in upper)
    found = {
        number
        for name, number in _MONTH_NAMES.items()
        if len(name) == len(mapped) and sum(a != b for a, b in zip(name.upper(), mapped)) <= 1
    }
    return found.pop() if len(found) == 1 else None


def _ocr_date_supported(text: str, m: re.Match, fused_clock: bool) -> bool:
    if fused_clock:
        return True  # "hh:mm" directly after the year is itself the evidence
    line_start = text.rfind("\n", 0, m.start()) + 1
    before = text[line_start : m.start()]
    if _DATE_LABEL.search(before):
        return True
    if not before.strip() and line_start > 1:  # the date starts its line: its label may be the line above
        prev_start = text.rfind("\n", 0, line_start - 1) + 1
        if _DATE_LABEL.search(text[prev_start : line_start - 1]):
            return True
    return _clock_style(text, m.start(), m.end()) != "none"


def _ocr_readings(text: str, taken: list[DateReading]) -> list[DateReading]:
    out: list[DateReading] = []
    spans = [(r.start, r.start + len(r.raw)) for r in taken]
    for m in _OCR_DMY_TOKEN.finditer(text):
        if any(m.start() < e and s < m.end() for s, e in spans):
            continue  # already read normally
        day_s, month_s, year_s = m.group(1), m.group(2), m.group(3)
        month_exact = month_s.lower() in _MONTH_NAMES
        month = _MONTH_NAMES[month_s.lower()] if month_exact else _ocr_month(month_s)
        if month is None:
            continue
        fused = re.match(r"\d{2}:\d{2}", text[m.end() : m.end() + 5]) is not None
        repaired = (not month_exact) or (not year_s.isdigit()) or ("\n" in m.group(0)) or fused
        if not repaired:
            continue
        year = int(year_s.translate(_OCR_YEAR_GLYPHS))
        val = _safe_date(year, month, int(day_s)) if year in _PLAUSIBLE_YEARS else None
        if val is None or not _ocr_date_supported(text, m, fused):
            continue
        out.append(DateReading(" ".join(m.group(0).split()), m.start(), val, (val,), "month_name_ocr", "MEDIUM"))
    return out


def has_adjacent_clock(text: str, reading: DateReading) -> bool:
    """True when a clock time ("10:15:48", "7:37 PM") is printed directly beside this printed date, as on a
    transaction timestamp. Used only to choose between several unlabelled dates (see bill_parser)."""
    end = reading.start + len(reading.raw)
    if reading.method == "month_name_ocr":  # `raw` is whitespace-collapsed: take the real printed span
        m = _OCR_DMY_TOKEN.match(text, reading.start)
        end = m.end() if m else end
    return _clock_style(text, reading.start, end) != "none"


def _with_order(r: DateReading, order: str, method: str, confidence: str) -> DateReading:
    dmy, mdy = r.candidates  # candidates are always (day-first, month-first) for an ambiguous token
    return DateReading(r.raw, r.start, dmy if order == "DMY" else mdy, r.candidates, method, confidence, r.signature, order)


def read_dates(text: str | None) -> list[DateReading]:
    """Every date in `text`, in document order, resolved from this document's own
    evidence only (tiers 1-2 above). Ambiguous numeric dates that this document
    cannot settle come back unresolved (value None)."""
    text = text or ""
    strong: list[DateReading] = []
    numeric: list[DateReading] = []
    for m in _ISO_TOKEN.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(3)), int(m.group(4))
        val = _safe_date(y, mo, d) if y in _PLAUSIBLE_YEARS else None
        strong.append(DateReading(m.group(0), m.start(), val, (val,) if val else (), "iso" if val else "invalid", "HIGH" if val else None))
    for m in _MDY_NAME_TOKEN.finditer(text):
        y = int(m.group(3))
        val = _safe_date(y, _MONTH_NAMES[m.group(1).lower()], int(m.group(2))) if y in _PLAUSIBLE_YEARS else None
        strong.append(DateReading(m.group(0), m.start(), val, (val,) if val else (), "month_name" if val else "invalid", "HIGH" if val else None))
    for m in _DMY_NAME_TOKEN.finditer(text):
        year = int(m.group(3)) if len(m.group(3)) == 4 else 2000 + int(m.group(3))
        val = _safe_date(year, _MONTH_NAMES[m.group(2).lower()], int(m.group(1))) if year in _PLAUSIBLE_YEARS else None
        strong.append(DateReading(m.group(0), m.start(), val, (val,) if val else (), "month_name" if val else "invalid", "HIGH" if val else None))
    for m in _NUM_TOKEN.finditer(text):
        if m.group(2) == "." and len(m.group(4)) != 4:
            continue
        numeric.append(_numeric_reading(text, m))

    cue_mdy, cue_dmy = bool(_CUE_MDY.search(text)), bool(_CUE_DMY.search(text))
    cue = "MDY" if cue_mdy and not cue_dmy else ("DMY" if cue_dmy and not cue_mdy else None)
    proven: dict[tuple, set[str]] = {}
    for r in numeric:
        if r.order and r.signature:
            proven.setdefault(r.signature[:2], set()).add(r.order)  # (separator, year digits)
    echoes = {r.value for r in strong + numeric if r.value is not None and r.method != "unresolved"}

    resolved: list[DateReading] = []
    for r in numeric:
        if r.method == "unresolved":
            same_format = proven.get(r.signature[:2], set())
            matching_echo = [c for c in r.candidates if c in echoes]
            if cue:
                r = _with_order(r, cue, "cue", "HIGH")
            elif len(same_format) == 1:
                r = _with_order(r, next(iter(same_format)), "same_document", "MEDIUM")
            elif len(matching_echo) == 1 and not same_format:
                order = "DMY" if matching_echo[0] == r.candidates[0] else "MDY"
                r = _with_order(r, order, "same_document", "MEDIUM")
        resolved.append(r)
    firm = strong + resolved
    return sorted(firm + _ocr_readings(text, firm), key=lambda r: r.start)


_WORD = re.compile(r"[A-Za-z]{3,}")
TEMPLATE_SIMILARITY = 0.6


def _template_words(text: str | None) -> frozenset[str]:
    return frozenset(w.lower() for w in _WORD.findall(text or ""))


def _similar_template(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= TEMPLATE_SIMILARITY


def apply_group_evidence(texts: list[str | None], readings: list[list[DateReading]]) -> list[list[DateReading]]:
    """Tier 3. An ambiguous date left unresolved by its own document is resolved
    from OTHER documents of the same template (word-set similarity) that print dates
    in the same numeric format and whose dates PROVE the order (a component > 12, or
    an explicit cue) - and only if every such proof agrees. Different templates in
    one PDF therefore keep their own conventions; a lone ambiguous receipt, or a
    group whose proofs contradict, stays unresolved. Group-derived resolutions are
    never used as proof for others (no chains)."""
    words = [_template_words(t) for t in texts]
    out: list[list[DateReading]] = []
    for i, doc in enumerate(readings):
        fixed: list[DateReading] = []
        for r in doc:
            if r.method == "unresolved" and r.signature:
                orders = {
                    o.order
                    for j, other in enumerate(readings)
                    if j != i and _similar_template(words[i], words[j])
                    for o in other
                    if o.signature == r.signature and o.order and o.method in ("numeric", "cue", "same_document")
                }
                if len(orders) == 1:
                    r = _with_order(r, next(iter(orders)), "report_group", "MEDIUM")
            fixed.append(r)
        out.append(fixed)
    return out


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
