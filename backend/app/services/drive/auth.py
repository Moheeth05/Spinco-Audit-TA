"""Service-account sign-in for the Drive API (OAuth 2.0 JWT-bearer grant), with no third-party Google library.

The JWT is signed RS256 with `cryptography` (already a bundled dependency) and exchanged at the token endpoint over httpx.
Nothing secret is ever logged or included in an exception message, and the objects here hide their secrets from repr().

Token acquisition (shared by this provider and the user-OAuth provider in oauth.py) is SINGLE-FLIGHT and BOUNDED:
  * a lock protects the cached token only for a few instructions; it is NEVER held during network or file I/O;
  * one thread at a time refreshes. A token is `fresh` until `expires_at - soft_margin` and still `usable` until `expires_at - hard_margin`: in
    between, every other thread keeps using the cached token instead of waiting for the refresh;
  * a thread that has no usable token waits for the refresher at most `wait_seconds` (2 s by default), then fails with DriveAuthTransientError;
  * a whole refresh (file checks, every retry and every sleep) is limited to `deadline_seconds` (never more than 15 s);
  * a failed refresh is remembered for a short window, so a failing endpoint is asked once per window, not once per thread; an UNEXPECTED exception
    during a refresh (a bug, a closed client, a malformed result) is turned into the same kind of remembered, transient failure with a fixed message;
  * the refresh flag is released in a `finally` whatever happens (even in the commit stage or on KeyboardInterrupt), so no waiter can be stranded;
  * `invalidate(token)` is compare-and-drop: a burst of 401s that all carry the same rejected token causes ONE refresh, not one per thread;
  * the lifetime in a token answer is validated and clamped (NaN/infinite/non-positive -> 5 min; above 24 h -> 24 h).
"""

import base64
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_REFRESH_MARGIN_SECONDS = 120  # the soft margin: a refresh starts this long before expiry (unchanged)
_HARD_MARGIN_SECONDS = 30  # a token is not handed out any more this long before expiry
MAX_TOKEN_DEADLINE_SECONDS = 15.0  # the hard ceiling of one whole refresh, whatever the configuration says
DEFAULT_TOKEN_WAIT_SECONDS = 2.0  # how long a thread WITHOUT a usable token waits behind the refresher (was 5 s)
MAX_TOKEN_LIFETIME_SECONDS = 24 * 3600.0  # an `expires_in` above this is cut to it
INVALID_LIFETIME_SECONDS = 300.0  # an `expires_in` that is NaN, infinite or not positive counts as this (longer than the refresh margins, so it cannot make every call refresh)
# HTTP answers of the token endpoint (or a gateway in front of it) that mean "not now, try again": request timeout, too early, rate limit. Plus every 5xx.
TRANSIENT_TOKEN_STATUSES = frozenset({408, 425, 429})


class DriveAuthError(Exception):
    """Sign-in failed. The message contains only a Google error code, never a credential or a token."""


class DriveAuthTransientError(DriveAuthError):
    """Sign-in failed for a reason that is expected to pass by itself: the token endpoint was unreachable, slow, rate-limiting or failing (429/5xx),
    its answer was unreadable, another thread's refresh is still in progress, or a refresh that just failed this way is being remembered. Safe to
    try again later; it is never a reason to give up on a document."""


class DriveAuthPermanentError(DriveAuthError):
    """Sign-in failed and trying again will not help until a person acts: bad or unreadable credentials, a revoked or expired authorization
    (`invalid_grant`), a rejected client, a wrong scope, an unusable token file."""


@dataclass(frozen=True)
class TokenSettings:
    deadline_seconds: float = MAX_TOKEN_DEADLINE_SECONDS  # one whole refresh, including file checks, retries and sleeps
    timeout_seconds: float = 10.0  # upper limit of the READ phase of one token request (the other phases get fixed shares of what is left)
    wait_seconds: float = DEFAULT_TOKEN_WAIT_SECONDS  # how long a thread without a usable token waits behind the refresher
    failure_window_seconds: float = 10.0  # a failed (transient) refresh is remembered this long
    permanent_failure_window_seconds: float = 60.0  # ... a permanent one this long
    soft_margin_seconds: float = float(_REFRESH_MARGIN_SECONDS)
    hard_margin_seconds: float = float(_HARD_MARGIN_SECONDS)

    def __post_init__(self):
        if not 0 < self.deadline_seconds <= MAX_TOKEN_DEADLINE_SECONDS:
            raise ValueError(f"the token refresh deadline must be between 0 and {MAX_TOKEN_DEADLINE_SECONDS:g} seconds")
        if min(self.timeout_seconds, self.wait_seconds, self.failure_window_seconds, self.permanent_failure_window_seconds) <= 0:
            raise ValueError("every token time setting must be positive")
        if not 0 <= self.hard_margin_seconds < self.soft_margin_seconds:
            raise ValueError("the hard margin must be smaller than the soft margin")

    def post_timeout(self, remaining: float) -> httpx.Timeout:
        """Timeouts for ONE token request. The four phases get shares that add up to at most `remaining`, so a single request cannot outlast the
        refresh deadline (the connect phase covers the TCP connect; a DNS lookup is not interruptible by httpx - see the report's remaining risks)."""
        r = max(remaining, 0.05)
        return httpx.Timeout(connect=min(5.0, 0.25 * r), write=min(5.0, 0.15 * r), read=min(self.timeout_seconds, 0.55 * r), pool=min(2.0, 0.05 * r))


class SingleFlightToken:
    """Caches one access token and refreshes it in a single flight (see the module docstring). Subclasses implement `_refresh`.
    `get_token()` / `invalidate()` are the whole interface the DriveClient uses."""

    def __init__(self, settings: TokenSettings | None, clock, monotonic):
        self._settings = settings or TokenSettings()
        self._clock = clock  # wall clock: token expiry
        self._monotonic = monotonic  # deadlines, waits and failure windows
        self._cond = threading.Condition(threading.Lock())  # protects the fields below; held only for a few instructions, never during I/O
        self._token: str | None = None
        self._expires_at = 0.0
        self._refreshing = False
        self._failed_until: float | None = None
        self._failure: tuple[type, str] | None = None

    def _usable(self, now: float) -> str | None:  # caller holds the lock
        if self._token and now < self._expires_at - self._settings.hard_margin_seconds:
            return self._token
        return None

    def _refresh(self, budget) -> tuple[str, float]:
        """Performs ONE refresh and returns (access token, lifetime in seconds). `budget()` is the time left of the refresh deadline. Raises
        DriveAuthTransientError / DriveAuthPermanentError. Called by at most one thread at a time and without any lock held."""
        raise NotImplementedError

    def get_token(self, max_wait: float | None = None) -> str:
        """The current access token. `max_wait` (seconds) lowers how long this call may wait behind another thread's refresh (a verification read passes
        what is left of its own budget). Raises DriveAuthTransientError / DriveAuthPermanentError; never blocks for longer than the settings allow."""
        settings = self._settings
        wait_cap = settings.wait_seconds if max_wait is None else min(settings.wait_seconds, max(0.0, max_wait))
        wait_until = None
        with self._cond:
            while True:
                now, mono = self._clock(), self._monotonic()
                if self._token and now < self._expires_at - settings.soft_margin_seconds:
                    return self._token  # fresh
                usable = self._usable(now)
                failing = self._failed_until is not None and mono < self._failed_until
                if usable and (self._refreshing or failing):
                    return usable  # somebody else is on it (or it just failed): keep going with the token that still works
                if failing:
                    kind, message = self._failure
                    raise kind(message)
                if not self._refreshing:
                    self._refreshing = True  # this thread is THE refresher; the flag is released by the `finally` below, whatever happens
                    break
                if wait_until is None:
                    wait_until = mono + wait_cap
                remaining = wait_until - mono
                if remaining <= 0:
                    raise DriveAuthTransientError("A Google token refresh is already in progress.")
                self._cond.wait(remaining)
        try:
            return self._refresh_as_owner()
        finally:
            # The ONLY place that guarantees the flag cannot stay set: if anything escaped before the normal exits cleared it (an unexpected exception, a
            # KeyboardInterrupt, a failing clock), it is cleared here and every waiter is woken, so nobody can be stranded behind a refresh that is over.
            with self._cond:
                if self._refreshing:
                    self._refreshing = False
                    self._wake()

    def _refresh_as_owner(self) -> str:
        """Everything the elected thread does, from reading the clock to committing the result. Called with the refresh flag set and NO lock held."""
        settings = self._settings
        try:
            started_wall = self._clock()
            deadline_at = self._monotonic() + settings.deadline_seconds

            def budget() -> float:
                return deadline_at - self._monotonic()

            access, lifetime = self._refresh(budget)
            lifetime = self._checked_result(access, lifetime)
        except DriveAuthError as exc:
            fallback = self._record_failure(exc)
            if fallback is not None:
                return fallback  # the refresh failed but the old token still works: callers are not affected yet
            raise
        except Exception:
            # Anything else - a bug, a closed HTTP client, an unsupported algorithm, a malformed result - is NOT allowed to escape as itself (its text can
            # carry request data) nor to be retried by every caller at once: it becomes a transient, remembered sign-in failure with a fixed message.
            error = DriveAuthTransientError("Google sign-in failed unexpectedly.")
            fallback = self._record_failure(error)
            if fallback is not None:
                return fallback
            raise error from None
        self._commit(access, started_wall + lifetime)
        return access

    @staticmethod
    def _checked_result(access, lifetime) -> float:
        """Validates what a refresh returned and clamps the lifetime. A token that is not a non-empty string, or a lifetime that is not a number,
        is a failed refresh (ValueError). A lifetime that is NaN/infinite/not positive is replaced by a short, safe default; one that is absurdly long
        is cut to 24 h - a bogus answer can neither make the token never refresh nor make every call refresh."""
        if not isinstance(access, str) or not access.strip():
            raise ValueError("no access token")
        if isinstance(lifetime, bool):
            raise ValueError("not a lifetime")
        value = float(lifetime)  # TypeError / ValueError for anything that is not a number
        if not math.isfinite(value) or value <= 0:
            return INVALID_LIFETIME_SECONDS
        return min(value, MAX_TOKEN_LIFETIME_SECONDS)

    def _record_failure(self, exc: DriveAuthError) -> str | None:
        """Remembers a failed refresh (transient: 10 s by default, permanent: 60 s), releases the flag and wakes the waiters. Returns the old token if
        it is still usable (the caller then carries on with it), otherwise None."""
        transient = isinstance(exc, DriveAuthTransientError)
        window = self._settings.failure_window_seconds if transient else self._settings.permanent_failure_window_seconds
        with self._cond:
            self._refreshing = False
            self._failure = (DriveAuthTransientError if transient else DriveAuthPermanentError, str(exc))
            self._failed_until = self._monotonic() + window
            fallback = self._usable(self._clock())
            self._wake()
        return fallback

    def _commit(self, access: str, expires_at: float) -> None:
        with self._cond:
            self._token, self._expires_at = access, expires_at
            self._refreshing = False
            self._failure, self._failed_until = None, None
            self._wake()

    def _wake(self) -> None:  # caller holds the lock
        """Wakes every waiter. A failing wake-up must not turn a finished refresh into a failure: a waiter whose wait times out re-reads the state
        before it gives up, so it finds the result anyway."""
        try:
            self._cond.notify_all()
        except Exception:  # noqa: BLE001
            pass

    def invalidate(self, token: str | None = None) -> None:
        """Drops the cached token. With no argument it always does. With `token` (the one a request was rejected with) it does so ONLY if that token is
        still the cached one: when other threads were rejected with the same token, the first invalidation wins and the others find a new token (or an
        ongoing refresh) instead of each forcing yet another refresh."""
        with self._cond:
            if token is not None and self._token != token:
                return
            self._token = None
            self._expires_at = 0.0
            self._failure, self._failed_until = None, None  # the caller learned something new (e.g. a 401): ask again

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class ServiceAccountCredentials:
    client_email: str
    private_key_pem: str = field(repr=False)
    token_uri: str = DEFAULT_TOKEN_URI

    def __repr__(self) -> str:  # never show the key
        return f"ServiceAccountCredentials(client_email={self.client_email!r}, private_key=<hidden>)"

    @classmethod
    def from_file(cls, path: Path) -> "ServiceAccountCredentials":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if data.get("type") != "service_account":
                raise ValueError("not a service-account key")
            email, key = data["client_email"], data["private_key"]
            if not isinstance(email, str) or not isinstance(key, str):
                raise ValueError("malformed")
        except (OSError, ValueError, KeyError, TypeError):
            raise DriveAuthPermanentError("The credentials file is not a valid service-account key file.") from None
        return cls(client_email=email, private_key_pem=key, token_uri=str(data.get("token_uri") or DEFAULT_TOKEN_URI))


def _error_code(response: httpx.Response) -> str:
    """Google's `error` code from a failed token response, reduced to harmless characters (never anything else from the body)."""
    try:
        raw = str(response.json().get("error", "unknown"))
    except (ValueError, AttributeError):
        raw = "unknown"
    return re.sub(r"[^A-Za-z0-9_ .-]", "", raw)[:60] or "unknown"


class TokenProvider(SingleFlightToken):
    """Service-account sign-in. Caches one access token and refreshes it shortly before it expires, in a single flight (see the module docstring).
    Thread-safe."""

    def __init__(self, credentials: ServiceAccountCredentials, scope: str, http: httpx.Client, clock=time.time, *, settings: TokenSettings | None = None, monotonic=time.monotonic):
        super().__init__(settings, clock, monotonic)
        self._credentials = credentials
        self._scope = scope
        self._http = http
        self._key = None

    def __repr__(self) -> str:
        return "TokenProvider(<hidden>)"

    def _assertion(self, now: int) -> str:
        if self._key is None:
            try:
                self._key = load_pem_private_key(self._credentials.private_key_pem.encode("utf-8"), password=None)
            except (ValueError, TypeError):
                raise DriveAuthPermanentError("The service-account private key could not be read.") from None
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64url(
            json.dumps(
                {"iss": self._credentials.client_email, "scope": self._scope, "aud": self._credentials.token_uri, "iat": now, "exp": now + 3600},
                separators=(",", ":"),
            ).encode()
        )
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{claims}.{_b64url(signature)}"

    def _refresh(self, budget) -> tuple[str, float]:
        assertion = self._assertion(int(self._clock()))
        remaining = budget()
        if remaining <= 0:
            raise DriveAuthTransientError("Google's token endpoint did not answer in time.")
        try:
            response = self._http.post(
                self._credentials.token_uri,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
                timeout=self._settings.post_timeout(remaining),
            )
        except httpx.HTTPError:  # timeouts and network errors alike: the details can carry request data, so none are kept
            raise DriveAuthTransientError("Could not reach the Google token endpoint.") from None
        if response.status_code != 200:
            message = f"Google rejected the service-account sign-in ({_error_code(response)})."
            if response.status_code in TRANSIENT_TOKEN_STATUSES or response.status_code >= 500:
                raise DriveAuthTransientError(message)  # Google (or a gateway) is timing out, rate-limiting or failing: it will pass
            raise DriveAuthPermanentError(message)  # e.g. invalid_grant: the key or the account is wrong
        try:
            body = response.json()
            access, lifetime = body["access_token"], float(body.get("expires_in", 3600))
            if not isinstance(access, str) or not access:
                raise ValueError("no token")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DriveAuthTransientError("Google's token response was not understood.") from None
        return access, lifetime
