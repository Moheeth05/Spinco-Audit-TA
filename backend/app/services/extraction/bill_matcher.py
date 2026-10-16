"""Deterministic expense-to-bill matching.

No single signal decides a match - amount, date, vendor/description text
similarity, category compatibility and bill-number references are all
weighted and combined into one score per (expense, bill) pair. An LLM is
never used for the final decision (see services/interfaces.py).

Weights (documented here, the one place they're defined):
    Amount exact match          +40   (also an employee-entered whole-rupee rounding
                                         of a decimal bill total, in EITHER direction -
                                         see money.amounts_match - never a looser
                                         percentage-based tolerance)
    Amount exact + unique       +15   (no other bill on the document has this amount)
    Amount present but wrong    -25   (a known amount that clearly differs is strong
                                         negative evidence, not neutral - stops date/
                                         category alone from looking "plausible")
    Date exact match             +30
    Date within 1 day            +12
    Vendor/description strong    +15   (text similarity >= 0.5)
    Vendor/description partial    +7   (text similarity >= 0.2)
    Category compatible          +10
    Bill number referenced        +5
    Page position matches order  +8    (bill's page lines up with the expense's
                                         position in the table, relative to the
                                         first bill page - a weak tiebreaker only,
                                         since bills may legitimately be out of
                                         order)
    Page position off by one      +3

Decision thresholds:
    score < 25                  -> NO_MATCH
    top two candidates within 12 of each other (both >= 25) -> MULTIPLE_CANDIDATES
    score >= 70                 -> MATCHED, confidence HIGH
    score >= 45                 -> MATCHED, confidence MEDIUM
    25 <= score < 45 (sole candidate) -> REVIEW_REQUIRED (too uncertain to auto-confirm)

This score is a RANKING signal only - it picks which bill (if any) is worth
evaluating for a given expense, and labels how confident that pick is. It is
NOT a gate on whether the candidate's own amount/date/category evidence gets
examined: that independent, field-by-field evaluation happens in
validation/checks.py, against MatchOutcome.candidate_bill_id, for every
status except NO_MATCH (including REVIEW_REQUIRED and MULTIPLE_CANDIDATES -
a candidate that isn't confident enough to auto-confirm, or isn't uniquely
distinguishable from a close second, can still have its own real evidence
checked and shown to the auditor, rather than being discarded outright).
MatchOutcome.matched_bill_id remains the separate, narrower "confirmed"
signal only MATCHED sets - every existing consumer of a confirmed match
(Excel export, API responses, duplicate-bill detection) is unaffected.
"""

import difflib
from dataclasses import dataclass, field, replace
from typing import Any

from app.services import category_map
from app.services.category_map import category_compatible as _category_compatible
from app.services.extraction.money import amounts_match

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
    # The best-scoring candidate's bill id whenever ANY candidate cleared
    # MIN_THRESHOLD - set for every status except NO_MATCH, including
    # REVIEW_REQUIRED and MULTIPLE_CANDIDATES, so validation/checks.py can
    # independently evaluate that candidate's real amount/date/category
    # evidence without waiting for this module's own ranking score to cross
    # the auto-confirm bar (see module docstring: this score ranks
    # candidates, it does not gate whether their evidence gets examined).
    candidate_bill_id: int | None = None
    # Set only by the split-payment fallback (see _find_split_payment_pair) -
    # the two bill ids it used as combined evidence. The caller (bill_pipeline.py)
    # must treat both as claimed, same as a confirmed matched_bill_id, so a
    # LATER expense's own matching/split-payment search can never reuse
    # either bill as if it were still unclaimed evidence.
    split_payment_bill_ids: tuple[int, int] | None = None


def score_pair(
    expense: dict[str, Any],
    bill: dict[str, Any],
    amount_is_unique: bool = False,
    first_bill_page: int | None = None,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    claimed = expense.get("claimed_amount")
    bill_amount = bill.get("amount")
    if claimed is not None and bill_amount is not None:
        # A travel-ticket bill's own total is "all inclusive" and can
        # legitimately be larger than what the employee claims when the bill
        # bundles in a catering charge the company doesn't reimburse as part
        # of the fare (see checks.py's Catering Check, which separately
        # flags this for auditor review) - comparing against
        # (bill total - catering) as well as the raw total means that
        # difference alone doesn't look like a real amount mismatch and
        # doesn't block an otherwise-correct match (Part 17: catering must
        # not break bill matching).
        catering = category_map.detect_catering_amount(bill.get("raw_text"))
        bill_excl_catering = bill_amount - catering if catering else None

        # amounts_match() accepts an exact match OR either whole-rupee
        # neighbour of a decimal bill amount (an employee commonly enters
        # the bill total rounded/truncated to a whole rupee, in either
        # direction) - see money.py for the exact rule. Deliberately NOT a
        # looser percentage-based tolerance.
        direct_match = amounts_match(claimed, bill_amount)
        excl_match = bill_excl_catering is not None and amounts_match(claimed, bill_excl_catering)
        if direct_match or excl_match:
            score += 40
            if direct_match:
                reasons.append(
                    "Amount: exact match" if claimed == bill_amount else "Amount: matches bill total rounded to the nearest rupee"
                )
            else:
                reasons.append(
                    "Amount: exact match once catering is excluded"
                    if claimed == bill_excl_catering
                    else "Amount: matches bill total (catering excluded) rounded to the nearest rupee"
                )
            if amount_is_unique:
                score += 15
                reasons.append("Amount: unique exact match - no other bill on this document shares it")
        else:
            # A bill whose amount IS known and is clearly different is strong
            # negative evidence - real accounting practice means a genuine
            # supporting bill's total essentially always matches the claimed
            # figure. Without this, a bill sharing only the expense's date and
            # category (common when several same-category expenses fall on
            # the same day) could still look "plausible" on those two signals
            # alone even though its amount rules it out, manufacturing false
            # MULTIPLE_CANDIDATES ambiguity between bills that are actually
            # both wrong. A bill whose amount simply couldn't be extracted
            # (bill_amount is None) gets no penalty - that's a different,
            # neutral kind of uncertainty.
            score -= 25
            reasons.append("Amount: present but clearly different from claimed - strong negative signal")

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

    expense_rank = expense.get("expense_rank")
    bill_page = bill.get("page_number")
    if expense_rank is not None and first_bill_page is not None and bill_page is not None:
        expected_page = first_bill_page + (expense_rank - 1)
        offset = abs(bill_page - expected_page)
        if offset == 0:
            score += 8
            reasons.append("Source position: bill page matches the expense's order in the table")
        elif offset == 1:
            score += 3
            reasons.append("Source position: bill page is close to the expense's order in the table")

    return score, reasons


def _describe_bill(bill: dict[str, Any] | None) -> str:
    """A short, evidence-bearing description of a candidate bill for use in
    a REVIEW_REQUIRED/NO_MATCH reason string - "page 12, amount Rs.3,900.00,
    date 2026-08-09" rather than an opaque bill id, so an auditor reading the
    reason alone knows exactly which document to look at (see Part 6:
    every REVIEW_REQUIRED reason must be specific, not generic)."""
    if bill is None:
        return "unknown bill"
    parts = []
    if bill.get("page_number") is not None:
        parts.append(f"page {bill['page_number']}")
    if bill.get("amount") is not None:
        parts.append(f"amount Rs.{bill['amount']:,.2f}")
    if bill.get("bill_date") is not None:
        parts.append(f"date {bill['bill_date']}")
    return ", ".join(parts) if parts else "no distinguishing evidence"


def _find_split_payment_pair(
    expense: dict[str, Any], bills: list[dict[str, Any]], excluded_bill_ids: set[int] | None = None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Fallback evidence check for when NO single bill plausibly supports
    this expense: two separate payments on the same document, both dated on
    (or within a day of) the expense date, whose amounts sum EXACTLY to the
    claimed amount, are real, named evidence worth a human review - not a
    confirmed match (this app's schema records one candidate bill per
    expense, not a combination), but clearly better than a silent MISSING
    BILL that hides this evidence entirely.

    Confirmed real case: an expense claims Rs.3,500 for a car booking; no
    single bill on the document comes close, but two UPI payments to the
    same payee, both dated the claimed day, are Rs.1,500 and Rs.2,000 -
    exactly Rs.3,500 combined (a fare evidently paid in two instalments).

    Deliberately conservative: requires an EXACT amount sum (the same
    whole-rupee tolerance as a single-bill match, via amounts_match) AND
    both dates within 1 day of the claimed date - a coincidental pair of
    amounts summing to a third, specific, already-claimed figure while both
    also fall on the right day is not a realistic accident. Returns the
    lowest-bill-id qualifying pair for deterministic, reproducible output."""
    claimed = expense.get("claimed_amount")
    exp_date = expense.get("expense_date")
    if claimed is None or exp_date is None:
        return None

    excluded = excluded_bill_ids or set()
    eligible = sorted(
        (
            b
            for b in bills
            if b["id"] not in excluded
            and b.get("amount") is not None
            and b.get("bill_date") is not None
            and abs((exp_date - b["bill_date"]).days) <= 1
        ),
        key=lambda b: b["id"],
    )
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            a, b = eligible[i], eligible[j]
            if amounts_match(claimed, a["amount"] + b["amount"]):
                return a, b
    return None


def match_expense(
    expense: dict[str, Any],
    bills: list[dict[str, Any]],
    first_bill_page: int | None = None,
    excluded_bill_ids: set[int] | None = None,
    ineligible_bill_ids: set[int] | None = None,
) -> MatchOutcome:
    """`excluded_bill_ids` only feeds the split-payment fallback (bills
    already claimed by an earlier expense must not be reused as split
    evidence). `ineligible_bill_ids` removes bills from candidacy entirely -
    used only when re-evaluating a claim that lost a competition for a shared
    bill (see reconcile_shared_bill_allocations). Amount uniqueness is still
    computed over ALL bills, so removing a bill never inflates another
    bill's "unique amount" credit."""
    if not bills:
        return MatchOutcome(
            status="NO_MATCH",
            matched_bill_id=None,
            score=None,
            confidence=None,
            reasons=["No supporting bills were found on this document."],
            candidates=[],
            candidate_bill_id=None,
        )

    ineligible = ineligible_bill_ids or set()
    if ineligible:
        excluded_bill_ids = set(excluded_bill_ids or set()) | ineligible
    candidates_pool = [b for b in bills if b["id"] not in ineligible]
    if not candidates_pool:
        return MatchOutcome(
            status="NO_MATCH",
            matched_bill_id=None,
            score=None,
            confidence=None,
            reasons=["No other supporting bill remains once the bill allocated to a better-fitting claim is set aside."],
            candidates=[],
            candidate_bill_id=None,
        )

    bills_by_id = {bill["id"]: bill for bill in bills}
    amount_counts: dict[float, int] = {}
    for bill in bills:
        if bill.get("amount") is not None:
            amount_counts[bill["amount"]] = amount_counts.get(bill["amount"], 0) + 1

    scored = []
    for bill in candidates_pool:
        amount_is_unique = bill.get("amount") is not None and amount_counts.get(bill["amount"], 0) == 1
        score, reasons = score_pair(expense, bill, amount_is_unique=amount_is_unique, first_bill_page=first_bill_page)
        scored.append(BillCandidate(bill_id=bill["id"], score=score, reasons=reasons))
    # Tie-break on bill_id, not just score: validation/checks.py now
    # evaluates the top candidate's own amount/date/category evidence (see
    # MatchOutcome.candidate_bill_id), so which specific bill wins a
    # genuine tie must be reproducible every time, not an accident of
    # whatever order `bills` happened to arrive in - "deterministic,
    # explainable, auditable" applies to candidate selection too, not only
    # to the final PASS/EXCEPTION/REVIEW_REQUIRED decision.
    scored.sort(key=lambda c: (-c.score, c.bill_id))

    best = scored[0]
    second = scored[1] if len(scored) > 1 else None
    # Only NO_MATCH (best below the plausibility floor) leaves this None -
    # every other status, including REVIEW_REQUIRED and MULTIPLE_CANDIDATES,
    # exposes the top candidate so its real field evidence can still be
    # evaluated (see MatchOutcome.candidate_bill_id).
    candidate_bill_id = best.bill_id if best.score >= MIN_THRESHOLD else None

    if best.score < MIN_THRESHOLD:
        pair = _find_split_payment_pair(expense, bills, excluded_bill_ids=excluded_bill_ids)
        if pair is not None:
            first, second = pair
            return MatchOutcome(
                status="REVIEW_REQUIRED",
                matched_bill_id=None,
                score=best.score,
                confidence=None,
                reasons=[
                    f"No single bill matches, but two separate payments together plausibly support this expense: "
                    f"{_describe_bill(first)} and {_describe_bill(second)} - their amounts sum to the claimed "
                    f"total and both fall on the claimed date. This app matches one bill per expense, so this "
                    f"needs human confirmation rather than an automated match."
                ],
                candidates=scored[:3],
                candidate_bill_id=None,
                split_payment_bill_ids=(first["id"], second["id"]),
            )
        claimed = expense.get("claimed_amount")
        exp_date = expense.get("expense_date")
        best_desc = _describe_bill(bills_by_id.get(best.bill_id))
        expense_bits = []
        if claimed is not None:
            expense_bits.append(f"claimed Rs.{claimed:,.2f}")
        if exp_date is not None:
            expense_bits.append(f"date {exp_date}")
        expense_desc = f" ({', '.join(expense_bits)})" if expense_bits else ""
        return MatchOutcome(
            status="NO_MATCH",
            matched_bill_id=None,
            score=best.score,
            confidence=None,
            reasons=[
                f"No candidate bill scored highly enough to plausibly support this expense{expense_desc}. "
                f"Closest candidate: {best_desc} (score {best.score}/{MIN_THRESHOLD} needed)."
            ],
            candidates=scored[:3],
            candidate_bill_id=None,
        )

    # A bill whose own evidence names a DIFFERENT service than the claimed
    # category (a fuel bill against a CAB CHARGES claim) is not "similarly
    # plausible" to one that fits the category, even when the +10 category
    # credit alone leaves the scores inside the margin - this is what lets
    # same-amount, same-day claims in different categories each find their own
    # bill. Only applies when the leading bill positively fits the category;
    # a rival with NO category evidence is never discounted.
    if second and _category_supports(expense, bills_by_id.get(best.bill_id)):
        rivals = [
            c
            for c in scored[1:]
            if c.score >= MIN_THRESHOLD
            and (best.score - c.score) < CANDIDATE_MARGIN
            and not _category_conflicts(expense, bills_by_id[c.bill_id])
        ]
        second = rivals[0] if rivals else None

    if second and second.score >= MIN_THRESHOLD and (best.score - second.score) < CANDIDATE_MARGIN:
        best_desc = _describe_bill(bills_by_id.get(best.bill_id))
        second_desc = _describe_bill(bills_by_id.get(second.bill_id))
        return MatchOutcome(
            status="MULTIPLE_CANDIDATES",
            matched_bill_id=None,
            score=best.score,
            confidence=None,
            reasons=[
                f"Two or more bills are similarly plausible matches: {best_desc} (score {best.score}) and "
                f"{second_desc} (score {second.score}) - neither is confidently distinguishable using the "
                f"available evidence."
            ],
            candidates=scored[:3],
            candidate_bill_id=candidate_bill_id,
        )

    if best.score >= HIGH_THRESHOLD:
        confidence = "HIGH"
    elif best.score >= MEDIUM_THRESHOLD:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    if confidence == "LOW":
        best_desc = _describe_bill(bills_by_id.get(best.bill_id))
        weak_evidence = "; ".join(best.reasons) if best.reasons else "no supporting signals matched"
        return MatchOutcome(
            status="REVIEW_REQUIRED",
            matched_bill_id=None,
            score=best.score,
            confidence=None,
            reasons=[
                f"Best candidate ({best_desc}, score {best.score}/{MEDIUM_THRESHOLD} needed to auto-confirm) "
                f"is too weak to auto-confirm: {weak_evidence}."
            ],
            candidates=scored[:3],
            candidate_bill_id=candidate_bill_id,
        )

    return MatchOutcome(
        status="MATCHED",
        matched_bill_id=best.bill_id,
        score=best.score,
        confidence=confidence,
        reasons=best.reasons,
        candidates=scored[:3],
        candidate_bill_id=candidate_bill_id,
    )


def _amount_supported(expense: dict[str, Any], bill: dict[str, Any]) -> bool:
    """The bill's own, READABLE amount agrees with the claim - same rule as
    score_pair (exact, whole-rupee rounding, or exact once catering is
    excluded). An unreadable amount (None) never counts as support."""
    claimed, amount = expense.get("claimed_amount"), bill.get("amount")
    if claimed is None or amount is None:
        return False
    if amounts_match(claimed, amount):
        return True
    catering = category_map.detect_catering_amount(bill.get("raw_text"))
    return bool(catering) and amounts_match(claimed, amount - catering)


def _date_delta(expense: dict[str, Any], bill: dict[str, Any]) -> int | None:
    exp_date, bill_date = expense.get("expense_date"), bill.get("bill_date")
    if not exp_date or not bill_date:
        return None
    return abs((exp_date - bill_date).days)


def _category_conflicts(expense: dict[str, Any], bill: dict[str, Any]) -> bool:
    """The bill's own extracted evidence names a service that contradicts
    the claimed category (e.g. a Lodging page for a CAB CHARGES claim). A
    bill with no category evidence at all is NOT a conflict."""
    expected = category_map.expected_bill_label(expense.get("category"))
    guess = bill.get("category_guess")
    return bool(expected and guess and not _category_compatible(expense.get("category"), guess))


def _category_supports(expense: dict[str, Any], bill: dict[str, Any] | None) -> bool:
    """The bill's own extracted evidence positively fits the claimed category
    (a bill with no category evidence neither supports nor contradicts it)."""
    if bill is None:
        return False
    expected = category_map.expected_bill_label(expense.get("category"))
    guess = bill.get("category_guess")
    return bool(expected and guess and _category_compatible(expense.get("category"), guess))


def _claim_label(expense: dict[str, Any], index: int) -> str:
    return f"S.No {expense['s_no']}" if expense.get("s_no") is not None else f"expense #{index + 1}"


def _order_key(expense: dict[str, Any], index: int) -> tuple[int, int]:
    s_no = expense.get("s_no")
    return (s_no if s_no is not None else index, index)


def _independent_same_day_candidate(
    expense: dict[str, Any], bills: list[dict[str, Any]], first_bill_page: int | None, ineligible: set[int]
) -> bool:
    """Another bill, dated exactly the claim's date and not contradicting its
    category, that could independently back this claim (typically because
    its own amount is unreadable). Its existence means 'one document reused'
    cannot be told apart from 'two separate bills'."""
    alt = match_expense(expense, bills, first_bill_page=first_bill_page, ineligible_bill_ids=ineligible)
    if alt.candidate_bill_id is None:
        return False
    alt_bill = next((b for b in bills if b["id"] == alt.candidate_bill_id), None)
    return alt_bill is not None and _date_delta(expense, alt_bill) == 0 and not _category_conflicts(expense, alt_bill)


def _demote_to_review(
    index: int,
    bill: dict[str, Any],
    note: str,
    expenses: list[dict[str, Any] | None],
    bills: list[dict[str, Any]],
    outcomes: list[MatchOutcome | None],
    lost: dict[int, set[int]],
    first_bill_page: int | None,
) -> None:
    """A claim that lost a shared bill is re-evaluated on its own evidence
    against the bills that remain. It is only ever confirmed again if the
    remaining bill is independently readable (known amount) - an unreadable
    amount, a weak or ambiguous result, or a category that contradicts the
    claim all end as a review state, never an automatic match."""
    expense = expenses[index]
    lost.setdefault(index, set()).add(bill["id"])
    rematch = match_expense(expense, bills, first_bill_page=first_bill_page, ineligible_bill_ids=lost[index])
    alt_bill = next((b for b in bills if b["id"] == rematch.candidate_bill_id), None)

    if rematch.status == "MATCHED" and alt_bill is not None and alt_bill.get("amount") is not None:
        outcomes[index] = replace(rematch, reasons=[note, *rematch.reasons])
        return

    if rematch.split_payment_bill_ids is not None:
        outcomes[index] = replace(rematch, reasons=[note, *rematch.reasons])
        return

    detail = "No other bill plausibly supports this claim."
    candidate_id = bill["id"]
    if alt_bill is not None:
        parts = [_describe_bill(alt_bill)]
        if alt_bill.get("amount") is None:
            parts.append("its amount could not be read, so it is only an uncertain candidate, not a confirmed match")
        if _category_conflicts(expense, alt_bill):
            parts.append(
                f"its own evidence ({alt_bill.get('category_guess')}) does not fit {expense.get('category')}, "
                "so it is not presented as this claim's bill"
            )
        else:
            candidate_id = alt_bill["id"]
        detail = "Nearest remaining candidate: " + "; ".join(parts) + "."
    status = "MULTIPLE_CANDIDATES" if rematch.status == "MULTIPLE_CANDIDATES" else "REVIEW_REQUIRED"
    outcomes[index] = MatchOutcome(
        status=status,
        matched_bill_id=None,
        score=rematch.score if rematch.score is not None else outcomes[index].score,
        confidence=None,
        reasons=[f"{note} {detail}"],
        candidates=rematch.candidates,
        candidate_bill_id=candidate_id,
    )


def reconcile_shared_bill_allocations(
    expenses: list[dict[str, Any] | None],
    bills: list[dict[str, Any]],
    outcomes: list[MatchOutcome | None],
    first_bill_page: int | None = None,
) -> list[MatchOutcome | None]:
    """Resolves a bill that more than one claim confidently chose, using the
    evidence rather than processing order or S.No.

    Root cause this fixes: each expense used to pick its best bill on its own
    evidence and nothing reconciled two claims picking the SAME bill, so the
    Duplicate Bill Check flagged the claim with the higher S.No - not the
    claim the bill actually fits - and a claim that merely resembled another
    (same amount, different date; the bill that really belongs to it having
    an unreadable amount) stayed 'confidently matched' to someone else's bill.

    For each contested bill:
      * 'Strong' claimants have a readable bill amount that agrees AND an
        exact date match - the well-supported evidence a genuine claim has.
      * Two or more strong claimants are equally supported: that is genuine
        reuse of one document, so they all stay matched and the Duplicate Bill
        Check flags it - UNLESS another bill dated exactly that day could
        independently back one of them (e.g. its amount is unreadable); then
        the evidence cannot separate 'reused' from 'two bills' and none of them
        is confidently allocated (review).
      * Exactly one strong claimant owns the bill.
      * With no strong claimant, the clearly better score (>= CANDIDATE_MARGIN,
        the module's existing 'too close to call' band) owns it; a near-tie
        leaves nobody confidently allocated.
    Every non-owner is re-evaluated on its own evidence (_demote_to_review).
    Never reassigns a bill merely because it is unclaimed, and never turns an
    uncertain result into a match."""
    outcomes = list(outcomes)
    bills_by_id = {b["id"]: b for b in bills}
    lost: dict[int, set[int]] = {}

    for _ in range(len(outcomes) + 1):
        groups: dict[int, list[int]] = {}
        for i, outcome in enumerate(outcomes):
            if outcome is not None and outcome.status == "MATCHED" and outcome.matched_bill_id is not None:
                groups.setdefault(outcome.matched_bill_id, []).append(i)
        contested = {bill_id: idxs for bill_id, idxs in groups.items() if len(idxs) > 1}
        if not contested:
            break

        changed = False
        for bill_id in sorted(contested):
            claimants = [
                i for i in contested[bill_id] if outcomes[i].status == "MATCHED" and outcomes[i].matched_bill_id == bill_id
            ]
            if len(claimants) < 2:
                continue
            bill = bills_by_id[bill_id]
            by_fit = sorted(claimants, key=lambda i: (-(outcomes[i].score or 0), _order_key(expenses[i], i)))
            strong = [i for i in claimants if _amount_supported(expenses[i], bill) and _date_delta(expenses[i], bill) == 0]

            if len(strong) >= 2:
                undecidable = any(
                    _independent_same_day_candidate(expenses[i], bills, first_bill_page, lost.get(i, set()) | {bill_id})
                    for i in strong
                )
                owners = [] if undecidable else strong
            elif len(strong) == 1:
                owners = strong
            else:
                top, runner_up = by_fit[0], by_fit[1]
                clear = (outcomes[top].score or 0) - (outcomes[runner_up].score or 0) >= CANDIDATE_MARGIN
                owners = [top] if clear else []

            others = [i for i in claimants if i not in owners]
            if not others:
                continue
            page = bill.get("page_number")
            labels = ", ".join(_claim_label(expenses[i], i) for i in by_fit)

            if owners:
                owner_text = ", ".join(_claim_label(expenses[i], i) for i in owners)
                why = (
                    "exact amount and exact date"
                    if all(i in strong for i in owners)
                    else f"best overall match (score {outcomes[owners[0]].score})"
                )
                for i in others:
                    note = (
                        f"Bill page {page} was chosen by more than one claim ({labels}); it fits {owner_text} better "
                        f"({why}), so it is not confidently allocated to {_claim_label(expenses[i], i)} "
                        f"(score {outcomes[i].score})."
                    )
                    _demote_to_review(i, bill, note, expenses, bills, outcomes, lost, first_bill_page)
                    changed = True
            else:
                for i in others:
                    outcomes[i] = replace(
                        outcomes[i],
                        status="REVIEW_REQUIRED",
                        matched_bill_id=None,
                        confidence=None,
                        reasons=[
                            f"Bill page {page} is an equally good match for {labels}, and the evidence cannot show whether "
                            "it is one document reused or separate bills, so no claim is confidently allocated it."
                        ],
                        candidate_bill_id=bill_id,
                    )
                    changed = True
        if not changed:
            break
    return outcomes


def _strong_bill_ids(expense: dict[str, Any], bills: list[dict[str, Any]], excluded: set[int]) -> list[dict[str, Any]]:
    """Bills whose own READABLE amount agrees with the claim, dated exactly
    the claim's date, and not contradicting its category - i.e. each could,
    on evidence alone, be this claim's bill."""
    return [
        b
        for b in bills
        if b["id"] not in excluded
        and _amount_supported(expense, b)
        and _date_delta(expense, b) == 0
        and not _category_conflicts(expense, b)
    ]


def assign_by_exclusion(
    expenses: list[dict[str, Any] | None],
    bills: list[dict[str, Any]],
    outcomes: list[MatchOutcome | None],
    first_bill_page: int | None = None,
) -> list[MatchOutcome | None]:
    """Resolves a near-tie when every competing bill but one is spoken for.

    A claim left at MULTIPLE_CANDIDATES because two or more bills are 'strong'
    for it (readable agreeing amount + exact date + no category contradiction)
    can still be settled by necessity: a bill that is the ONLY strong option
    for some OTHER claim cannot also be this claim's bill. If exactly one of
    this claim's strong bills remains, it is the answer.

    Real case (ER-38784): Rs.96 claimed 13 July is strong for the Rs.96.21 and
    the Rs.95.18 receipts of that day, but the Rs.95.18 receipt is the only
    possible bill for the Rs.95 claim of the same day - so the Rs.96 claim can
    only be the Rs.96.21 receipt. Nothing is guessed: it is the only assignment
    in which every claim keeps a bill.

    Sound by construction - it only ever acts when
      * the claim is MULTIPLE_CANDIDATES with >= 2 strong bills;
      * a strong bill is excluded only because another claim has NO other
        strong bill; exactly one strong bill remains afterwards;
      * that bill is not already confidently owned by a different claim, and
        its pairing scores at least MEDIUM.
    Unreadable-amount bills are never strong, so never chosen here. Repeats
    until nothing changes, so a resolved claim can free the next one."""
    outcomes = list(outcomes)
    amount_counts: dict[float, int] = {}
    for bill in bills:
        if bill.get("amount") is not None:
            amount_counts[bill["amount"]] = amount_counts.get(bill["amount"], 0) + 1
    strong_by_claim = {
        i: _strong_bill_ids(expense, bills, set()) for i, expense in enumerate(expenses) if expense is not None
    }

    for _ in range(len(outcomes) + 1):
        changed = False
        owned_by = {
            o.matched_bill_id: i
            for i, o in enumerate(outcomes)
            if o is not None and o.status == "MATCHED" and o.matched_bill_id is not None
        }
        for i, outcome in enumerate(outcomes):
            if outcome is None or outcome.status != "MULTIPLE_CANDIDATES" or i not in strong_by_claim:
                continue
            strong = strong_by_claim[i]
            if len(strong) < 2:
                continue
            spoken_for = {
                b["id"] for j, other in strong_by_claim.items() if j != i and len(other) == 1 for b in other
            }
            free = [b for b in strong if b["id"] not in spoken_for]
            if len(free) != 1 or len(free) == len(strong):
                continue
            bill = free[0]
            if owned_by.get(bill["id"], i) != i:
                continue
            score, reasons = score_pair(
                expenses[i], bill, amount_is_unique=amount_counts.get(bill["amount"], 0) == 1, first_bill_page=first_bill_page
            )
            if score < MEDIUM_THRESHOLD:
                continue
            taken = [b for b in strong if b["id"] in spoken_for]
            needed_by = sorted(
                {_claim_label(expenses[j], j) for j, other in strong_by_claim.items() if j != i and len(other) == 1 and other[0]["id"] in {t["id"] for t in taken}}
            )
            note = (
                f"{_claim_label(expenses[i], i)} fits more than one bill on its date, but "
                f"page(s) {', '.join(str(b.get('page_number')) for b in taken)} can only be the bill of "
                f"{', '.join(needed_by)} (no other bill fits that claim), so page {bill.get('page_number')} is the "
                f"only bill left for it."
            )
            outcomes[i] = replace(
                outcome,
                status="MATCHED",
                matched_bill_id=bill["id"],
                score=score,
                confidence="HIGH" if score >= HIGH_THRESHOLD else "MEDIUM",
                reasons=[note, *reasons],
                candidate_bill_id=bill["id"],
            )
            changed = True
        if not changed:
            break
    return outcomes


def assign_interchangeable_claims(
    expenses: list[dict[str, Any] | None],
    bills: list[dict[str, Any]],
    outcomes: list[MatchOutcome | None],
    first_bill_page: int | None = None,
) -> list[MatchOutcome | None]:
    """k separate claims, exactly k equally-fitting bills.

    Several legitimate claims can share an amount, date and category (two
    Rs.500 cab rides on one day, each with its own UPI receipt). Matched
    independently, every one of those claims sees the same k bills scoring
    identically and is left at 'similarly plausible matches - not
    distinguishable' (MULTIPLE_CANDIDATES), although nothing is wrong: the
    set-level evidence is complete - k claims, k distinct bills.

    Only that exact situation is resolved here. Every one of these must hold,
    otherwise nothing changes and the claims stay in review:
      * each claim is currently MULTIPLE_CANDIDATES;
      * the claim's 'strong' bills (readable agreeing amount + exact date + no
        category contradiction) are the same set K for every claim in the
        group, with |K| == number of claims in the group;
      * each claim scores every bill in K identically (the evidence really
        cannot tell them apart - a difference of even one point means it can);
      * no other claim, whatever its status, could also use a bill in K, and
        no bill in K is already confidently owned by another claim.
    The claims are then paired with the bills in document order (S.No <-> page
    number). Which bill goes with which claim carries no evidential weight -
    the reasons say so - but every claim ends up with its own distinct bill, so
    no bill is shared and no genuine claim is reported as having no bill. An
    unreadable-amount bill is never 'strong', so it can never be assigned here.
    """
    outcomes = list(outcomes)
    owned = {
        o.matched_bill_id for o in outcomes if o is not None and o.status == "MATCHED" and o.matched_bill_id is not None
    }
    amount_counts: dict[float, int] = {}
    for bill in bills:
        if bill.get("amount") is not None:
            amount_counts[bill["amount"]] = amount_counts.get(bill["amount"], 0) + 1

    strong_by_claim: dict[int, list[dict[str, Any]]] = {}
    for i, expense in enumerate(expenses):
        if expense is not None:
            strong_by_claim[i] = _strong_bill_ids(expense, bills, owned)

    groups: dict[frozenset[int], list[int]] = {}
    for i, outcome in enumerate(outcomes):
        if outcome is None or outcome.status != "MULTIPLE_CANDIDATES" or i not in strong_by_claim:
            continue
        strong = strong_by_claim[i]
        if len(strong) < 2:
            continue
        scores = {
            b["id"]: score_pair(
                expenses[i], b, amount_is_unique=amount_counts.get(b["amount"], 0) == 1, first_bill_page=first_bill_page
            )[0]
            for b in strong
        }
        if len(set(scores.values())) != 1:
            continue
        groups.setdefault(frozenset(scores), []).append(i)

    for bill_ids, idxs in groups.items():
        if len(idxs) != len(bill_ids):
            continue
        competing = [
            j for j, strong in strong_by_claim.items() if j not in idxs and any(b["id"] in bill_ids for b in strong)
        ]
        if competing:
            continue
        ordered_claims = sorted(idxs, key=lambda i: _order_key(expenses[i], i))
        ordered_bills = sorted(
            (b for b in bills if b["id"] in bill_ids), key=lambda b: (b.get("page_number") or 0, b["id"])
        )
        score = score_pair(
            expenses[ordered_claims[0]],
            ordered_bills[0],
            amount_is_unique=amount_counts.get(ordered_bills[0]["amount"], 0) == 1,
            first_bill_page=first_bill_page,
        )[0]
        if score < MEDIUM_THRESHOLD:
            continue
        confidence = "HIGH" if score >= HIGH_THRESHOLD else "MEDIUM"
        labels = ", ".join(_claim_label(expenses[i], i) for i in ordered_claims)
        pages = ", ".join(str(b.get("page_number")) for b in ordered_bills)
        for i, bill in zip(ordered_claims, ordered_bills):
            _, pair_reasons = score_pair(
                expenses[i],
                bill,
                amount_is_unique=amount_counts.get(bill["amount"], 0) == 1,
                first_bill_page=first_bill_page,
            )
            note = (
                f"{labels} are separate claims with the same amount, date and category, and the document holds exactly "
                f"{len(ordered_bills)} equally fitting bills (pages {pages}); each claim is supported by its own bill. "
                f"The evidence cannot tell which bill belongs to which claim, so they were paired in document order "
                f"- the pairing does not affect the result."
            )
            outcomes[i] = replace(
                outcomes[i],
                status="MATCHED",
                matched_bill_id=bill["id"],
                score=score,
                confidence=confidence,
                reasons=[note, *pair_reasons],
                candidate_bill_id=bill["id"],
            )
    return outcomes


def allocate_bills(
    expenses: list[dict[str, Any] | None],
    bills: list[dict[str, Any]],
    first_bill_page: int | None = None,
) -> list[MatchOutcome | None]:
    """Whole-report allocation: the single entry point bill_pipeline.py uses.
    `expenses` is in report order; a None entry (e.g. BATTA, which has no
    supporting-document requirement) is skipped and yields a None outcome.
    Each expense is first matched on its own evidence (claimed bills only
    constrain the split-payment fallback, exactly as before), then any bill
    that ended up confidently allocated to more than one claim is reconciled
    by evidence - see reconcile_shared_bill_allocations."""
    claimed: set[int] = set()
    outcomes: list[MatchOutcome | None] = []
    for expense in expenses:
        if expense is None:
            outcomes.append(None)
            continue
        outcome = match_expense(expense, bills, first_bill_page=first_bill_page, excluded_bill_ids=claimed)
        if outcome.status == "MATCHED" and outcome.matched_bill_id is not None:
            claimed.add(outcome.matched_bill_id)
        elif outcome.split_payment_bill_ids is not None:
            claimed.update(outcome.split_payment_bill_ids)
        outcomes.append(outcome)
    outcomes = reconcile_shared_bill_allocations(expenses, bills, outcomes, first_bill_page=first_bill_page)
    outcomes = assign_by_exclusion(expenses, bills, outcomes, first_bill_page=first_bill_page)
    return assign_interchangeable_claims(expenses, bills, outcomes, first_bill_page=first_bill_page)
