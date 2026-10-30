"""Keep a manual verification attached to the SAME CLAIM when a full reprocess
deletes and recreates a report's Expense rows (new ids).

Why this exists. `ExceptionVerification` is keyed by (expense_report_id,
expense_id, exception_type) and `exception_engine.evaluate_report` re-applies it
by that key. A full reprocess (pipeline._extract_expense_report) deletes the
report's Expense rows and inserts new ones; nothing re-keyed the verification, so
it stayed pointing at an expense that no longer exists and the regenerated
exception came back OPEN. (A single-report test database hides this: SQLite hands
the freed ids straight back, so the old key matches again by coincidence.)

The policy (deliberately conservative - an approval must never follow the wrong
claim, and a changed claim must go back to review):

  * Only expense-level verifications (expense_id NOT NULL) of THIS report are
    considered; report-level ones (expense_id NULL) are never touched.
  * A verification follows a recreated expense only on a CONFIDENT match:
    the same non-NULL s_no, and non-NULL, equal date, normalized category and
    claimed amount (compared as Decimal to two places) on both rows, and the match
    is unambiguous. s_no alone is never enough. There is no renumbering fallback.
  * Anything else - a missing fact, a changed fact, no row with that s_no, an
    ambiguous match - leaves the verification row exactly as it is (nothing is
    deleted or rewritten), the regenerated claim comes back OPEN for an auditor,
    and a system audit event records why.
  * Because evaluate_report matches by id, a verification that stays put must not
    land on a DIFFERENT claim that happens to receive its old id after SQLite id
    reuse. So when such a verification exists, the new Expense rows are given
    explicit ids above every id any verification of the report points at
    (reserve_ids). When none exists the ids are allocated exactly as before.

Everything here runs inside the caller's transaction, never commits, and never
queries or changes another report's rows. See candidate_cleanup.py for the same
scoped-helper style.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.expense_report import Expense
from app.models.validation import ExceptionVerification
from app.services import audit
from app.services.category_map import normalize_category

FACTS_CHANGED = "facts_changed"
NO_ROW_FOR_S_NO = "no_row_for_s_no"
MISSING_FACTS = "missing_facts"
AMBIGUOUS_MATCH = "ambiguous_match"


@dataclass(frozen=True)
class ClaimFacts:
    s_no: int | None
    expense_date: date | None
    category: str | None  # normalized
    amount: Decimal | None  # quantized to two places


def _amount(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def facts_of(expense: Expense) -> ClaimFacts:
    return ClaimFacts(expense.s_no, expense.expense_date, normalize_category(expense.category), _amount(expense.claimed_amount))


@dataclass
class Snapshot:
    """Taken BEFORE the report's Expense rows are deleted."""

    facts_by_old_id: dict[int, ClaimFacts] = field(default_factory=dict)  # old expense id -> its facts (only expenses that carry a verification)
    held_expense_ids: set[int] = field(default_factory=set)  # every non-NULL expense_id a verification of this report points at


@dataclass
class Outcome:
    old_expense_id: int
    carry_to: Expense | None  # the NEW expense the verification follows (None = it stays where it is)
    reason: str | None = None
    changed: tuple[str, ...] = ()
    old: ClaimFacts | None = None  # the old claim's facts (they exist nowhere else once its row is deleted) - for the audit text


@dataclass
class Plan:
    outcomes: list[Outcome] = field(default_factory=list)
    protected_ids: set[int] = field(default_factory=set)  # ids no new expense may take


def snapshot(db: Session, report_id: int) -> Snapshot:
    verifications = (
        db.query(ExceptionVerification)
        .filter(ExceptionVerification.expense_report_id == report_id, ExceptionVerification.expense_id.isnot(None))
        .all()
    )
    held = {v.expense_id for v in verifications}
    old = (
        db.query(Expense).filter(Expense.expense_report_id == report_id, Expense.id.in_(held)).all() if held else []
    )
    return Snapshot({e.id: facts_of(e) for e in old}, held)


def _decide(old_id: int, old: ClaimFacts, by_s_no: dict[int | None, list[Expense]]) -> Outcome:
    if old.s_no is None or old.expense_date is None or old.category is None or old.amount is None:
        return Outcome(old_id, None, MISSING_FACTS, old=old)
    candidates = by_s_no.get(old.s_no, [])
    if not candidates:
        return Outcome(old_id, None, NO_ROW_FOR_S_NO, old=old)
    if len(candidates) > 1:
        return Outcome(old_id, None, AMBIGUOUS_MATCH, old=old)
    new = facts_of(candidates[0])
    if new.expense_date is None or new.category is None or new.amount is None:
        return Outcome(old_id, None, MISSING_FACTS, old=old)
    changed = tuple(
        name
        for name, before, after in (("date", old.expense_date, new.expense_date), ("category", old.category, new.category), ("amount", old.amount, new.amount))
        if before != after
    )
    if changed:
        return Outcome(old_id, None, FACTS_CHANGED, changed, old=old)
    return Outcome(old_id, candidates[0], old=old)


def plan(snap: Snapshot, new_expenses: list[Expense]) -> Plan:
    """Decide, from facts alone (no ids needed), what happens to each verified expense."""
    by_s_no: dict[int | None, list[Expense]] = {}
    for expense in new_expenses:
        by_s_no.setdefault(expense.s_no, []).append(expense)
    outcomes = [_decide(old_id, snap.facts_by_old_id[old_id], by_s_no) for old_id in sorted(snap.facts_by_old_id)]
    # two old claims may never share one new expense: that would be a guess
    targets: dict[int, int] = {}
    for outcome in outcomes:
        if outcome.carry_to is not None:
            targets[id(outcome.carry_to)] = targets.get(id(outcome.carry_to), 0) + 1
    for outcome in outcomes:
        if outcome.carry_to is not None and targets[id(outcome.carry_to)] > 1:
            outcome.carry_to, outcome.reason = None, AMBIGUOUS_MATCH
    stays = snap.held_expense_ids - {o.old_expense_id for o in outcomes if o.carry_to is not None}
    return Plan(outcomes, stays)


def reserve_ids(db: Session, new_expenses: list[Expense], the_plan: Plan) -> None:
    """Only when a verification stays where it is (or an older one is already dormant):
    give the new rows explicit ids above every id such a verification points at, so SQLite
    id reuse can never hand a stranded verification to a different claim. Call after the
    old rows are deleted and before the new ones are added."""
    if not the_plan.protected_ids or not new_expenses:
        return
    current_max = db.query(func.max(Expense.id)).scalar() or 0
    start = max(current_max, max(the_plan.protected_ids)) + 1
    for offset, expense in enumerate(new_expenses):
        expense.id = start + offset


def apply(db: Session, report_id: int, the_plan: Plan) -> None:
    """Call after the new Expense rows are flushed (their ids exist). Re-keys the confident
    matches (a single UPDATE of expense_id on the existing row - verifier, timestamp, note,
    original severity/reason and the row id are untouched) and writes one system audit event
    per verification. Never commits."""
    per_outcome: list[tuple[Outcome, list[ExceptionVerification]]] = []
    for outcome in the_plan.outcomes:
        rows = (
            db.query(ExceptionVerification)
            .filter(ExceptionVerification.expense_report_id == report_id, ExceptionVerification.expense_id == outcome.old_expense_id)
            .order_by(ExceptionVerification.id)
            .all()
        )
        per_outcome.append((outcome, rows))

    moves: list[tuple[ExceptionVerification, int, int]] = []  # (row, old id, new id)
    for outcome, rows in per_outcome:
        if outcome.carry_to is not None:
            new_id = outcome.carry_to.id
            if new_id != outcome.old_expense_id:  # same id (reused) = already attached to the right claim
                moves.extend((row, outcome.old_expense_id, new_id) for row in rows)

    # two phases so a swap of ids can never trip the (report, expense_id, type) unique key mid-way
    for row, _old, _new in moves:
        row.expense_id = None
    db.flush()
    for row, _old, new_id in moves:
        row.expense_id = new_id
    db.flush()
    for row, old_id, new_id in moves:
        audit.log_action(
            db,
            user_id=None,
            action=audit.VERIFICATION_CARRIED_OVER,
            entity_type="ExceptionVerification",
            entity_id=str(row.id),
            details=f"report_id={report_id}; verification_id={row.id}; exception_type={row.exception_type}; old_expense_id={old_id}; new_expense_id={new_id}",
        )

    for outcome, rows in per_outcome:
        if outcome.carry_to is not None:
            continue
        for row in rows:
            audit.log_action(
                db,
                user_id=None,
                action=audit.VERIFICATION_NOT_CARRIED_OVER,
                entity_type="ExceptionVerification",
                entity_id=str(row.id),
                details=_not_carried_details(report_id, row, outcome, outcome.old),
            )


def _not_carried_details(report_id: int, row: ExceptionVerification, outcome: Outcome, old: ClaimFacts | None) -> str:
    parts = [
        f"reason={outcome.reason}",
        f"report_id={report_id}",
        f"verification_id={row.id}",
        f"exception_type={row.exception_type}",
        f"old_expense_id={outcome.old_expense_id}",
    ]
    if old is not None:
        parts += [f"old_s_no={old.s_no}", f"old_date={old.expense_date}", f"old_category={old.category}", f"old_amount={old.amount}"]
    if outcome.changed:
        parts.append("changed=" + ",".join(outcome.changed))
    parts.append(f"verifier_id={row.verified_by_id}")
    return "; ".join(parts)
