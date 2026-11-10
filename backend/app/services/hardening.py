"""Server hardening that has to be configurable from the OUTSIDE (environment) and must be safe by default:

  * sign-in throttling  - a burst of wrong passwords is answered with 429 + Retry-After instead of being allowed to guess;
  * upload limits       - a per-file size limit (configurable) that is enforced while READING the file, and a per-request cap that is enforced before the body is read;
  * CORS origins        - an explicit list (never "*" together with cookies); a network-reachable backend serves the desktop client from its own origin and needs none.

Nothing here logs or returns a password, token or document content.
"""

import os
import threading
import time
from collections import deque

# ------------------------------------------------------------------------------------------------------------------------------ sign-in throttling

LOGIN_MAX_FAILURES_ENV = "SPINCO_LOGIN_MAX_FAILURES"  # wrong passwords per (account, client address) within the window before a lock-out (default 5)
LOGIN_WINDOW_ENV = "SPINCO_LOGIN_WINDOW_SECONDS"  # the sliding window (default 300 s)
LOGIN_LOCKOUT_ENV = "SPINCO_LOGIN_LOCKOUT_SECONDS"  # how long a lock-out lasts (default 300 s)
_MAX_TRACKED_KEYS = 20_000


def _int_env(env, name: str, default: int, low: int, high: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(low, min(high, value))


class LoginThrottle:
    """In-memory (per backend process) sliding-window throttle with three independent counters, so that neither a single guesser nor a distributed one gets through,
    and a stranger cannot easily lock a real user out from another address:
        (account, client address)   `max_failures`        the normal guard, limits guessing from one place
        account alone               `max_failures * 6`    a guesser rotating addresses
        client address alone        `max_failures * 10`   one place spraying many accounts
    An unknown account is counted exactly like a known one (the answer never reveals which accounts exist). Behind a reverse proxy the 'client address' is the one
    the proxy reports (SPINCO_TRUSTED_PROXY_IPS makes uvicorn honour X-Forwarded-For); the state is per process and is lost on a restart."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[tuple, deque] = {}
        self._locked_until: dict[tuple, float] = {}

    @staticmethod
    def settings(env=None) -> tuple[int, int, int]:
        env = os.environ if env is None else env
        return (
            _int_env(env, LOGIN_MAX_FAILURES_ENV, 5, 1, 1000),
            _int_env(env, LOGIN_WINDOW_ENV, 300, 10, 86400),
            _int_env(env, LOGIN_LOCKOUT_ENV, 300, 5, 86400),
        )

    @staticmethod
    def _keys(email: str, ip: str):
        return (("pair", email, ip), 1), (("account", email), 6), (("address", ip), 10)

    def retry_after(self, email: str, ip: str, env=None) -> int | None:
        """Seconds the caller must wait, or None when a sign-in attempt is allowed."""
        now = self._clock()
        with self._lock:
            waits = [self._locked_until[key] - now for key, _ in self._keys(email, ip) if self._locked_until.get(key, 0) > now]
        return max(1, int(max(waits) + 0.999)) if waits else None

    def record_failure(self, email: str, ip: str, env=None) -> None:
        max_failures, window, lockout = self.settings(env)
        now = self._clock()
        with self._lock:
            if len(self._failures) > _MAX_TRACKED_KEYS:
                self._prune(now, window)
            for key, factor in self._keys(email, ip):
                times = self._failures.setdefault(key, deque())
                times.append(now)
                while times and times[0] < now - window:
                    times.popleft()
                if len(times) >= max_failures * factor:
                    self._locked_until[key] = now + lockout
                    times.clear()

    def record_success(self, email: str, ip: str) -> None:
        """A correct sign-in clears that account+address pair's history (not the broader counters)."""
        with self._lock:
            self._failures.pop(("pair", email, ip), None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()

    def _prune(self, now: float, window: float) -> None:  # caller holds the lock
        for key in [k for k, v in self._failures.items() if not v or v[-1] < now - window]:
            del self._failures[key]
        for key in [k for k, until in self._locked_until.items() if until <= now]:
            del self._locked_until[key]


login_throttle = LoginThrottle()

# ------------------------------------------------------------------------------------------------------------------------------ upload limits

MAX_UPLOAD_MB_ENV = "SPINCO_MAX_UPLOAD_MB"  # per PDF (default 25)
MAX_REQUEST_MB_ENV = "SPINCO_MAX_REQUEST_MB"  # per upload request, all files together (default 512)
DEFAULT_MAX_UPLOAD_MB = 25
DEFAULT_MAX_REQUEST_MB = 512


def max_upload_bytes(env=None) -> int:
    env = os.environ if env is None else env
    return _int_env(env, MAX_UPLOAD_MB_ENV, DEFAULT_MAX_UPLOAD_MB, 1, 500) * 1024 * 1024


def max_request_bytes(env=None) -> int:
    env = os.environ if env is None else env
    return _int_env(env, MAX_REQUEST_MB_ENV, DEFAULT_MAX_REQUEST_MB, 1, 4096) * 1024 * 1024


class RequestSizeLimitMiddleware:
    """Plain ASGI middleware (not BaseHTTPMiddleware - see SecurityHeadersMiddleware in main.py for why): an upload request that DECLARES a body larger than the
    per-request cap is refused with 413 before any of it is read. (A client that sends no Content-Length is still bounded per file, while the file is read.)"""

    def __init__(self, app, env=None):
        self.app = app
        self._env = env

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") == "POST" and scope.get("path", "").startswith("/api/uploads"):
            declared = None
            for name, value in scope.get("headers", []):
                if name == b"content-length":
                    try:
                        declared = int(value)
                    except ValueError:
                        declared = None
                    break
            if declared is not None and declared > max_request_bytes(self._env):
                body = b'{"detail":"The upload is too large."}'
                await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# ------------------------------------------------------------------------------------------------------------------------------ CORS

CORS_ORIGINS_ENV = "SPINCO_CORS_ORIGINS"
DEV_CORS_ORIGINS = ["http://localhost:5200", "http://127.0.0.1:5200"]


class CorsConfigError(RuntimeError):
    pass


def cors_origins(env=None) -> list[str]:
    """SPINCO_CORS_ORIGINS (comma separated origins, e.g. https://spinco.corp.example). Unset: the Vite development origins on a loopback-only backend (exactly as
    before), NONE on a network-reachable one (the desktop client is served by the backend itself, so it is same-origin and needs no CORS). A wildcard, or anything that
    is not a bare http(s) origin, is refused - cookies are used, and '*' with credentials is never acceptable."""
    from urllib.parse import urlparse

    env = os.environ if env is None else env
    raw = str(env.get(CORS_ORIGINS_ENV, "")).strip()
    if not raw:
        from app.services import startup_checks

        return [] if startup_checks.network_exposed(env) else list(DEV_CORS_ORIGINS)
    origins: list[str] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        parsed = urlparse(item)
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname) and parsed.path in ("", "/") and not (parsed.query or parsed.fragment or parsed.username or parsed.password)
        if item == "*" or not valid:
            raise CorsConfigError(f"{CORS_ORIGINS_ENV} must list bare http(s) origins (scheme://host[:port]); '*' and paths are not allowed.")
        origins.append(f"{parsed.scheme}://{parsed.netloc}")
    return origins
