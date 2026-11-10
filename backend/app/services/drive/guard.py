"""Production destination guard: no Drive write happens unless the destination is VERIFIED to be inside the one expected Shared Drive.

What is verified (by asking Google, never by trusting configuration):
  * the configured destination folder exists, is a folder, is not in the trash and the signed-in identity may add and move items in it;
  * its `driveId` is present (a My Drive folder has none) and equals the explicitly configured expected Shared Drive id;
  * its PARENT HIERARCHY: every ancestor is a live folder of the same Shared Drive, each has exactly one parent, there is no loop, and the chain
    ends at the Shared Drive's own root (the item whose id is the drive id) within a fixed depth.

Behaviour:
  * FAIL CLOSED. Any refusal - wrong drive, My Drive, missing, no permission, an outage or an unreadable answer - raises DriveTargetError and
    nothing is written.
  * A successful verification is cached only for `ttl_seconds` (default 5 minutes) and dropped at once whenever a write is refused with
    "not found"/"permission denied", when a write result reports another drive, or on invalidate().
  * An OUTAGE (Google unreachable or unreadable), seen by the destination chain OR by the check of one object, opens a short circuit breaker
    (`failure_window_seconds`, default 15 s) and voids the cached verification: for that window every write is refused immediately with no network
    wait, then the next write verifies again and recovers by itself. Verification reads use few retries (`verify_attempts`, default 2), never
    pause longer than `verify_max_backoff_seconds` (default 2 s, whatever Retry-After says) and share an overall deadline
    (`verify_deadline_seconds`, default 15 s; a single object check gets a smaller budget), so one failed verification cannot hold a caller for long.
  * Verification is SINGLE-FLIGHT: concurrent writers share one verification. The network wait happens under a flight lock only, and a writer
    queued behind it waits at most `waiter_seconds` (default 1.5 s) and is then refused, so one Google outage cannot occupy every request thread;
    reading the state (`verified`, `status()`) or calling `invalidate()` never waits for the network.
  * Every object that a write touches (the file being renamed/moved/archived/restored and the folder it is moved or uploaded INTO) is checked
    individually and must be a live object of the expected drive; every write result is confirmed to be in the expected drive afterwards; and
    every EXISTING object that would be adopted as "ours" (a retried upload, a recovered file, a folder created by an earlier attempt) must belong
    to the expected drive too.
  * Messages carry a reason code and no Drive id, path or credential.

The guard performs READS only. The DriveClient OWNS its guard (it builds it from the configuration) and consults it before every
POST/PUT/PATCH/DELETE (see DriveClient._request).
"""

import logging
import threading
import time

from app.services.drive.auth import DriveAuthError
from app.services.drive.errors import DriveError, DriveNotFound, DrivePermissionError

log = logging.getLogger("app.drive.guard")

_FOLDER_MIME = "application/vnd.google-apps.folder"  # equal to DriveClient's FOLDER_MIME (a test pins that); defined here so the guard does not import the client
DESTINATION_FIELDS = "id,mimeType,driveId,parents,trashed,explicitlyTrashed,capabilities(canAddChildren,canMoveItemWithinDrive)"
OBJECT_FIELDS = "id,driveId,trashed,explicitlyTrashed"
MAX_HIERARCHY_DEPTH = 20

_MESSAGES = {
    "not_configured": "No expected Shared Drive is configured, so every Drive write is refused.",
    "unverifiable": "The Drive destination could not be verified (Google did not give a usable answer), so the write was refused.",
    "destination_missing": "The Drive destination was not found, or is not visible to this account, so the write was refused.",
    "destination_denied": "This account is not allowed to read the Drive destination, so the write was refused.",
    "my_drive": "The Drive destination is in a My Drive, not in the expected Shared Drive, so the write was refused.",
    "wrong_drive": "The Drive destination belongs to a different Shared Drive than the expected one, so the write was refused.",
    "not_folder": "The Drive destination is not a folder, so the write was refused.",
    "trashed": "The Drive destination is in the trash, so the write was refused.",
    "no_capability": "This account may not add and move items in the Drive destination, so the write was refused.",
    "hierarchy": "The parent hierarchy of the Drive destination does not lead to the root of the expected Shared Drive, so the write was refused.",
    "write_outside_drive": "A Drive write result reports a different drive than expected; further writes are refused until the destination is verified again.",
    "adopt_my_drive": "An existing Drive object that this operation would adopt is in a My Drive, so it was not adopted.",
    "adopt_wrong_drive": "An existing Drive object that this operation would adopt belongs to a different Shared Drive than the expected one, so it was not adopted.",
    "adopt_trashed": "An existing Drive object that this operation would adopt is in the trash, so it was not adopted.",
}


class DriveTargetError(DriveError):
    """A Drive write (or an adoption) was refused because the destination, or the object, is not verified to be in the expected Shared Drive.
    `reason` is a short code.

    Destination refusals are `retryable` (the operation can be repeated once the configuration or Drive is fixed, so queued uploads stay
    recoverable instead of being marked failed). ADOPTION refusals (reason adopt_*) are NOT retryable: retrying cannot make a foreign object ours."""

    def __init__(self, reason: str, *, retryable: bool = True):
        super().__init__(_MESSAGES[reason], reason=reason, retryable=retryable)


class DriveTargetGuard:
    def __init__(
        self,
        client,
        root_folder_id: str,
        expected_drive_id: str,
        *,
        ttl_seconds: float = 300.0,
        failure_window_seconds: float = 15.0,
        verify_attempts: int = 2,
        verify_deadline_seconds: float = 15.0,
        verify_max_backoff_seconds: float = 2.0,
        waiter_seconds: float = 1.5,
        object_read_budget_seconds: float = 5.0,
        clock=time.monotonic,
        max_depth: int = MAX_HIERARCHY_DEPTH,
    ):
        if not isinstance(expected_drive_id, str) or not expected_drive_id.strip():
            raise DriveTargetError("not_configured")
        if not isinstance(root_folder_id, str) or not root_folder_id.strip():
            raise DriveTargetError("not_configured")
        if min(ttl_seconds, failure_window_seconds, verify_deadline_seconds, verify_max_backoff_seconds, waiter_seconds, object_read_budget_seconds) <= 0 or verify_attempts < 1:
            raise ValueError("every guard time setting must be positive and verify_attempts at least 1")
        self._client = client
        self._deadline = float(verify_deadline_seconds)  # ONE destination verification (all its reads together) is abandoned after this long
        self._max_backoff = float(verify_max_backoff_seconds)  # a verification read never sleeps longer than this between attempts, whatever Retry-After says
        self._waiter_seconds = float(waiter_seconds)  # how long a writer queues behind a verification in flight before it is refused
        self._object_budget = float(object_read_budget_seconds)  # time limit of one object-level read
        self._root = root_folder_id.strip()
        self._expected = expected_drive_id.strip()
        self._ttl = float(ttl_seconds)
        self._failure_window = float(failure_window_seconds)
        self._verify_attempts = int(verify_attempts)
        self._clock = clock
        self._max_depth = int(max_depth)
        self._state = threading.Lock()  # guards the fields below; held only for a few instructions, NEVER during network I/O
        self._flight = threading.Lock()  # single-flight: held during a verification so concurrent writers share it
        self._valid_until: float | None = None
        self._failed_until: float | None = None  # outage circuit breaker
        self._failed_reason = "unverifiable"
        self._generation = 0  # bumped by invalidate(): a verification that raced with one is used once but not cached
        self.verifications = 0  # how many full verifications ran (observable in tests and diagnostics)
        self.last_refusal: str | None = None

    def __repr__(self) -> str:
        return "DriveTargetGuard(<hidden>)"

    # ------------------------------------------------------------------------------------------------------------ state

    def _valid(self, now: float) -> bool:
        return self._valid_until is not None and now < self._valid_until

    @property
    def verified(self) -> bool:
        with self._state:
            return self._valid(self._clock())

    @property
    def root_folder_id(self) -> str:
        return self._root

    def status(self) -> dict:
        """Safe to show an administrator: flags and a reason code, never an id."""
        with self._state:
            now = self._clock()
            return {"verified": self._valid(now), "last_refusal": self.last_refusal, "verifications": self.verifications, "circuit_open": self._failed_until is not None and now < self._failed_until}

    def invalidate(self) -> None:
        with self._state:
            self._valid_until = None
            self._generation += 1

    def _refuse(self, reason: str) -> DriveTargetError:
        with self._state:
            self._valid_until = None  # a refusal is never cached as success
            self.last_refusal = reason
        log.warning("Drive write guard refused the destination (%s).", reason)
        return DriveTargetError(reason)

    def _reject_object(self, reason: str) -> DriveTargetError:
        """An ADOPTION refusal: about one object, not about the destination, so the cached destination verification is left alone."""
        with self._state:
            self.last_refusal = reason
        log.warning("Drive write guard refused to adopt an existing object (%s).", reason)
        return DriveTargetError(reason, retryable=False)

    # --------------------------------------------------------------------------------------------------------- reading

    def _open_breaker(self) -> None:
        """Google could not be reached (or gave no usable answer): refuse every writer at once, without a network wait, for the failure window."""
        with self._state:
            self._failed_until = self._clock() + self._failure_window
            self._failed_reason = "unverifiable"
            self._valid_until = None  # an outage seen by ANY read (the destination chain or one object) voids the cached verification: no writer rides on it

    def _fetch(self, file_id: str, fields: str, *, missing_is_refusal: bool = True, budget: float | None = None) -> dict:
        try:
            data = self._client.get_raw(file_id, fields, attempts=self._verify_attempts, max_backoff=self._max_backoff, budget=budget)
        except DriveNotFound:
            if not missing_is_refusal:
                raise
            raise self._refuse("destination_missing") from None
        except DrivePermissionError:
            raise self._refuse("destination_denied") from None
        except (DriveError, DriveAuthError, ValueError):  # outage, retries exhausted, deadline, sign-in failure, unreadable body: all fail closed
            self._open_breaker()  # root-level AND object-level failures alike
            raise self._refuse("unverifiable") from None
        if not isinstance(data, dict):
            self._open_breaker()
            raise self._refuse("unverifiable")
        return data

    def _verify(self) -> None:
        with self._state:
            self.verifications += 1
        seen: set[str] = set()
        current = self._root
        first = True
        deadline = self._clock() + self._deadline  # ONE budget for the whole walk up the hierarchy
        for _ in range(self._max_depth + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._open_breaker()
                raise self._refuse("unverifiable")
            meta = self._fetch(current, DESTINATION_FIELDS, budget=remaining)
            if meta.get("id") != current:
                raise self._refuse("unverifiable")
            if meta.get("trashed") or meta.get("explicitlyTrashed"):
                raise self._refuse("trashed")
            drive_id = meta.get("driveId")
            if not isinstance(drive_id, str) or not drive_id:
                raise self._refuse("my_drive")  # items in a My Drive carry no driveId
            if drive_id != self._expected:
                raise self._refuse("wrong_drive")
            if meta.get("mimeType") != _FOLDER_MIME:
                raise self._refuse("not_folder")
            if first:
                capabilities = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
                if capabilities.get("canAddChildren") is not True or capabilities.get("canMoveItemWithinDrive") is not True:
                    raise self._refuse("no_capability")
            if current == self._expected:  # reached the Shared Drive's own root
                if meta.get("parents"):
                    raise self._refuse("hierarchy")  # a drive root has no parent
                return
            parents = meta.get("parents")
            if not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], str) or not parents[0]:
                raise self._refuse("hierarchy")
            following = parents[0]
            if following == current or following in seen:
                raise self._refuse("hierarchy")  # a loop
            seen.add(current)
            current = following
            first = False
        raise self._refuse("hierarchy")  # deeper than any legitimate destination

    # ----------------------------------------------------------------------------------------------------------- guard

    def _answer_from_state(self) -> bool:
        """True = verified and fresh. Raises while the outage circuit breaker is open (no network). False = a verification is needed."""
        with self._state:
            now = self._clock()
            if self._valid(now):
                return True
            if self._failed_until is not None and now < self._failed_until:
                raise DriveTargetError(self._failed_reason)
            return False

    def assert_ready(self) -> None:
        """Called before EVERY write. Free while the cached verification is fresh; refuses at once while the outage breaker is open; otherwise
        verifies (single-flight) and raises DriveTargetError unless the destination is verified."""
        if self._answer_from_state():
            return
        # Single-flight with a bounded wait: the writer that finds no verification in flight performs it; the others queue behind it for at most
        # `waiter_seconds` and are then refused (retryable) instead of holding their request thread for the whole verification.
        if not self._flight.acquire(timeout=self._waiter_seconds):
            raise DriveTargetError("unverifiable")
        try:
            if self._answer_from_state():  # another writer finished the verification while this one waited for its turn
                return
            with self._state:
                generation = self._generation
            try:
                self._verify()  # network I/O, bounded by the verification deadline: only the flight lock is held
            except DriveTargetError as exc:
                with self._state:
                    self._valid_until = None
                    self.last_refusal = exc.reason
                if exc.reason == "unverifiable":  # an outage: stop every writer from waiting for it again for a short while
                    self._open_breaker()
                raise
            with self._state:
                self.last_refusal = None
                self._failed_until = None
                if self._generation == generation:  # an invalidate() that raced with this verification means "do not cache it"
                    self._valid_until = self._clock() + self._ttl
        finally:
            self._flight.release()

    def try_verify(self) -> bool:
        """Non-fatal start-up/first-use check: logs and reports, never raises."""
        try:
            self.assert_ready()
        except DriveTargetError:
            return False
        return True

    def assert_object_in_drive(self, file_id: str, *, allow_trashed: bool = False) -> None:
        """The individual file or folder that a write is about to touch (or write into) must itself be a LIVE object of the expected Shared Drive.
        This catches an object moved away, or trashed, after the cached destination check. A trashed object is accepted only where the caller
        says so explicitly (restoring it). A genuinely missing object still raises DriveNotFound."""
        self.assert_ready()
        meta = self._fetch(file_id, OBJECT_FIELDS, missing_is_refusal=False, budget=self._object_budget)
        drive_id = meta.get("driveId")
        if not isinstance(drive_id, str) or not drive_id:
            raise self._refuse("my_drive")
        if drive_id != self._expected:
            raise self._refuse("wrong_drive")
        if not allow_trashed and (meta.get("trashed") or meta.get("explicitlyTrashed")):
            raise self._refuse("trashed")

    def assert_adoptable(self, found) -> None:
        """An EXISTING object that an operation is about to treat as ours (a file left by an earlier, ambiguous upload; a recovered file; a folder
        created by an earlier attempt) must belong to the expected Shared Drive and be live. `found` is the DriveObject just read from Drive.
        Needs no network and does not depend on the cached destination verification."""
        drive_id = getattr(found, "drive_id", None)
        if not isinstance(drive_id, str) or not drive_id:
            raise self._reject_object("adopt_my_drive")
        if drive_id != self._expected:
            raise self._reject_object("adopt_wrong_drive")
        if getattr(found, "trashed", False):
            raise self._reject_object("adopt_trashed")

    def confirm(self, written) -> None:
        """After a write: the object Drive returned must report the expected drive. If not, stop all writes until the destination is verified again."""
        if getattr(written, "drive_id", None) != self._expected:
            raise self._refuse("write_outside_drive")
