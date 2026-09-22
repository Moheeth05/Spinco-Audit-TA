"""Deterministic Batta-expense-to-Trip matching.

Mirrors bill_matcher.py's structure exactly (same MATCHED/MULTIPLE_CANDIDATES/
NO_MATCH/REVIEW_REQUIRED vocabulary, same top-N candidate scoring, same
threshold shape) but scores an expense against candidate trips (embedded
TRIP SUMMARY sections and/or standalone Trip Report PDFs) instead of bills.
An LLM is never used for the final decision.

Weights (documented here, the one place they're defined):
    Trip is embedded in this exact Expense Report   +50   (direct source-document
                                                             evidence - the strongest
                                                             signal available)
    Trip number matches this report's own embedded
    trip summary (standalone candidate sharing the
    same trip number)                                +40
    Employee code exact match                        +35
    Employee email exact match                        +35
    Employee name strong text match (no code/email)   +15   (text similarity >= 0.6)
    Employee name partial text match                   +7   (text similarity >= 0.3)
    Expense date within trip start/end (inclusive)    +30
    Expense date within a specific leg's date range   +10   (bonus, on top of the
                                                             trip-level date match)
    Destination text similarity strong                +10   (>= 0.5, vs expense details)
    Destination text similarity partial                +5   (>= 0.2)

A candidate whose employee_code is known and differs from the expense's own
employee_code is never scored positively - it is excluded from the normal
candidate pool and instead recorded as evidence of a confirmed employee
mismatch (see `employee_mismatch` on TripMatchOutcome), so a genuinely wrong
employee's trip can never be silently selected just because dates/destination
happen to line up.

Same-trip deduplication: when an embedded trip summary and a standalone Trip
Report both carry the same (non-null) trip number, they represent the same
real-world trip described twice, not two competing candidates - only the
higher-scoring representative (ties broken toward the embedded one, since it
is direct evidence on the source document) is kept before the
MATCHED/MULTIPLE_CANDIDATES decision is made, so this never produces a false
"multiple candidates" ambiguity. Source provenance for the collapsed
duplicate is preserved in the reasons text, never silently discarded.

Decision thresholds (identical shape to bill_matcher.py):
    score < 25                  -> NO_MATCH (or, with a recorded employee
                                    mismatch, that mismatch is surfaced instead)
    top two candidates within 12 of each other (both >= 25) -> MULTIPLE_CANDIDATES
    score >= 70                 -> MATCHED, confidence HIGH
    score >= 45                 -> MATCHED, confidence MEDIUM
    25 <= score < 45 (sole candidate) -> REVIEW_REQUIRED (too uncertain to auto-confirm)
"""

import difflib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

MIN_THRESHOLD = 25
MEDIUM_THRESHOLD = 45
HIGH_THRESHOLD = 70
CANDIDATE_MARGIN = 12


def _text_similarity(a: str, b: str) -> float:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class TripCandidate:
    trip_report_id: int
    score: int
    reasons: list[str] = field(default_factory=list)
    # Other trip_report_ids collapsed into this one by _dedupe_same_trip because
    # they share the same trip number (e.g. the standalone Trip Report for a
    # trip whose embedded summary won). Recorded so the caller can still
    # persist a DB-level link for them - source provenance is never silently
    # discarded just because it wasn't the primary match.
    merged_trip_report_ids: list[int] = field(default_factory=list)


@dataclass
class TripMatchOutcome:
    status: str  # MATCHED | MULTIPLE_CANDIDATES | NO_MATCH | REVIEW_REQUIRED
    matched_trip_report_id: int | None
    score: int | None
    confidence: str | None  # HIGH | MEDIUM | LOW
    reasons: list[str]
    candidates: list[TripCandidate]
    employee_mismatch: bool = False


def _employee_codes_conflict(expense: dict[str, Any], trip: dict[str, Any]) -> bool:
    exp_code = (expense.get("employee_code") or "").strip().upper()
    trip_code = (trip.get("employee_code") or "").strip().upper()
    return bool(exp_code) and bool(trip_code) and exp_code != trip_code


def _date_in_range(d: date | None, start: date | None, end: date | None) -> bool:
    if d is None or start is None or end is None:
        return False
    return start <= d <= end


def score_pair(expense: dict[str, Any], trip: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if trip.get("is_embedded"):
        score += 50
        reasons.append("Trip is embedded directly in this Expense Report's own source document")

    embedded_trip_id = (expense.get("embedded_trip_id") or "").strip().upper()
    trip_id = (trip.get("trip_id") or "").strip().upper()
    if not trip.get("is_embedded") and embedded_trip_id and trip_id and embedded_trip_id == trip_id:
        score += 40
        reasons.append(f"Trip number matches this report's own embedded trip summary ({trip.get('trip_id')})")

    exp_code = (expense.get("employee_code") or "").strip().upper()
    trip_code = (trip.get("employee_code") or "").strip().upper()
    exp_email = (expense.get("employee_email") or "").strip().lower()
    trip_email = (trip.get("employee_email") or "").strip().lower()
    if exp_code and trip_code and exp_code == trip_code:
        score += 35
        reasons.append("Employee code matches exactly")
    if exp_email and trip_email and exp_email == trip_email:
        score += 35
        reasons.append("Employee email matches exactly")
    if not (exp_code and trip_code) and not (exp_email and trip_email):
        sim = _text_similarity(expense.get("employee_name") or "", trip.get("employee_name") or "")
        if sim >= 0.6:
            score += 15
            reasons.append("Employee name: strong text match")
        elif sim >= 0.3:
            score += 7
            reasons.append("Employee name: partial text match")

    exp_date = expense.get("expense_date")
    if _date_in_range(exp_date, trip.get("start_date"), trip.get("end_date")):
        score += 30
        reasons.append(f"Expense date falls within the trip's travel period ({trip.get('start_date')} - {trip.get('end_date')})")
        for leg in trip.get("legs") or []:
            if _date_in_range(exp_date, leg.get("from_date"), leg.get("to_date")):
                score += 10
                reasons.append("Expense date matches a specific travel leg's date range")
                break

    sim = _text_similarity(expense.get("expense_details") or "", trip.get("destination") or "")
    if sim >= 0.5:
        score += 10
        reasons.append("Destination: strong text match")
    elif sim >= 0.2:
        score += 5
        reasons.append("Destination: partial text match")

    return score, reasons


def _dedupe_same_trip(candidates: list[TripCandidate], trips_by_id: dict[int, dict[str, Any]]) -> list[TripCandidate]:
    """Collapses candidates that share the same non-null trip number - they
    describe the same real-world trip (typically an embedded summary and a
    standalone Trip Report for it), not competing evidence. Keeps the
    higher-scoring representative, preferring the embedded one on a tie, and
    folds the other's reasons in so its provenance is never silently lost."""
    groups: dict[str, list[TripCandidate]] = {}
    standalone: list[TripCandidate] = []
    for c in candidates:
        trip_id = (trips_by_id[c.trip_report_id].get("trip_id") or "").strip().upper()
        if trip_id:
            groups.setdefault(trip_id, []).append(c)
        else:
            standalone.append(c)

    result = list(standalone)
    for trip_id, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
            continue
        group.sort(key=lambda c: (trips_by_id[c.trip_report_id].get("is_embedded") is True, c.score), reverse=True)
        winner = group[0]
        others = group[1:]
        merged_reasons = list(winner.reasons)
        for other in others:
            src = "embedded trip summary" if trips_by_id[other.trip_report_id].get("is_embedded") else "standalone Trip Report"
            merged_reasons.append(f"Also corroborated by a matching {src} for trip number {trip_id} (not selected as the primary source)")
        result.append(
            TripCandidate(
                trip_report_id=winner.trip_report_id,
                score=winner.score,
                reasons=merged_reasons,
                merged_trip_report_ids=[other.trip_report_id for other in others],
            )
        )
    return result


def match_batta(expense: dict[str, Any], candidate_trips: list[dict[str, Any]]) -> TripMatchOutcome:
    if not candidate_trips:
        return TripMatchOutcome(
            status="NO_MATCH",
            matched_trip_report_id=None,
            score=None,
            confidence=None,
            reasons=["No candidate trips (embedded or standalone) were available to match against."],
            candidates=[],
        )

    trips_by_id = {t["id"]: t for t in candidate_trips}
    eligible: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    for trip in candidate_trips:
        if _employee_codes_conflict(expense, trip):
            mismatched.append(trip)
        else:
            eligible.append(trip)

    if not eligible:
        return TripMatchOutcome(
            status="NO_MATCH",
            matched_trip_report_id=None,
            score=None,
            confidence=None,
            reasons=[
                f"The only candidate trip(s) found belong to a different employee "
                f"(expected employee code {expense.get('employee_code')!r})."
            ],
            candidates=[],
            employee_mismatch=True,
        )

    scored = []
    for trip in eligible:
        score, reasons = score_pair(expense, trip)
        scored.append(TripCandidate(trip_report_id=trip["id"], score=score, reasons=reasons))
    scored = _dedupe_same_trip(scored, trips_by_id)
    scored.sort(key=lambda c: c.score, reverse=True)

    best = scored[0]
    second = scored[1] if len(scored) > 1 else None

    if best.score < MIN_THRESHOLD:
        return TripMatchOutcome(
            status="NO_MATCH",
            matched_trip_report_id=None,
            score=best.score,
            confidence=None,
            reasons=["No candidate trip scored highly enough to plausibly support this Batta expense."],
            candidates=scored[:3],
        )

    if second and second.score >= MIN_THRESHOLD and (best.score - second.score) < CANDIDATE_MARGIN:
        return TripMatchOutcome(
            status="MULTIPLE_CANDIDATES",
            matched_trip_report_id=None,
            score=best.score,
            confidence=None,
            reasons=["Multiple trips are plausible matches; auditor review required."],
            candidates=scored[:3],
        )

    if best.score >= HIGH_THRESHOLD:
        confidence = "HIGH"
    elif best.score >= MEDIUM_THRESHOLD:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    if confidence == "LOW":
        return TripMatchOutcome(
            status="REVIEW_REQUIRED",
            matched_trip_report_id=None,
            score=best.score,
            confidence=None,
            reasons=best.reasons or ["Best candidate's evidence was too weak to auto-confirm."],
            candidates=scored[:3],
        )

    return TripMatchOutcome(
        status="MATCHED",
        matched_trip_report_id=best.trip_report_id,
        score=best.score,
        confidence=confidence,
        reasons=best.reasons,
        candidates=scored[:3],
    )
