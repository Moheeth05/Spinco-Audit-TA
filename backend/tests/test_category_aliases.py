"""FIX: Normalize Equivalent Policy Categories - Local Conveyance vs Conveyance.

Real case: an expense row reading "CONVEYANCE" (Rs.60.00) failed to resolve
against the Policy Master's own "LOCAL CONVEYANCE" entry, producing a false
POLICY NOT FOUND / REVIEW REQUIRED. category_map.py's normalize_category()
now resolves a small, explicit, hand-verified alias table (CATEGORY_ALIASES)
before returning - see that module for the full rationale.
"""

from app.database import SessionLocal
from app.models.policy import PolicyMaster
from app.services.category_map import CATEGORY_ALIASES, POLICY_MASTER_CATEGORIES, normalize_category
from app.services.validation.policy_lookup import find_policy


def test_conveyance_normalizes_to_local_conveyance():
    assert normalize_category("CONVEYANCE") == "LOCAL CONVEYANCE"


def test_local_conveyance_normalizes_to_itself():
    assert normalize_category("LOCAL CONVEYANCE") == "LOCAL CONVEYANCE"


def test_conveyance_alias_is_case_and_whitespace_insensitive():
    assert normalize_category("conveyance") == "LOCAL CONVEYANCE"
    assert normalize_category("  Conveyance  ") == "LOCAL CONVEYANCE"
    assert normalize_category("Local   Conveyance") == "LOCAL CONVEYANCE"
    assert normalize_category("local conveyance") == "LOCAL CONVEYANCE"


def test_every_alias_resolves_to_a_real_policy_master_category():
    """Guards against a future typo in CATEGORY_ALIASES silently mapping a
    real category onto a nonexistent one."""
    for alias, canonical in CATEGORY_ALIASES.items():
        assert canonical in POLICY_MASTER_CATEGORIES, f"{alias!r} -> {canonical!r} is not a real Policy Master category"


def test_find_policy_resolves_conveyance_to_the_local_conveyance_policy(client):
    """The Rs.60 example, end to end against the real seeded Policy Master -
    confirms find_policy() (used by every validation check) no longer
    produces POLICY NOT FOUND for the 'CONVEYANCE' label."""
    db = SessionLocal()
    try:
        policy = find_policy(db, "CONVEYANCE")
        assert policy is not None
        assert policy.category == "LOCAL CONVEYANCE"

        direct = find_policy(db, "LOCAL CONVEYANCE")
        assert direct is not None
        assert direct.id == policy.id
    finally:
        db.close()


def test_conveyance_and_local_conveyance_resolve_to_the_same_policy_limit(client):
    """The correct existing policy limit must still be enforced - aliasing
    must never change what limit applies, only which row is found."""
    db = SessionLocal()
    try:
        via_alias = find_policy(db, "CONVEYANCE")
        via_canonical = find_policy(db, "LOCAL CONVEYANCE")
        assert via_alias.limit_amount == via_canonical.limit_amount
        assert via_alias.reimbursement_basis == via_canonical.reimbursement_basis
        assert via_alias.document_required == via_canonical.document_required
    finally:
        db.close()


def test_genuinely_unknown_category_still_produces_policy_not_found(client):
    db = SessionLocal()
    try:
        assert find_policy(db, "Totally Unknown Category") is None
        assert find_policy(db, None) is None
    finally:
        db.close()


def test_no_duplicate_policy_records_created_by_alias_lookup(client):
    """Aliasing must only ever resolve to the EXISTING Policy Master row -
    never create, duplicate, or modify one."""
    db = SessionLocal()
    try:
        before = db.query(PolicyMaster).filter(PolicyMaster.category == "LOCAL CONVEYANCE").count()
        find_policy(db, "CONVEYANCE")
        find_policy(db, "conveyance")
        find_policy(db, "  Conveyance  ")
        after = db.query(PolicyMaster).filter(PolicyMaster.category == "LOCAL CONVEYANCE").count()
        assert before == after == 1
    finally:
        db.close()


def test_resolve_category_stores_canonical_name_for_the_conveyance_alias(client):
    """Mirrors the existing _resolve_category cosmetic-noise canonicalization
    pattern (test_extraction.py's 'BATTA [1]' -> 'BATTA' case) - an alias is
    the same kind of equivalence, so it resolves through the same existing,
    already-tested chokepoint rather than a special-cased second path, and
    for the same reason that existing behaviour exists: a category must
    always persist as an exact, verbatim Policy Master name so Analytics/
    Excel never fragment one real category into separate CONVEYANCE and
    LOCAL CONVEYANCE buckets."""
    from app.services.extraction.pipeline import _resolve_category

    db = SessionLocal()
    try:
        assert _resolve_category(db, "CONVEYANCE", None) == "LOCAL CONVEYANCE"
        assert _resolve_category(db, "conveyance", None) == "LOCAL CONVEYANCE"
    finally:
        db.close()
