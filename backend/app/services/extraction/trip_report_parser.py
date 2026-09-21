"""Field extraction for standalone Trip Report PDFs, based on the actual
layout observed in the real sample documents (a Zoho-style export: title,
Trip ID, trip type, destination, then labeled header fields, itinerary
reservation blocks, and an approval history)."""

import re
from typing import Any

from app.services.extraction.dates import parse_date

TRIP_ID_RE = re.compile(r"^(TRIP-\d+)$")
TRIP_TYPE_RE = re.compile(r"^[A-Za-z ]*\bTrip$")
DURATION_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})")
SIGNATURE_RE = re.compile(r"^([A-Z][A-Za-z .]+?)\s*\(\s*([A-Z]{1,6}\d{1,6})\s*\)$")
RESERVATION_HEADER_RE = re.compile(r"RESERVATIONS$")


def _flatten(pages: list[dict[str, Any]]) -> list[tuple[int, str]]:
    flat = []
    for page in pages:
        for line in page["text"].split("\n"):
            flat.append((page["page_number"], line))
    return flat


def _next_nonblank(flat: list[tuple[int, str]], start: int) -> tuple[int | None, str | None, int]:
    i = start
    while i < len(flat) and not flat[i][1].strip():
        i += 1
    if i < len(flat):
        return flat[i][0], flat[i][1].strip(), i
    return None, None, i


def extract_trip_details(pages: list[dict[str, Any]]) -> dict[str, Any]:
    flat = _flatten(pages)
    n = len(flat)
    warnings: list[str] = []

    result: dict[str, Any] = {
        "trip_id": None,
        "trip_id_page": None,
        "destination": None,
        "destination_page": None,
        "start_date": None,
        "end_date": None,
        "duration_page": None,
        "purpose": None,
        "purpose_page": None,
        "employee_name": None,
        "employee_name_page": None,
        "employee_code": None,
        "submitted_on": None,
        "submitted_on_page": None,
        "approver": None,
        "approver_page": None,
        "approval_status": None,
        "approval_date": None,
        "legs": [],
    }

    trip_id_index = None
    for i, (page_num, line) in enumerate(flat):
        if TRIP_ID_RE.match(line.strip()):
            result["trip_id"], result["trip_id_page"] = line.strip(), page_num
            trip_id_index = i
            break
    if trip_id_index is None:
        warnings.append("Trip ID could not be found in the document.")

    if trip_id_index is not None:
        for i in range(trip_id_index + 1, min(trip_id_index + 4, n)):
            page_num, line = flat[i]
            if TRIP_TYPE_RE.match(line.strip()):
                p, val, _ = _next_nonblank(flat, i + 1)
                if val:
                    result["destination"], result["destination_page"] = val, p
                break

    for i, (page_num, line) in enumerate(flat):
        if line.strip() == "Duration:":
            p, val, _ = _next_nonblank(flat, i + 1)
            if val:
                m = DURATION_RE.search(val)
                if m:
                    result["start_date"] = parse_date(m.group(1))
                    result["end_date"] = parse_date(m.group(2))
                    result["duration_page"] = p
            break
    if result["start_date"] is None or result["end_date"] is None:
        warnings.append("Travel period could not be reliably extracted.")

    for i, (page_num, line) in enumerate(flat):
        if line.strip() == "Business Purpose:":
            parts = []
            j = i + 1
            part_page = None
            while j < n:
                p2, l2 = flat[j]
                s2 = l2.strip()
                if not s2:
                    break
                if part_page is None:
                    part_page = p2
                parts.append(s2)
                j += 1
            if parts:
                result["purpose"] = " ".join(parts)
                result["purpose_page"] = part_page
            break

    # First "Submitted By" (employee); a later "Submitted By:" (with colon) reappears near the signature block.
    for i, (page_num, line) in enumerate(flat):
        if line.strip() == "Submitted By":
            p, val, idx = _next_nonblank(flat, i + 1)
            m = SIGNATURE_RE.match(val or "")
            if m:
                result["employee_name"] = m.group(1).strip()
                result["employee_code"] = m.group(2).strip()
                result["employee_name_page"] = p
            elif val:
                result["employee_name"], result["employee_name_page"] = val, p
            break
    if not result["employee_name"]:
        warnings.append("Employee name could not be reliably extracted.")

    for i, (page_num, line) in enumerate(flat):
        if line.strip() == "Submitted On":
            p, val, _ = _next_nonblank(flat, i + 1)
            d = parse_date(val)
            if d:
                result["submitted_on"], result["submitted_on_page"] = d, p
            break

    for i, (page_num, line) in enumerate(flat):
        if line.strip() == "Approved By:":
            p, val, _ = _next_nonblank(flat, i + 1)
            if val:
                result["approver"], result["approver_page"] = val, p
            break

    for i, (page_num, line) in enumerate(flat):
        s = line.strip()
        if s.startswith("Trip approved"):
            result["approval_status"] = "Approved"
            if i > 0:
                prev_page, prev_line = flat[i - 1]
                result["approval_date"] = parse_date(prev_line.strip())
            break
        if s.startswith("Trip submitted") and result["approval_status"] is None:
            result["approval_status"] = "Submitted"

    legs, leg_warnings = _parse_itinerary_legs(flat, result["start_date"], result["end_date"])
    result["legs"] = legs
    warnings.extend(leg_warnings)
    result["origin"] = legs[0]["from_location"] if legs else None

    result["warnings"] = warnings
    return result


_BARE_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_STOP_LABELS = ("Submitted By:", "Approved By:")


def _consume_endpoint(flat: list[tuple[int, str]], start: int) -> tuple[str | None, int]:
    """Reads one itinerary endpoint. Bus/train reservations give a single plain
    city line; flight reservations give a short code (e.g. "BLR") followed by a
    fuller, sometimes line-wrapped, parenthesised name - prefer the fuller name
    when present."""
    page, first, idx = _next_nonblank(flat, start)
    if first is None:
        return None, idx
    j = idx + 1

    if re.fullmatch(r"[A-Z]{2,5}", first):
        _, second, idx2 = _next_nonblank(flat, j)
        if second and second != "Description" and not _BARE_DATE_RE.match(second):
            parts = [second]
            open_count, close_count = second.count("("), second.count(")")
            k = idx2 + 1
            while open_count > close_count and k < len(flat):
                _, cont = flat[k]
                cont_s = cont.strip()
                if not cont_s:
                    k += 1
                    continue
                parts.append(cont_s)
                open_count += cont_s.count("(")
                close_count += cont_s.count(")")
                k += 1
            return " ".join(parts), k

    return first, j


def _parse_itinerary_legs(
    flat: list[tuple[int, str]], trip_start, trip_end
) -> tuple[list[dict[str, Any]], list[str]]:
    n = len(flat)
    legs: list[dict[str, Any]] = []
    warnings: list[str] = []
    i = 0
    while i < n:
        page_num, line = flat[i]
        s = line.strip()
        if RESERVATION_HEADER_RE.search(s) and "HOTEL" not in s.upper():
            j = i + 1
            while j < n:
                pj, lj = flat[j]
                sj = lj.strip()
                if RESERVATION_HEADER_RE.search(sj) or sj in _STOP_LABELS:
                    break
                if _BARE_DATE_RE.match(sj):
                    leg_date = parse_date(sj)
                    from_loc, j2 = _consume_endpoint(flat, j + 1)
                    to_loc, j2 = _consume_endpoint(flat, j2)
                    _, label, idx3 = _next_nonblank(flat, j2)
                    j2 = idx3 + 1
                    description = None
                    if label == "Description":
                        _, val, idx4 = _next_nonblank(flat, j2)
                        description = None if (not val or val == "-") else val
                        j2 = idx4 + 1
                    legs.append(
                        {
                            "leg_no": len(legs) + 1,
                            "from_location": from_loc,
                            "from_date": leg_date,
                            "to_location": to_loc,
                            "to_date": leg_date,
                            "description": description,
                            "source_page": pj,
                        }
                    )
                    j = j2
                    continue
                j += 1
            i = j
            continue
        i += 1

    if trip_start and trip_end:
        in_range = []
        for leg in legs:
            if leg["from_date"] and not (trip_start <= leg["from_date"] <= trip_end):
                warnings.append(f"Itinerary leg {leg['leg_no']} date fell outside the trip's travel period and was dropped.")
                continue
            in_range.append(leg)
        if len(in_range) != len(legs):
            for idx, leg in enumerate(in_range, start=1):
                leg["leg_no"] = idx
        legs = in_range

    return legs, warnings
