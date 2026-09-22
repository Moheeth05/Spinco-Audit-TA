"""Policy Master lookup for validation. The Policy Master table is the only
source of policy configuration - nothing here hardcodes a limit, a basis, or
a document requirement."""

from sqlalchemy.orm import Session

from app.models.policy import PolicyMaster
from app.services.category_map import normalize_category


def find_policy(db: Session, category: str | None) -> PolicyMaster | None:
    """Looks up the active policy for a (possibly noisy) extracted category
    string. Normalization only strips cosmetic extraction artifacts (see
    category_map.normalize_category) - it never maps one real category onto
    an unrelated one, and returns None rather than guessing when nothing
    matches."""
    normalized = normalize_category(category)
    if not normalized:
        return None
    return (
        db.query(PolicyMaster)
        .filter(PolicyMaster.category == normalized, PolicyMaster.status == "ACTIVE")
        .first()
    )


# Document-requirement vocabulary is transcribed verbatim from the Policy Master
# spec (DA/DNA for ACTUAL-basis categories, YES/NO for fixed-limit categories) and
# is never redefined elsewhere in this codebase. Read literally - DA and YES both
# read as "a supporting document is expected"; DNA and NO both read as "it is
# not" - this is the minimal, non-inventive reading of those four values, not an
# invented business rule. Anything outside this vocabulary is left undetermined
# rather than guessed.
DOCUMENT_REQUIRED_VALUES = {"DA", "YES"}
DOCUMENT_NOT_REQUIRED_VALUES = {"DNA", "NO"}


def is_document_required(policy: PolicyMaster) -> bool | None:
    value = (policy.document_required or "").strip().upper()
    if value in DOCUMENT_REQUIRED_VALUES:
        return True
    if value in DOCUMENT_NOT_REQUIRED_VALUES:
        return False
    return None
