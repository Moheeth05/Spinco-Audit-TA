"""User OAuth 2.0 sign-in (installed-app / loopback flow with PKCE) for the Drive API, as an ADDITIVE alternative to the service account.

`UserOAuthTokenProvider` offers the same two methods the DriveClient already uses from `auth.TokenProvider` (`get_token()` and `invalidate()`), so
neither the client nor the storage layer changes. Files created through it belong to, and count against, the signed-in company user.

Built on httpx + the standard library only (no google-* packages):
  * authorization: random `state` and a PKCE (S256) verifier, a loopback receiver bound to 127.0.0.1 on a random port, single use, Host-checked;
  * the refresh token lives in ONE file outside the repository, created with a Windows ACL that grants only the current user (checked again on
    every use); an existing token file is never overwritten by the authorization flow;
  * nothing secret (client secret, authorization code, PKCE verifier, access or refresh token) is ever logged, printed, put in a repr or in an
    exception message - errors carry only a sanitised Google error code.
"""

import base64
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.services.drive.auth import DriveAuthError

DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_ALLOWED_AUTH_HOSTS = {"accounts.google.com"}
_ALLOWED_TOKEN_HOSTS = {"oauth2.googleapis.com"}
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{10,200}$")
_REFRESH_MARGIN_SECONDS = 120
_SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
_SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
TOKEN_FILE_VERSION = 1

REAUTHORIZE_HINT = "Run backend/drive_spike/oauth_authorize.py --authorize to sign in again."


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _safe_code(value) -> str:
    """Only a Google error CODE (lower-case letters and underscores) may ever appear in a message."""
    return re.sub(r"[^a-z_]", "", str(value).lower())[:40] or "unknown"


# ------------------------------------------------------------------------------------------------------------------ client config


@dataclass(frozen=True)
class OAuthClientConfig:
    client_id: str
    client_secret: str = field(repr=False)
    auth_uri: str = DEFAULT_AUTH_URI
    token_uri: str = DEFAULT_TOKEN_URI

    def __repr__(self) -> str:
        return f"OAuthClientConfig(client_id={self.client_id!r}, client_secret=<hidden>)"

    @classmethod
    def from_file(cls, path: Path) -> "OAuthClientConfig":
        """Reads the Desktop-app client file downloaded from Google Cloud ({"installed": {...}}). The file is referenced by path only; its
        content is never echoed. A web-application client is refused: its redirect URIs are fixed, a loopback flow needs a free port."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("malformed")
        except (OSError, ValueError):
            raise DriveAuthError("The OAuth client file could not be read as JSON.") from None
        section = data.get("installed")
        if not isinstance(section, dict):
            raise DriveAuthError("The OAuth client file is not a Desktop-app client (expected an 'installed' section).")
        client_id, secret = section.get("client_id"), section.get("client_secret")
        if not isinstance(client_id, str) or not _CLIENT_ID_RE.match(client_id) or not isinstance(secret, str) or not secret:
            raise DriveAuthError("The OAuth client file does not contain a usable client id and secret.")
        auth_uri = str(section.get("auth_uri") or DEFAULT_AUTH_URI)
        token_uri = str(section.get("token_uri") or DEFAULT_TOKEN_URI)
        for uri, hosts, what in ((auth_uri, _ALLOWED_AUTH_HOSTS, "authorization"), (token_uri, _ALLOWED_TOKEN_HOSTS, "token")):
            parsed = urlparse(uri)
            if parsed.scheme != "https" or parsed.hostname not in hosts:
                # The client secret is sent to the token endpoint, so it must really be Google's.
                raise DriveAuthError(f"The OAuth client file names an unexpected {what} endpoint.")
        return cls(client_id=client_id, client_secret=secret, auth_uri=auth_uri, token_uri=token_uri)


# ------------------------------------------------------------------------------------------------------------ token-file permissions

_IS_WINDOWS = sys.platform == "win32"
# Besides the current user, only these may appear in the ACL: the operating system itself, local administrators (who can take ownership of any
# file anyway) and "OWNER RIGHTS" (the file's owner - the current user - which Windows adds in directories created with a private mode).
# Anything else (Everyone, Users, Authenticated Users, another account) makes the file unusable.
_ALLOWED_OTHER_PRINCIPALS = {"nt authority\\system", "builtin\\administrators", "owner rights"}


def _system_tool(name: str) -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name)


def _run(args: list[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)


def _windows_user() -> tuple[str, str]:
    """(DOMAIN\\name, SID) of the account running this process."""
    result = _run([_system_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"])
    row = next(csv.reader([result.stdout.strip()]), [])
    if result.returncode != 0 or len(row) < 2:
        raise OSError("could not identify the current Windows user")
    return row[0].strip(), row[1].strip()


def restrict_to_current_user(path: Path) -> None:
    """Windows: drop inherited ACL entries and grant ONLY the current user. POSIX: mode 0600. Verified afterwards; raises DriveAuthError."""
    try:
        if _IS_WINDOWS:
            _name, sid = _windows_user()
            result = _run([_system_tool("icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"])
            if result.returncode != 0:
                raise OSError("icacls failed")
        else:
            os.chmod(path, 0o600)
    except (OSError, subprocess.SubprocessError):
        raise DriveAuthError("Could not restrict the permissions of the OAuth token file to the current user.") from None
    problem = permission_problem(path)
    if problem:
        raise DriveAuthError(problem)


def permission_problem(path: Path, subject: str = "an OAuth token file") -> str | None:
    """None when only the current user (plus SYSTEM / Administrators on Windows) can access the file; otherwise a fail-closed explanation."""
    path = Path(path)
    if not _IS_WINDOWS:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            return f"The permissions of {path} could not be read."
        return None if not mode & 0o077 else f"{path} is accessible to other users (mode {oct(mode)}); it must be 0600."
    try:
        me, _sid = _windows_user()
        result = _run([_system_tool("icacls.exe"), str(path)])
    except (OSError, subprocess.SubprocessError):
        return f"The permissions of {path} could not be read."
    if result.returncode != 0:
        return f"The permissions of {path} could not be read."
    listing = re.sub(re.escape(str(path)), "", result.stdout, flags=re.IGNORECASE)
    principals = {p.strip().lower() for p in re.findall(r"^\s*(\S[^:\r\n]*?):\(", listing, flags=re.MULTILINE)}
    others = sorted(principals - {me.lower()} - _ALLOWED_OTHER_PRINCIPALS)
    if others:
        return f"{path} is accessible to other accounts ({', '.join(others)}); only the current user may access {subject}."
    return None


# --------------------------------------------------------------------------------------------------------------------- token file


@dataclass(frozen=True)
class StoredToken:
    refresh_token: str = field(repr=False)
    client_id: str
    scope: str
    created_at: str

    def __repr__(self) -> str:
        return f"StoredToken(client_id={self.client_id!r}, scope={self.scope!r}, refresh_token=<hidden>)"


class OAuthTokenStore:
    """The refresh token, in one JSON file OUTSIDE the repository. Read only after its permissions pass; never overwritten by the
    authorization flow (`save_new`), replaced atomically only when Google rotates the token (`replace`)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def __repr__(self) -> str:
        return f"OAuthTokenStore({str(self.path)!r})"

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> StoredToken:
        if not self.exists():
            raise DriveAuthError(f"No stored Google authorization was found at {self.path}. {REAUTHORIZE_HINT}")
        problem = permission_problem(self.path)
        if problem:
            raise DriveAuthError(problem)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != TOKEN_FILE_VERSION or data.get("type") != "authorized_user":
                raise ValueError("unsupported")
            refresh, client_id, scope = data["refresh_token"], data["client_id"], data["scope"]
            if not all(isinstance(v, str) and v for v in (refresh, client_id, scope)):
                raise ValueError("malformed")
            return StoredToken(refresh_token=refresh, client_id=client_id, scope=scope, created_at=str(data.get("created_at", "")))
        except (OSError, ValueError, KeyError):
            raise DriveAuthError("The OAuth token file is not valid. " + REAUTHORIZE_HINT) from None

    def save_new(self, token: StoredToken) -> None:
        self._write(token, replace=False)

    def replace(self, token: StoredToken) -> None:
        self._write(token, replace=True)

    def _write(self, token: StoredToken, *, replace: bool) -> None:
        directory = self.path.parent
        if not directory.is_dir():
            raise DriveAuthError(f"The folder for the OAuth token file does not exist: {directory}")
        if not replace and self.path.exists():
            raise DriveAuthError(f"An OAuth token file already exists at {self.path}; it is never overwritten. Move it aside to authorize again.")
        temp = directory / f".{self.path.name}.{secrets.token_hex(6)}.tmp"
        try:
            # Created empty and locked down BEFORE the secret is written, so it is never readable by another account, not even briefly.
            os.close(os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600))
            restrict_to_current_user(temp)
            payload = {"version": TOKEN_FILE_VERSION, "type": "authorized_user", "client_id": token.client_id, "scope": token.scope,
                       "created_at": token.created_at, "refresh_token": token.refresh_token}
            temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            if replace:
                os.replace(temp, self.path)
            elif _IS_WINDOWS:
                os.rename(temp, self.path)  # on Windows this fails rather than overwrite
            else:
                os.link(temp, self.path)  # atomic and refuses to overwrite
        except FileExistsError:
            raise DriveAuthError(f"An OAuth token file already exists at {self.path}; it is never overwritten.") from None
        except OSError:
            raise DriveAuthError("The OAuth token file could not be written.") from None
        finally:
            if temp.exists():
                temp.unlink()


def inspect_token_file(store: OAuthTokenStore, client: OAuthClientConfig | None, scope: str | None) -> dict:
    """Secret-free status for a pre-flight check. Reads the token file only if its permissions are acceptable."""
    status = {"exists": store.exists(), "permissions_ok": None, "valid": None, "client_matches": None, "scope_ok": None, "created_at": None, "problem": None}
    if not status["exists"]:
        return status
    problem = permission_problem(store.path)
    status["permissions_ok"] = problem is None
    if problem:
        status["problem"] = problem
        return status
    try:
        token = store.load()
    except DriveAuthError as exc:
        status.update(valid=False, problem=str(exc))
        return status
    status.update(valid=True, created_at=token.created_at)
    if client is not None:
        status["client_matches"] = token.client_id == client.client_id
    if scope is not None:
        status["scope_ok"] = _scope_covers(set(token.scope.split()), scope)
    return status


def _scope_covers(granted: set[str], wanted: str) -> bool:
    wanted_set = set(wanted.split())
    return all(w in granted or (w == _SCOPE_DRIVE_FILE and _SCOPE_DRIVE in granted) for w in wanted_set)


# ------------------------------------------------------------------------------------------------------------------ token endpoint


def _token_request(client: OAuthClientConfig, http: httpx.Client, form: dict, *, what: str, attempts: int = 1, sleep=time.sleep) -> dict:
    """POSTs to Google's token endpoint. Retries only network errors / 429 / 5xx (when `attempts` > 1); everything else is final."""
    data = {**form, "client_id": client.client_id, "client_secret": client.client_secret}
    for attempt in range(1, attempts + 1):
        try:
            response = http.post(client.token_uri, data=data, timeout=20.0)
        except httpx.HTTPError:
            if attempt < attempts:
                sleep(2.0 ** (attempt - 1))
                continue
            raise DriveAuthError("Could not reach the Google token endpoint.") from None
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                body = None
            if not isinstance(body, dict) or not isinstance(body.get("access_token"), str) or not body["access_token"]:
                raise DriveAuthError(f"Google's {what} response was not understood.")
            return body
        code = "unknown"
        try:
            code = _safe_code(response.json().get("error"))
        except (ValueError, AttributeError):
            pass
        if (response.status_code == 429 or response.status_code >= 500) and attempt < attempts:
            sleep(2.0 ** (attempt - 1))
            continue
        if code == "invalid_grant":
            raise DriveAuthError(f"Google rejected the {what} (invalid_grant): the authorization was revoked or has expired. {REAUTHORIZE_HINT}")
        raise DriveAuthError(f"Google rejected the {what} ({code}).")
    raise DriveAuthError(f"Google's {what} did not complete.")  # unreachable; keeps the type checker honest


# ------------------------------------------------------------------------------------------------------------------------ provider


class UserOAuthTokenProvider:
    """Drop-in alternative to auth.TokenProvider (same `get_token()` / `invalidate()`), signing in as a person through their stored refresh token."""

    def __init__(self, client: OAuthClientConfig, store: OAuthTokenStore, scope: str, http: httpx.Client, clock=time.time, sleep=time.sleep):
        self._client = client
        self._store = store
        self._scope = scope
        self._http = http
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def __repr__(self) -> str:
        return "UserOAuthTokenProvider(<hidden>)"

    def _stored_token(self) -> StoredToken:
        stored = self._store.load()  # checks the permissions first; re-read at every refresh, so a re-authorization is picked up
        if stored.client_id != self._client.client_id:
            raise DriveAuthError("The stored authorization belongs to a different OAuth client. " + REAUTHORIZE_HINT)
        if not _scope_covers(set(stored.scope.split()), self._scope):
            raise DriveAuthError("The stored authorization does not cover the configured Drive scope. " + REAUTHORIZE_HINT)
        return stored

    def get_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._expires_at - _REFRESH_MARGIN_SECONDS:
                return self._token
            stored = self._stored_token()
            body = _token_request(
                self._client, self._http, {"grant_type": "refresh_token", "refresh_token": stored.refresh_token}, what="token refresh", attempts=3, sleep=self._sleep
            )
            rotated = body.get("refresh_token")
            if isinstance(rotated, str) and rotated and not hmac.compare_digest(rotated.encode("utf-8"), stored.refresh_token.encode("utf-8")):  # bytes: str input must be ASCII
                self._store.replace(StoredToken(refresh_token=rotated, client_id=stored.client_id, scope=stored.scope, created_at=stored.created_at))
            try:
                expires_in = float(body.get("expires_in", 3600))
            except (TypeError, ValueError):
                expires_in = 3600.0
            self._token = body["access_token"]
            self._expires_at = now + expires_in
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0


# ------------------------------------------------------------------------------------------------------------ interactive authorization


def new_pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge). 64 random bytes -> an 86-character verifier (RFC 7636 allows 43-128)."""
    verifier = secrets.token_urlsafe(64)
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    return secrets.token_urlsafe(32)


class _CallbackReceiver:
    """One-shot HTTP receiver for the redirect, bound to the loopback interface only, on a random port."""

    def __init__(self, expected_state: str):
        self._expected = expected_state
        self._lock = threading.Lock()
        self.done = threading.Event()
        self.outcome: tuple[str, str] | None = None  # ("code", value) or ("error", sanitised code)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10
            server_version = "SpincoOAuthReceiver"
            sys_version = ""

            def log_message(self, *args):  # the query string holds the one-time code: never log requests
                pass

            def _reply(self, status: int, text: str) -> None:
                body = f"<html><body><p>{text}</p></body></html>".encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                port = self.server.server_address[1]
                if (self.headers.get("Host") or "").lower() != f"127.0.0.1:{port}":  # DNS-rebinding guard
                    return self._reply(400, "Bad request.")
                parsed = urlparse(self.path)
                if parsed.path != "/":
                    return self._reply(404, "Not found.")
                query = parse_qs(parsed.query, keep_blank_values=True)
                with receiver._lock:
                    if receiver.done.is_set():
                        return self._reply(410, "This authorization link has already been used.")
                    states = query.get("state", [])
                    if len(states) != 1 or not hmac.compare_digest(states[0].encode(), receiver._expected.encode()):
                        receiver.outcome = ("error", "state_mismatch")
                    elif "error" in query:
                        receiver.outcome = ("error", _safe_code(query["error"][0]))
                    elif len(query.get("code", [])) == 1 and 0 < len(query["code"][0]) <= 2048:
                        receiver.outcome = ("code", query["code"][0])
                    else:
                        receiver.outcome = ("error", "missing_code")
                    receiver.done.set()
                ok = receiver.outcome[0] == "code"
                self._reply(200 if ok else 400, "Authorization received. You can close this window and return to the terminal." if ok else "Authorization failed. Return to the terminal.")

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@dataclass(frozen=True)
class AuthorizationResult:
    scope: str
    token_file: Path
    created_at: str


def build_authorization_url(client: OAuthClientConfig, *, redirect_uri: str, scope: str, state: str, challenge: str, login_hint: str | None = None) -> str:
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",  # a refresh token
        "prompt": "consent",  # ... and Google returns it even if this account has authorized before
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{client.auth_uri}?{urlencode(params)}"


def authorize_interactively(
    client: OAuthClientConfig,
    store: OAuthTokenStore,
    scope: str,
    *,
    http: httpx.Client | None = None,
    open_browser=webbrowser.open,
    announce=None,
    timeout: float = 300.0,
    login_hint: str | None = None,
    clock=time.time,
) -> AuthorizationResult:
    """The whole installed-app flow. Writes ONLY the token file; it contacts Google's consent page and token endpoint, never the Drive API."""
    if store.exists():
        raise DriveAuthError(f"An OAuth token file already exists at {store.path}; it is never overwritten. Move it aside to authorize again.")
    if not store.path.parent.is_dir():
        raise DriveAuthError(f"The folder for the OAuth token file does not exist: {store.path.parent}")
    verifier, challenge = new_pkce()
    state = new_state()
    receiver = _CallbackReceiver(state)
    receiver.start()
    try:
        url = build_authorization_url(client, redirect_uri=receiver.redirect_uri, scope=scope, state=state, challenge=challenge, login_hint=login_hint)
        if announce:
            announce(url)
        try:
            open_browser(url)
        except Exception:  # noqa: BLE001 - a browser that cannot be opened is not fatal: the URL was announced
            pass
        if not receiver.done.wait(timeout):
            raise DriveAuthError("Timed out waiting for the Google sign-in to complete.")
        kind, value = receiver.outcome or ("error", "unknown")
    finally:
        receiver.close()
    if kind == "error":
        if value == "state_mismatch":
            raise DriveAuthError("The sign-in response did not match this authorization request (state mismatch); nothing was saved.")
        raise DriveAuthError(f"Google did not grant the authorization ({value}).")
    owns_http = http is None
    http = http or httpx.Client()
    try:
        body = _token_request(
            client,
            http,
            {"grant_type": "authorization_code", "code": value, "redirect_uri": receiver.redirect_uri, "code_verifier": verifier},
            what="authorization code",
        )
    finally:
        if owns_http:
            http.close()
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise DriveAuthError("Google did not return a refresh token. Remove this app's access in the Google account settings and authorize again.")
    granted = str(body.get("scope") or "")
    if not _scope_covers(set(granted.split()), scope):
        raise DriveAuthError("Google granted less access than requested; nothing was saved.")
    created = datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.save_new(StoredToken(refresh_token=refresh, client_id=client.client_id, scope=granted, created_at=created))
    return AuthorizationResult(scope=granted, token_file=store.path, created_at=created)


__all__ = [
    "AuthorizationResult",
    "OAuthClientConfig",
    "OAuthTokenStore",
    "StoredToken",
    "UserOAuthTokenProvider",
    "authorize_interactively",
    "build_authorization_url",
    "inspect_token_file",
    "new_pkce",
    "new_state",
    "permission_problem",
    "restrict_to_current_user",
]
