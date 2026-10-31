"""Best-effort copy of every ACCEPTED upload to the TA Backend (the Node.js archive in "TA Backend/").

Why: the desktop app keeps uploaded PDFs in its own documents folder; the TA Backend keeps a second, sorted and de-duplicated
archive. This sends each file there the moment it is accepted, on the same channel the user chose (expense report / trip report).

Rules
  * Never affects the upload: it runs in its own daemon thread, every failure is caught and logged, and nothing is raised.
    If the TA Backend is not running, the upload is exactly as before (its folder sync, SYNC_FOLDER, can catch up later).
  * Only files the app itself accepted are sent (valid PDF, not a duplicate of an earlier upload).
  * Configuration is read from the environment at call time:
        TA_BACKEND_URL            default http://127.0.0.1:4000 (this computer only). Set to "off" (or empty) to disable.
        TA_BACKEND_API_KEY        sent as "Authorization: Bearer <key>" when set (required by a TA Backend started with TA_API_KEY)
        TA_BACKEND_TIMEOUT_SECONDS  default 30
  * Only http/https URLs are used. The file content is never logged.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:4000"
_OFF_WORDS = {"", "off", "false", "0", "no", "none", "disabled"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_RETRY_DELAYS_SECONDS = (1.0, 3.0)  # two quick retries after the first attempt, for a server that is just starting
_FAILURE_LOG_INTERVAL_SECONDS = 600
_last_failure_log = 0.0
_pending: list[threading.Thread] = []
_pending_lock = threading.Lock()


@dataclass(frozen=True)
class Settings:
    url: str | None  # None = disabled
    api_key: str | None
    timeout: float

    @property
    def enabled(self) -> bool:
        return self.url is not None


def load_settings(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    raw = env.get("TA_BACKEND_URL")
    url = DEFAULT_URL if raw is None else raw.strip()
    if url.lower() in _OFF_WORDS:
        url = None
    else:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            logger.warning("TA_BACKEND_URL is not a valid http(s) URL; forwarding to the TA Backend is disabled.")
            url = None
        else:
            url = url.rstrip("/")
    try:
        timeout = float(env.get("TA_BACKEND_TIMEOUT_SECONDS") or 30)
    except ValueError:
        timeout = 30.0
    key = (env.get("TA_BACKEND_API_KEY") or "").strip() or None
    return Settings(url=url, api_key=key, timeout=max(1.0, min(timeout, 300.0)))


def _endpoint(settings: Settings, expense: bool) -> str:
    return f"{settings.url}/api/uploads/{'expense-reports' if expense else 'trip-reports'}"


def _log_failure(message: str) -> None:
    """A server that is simply not running would otherwise log on every upload; say it at most every 10 minutes."""
    global _last_failure_log
    now = time.monotonic()
    if now - _last_failure_log >= _FAILURE_LOG_INTERVAL_SECONDS:
        _last_failure_log = now
        logger.warning("TA Backend copy skipped: %s", message)
    else:
        logger.debug("TA Backend copy skipped: %s", message)


def send_upload(*, expense: bool, filename: str, content: bytes, settings: Settings | None = None) -> str:
    """Synchronous send with retries. Returns 'stored', 'exists', 'rejected', 'disabled' or 'unreachable'. Never raises."""
    settings = settings or load_settings()
    if not settings.enabled:
        return "disabled"
    headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
    host = (urlparse(settings.url).hostname or "").lower()
    outcome = "unreachable"
    for attempt in range(len(_RETRY_DELAYS_SECONDS) + 1):
        try:
            # trust_env=False for this computer: a system proxy must never be asked to relay a loopback request
            with httpx.Client(timeout=settings.timeout, trust_env=host not in _LOOPBACK_HOSTS) as client:
                response = client.post(
                    _endpoint(settings, expense), headers=headers, files=[("files", (filename, content, "application/pdf"))]
                )
            if response.status_code == 401:
                _log_failure("the TA Backend refused the API key (set TA_BACKEND_API_KEY).")
                return "rejected"
            if response.status_code >= 500:
                outcome = "unreachable"
                _log_failure(f"the TA Backend answered HTTP {response.status_code}.")
            elif response.status_code != 200:
                _log_failure(f"the TA Backend answered HTTP {response.status_code}.")
                return "rejected"
            else:
                item = (response.json().get("results") or [{}])[0]
                if item.get("success"):
                    logger.info("Copied %r to the TA Backend (%s).", filename, item.get("document_id"))
                    return "stored"
                if item.get("error_message") == "File already exists.":
                    return "exists"
                logger.warning("The TA Backend did not accept %r: %s", filename, item.get("error_message"))
                return "rejected"
        except (httpx.HTTPError, OSError, ValueError) as exc:
            outcome = "unreachable"
            _log_failure(f"{type(exc).__name__}")
        if attempt < len(_RETRY_DELAYS_SECONDS):
            time.sleep(_RETRY_DELAYS_SECONDS[attempt])
    return outcome


def forward_upload(*, expense: bool, filename: str, content: bytes) -> None:
    """Fire and forget: start the send in a daemon thread so the request and the extraction are never delayed."""
    settings = load_settings()
    if not settings.enabled:
        return

    def run() -> None:
        try:
            send_upload(expense=expense, filename=filename, content=content, settings=settings)
        except Exception:  # noqa: BLE001 - this thread must never raise
            logger.exception("Unexpected error while copying an upload to the TA Backend.")

    thread = threading.Thread(target=run, name="ta-backend-forward", daemon=True)
    with _pending_lock:
        _pending[:] = [t for t in _pending if t.is_alive()]
        _pending.append(thread)
    thread.start()


def wait_for_pending(timeout: float = 10.0) -> None:
    """For tests and orderly shutdown: wait for in-flight copies."""
    deadline = time.monotonic() + timeout
    with _pending_lock:
        threads = list(_pending)
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
