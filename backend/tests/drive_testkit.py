"""Builds the full Drive stack (credentials -> token provider -> client -> folders -> adapter -> manager) on top of a FakeDrive.
Everything is synthetic: a throw-away RSA key generated in memory, a service-account file in the test's temp folder, fake ids."""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.drive.auth import ServiceAccountCredentials, TokenProvider
from app.services.drive.client import DriveClient
from app.services.drive.config import DEFAULT_SCOPE, DriveConfig
from app.services.drive.lifecycle import StorageManager
from app.services.drive.storage import DriveFolders, GoogleDriveStorage
from fake_drive import FakeDrive

_KEY = None


def rsa_key():
    global _KEY
    if _KEY is None:
        _KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # synthetic, never persisted outside a test temp dir
    return _KEY


def private_pem() -> str:
    return rsa_key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def write_service_account_file(directory: Path) -> Path:
    path = Path(directory) / "synthetic-service-account.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "synthetic-test@example-test.iam.gserviceaccount.com",
                "private_key": private_pem(),
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        encoding="utf-8",
    )
    return path


@dataclass
class Stack:
    fake: FakeDrive
    config: DriveConfig
    http: httpx.Client
    tokens: TokenProvider
    client: DriveClient
    folders: DriveFolders
    storage: GoogleDriveStorage
    manager: StorageManager
    sleeps: list
    credentials_path: Path


def make_stack(tmp_path: Path, *, max_attempts: int = 6, drive_id: str | None = None, rate: float = 1000.0, with_spool: bool = True, clock=None) -> Stack:
    fake = FakeDrive(service_account_public_key=rsa_key().public_key(), drive_id=drive_id)
    root = next(iter(fake.files))
    credentials_path = write_service_account_file(tmp_path)
    config = DriveConfig(root_folder_id=root, credentials_file=credentials_path, scope=DEFAULT_SCOPE, rate_limit_per_second=rate, max_attempts=max_attempts)
    http = httpx.Client(transport=fake.transport())
    credentials = ServiceAccountCredentials.from_file(credentials_path)
    tokens = TokenProvider(credentials, config.scope, http, **({"clock": clock} if clock else {}))
    sleeps: list = []
    client = DriveClient(config, tokens, http, sleep=sleeps.append, rng=lambda: 0.5)
    folders = DriveFolders(client, root)
    storage = GoogleDriveStorage(client, folders, temp_root=tmp_path)
    manager = StorageManager(storage, spool_dir=(tmp_path / "spool") if with_spool else None)
    return Stack(fake, config, http, tokens, client, folders, storage, manager, sleeps, credentials_path)


SYNTHETIC_CLIENT_ID = "synthetic-client-0001.apps.googleusercontent.com"
SYNTHETIC_CLIENT_SECRET = "SYNTHETIC-CLIENT-SECRET-must-never-leak"


def write_oauth_client_file(directory: Path, *, client_id: str = SYNTHETIC_CLIENT_ID, secret: str = SYNTHETIC_CLIENT_SECRET, section: str = "installed", **overrides) -> Path:
    """A synthetic Desktop-client file in Google's download format. Nothing here is a real credential."""
    body = {"client_id": client_id, "project_id": "synthetic-project", "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token", "client_secret": secret, "redirect_uris": ["http://localhost"]}
    body.update(overrides)
    path = Path(directory) / "synthetic-oauth-client.json"
    path.write_text(json.dumps({section: body}), encoding="utf-8")
    return path


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class FakeGoogleOAuth:
    """Stands in for Google's consent page and token endpoint (an httpx.MockTransport, no network). It enforces what Google enforces for an
    installed-app flow: client id + secret, single-use codes bound to the redirect URI, PKCE S256, revocable refresh tokens.
    Everything else is delegated to a FakeDrive, so a whole Drive stack can run on OAuth tokens. Proves OUR code only - not Google's behaviour."""

    def __init__(self, *, client_id: str = SYNTHETIC_CLIENT_ID, client_secret: str = SYNTHETIC_CLIENT_SECRET, rotate: bool = False, expires_in: int = 3600):
        self.client_id, self.client_secret, self.rotate, self.expires_in = client_id, client_secret, rotate, expires_in
        self.codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, str] = {}
        self.forms: list[dict] = []
        self.access_issued = 0
        self.refresh_issued = 0
        self.failures: list[dict] = []

    # ---- the consent page
    def issue_code(self, authorization_url: str, *, approve: bool = True) -> str:
        """What the user's click on 'Allow' produces. Fails loudly if the request is not a well-formed installed-app request."""
        query = {k: v[0] for k, v in parse_qs(urlparse(authorization_url).query).items()}
        assert urlparse(authorization_url).hostname == "accounts.google.com"
        assert query["client_id"] == self.client_id and query["response_type"] == "code" and query["code_challenge_method"] == "S256"
        assert query["redirect_uri"].startswith("http://127.0.0.1:") and query["state"] and query["code_challenge"] and query["access_type"] == "offline"
        code = f"fake-code-{len(self.codes) + 1}"
        self.codes[code] = {"challenge": query["code_challenge"], "redirect_uri": query["redirect_uri"], "scope": query["scope"]}
        return code

    # ---- the token endpoint
    def fail_next(self, times: int, status: int = 503, error: str = "backend_error") -> None:
        self.failures.append({"times": times, "status": status, "error": error})

    def revoke_all(self) -> None:
        self.refresh_tokens.clear()

    def _error(self, status: int, error: str) -> httpx.Response:
        return httpx.Response(status, json={"error": error, "error_description": "synthetic"})

    def _access(self) -> str:
        self.access_issued += 1
        return f"fake-token-oauth-{self.access_issued}"

    def _token(self, form: dict) -> httpx.Response:
        for failure in self.failures:
            if failure["times"] > 0:
                failure["times"] -= 1
                return self._error(failure["status"], failure["error"])
        if form.get("client_id") != self.client_id or not hmac.compare_digest(form.get("client_secret", ""), self.client_secret):
            return self._error(401, "invalid_client")
        if form.get("grant_type") == "authorization_code":
            record = self.codes.pop(form.get("code", ""), None)  # a code works once
            verifier = form.get("code_verifier", "")
            if record is None or form.get("redirect_uri") != record["redirect_uri"]:
                return self._error(400, "invalid_grant")
            if not 43 <= len(verifier) <= 128 or _b64url(hashlib.sha256(verifier.encode("ascii")).digest()) != record["challenge"]:
                return self._error(400, "invalid_grant")  # PKCE verification failed
            self.refresh_issued += 1
            refresh = f"fake-refresh-{self.refresh_issued}"
            self.refresh_tokens[refresh] = record["scope"]
            return httpx.Response(200, json={"access_token": self._access(), "refresh_token": refresh, "expires_in": self.expires_in, "scope": record["scope"], "token_type": "Bearer"})
        if form.get("grant_type") == "refresh_token":
            scope = self.refresh_tokens.get(form.get("refresh_token", ""))
            if scope is None:
                return self._error(400, "invalid_grant")
            body = {"access_token": self._access(), "expires_in": self.expires_in, "scope": scope, "token_type": "Bearer"}
            if self.rotate:
                self.refresh_issued += 1
                del self.refresh_tokens[form["refresh_token"]]
                body["refresh_token"] = f"fake-refresh-{self.refresh_issued}"
                self.refresh_tokens[body["refresh_token"]] = scope
            return httpx.Response(200, json=body)
        return self._error(400, "unsupported_grant_type")

    def transport(self, drive=None) -> httpx.MockTransport:
        inner = drive.transport() if drive is not None else None

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.googleapis.com" and request.url.path == "/token":
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                if form.get("grant_type") in ("authorization_code", "refresh_token"):
                    self.forms.append(form)
                    return self._token(form)
            if inner is None:
                raise httpx.ConnectError("no Drive behind this fake", request=request)
            return inner.handle_request(request)

        return httpx.MockTransport(handler)


def pdf_bytes(marker: str = "a") -> bytes:
    """A small, valid, synthetic one-page PDF whose bytes differ per marker."""
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200 + (sum(map(ord, marker)) % 400), height=200)
    writer.add_metadata({"/Title": f"synthetic-{marker}"})
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
