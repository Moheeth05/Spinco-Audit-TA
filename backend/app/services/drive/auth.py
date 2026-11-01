"""Service-account sign-in for the Drive API (OAuth 2.0 JWT-bearer grant), with no third-party Google library.

The JWT is signed RS256 with `cryptography` (already a bundled dependency) and exchanged at the token endpoint over httpx.
Nothing secret is ever logged or included in an exception message, and the objects here hide their secrets from repr().
"""

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_REFRESH_MARGIN_SECONDS = 120


class DriveAuthError(Exception):
    """Sign-in failed. The message contains only a Google error code, never a credential or a token."""


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
            raise DriveAuthError("The credentials file is not a valid service-account key file.") from None
        return cls(client_email=email, private_key_pem=key, token_uri=str(data.get("token_uri") or DEFAULT_TOKEN_URI))


class TokenProvider:
    """Caches one access token and refreshes it shortly before it expires. Thread-safe."""

    def __init__(self, credentials: ServiceAccountCredentials, scope: str, http: httpx.Client, clock=time.time):
        self._credentials = credentials
        self._scope = scope
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._key = None

    def __repr__(self) -> str:
        return "TokenProvider(<hidden>)"

    def _assertion(self, now: int) -> str:
        if self._key is None:
            try:
                self._key = load_pem_private_key(self._credentials.private_key_pem.encode("utf-8"), password=None)
            except (ValueError, TypeError):
                raise DriveAuthError("The service-account private key could not be read.") from None
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

    def get_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._expires_at - _REFRESH_MARGIN_SECONDS:
                return self._token
            assertion = self._assertion(int(now))
            try:
                response = self._http.post(
                    self._credentials.token_uri,
                    data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
                    timeout=20.0,
                )
            except httpx.HTTPError:
                raise DriveAuthError("Could not reach the Google token endpoint.") from None
            if response.status_code != 200:
                code = "unknown"
                try:
                    code = str(response.json().get("error", "unknown"))[:60]
                except ValueError:
                    pass
                raise DriveAuthError(f"Google rejected the service-account sign-in ({code}).")
            body = response.json()
            self._token = body["access_token"]
            self._expires_at = now + float(body.get("expires_in", 3600))
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0
