"""Deterministic expense-to-bill matching.

No single signal decides a match - amount, date, vendor/description text
similarity, category compatibility and bill-number references are all
weighted and combined into one score per (expense, bill) pair. An LLM is
never used for the final decision (see services/interfaces.py).

Weights (documented here, the one place they're defined):
    Amount exact match          +40
    Amount exact + unique       +15   (no other bill on the document has this amount)
    Amount close (<=1%/Rs.1)    +20
    Date exact match             +30
    Date within 1 day            +12
    Vendor/description strong    +15   (text similarity >= 0.5)
    Vendor/description partial    +7   (text similarity >= 0.2)
    Category compatible          +10
    Bill number referenced        +5

Decision thresholds:
    score < 25                  -> NO_MATCH
    top two candidates within 12 of each other (both >= 25) -> MULTIPLE_CANDIDATES
    score >= 70                 -> MATCHED, confidence HIGH
    score >= 45                 -> MATCHED, confidence MEDIUM
    25 <= score < 45 (sole candidate) -> REVIEW_REQUIRED (too uncertain to auto-confirm)
"""

import difflib
from dataclasses import dataclass, field
from typing import Any

MIN_THRESHOLD = 25
MEDIUM_THRESHOLD = 45
HIGH_THRESHOLD = 70
CANDIDATE_MARGIN = 12

_CATEGORY_TO_BILL_LABEL = {
    "OWN CAR PETROL": "Fuel",
    "COMPANY CAR PETROL": "Fuel",
    "TWO WHEELER PETROL": "Fuel",
    "LODGING": "Lodging",
    "CAB CHARGES": "Cab/Taxi",
    "LOCAL CONVEYANCE": "Cab/Taxi",
    "STAFF WELFARE": "Food",
    "MARKETING EXPENSES - FOOD": "Food",
    "COURIER CHARGES": "Courier",
    "PRINTING & STATIONERY": "Printing & Stationery",
    "TRAIN / BUS FARE": "Train",
    "TRANSPORT CHARGES": "Bus",
}


def _normalize_category(category: str | None) -> str | None:
    if not category:
        return None
    return category.split("[")[0].strip().upper()


def _category_compatible(expense_category: str | None, bill_category_guess: str | None) -> bool:
    if not expense_category or not bill_category_guess:
        return False
    expected = _CATEGORY_TO_BILL_LABEL.get(_normalize_category(expense_category))
    if expected is None:
        return False
    if bill_category_guess == expected:
        return True
    if expected == "Train" and bill_category_guess == "Bus":
        return True
    if expected == "Bus" and bill_category_guess == "Train":
        return True
    return False


def _text_similarity(a: str, b: str) -> float:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class BillCandidate:
    bill_id: int
    score: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class MatchOutcome:
    status: str  # MATCHED | MULTIPLE_CANDIDATES | NO_MATCH | REVIEW_REQUIRED
    matched_bill_id: int | None
    score: int | None
    confidence: str | None  # HIGH | MEDIUM | LOW
    reasons: list[str]
    candidates: list[BillCandidate]


def score_pair(expense: dict[str, Any], bill: dict[str, Any], amount_is_unique: bool = False) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    claimed = expense.get("claimed_amount")
    bill_amount = bill.get("amount")
    if claimed is not None and bill_amount is not None:
        diff = abs(claimed - bill_amount)
        if diff < 0.01:
            score += 40
            reasons.append("Amount: exact match")
            if amount_is_unique:
                score += 15
                reasons.append("Amount: unique exact match - no other bill on this document shares it")
        elif diff <= max(1.0, claimed * 0.01):
            score += 20
            reasons.append("Amount: close match (minor rounding)")

    exp_date = expense.get("expense_date")
    bill_date = bill.get("bill_date")
    if exp_date and bill_date:
        delta = abs((exp_date - bill_date).days)
        if delta == 0:
            score += 30
            reasons.append("Date: exact match")
        elif delta <= 1:
            score += 12
            reasons.append("Date: within 1 day")

    sim = _text_similarity(expense.get("expense_details") or "", f"{bill.get('vendor') or ''} {bill.get('description') or ''}")
    if sim >= 0.5:
        score += 15
        reasons.append("Vendor/description: strong text match")
    elif sim >= 0.2:
        score += 7
        reasons.append("Vendor/description: partial text match")

    if _category_compatible(expense.get("category"), bill.get("category_guess")):
        score += 10
        reasons.append("Category: compatible")

    bill_number = bill.get("bill_number")
    details = (expense.get("expense_details") or "").lower()
    if bill_number and bill_number.lower() in details:
        score += 5
        reasons.append("Bill number referenced in expense details")

    return score, reasons


def match_expense(expense: dict[str, Any], bills: list[dict[str, Any]]) -> MatchOutcome:
    if not bills:
        return MatchOutcome(
            status="NO_MATCH",
            matched_bill_id=None,
            score=None,
            confidence=None,
            reasons=["No supporting bills were found on this document."],
            candidates=[],
        )

    amount_counts: dict[float, int] = {}
    for bill in bills:
        if bill.get("amount") is not None:
            amount_counts[bill["amount"]] = amount_counts.get(bill["amount"], 0) + 1

    scored = []
    for bill in bills:
        amount_is_unique = bill.get("amount") is not None and amount_counts.get(bill["amount"], 0) == 1
        score, reasons = score_pair(expense, bill, amount_is_unique=amount_is_unique)
        scored.append(BillCandidate(bill_id=bill["id"], score=score, reasons=reasons))
    scored.sort(key=lambda c: c.score, reverse=True)

    best = scored[0]
    second = scored[1] if len(scored) > 1 else None

    if best.score < MIN_THRESHOLD:
        return MatchOutcome(
            status="NO_MATCH",
            matched_bill_id=None,
            score=best.score,
            confidence=None,
            reasons=["No candidate bill scored highly enough to plausibly support this expense."],
            candidates=scored[:3],
        )

    if second and second.score >= MIN_THRESHOLD and (best.score - second.score) < CANDIDATE_MARGIN:
        return MatchOutcome(
            status="MULTIPLE_CANDIDATES",
            matched_bill_id=None,
            score=best.score,
            confidence=None,
            reasons=["Multiple bills are plausible matches; auditor review required."],
            candidates=scored[:3],
        )

    if best.score >= HIGH_THRESHOLD:
        confidence = "HIGH"
    elif best.score >= MEDIUM_THRESHOLD:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    if confidence == "LOW":
        return MatchOutcome(
            status="REVIEW_REQUIRED",
            matched_bill_id=None,
            score=best.score,
            confidence=None,
            reasons=best.reasons or ["Best candidate's evidence was too weak to auto-confirm."],
            candidates=scored[:3],
        )

    return MatchOutcome(
        status="MATCHED",
        matched_bill_id=best.bill_id,
        score=best.score,
        confidence=confidence,
        reasons=best.reasons,
        candidates=scored[:3],
    )
