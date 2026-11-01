"""Thin Google Drive REST v3 client over httpx: retries with backoff, timeouts, a rate limiter and structured errors.

Every call passes supportsAllDrives=true so the same code works in My Drive folders and in Shared Drives (which of the two the
supplied folder is must be established by the live spike, not assumed). Nothing here logs a request body, a token or a credential.
"""

import random
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.services.drive.auth import DriveAuthError, TokenProvider
from app.services.drive.config import DriveConfig

FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = (
    "id,name,mimeType,size,md5Checksum,parents,trashed,explicitlyTrashed,modifiedTime,createdTime,appProperties,driveId,ownedByMe,"
    "capabilities(canEdit,canTrash,canAddChildren,canMoveItemWithinDrive,canDownload)"
)
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "sharingRateLimitExceeded", "backendError", "internalError"}
_QUOTA_REASONS = {"storageQuotaExceeded", "dailyLimitExceeded", "quotaExceeded"}


class DriveError(Exception):
    # What Google itself said, when the failure was an HTTP answer. Always passed through safe_text(), so it carries no token, e-mail address or key.
    detail = ""  # Google's error message
    google_status = ""  # e.g. PERMISSION_DENIED
    detail_reason = ""  # the machine reason inside `details`, e.g. SERVICE_DISABLED

    def __init__(self, message: str, *, status: int | None = None, reason: str = "", retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retryable = retryable


class DriveNotFound(DriveError): ...
class DrivePermissionError(DriveError): ...
class DriveQuotaError(DriveError): ...
class DriveConflict(DriveError): ...
class DriveTransientError(DriveError): ...
class DriveChangeTokenExpired(DriveError): ...


class DriveIntegrityError(DriveError):
    """Bytes in Drive do not match what was sent (checksum or size). Never retried blindly."""


@dataclass(frozen=True)
class DriveObject:
    id: str
    name: str = ""
    mime_type: str = ""
    size: int | None = None
    md5: str | None = None
    parents: tuple[str, ...] = ()
    trashed: bool = False
    app_properties: dict = field(default_factory=dict)
    modified_time: str | None = None
    created_time: str | None = None
    drive_id: str | None = None

    @classmethod
    def from_api(cls, data: dict) -> "DriveObject":
        size = data.get("size")
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            mime_type=data.get("mimeType", ""),
            size=int(size) if size is not None else None,
            md5=data.get("md5Checksum"),
            parents=tuple(data.get("parents") or ()),
            trashed=bool(data.get("trashed") or data.get("explicitlyTrashed")),
            app_properties=dict(data.get("appProperties") or {}),
            modified_time=data.get("modifiedTime"),
            created_time=data.get("createdTime"),
            drive_id=data.get("driveId"),
        )


def escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


_SECRET_SHAPES = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bya29\.[A-Za-z0-9._-]+"),  # Google access token
    re.compile(r"\b1//[A-Za-z0-9._-]+"),  # Google refresh token
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),  # a JWT
    re.compile(r"(?i)(access_token|refresh_token|client_secret|authorization|api[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"\S+@\S+"),  # an e-mail address
    re.compile(r"[A-Za-z0-9_-]{40,}"),  # any other long opaque string (Drive ids are shorter than this)
)


def safe_text(value, limit: int = 300) -> str:
    """Text that may be shown or stored: token/key shapes and e-mail addresses are masked and the length is capped."""
    text = str(value or "")
    for shape in _SECRET_SHAPES:
        text = shape.sub("[masked]", text)
    return " ".join(text.split())[:limit]


def _error_info(response: httpx.Response) -> dict:
    """Google's own error message, status and `details` reason (sanitised). Never raises."""
    info = {"message": "", "google_status": "", "detail_reason": ""}
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            return info
        info["message"] = safe_text(error.get("message"), 300)
        info["google_status"] = safe_text(error.get("status"), 40)
        for item in error.get("details") or []:
            if isinstance(item, dict) and item.get("reason"):
                info["detail_reason"] = safe_text(item["reason"], 80)
                break
    except (ValueError, AttributeError, TypeError):
        pass
    return info


def _error_details(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        errors = error.get("errors") or [{}]
        reason = str(errors[0].get("reason") or error.get("status") or "")[:80]
        return reason, str(error.get("message") or "")[:200]
    except (ValueError, AttributeError, IndexError):
        return "", ""


def _classify(status: int, reason: str, message: str) -> DriveError:
    text = f"Drive API error {status}" + (f" ({reason})" if reason else "")
    if status == 404:
        return DriveNotFound(text, status=status, reason=reason)
    if status == 409:
        return DriveConflict(text, status=status, reason=reason)
    if status == 403 and reason in _RETRYABLE_403_REASONS:
        return DriveTransientError(text, status=status, reason=reason, retryable=True)
    if status == 403 and reason in _QUOTA_REASONS:
        return DriveQuotaError(text, status=status, reason=reason)
    if status in (401, 403):
        return DrivePermissionError(text, status=status, reason=reason)
    if status in _RETRYABLE_STATUS:
        return DriveTransientError(text, status=status, reason=reason, retryable=True)
    return DriveError(text, status=status, reason=reason)


class _RateLimiter:
    def __init__(self, per_second: float, monotonic, sleep):
        self._rate = per_second
        self._capacity = max(1.0, per_second)
        self._tokens = self._capacity
        self._last = monotonic()
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                delay = (1.0 - self._tokens) / self._rate
            self._sleep(delay)


class DriveDownload:
    """An open streaming response. Always use as a context manager (or call close())."""

    def __init__(self, response: httpx.Response):
        self._response = response
        self.status = response.status_code
        self.headers = dict(response.headers)

    def iter_bytes(self, chunk_size: int = 256 * 1024):
        yield from self._response.iter_bytes(chunk_size)

    def close(self) -> None:
        self._response.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class DriveClient:
    def __init__(self, config: DriveConfig, tokens: TokenProvider, http: httpx.Client, *, sleep=time.sleep, monotonic=time.monotonic, rng=random.random):
        self._config = config
        self._tokens = tokens
        self._http = http
        self._sleep = sleep
        self._rng = rng
        self._limiter = _RateLimiter(config.rate_limit_per_second, monotonic, sleep)

    def __repr__(self) -> str:
        return "DriveClient(<hidden>)"

    # ------------------------------------------------------------------ transport

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(120.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(60.0, 2.0 ** (attempt - 1)) * (0.5 + 0.5 * self._rng())

    def _request(self, method: str, url: str, *, params=None, json_body=None, content=None, headers=None, transfer=False, stream=False, ok=(200,), send_token_to=None):
        timeout = httpx.Timeout(
            connect=self._config.connect_timeout,
            read=self._config.transfer_timeout if transfer else self._config.read_timeout,
            write=self._config.transfer_timeout if transfer else self._config.read_timeout,
            pool=self._config.connect_timeout,
        )
        refreshed = False
        last_error: DriveError | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            self._limiter.wait()
            token = self._tokens.get_token()  # DriveAuthError is not retryable and propagates
            request_headers = dict(headers or {})
            if send_token_to is None or urlparse(url).hostname == send_token_to:
                request_headers["Authorization"] = f"Bearer {token}"
            try:
                request = self._http.build_request(method, url, params=params, json=json_body, content=content, headers=request_headers, timeout=timeout)
                response = self._http.send(request, stream=stream)
            except httpx.TransportError as exc:
                last_error = DriveTransientError(f"Network error talking to Drive ({type(exc).__name__}).", retryable=True)
                if attempt >= self._config.max_attempts:
                    raise last_error from None
                self._sleep(self._backoff(attempt, None))
                continue
            if response.status_code in ok:
                return response
            if stream:
                response.read()
            reason, message = _error_details(response)
            info = _error_info(response)
            status = response.status_code
            retry_after = response.headers.get("Retry-After")
            response.close()
            if status == 401 and not refreshed:
                refreshed = True
                self._tokens.invalidate()
                continue
            error = _classify(status, reason, message)
            error.detail, error.google_status, error.detail_reason = info["message"], info["google_status"], info["detail_reason"]
            if error.retryable and attempt < self._config.max_attempts:
                last_error = error
                self._sleep(self._backoff(attempt, retry_after))
                continue
            raise error
        raise last_error or DriveError("Drive request failed.")

    # ------------------------------------------------------------------ files

    def generate_ids(self, count: int = 1) -> list[str]:
        response = self._request("GET", f"{self._config.api_base}/files/generateIds", params={"count": count, "space": "drive"})
        return list(response.json()["ids"])

    def get_file(self, file_id: str, fields: str = FILE_FIELDS) -> DriveObject:
        response = self._request("GET", f"{self._config.api_base}/files/{file_id}", params={"fields": fields, "supportsAllDrives": "true"})
        return DriveObject.from_api(response.json())

    def list_files(self, query: str, *, fields: str = FILE_FIELDS, page_size: int = 100):
        token = None
        while True:
            params = {
                "q": query,
                "fields": f"nextPageToken,files({fields})",
                "pageSize": page_size,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "corpora": "allDrives",
            }
            if token:
                params["pageToken"] = token
            body = self._request("GET", f"{self._config.api_base}/files", params=params).json()
            for item in body.get("files", []):
                yield DriveObject.from_api(item)
            token = body.get("nextPageToken")
            if not token:
                return

    def find_children(self, parent_id: str, *, name: str | None = None, folders_only: bool = False, app_property: tuple[str, str] | None = None, include_trashed: bool = False) -> list[DriveObject]:
        clauses = [f"'{escape_query(parent_id)}' in parents"]
        if not include_trashed:
            clauses.append("trashed = false")
        if name is not None:
            clauses.append(f"name = '{escape_query(name)}'")
        if folders_only:
            clauses.append(f"mimeType = '{FOLDER_MIME}'")
        if app_property is not None:
            clauses.append(f"appProperties has {{ key='{escape_query(app_property[0])}' and value='{escape_query(app_property[1])}' }}")
        return list(self.list_files(" and ".join(clauses)))

    def create_folder(self, name: str, parent_id: str, *, file_id: str | None = None, app_properties: dict | None = None) -> DriveObject:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        if file_id:
            body["id"] = file_id
        if app_properties:
            body["appProperties"] = app_properties
        response = self._request("POST", f"{self._config.api_base}/files", params={"supportsAllDrives": "true", "fields": FILE_FIELDS}, json_body=body)
        return DriveObject.from_api(response.json())

    def upload_file(self, *, file_id: str, name: str, parent_id: str, content: bytes, mime_type: str = "application/pdf", app_properties: dict | None = None) -> DriveObject:
        """Resumable upload with a caller-chosen (pre-generated) id. A second attempt with the same id answers 409 (DriveConflict),
        which the caller treats as 'an earlier attempt already created it' and adopts after verifying checksum and size."""
        metadata = {"id": file_id, "name": name, "parents": [parent_id], "mimeType": mime_type}
        if app_properties:
            metadata["appProperties"] = app_properties
        init = self._request(
            "POST",
            f"{self._config.upload_base}/files",
            params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": FILE_FIELDS},
            json_body=metadata,
            headers={"X-Upload-Content-Type": mime_type, "X-Upload-Content-Length": str(len(content))},
        )
        session_url = init.headers.get("Location", "")
        host = urlparse(session_url).hostname or ""
        if not session_url.startswith("https://") or not host.endswith("googleapis.com"):
            raise DriveError("Drive returned an unexpected upload session address.")
        response = self._request(
            "PUT", session_url, content=content, headers={"Content-Type": mime_type}, transfer=True, ok=(200, 201), send_token_to=host
        )
        return DriveObject.from_api(response.json())

    @contextmanager
    def open_download(self, file_id: str, *, range_header: str | None = None):
        headers = {"Range": range_header} if range_header else None
        response = self._request(
            "GET", f"{self._config.api_base}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}, headers=headers, transfer=True, stream=True, ok=(200, 206)
        )
        download = DriveDownload(response)
        try:
            yield download
        finally:
            download.close()

    def download_bytes(self, file_id: str) -> bytes:
        with self.open_download(file_id) as download:
            return b"".join(download.iter_bytes())

    def update_file(self, file_id: str, *, name: str | None = None, add_parents: str | None = None, remove_parents: str | None = None, trashed: bool | None = None, app_properties: dict | None = None) -> DriveObject:
        body: dict = {}
        if name is not None:
            body["name"] = name
        if trashed is not None:
            body["trashed"] = trashed
        if app_properties is not None:
            body["appProperties"] = app_properties
        params = {"supportsAllDrives": "true", "fields": FILE_FIELDS}
        if add_parents:
            params["addParents"] = add_parents
        if remove_parents:
            params["removeParents"] = remove_parents
        response = self._request("PATCH", f"{self._config.api_base}/files/{file_id}", params=params, json_body=body)
        return DriveObject.from_api(response.json())

    # ------------------------------------------------------------------ read-only diagnostics (used by the live spike)

    def get_raw(self, file_id: str, fields: str) -> dict:
        """Raw metadata for the given fields (e.g. capabilities, owners, driveId). Read-only."""
        return self._request("GET", f"{self._config.api_base}/files/{file_id}", params={"fields": fields, "supportsAllDrives": "true"}).json()

    def list_permissions(self, file_id: str) -> list[dict]:
        """Who can access the file: only the permission type and role are requested, never e-mail addresses. Read-only; the caller
        needs to be allowed to see sharing settings (otherwise Drive answers 403 and this raises DrivePermissionError)."""
        body = self._request(
            "GET", f"{self._config.api_base}/files/{file_id}/permissions", params={"fields": "permissions(type,role)", "supportsAllDrives": "true", "pageSize": 100}
        ).json()
        return list(body.get("permissions", []))

    def get_shared_drive(self, drive_id: str, fields: str = "id,name,hidden,capabilities(canAddChildren,canListChildren)") -> dict:
        """drives.get: the Shared Drive itself (not a file in it). Read-only; used to diagnose a refused read of the configured root."""
        return self._request("GET", f"{self._config.api_base}/drives/{drive_id}", params={"fields": fields}).json()

    def get_about(self) -> dict:
        """The signed-in account's storage quota (no personal data requested). Read-only."""
        return self._request("GET", f"{self._config.api_base}/about", params={"fields": "storageQuota"}).json()

    # ------------------------------------------------------------------ change tracking

    def get_start_page_token(self, drive_id: str | None = None) -> str:
        params = {"supportsAllDrives": "true"}
        if drive_id:
            params["driveId"] = drive_id
        return self._request("GET", f"{self._config.api_base}/changes/startPageToken", params=params).json()["startPageToken"]

    def list_changes(self, page_token: str, *, drive_id: str | None = None, page_size: int = 100) -> dict:
        params = {
            "pageToken": page_token,
            "pageSize": page_size,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "includeRemoved": "true",
            "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,time,file({FILE_FIELDS}))",
        }
        if drive_id:
            params["driveId"] = drive_id
        try:
            return self._request("GET", f"{self._config.api_base}/changes", params=params).json()
        except DriveError as exc:
            if exc.status == 410 or (exc.status == 400 and "page" in (exc.reason + " ").lower()):
                raise DriveChangeTokenExpired("The Drive change token has expired.", status=exc.status, reason=exc.reason) from None
            raise


__all__ = [
    "DriveAuthError",
    "DriveChangeTokenExpired",
    "DriveClient",
    "DriveConflict",
    "DriveDownload",
    "DriveError",
    "DriveIntegrityError",
    "DriveNotFound",
    "DriveObject",
    "DrivePermissionError",
    "DriveQuotaError",
    "DriveTransientError",
    "FOLDER_MIME",
    "escape_query",
]
