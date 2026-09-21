"""Field extraction for Expense Report PDFs, based on the actual layout
PyMuPDF produces for the real sample documents (one label per line, followed
by its value on the next line(s)). Never invents a value - a field that
can't be found stays None and is surfaced as a warning."""

import re
from typing import Any

from app.services.extraction.dates import parse_date
from app.services.extraction.money import parse_amount

ER_ID_RE = re.compile(r"^(ER-\d+)$")
ER_ID_FALLBACK_RE = re.compile(r"\b(ER-\d+)\b")
TRIP_NUMBER_RE = re.compile(r"Trip Number:\s*(TRIP-\d+)", re.IGNORECASE)
AMOUNT_ONLY_RE = re.compile(r"^(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d+)?$", re.IGNORECASE)
ROW_START_RE = re.compile(r"^(\d+)\.$")
SIGNATURE_RE = re.compile(r"^([A-Z][A-Za-z .]+?)\s*\(\s*([A-Z]{1,6}\d{1,6})\s*\)$")


def _flatten(pages: list[dict[str, Any]]) -> list[tuple[int, str]]:
    flat = []
    for page in pages:
        for line in page["text"].split("\n"):
            flat.append((page["page_number"], line))
    return flat


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
            i = idx + 1

            detail_lines: list[str] = []
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
    pending: list[tuple[str, Any, int]] = []  # (location, date-or-None, page)
    source_page = None

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
        if d is not None and pending and pending[-1][1] is None:
            loc, _, p = pending[-1]
            pending[-1] = (loc, d, p)
        elif d is None:
            pending.append((line, None, page_num))

    legs = []
    for idx in range(0, len(pending) - 1, 2):
        from_loc, from_date, from_page = pending[idx]
        to_loc, to_date, to_page = pending[idx + 1]
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
