"""Field extraction for Expense Report PDFs, based on the actual layout
PyMuPDF produces for the real sample documents (one label per line, followed
by its value on the next line(s)). Never invents a value - a field that
can't be found stays None and is surfaced as a warning."""

import re
from typing import Any

from app.services.category_map import is_policy_master_category
from app.services.extraction.dates import parse_date
from app.services.extraction.money import parse_amount

ER_ID_RE = re.compile(r"^(ER-\d+)$")
ER_ID_FALLBACK_RE = re.compile(r"\b(ER-\d+)\b")
TRIP_NUMBER_RE = re.compile(r"Trip Number:\s*(TRIP-\d+)", re.IGNORECASE)
AMOUNT_ONLY_RE = re.compile(r"^(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d+)?$", re.IGNORECASE)
ROW_START_RE = re.compile(r"^(\d+)\.$")
SIGNATURE_RE = re.compile(r"^([A-Z][A-Za-z .]+?)\s*\(\s*([A-Z]{1,6}\d{1,6})\s*\)$")
# Some real Expense Report layouts print a "Merchant : <name>" line between
# the date and the category (observed on a real document post-UAT - the
# original 4 sample ERs this parser was built against don't have it, so
# this is opt-in: only consumed when actually present, never assumed).
MERCHANT_LINE_RE = re.compile(r"^Merchant\s*:\s*(.+)$", re.IGNORECASE)
# A second, independently-optional prefix line some real layouts print
# (observed real case: "Ref# : T2609020947244431394997"). Confirmed real P0
# defect this fixes: code that only ever rescued ONE specific labelled line
# (hardcoded to "Merchant") stored a Ref# line's text directly as the
# category whenever one was present - the reference number ended up in
# Expense.category, and the real category got pushed down into
# expense_details as free text.
REFERENCE_LINE_RE = re.compile(r"^Ref(?:erence)?#?\s*:\s*(.+)$", re.IGNORECASE)
# A real multi-page document (ER-40513, 28 rows) was found to print at LEAST
# five distinct kinds of optional "Label : value" line ahead of the category
# (Merchant, Ref#, Expense Location, PNR Number, Attendees, Start & End KM) -
# an open-ended set no fixed list of per-label regexes can ever fully
# enumerate for an unseen future report. Rather than keep adding one more
# hardcoded label pattern every time a new document surfaces another one,
# extract_expense_lines() instead recognizes the category line by what it
# IS (a real, closed, 29-name Policy Master category - see
# category_map.is_policy_master_category) rather than by what line position
# it happens to occupy. Every line between the date and the first such match
# is preserved - Merchant/Ref# into their own fields since those are the two
# the rest of the system needs separately, everything else folded into
# expense_details as evidence text - never discarded, never mistaken for the
# category.
MAX_PRE_CATEGORY_LINES = 8


def _flatten(pages: list[dict[str, Any]]) -> list[tuple[int, str]]:
    flat = []
    for page in pages:
        for line in page["text"].split("\n"):
            flat.append((page["page_number"], line))
    return flat


def find_report_summary_page(pages: list[dict[str, Any]]) -> int | None:
    """The page carrying "REPORT SUMMARY" - the report's own closing section,
    right after the expense table and before any supporting bills. Used to
    cap the content-page range for bill extraction: content_pages()'s
    stop-at-first-blank-page heuristic over-includes when a report's
    supporting bills also happen to have native PDF text (e.g. emailed
    invoices/e-receipts rather than photographed paper receipts), since
    nothing in that heuristic tells it those later pages aren't more report
    content."""
    for page in pages:
        if "REPORT SUMMARY" in page["text"].upper():
            return page["page_number"]
    return None


def _next_nonblank(flat: list[tuple[int, str]], start: int) -> tuple[int | None, str | None, int]:
    """Returns (page_number, line_text, index) for the first non-blank line at/after start."""
    i = start
    while i < len(flat) and not flat[i][1].strip():
        i += 1
    if i < len(flat):
        return flat[i][0], flat[i][1].strip(), i
    return None, None, i


def extract_header(pages: list[dict[str, Any]]) -> dict[str, Any]:
    flat = _flatten(pages)
    full_text = "\n".join(line for _, line in flat)
    warnings: list[str] = []

    result: dict[str, Any] = {
        "er_id": None,
        "er_id_page": None,
        "employee_name": None,
        "employee_name_page": None,
        "employee_code": None,
        "employee_code_page": None,
        "submitted_on": None,
        "submitted_on_page": None,
        "total_claimed": None,
        "total_claimed_page": None,
        "trip_number": None,
        "trip_number_page": None,
    }

    for idx, (page_num, line) in enumerate(flat):
        if line.strip() == "Expense Report":
            p, val, _ = _next_nonblank(flat, idx + 1)
            if val and ER_ID_RE.match(val):
                result["er_id"], result["er_id_page"] = val, p
            break
    if not result["er_id"]:
        m = ER_ID_FALLBACK_RE.search(full_text)
        if m:
            result["er_id"] = m.group(1)
            warnings.append("ER ID recovered via fallback search rather than the expected header position.")
        else:
            warnings.append("Expense Report ID could not be found in the document.")

    for idx, (page_num, line) in enumerate(flat):
        if line.strip() == "Amount to be Reimbursed":
            p, val, _ = _next_nonblank(flat, idx + 1)
            amt = parse_amount(val)
            if amt is not None:
                result["total_claimed"], result["total_claimed_page"] = amt, p
            break

    for idx, (page_num, line) in enumerate(flat):
        if line.strip() == "Total Reimbursable Amount":
            p, val, _ = _next_nonblank(flat, idx + 1)
            amt = parse_amount(val)
            if amt is not None:
                result["total_claimed"], result["total_claimed_page"] = amt, p
            break

    if result["total_claimed"] is None:
        warnings.append("Total claimed amount could not be reliably extracted.")

    for idx, (page_num, line) in enumerate(flat):
        if line.strip() == "Submitted By":
            p, val, _ = _next_nonblank(flat, idx + 1)
            if val and "@" not in val:
                result["employee_name"], result["employee_name_page"] = val, p
            break
    if not result["employee_name"]:
        warnings.append("Employee name could not be reliably extracted.")

    for idx, (page_num, line) in enumerate(flat):
        if line.strip() == "Submitted On":
            p, val, _ = _next_nonblank(flat, idx + 1)
            d = parse_date(val)
            if d:
                result["submitted_on"], result["submitted_on_page"] = d, p
            break

    name_prefix = result["employee_name"].upper()[:8] if result["employee_name"] else None
    for page_num, line in flat:
        m = SIGNATURE_RE.match(line.strip())
        if m:
            name, code = m.group(1).strip(), m.group(2).strip()
            if not name_prefix or name.upper().startswith(name_prefix):
                result["employee_code"], result["employee_code_page"] = code, page_num
                break

    m = TRIP_NUMBER_RE.search(full_text)
    if m:
        for page_num, line in flat:
            if TRIP_NUMBER_RE.search(line):
                result["trip_number"], result["trip_number_page"] = m.group(1), page_num
                break

    result["warnings"] = warnings
    return result


def extract_expense_lines(pages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    flat = _flatten(pages)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    in_table = False
    i = 0
    n = len(flat)

    while i < n:
        page_num, raw_line = flat[i]
        line = raw_line.strip()

        if line == "EXPENSE SUMMARY":
            in_table = True
            i += 1
            continue
        if in_table and line.startswith("REPORT SUMMARY"):
            break
        if in_table and ROW_START_RE.match(line):
            row_no = int(ROW_START_RE.match(line).group(1))
            row_page = page_num
            i += 1

            date_page, date_text, idx = _next_nonblank(flat, i)
            expense_date = parse_date(date_text)
            if expense_date is None:
                warnings.append(f"Row {row_no}: could not parse a date from '{date_text}'.")
            i = idx + 1

            cat_page, category, idx = _next_nonblank(flat, i)
            merchant, merchant_page = None, None
            reference, reference_page = None, None
            pre_category_lines: list[str] = []
            # Scan forward from the date line for the first line that
            # actually IS one of the 29 real categories - see
            # MAX_PRE_CATEGORY_LINES's comment above for why membership,
            # not position, is what identifies it. `i` is only ever advanced
            # once a line has actually been consumed (as Merchant/Ref#/
            # evidence/category) - if the scan instead runs into the next
            # row marker or the amount line, it stops WITHOUT consuming that
            # line, so the amount-scan loop below still sees it.
            while category is not None:
                if ROW_START_RE.match(category) or AMOUNT_ONLY_RE.match(category):
                    category = None
                    break
                m = MERCHANT_LINE_RE.match(category)
                if m:
                    merchant, merchant_page = m.group(1).strip(), cat_page
                    i = idx + 1
                    cat_page, category, idx = _next_nonblank(flat, i)
                    continue
                m = REFERENCE_LINE_RE.match(category)
                if m:
                    reference, reference_page = m.group(1).strip(), cat_page
                    i = idx + 1
                    cat_page, category, idx = _next_nonblank(flat, i)
                    continue
                if is_policy_master_category(category):
                    i = idx + 1
                    break
                if len(pre_category_lines) >= MAX_PRE_CATEGORY_LINES:
                    # Never seen a genuine category within a generous bound -
                    # give up rather than risk scanning past the amount line
                    # into the next row entirely; downstream policy lookup
                    # will correctly fail to resolve whatever raw text this
                    # row does end up with, surfacing as REVIEW_REQUIRED
                    # rather than silently guessing.
                    category = None
                    break
                pre_category_lines.append(category)
                i = idx + 1
                cat_page, category, idx = _next_nonblank(flat, i)

            detail_lines: list[str] = list(pre_category_lines)
            amount = None
            amount_page = row_page
            found_amount = False
            while i < n:
                p2, l2 = flat[i]
                s2 = l2.strip()
                if not s2:
                    i += 1
                    continue
                if ROW_START_RE.match(s2):
                    warnings.append(f"Row {row_no}: amount not found before the next row started.")
                    break
                if AMOUNT_ONLY_RE.match(s2):
                    amount = parse_amount(s2)
                    amount_page = p2
                    found_amount = True
                    i += 1
                    break
                detail_lines.append(s2)
                i += 1

            if not found_amount:
                warnings.append(f"Row {row_no}: claimed amount could not be reliably extracted.")

            details = " ".join(detail_lines).strip() or None
            is_complete = expense_date is not None and category is not None and amount is not None
            rows.append(
                {
                    "s_no": row_no,
                    "expense_date": expense_date,
                    "category": category,
                    "merchant": merchant,
                    "reference_id": reference,
                    "expense_details": details,
                    "claimed_amount": amount,
                    "source_page": row_page,
                    "amount_source_page": amount_page,
                    "status": "EXTRACTED" if is_complete else "REVIEW_REQUIRED",
                }
            )
            continue
        i += 1

    if not rows:
        warnings.append("No expense line items could be found in the EXPENSE SUMMARY section.")
    return rows, warnings


def extract_embedded_trip_summary(pages: list[dict[str, Any]]) -> dict[str, Any] | None:
    flat = _flatten(pages)
    has_section = any(line.strip() == "TRIP SUMMARY" for _, line in flat)
    if not has_section:
        return None

    full_text = "\n".join(line for _, line in flat)
    trip_number, trip_number_page = None, None
    m = TRIP_NUMBER_RE.search(full_text)
    if m:
        for page_num, line in flat:
            if TRIP_NUMBER_RE.search(line):
                trip_number, trip_number_page = m.group(1), page_num
                break

    city, city_page = None, None
    in_section = False
    source_page = None
    # (location, date, page) pairs - one per confirmed from/to location. A
    # bare text line only becomes a pair once the very next line turns out to
    # be a parseable date; this layout also prints a transport-mode label
    # ("Train"/"Bus", no date of its own) ahead of each leg's from-location
    # and a free-text description after each leg's to-date, both of which are
    # bare text lines indistinguishable from a real location by content alone
    # - so anything that never gets confirmed by a following date (mode
    # labels, descriptions, a trailing unconfirmed line at the section's end)
    # is simply never turned into a pair, rather than guessed at.
    pairs: list[tuple[str, Any, int]] = []
    pending_loc: tuple[str, int] | None = None

    for i, (page_num, raw_line) in enumerate(flat):
        line = raw_line.strip()
        if line == "TRIP SUMMARY":
            in_section = True
            source_page = page_num
            continue
        if not in_section:
            continue
        if line == "EXPENSE SUMMARY":
            break
        if line.lower().startswith("city:"):
            city, city_page = line.split(":", 1)[1].strip(), page_num
            continue
        if line in ("From", "To", "Description") or not line:
            continue

        d = parse_date(line)
        if d is not None:
            if pending_loc is not None:
                loc, p = pending_loc
                pairs.append((loc, d, p))
                pending_loc = None
        else:
            pending_loc = (line, page_num)

    legs = []
    for idx in range(0, len(pairs) - 1, 2):
        from_loc, from_date, from_page = pairs[idx]
        to_loc, to_date, to_page = pairs[idx + 1]
        legs.append(
            {
                "leg_no": idx // 2 + 1,
                "from_location": from_loc,
                "from_date": from_date,
                "to_location": to_loc,
                "to_date": to_date,
                "description": None,
            }
        )

    return {
        "trip_number": trip_number,
        "trip_number_page": trip_number_page,
        "destination": city,
        "destination_page": city_page,
        "source_page": source_page,
        "legs": legs,
    }
