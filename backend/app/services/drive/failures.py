"""How an upload failure is classified, and the retry policy that follows from the class (Phase 3A.2-B).

One question decides everything: does this failure say something about THIS upload, or about the world around it?

  * A genuine TRANSIENT failure (Drive answered 429/5xx, the network dropped, a listing was incomplete) is an attempt that really happened and
    really failed. It consumes the retry budget (`attempts`), waits out a doubling backoff and ends in FAILED at the configured limit.
  * Everything that is not the upload's own fault is a DEFERRAL: sign-in trouble, a destination the guard will not write to, a missing folder, a
    permission or quota refusal. It consumes NO budget (a long Google outage must not abandon documents that are perfectly fine); it is counted in
    `deferrals`, retried on a growing schedule and, when it lasts, shown as needing attention rather than failed.
  * A revoked authorization (AUTH_PERMANENT) is parked: no budget, no lease, no automatic retry until an administrator acts.
  * A PERMANENT or INTEGRITY failure ends in FAILED at once and is never retried automatically.

The mapping is deliberately explicit and exhaustive: every guard reason code has an entry (a test enforces it), and anything unrecognised falls to
the most cautious reading, never to "consume budget and retry forever".
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.drive.auth import DriveAuthError, DriveAuthTransientError
from app.services.drive.config import DriveConfigError
from app.services.drive.errors import (
    DriveDestinationMissing,
    DriveError,
    DriveIdentityMismatch,
    DriveIncompleteListing,
    DriveIntegrityError,
    DrivePermissionError,
    DriveQuotaError,
    DriveTransientError,
)
from app.services.drive.guard import DriveTargetError
from app.services.drive.storage import DriveFolderMissing

TRANSIENT = "transient"
AUTH_DEFERRAL = "auth_deferral"
OUTAGE = "outage"
CONFIGURATION = "configuration"
SECURITY = "security"
PERMISSION = "permission"
QUOTA = "quota"
AUTH_PERMANENT = "auth_permanent"
PERMANENT = "permanent"
INTEGRITY = "integrity"
ALL_CLASSES = (TRANSIENT, AUTH_DEFERRAL, OUTAGE, CONFIGURATION, SECURITY, PERMISSION, QUOTA, AUTH_PERMANENT, PERMANENT, INTEGRITY)

# Waits for DEFERRALS (they grow with `deferrals`, not with `attempts`). Quick problems are looked at again quickly; the ones a person has to fix
# (configuration, security, permission, quota) are looked at rarely, so a standing problem does not become a standing stream of requests.
FAST_DEFERRAL_BASE_SECONDS, FAST_DEFERRAL_CAP_SECONDS = 60.0, 900.0
SLOW_DEFERRAL_BASE_SECONDS, SLOW_DEFERRAL_CAP_SECONDS = 900.0, 3600.0
NEEDS_ATTENTION_AFTER_SECONDS = 24 * 3600.0  # a deferral episode this old is reported as needing attention (the record itself stays UPLOADING)
JITTER_FRACTION = 0.10  # every scheduled wait is stretched or shrunk by up to this much, so records that failed together do not all return together


@dataclass(frozen=True)
class Policy:
    counts_budget: bool  # the attempt claimed for this try stays charged (otherwise it is refunded)
    terminal: bool  # the record ends in FAILED now
    parked: bool  # excluded from AUTOMATIC retries until an administrator acts
    deferral: str | None  # "fast" | "slow" backoff track for deferrals; None = not a deferral


POLICIES: dict[str, Policy] = {
    TRANSIENT: Policy(counts_budget=True, terminal=False, parked=False, deferral=None),  # FAILED only when the budget is used up (decided by the manager)
    AUTH_DEFERRAL: Policy(counts_budget=False, terminal=False, parked=False, deferral="fast"),
    OUTAGE: Policy(counts_budget=False, terminal=False, parked=False, deferral="fast"),
    CONFIGURATION: Policy(counts_budget=False, terminal=False, parked=False, deferral="slow"),
    SECURITY: Policy(counts_budget=False, terminal=False, parked=False, deferral="slow"),
    PERMISSION: Policy(counts_budget=False, terminal=False, parked=False, deferral="slow"),
    QUOTA: Policy(counts_budget=False, terminal=False, parked=False, deferral="slow"),
    AUTH_PERMANENT: Policy(counts_budget=False, terminal=False, parked=True, deferral=None),
    PERMANENT: Policy(counts_budget=True, terminal=True, parked=False, deferral=None),
    INTEGRITY: Policy(counts_budget=True, terminal=True, parked=False, deferral=None),
}

# One entry for EVERY reason code of the destination guard (tests compare this table with guard._MESSAGES).
GUARD_REASON_CLASS: dict[str, str] = {
    "unverifiable": OUTAGE,  # Google gave no usable answer: not a statement about the document
    "not_configured": CONFIGURATION,
    "destination_missing": CONFIGURATION,
    "not_folder": CONFIGURATION,
    "trashed": CONFIGURATION,
    "no_capability": CONFIGURATION,
    "hierarchy": CONFIGURATION,
    "destination_denied": PERMISSION,
    "my_drive": SECURITY,
    "wrong_drive": SECURITY,
    "write_outside_drive": SECURITY,
    # An existing object that would be adopted as "ours" is not in the expected drive: retrying cannot make it ours.
    "adopt_my_drive": PERMANENT,
    "adopt_wrong_drive": PERMANENT,
    "adopt_trashed": PERMANENT,
}


def classify(exc: BaseException) -> str:
    """The failure class of an exception raised while uploading or completing an upload. Total: every exception gets a class."""
    if isinstance(exc, DriveAuthTransientError):
        return AUTH_DEFERRAL
    if isinstance(exc, DriveAuthError):  # DriveAuthPermanentError and any other sign-in failure we do not recognise: do not hammer, ask a person
        return AUTH_PERMANENT
    if isinstance(exc, DriveTargetError):
        if not exc.retryable:
            return PERMANENT
        return GUARD_REASON_CLASS.get(exc.reason, SECURITY)  # an unknown reason is read as the cautious one, and a test makes sure none is unknown
    if isinstance(exc, (DriveFolderMissing, DriveDestinationMissing, DriveConfigError)):
        return CONFIGURATION
    if isinstance(exc, DrivePermissionError):
        return PERMISSION
    if isinstance(exc, DriveQuotaError):
        return QUOTA
    if isinstance(exc, (DriveIntegrityError, DriveIdentityMismatch)):
        return INTEGRITY
    if isinstance(exc, (DriveTransientError, DriveIncompleteListing)):
        return TRANSIENT
    if isinstance(exc, DriveError):
        return TRANSIENT if exc.retryable else PERMANENT
    return TRANSIENT  # an unexpected exception: the attempt is charged (so a poison document cannot loop forever) and the error text is recorded


def policy_for(failure_class: str) -> Policy:
    return POLICIES[failure_class]


def doubling(base: float, cap: float, n: int) -> float:
    """base * 2**(n-1), at most cap; n counts from 1."""
    return min(cap, base * (2 ** min(max(n, 1) - 1, 30)))


def backoff_seconds(failure_class: str, *, attempts: int, deferrals: int, transient_base: float, transient_cap: float) -> float | None:
    """How long to wait before the next automatic attempt, BEFORE jitter. None = never automatically (terminal or parked)."""
    policy = POLICIES[failure_class]
    if policy.terminal or policy.parked:
        return None
    if policy.deferral == "fast":
        return doubling(FAST_DEFERRAL_BASE_SECONDS, FAST_DEFERRAL_CAP_SECONDS, deferrals)
    if policy.deferral == "slow":
        return doubling(SLOW_DEFERRAL_BASE_SECONDS, SLOW_DEFERRAL_CAP_SECONDS, deferrals)
    return doubling(transient_base, transient_cap, attempts)


def jittered(seconds: float, rng) -> float:
    """`seconds` +/- JITTER_FRACTION; `rng` returns a float in [0, 1). Never negative."""
    return max(0.0, seconds * (1.0 + (rng() * 2.0 - 1.0) * JITTER_FRACTION))


def needs_attention(record, now: datetime) -> str | None:
    """Why an administrator should look at this record, or None. Read-only; it changes nothing and is safe to call from a listing."""
    if record.state == "FAILED":
        return "failed"
    if record.state != "UPLOADING":
        return None
    if record.failure_class == AUTH_PERMANENT:
        return "authorization_needed"
    since = record.deferred_since
    if since is not None and (now - since) >= timedelta(seconds=NEEDS_ATTENTION_AFTER_SECONDS):
        return f"deferred_{record.failure_class or 'unknown'}"
    return None
