"""Workload Identity Federation (Google "external account") sign-in for the Drive API, with no third-party Google library.

Why: a company server should not hold a long-lived Google private key, and an organisation can forbid creating service-account keys. With federation the server holds
NO Google secret at all: the company identity provider (for example Microsoft Entra ID or AD FS) issues a short-lived identity token, which Google's Security Token
Service (STS) exchanges for a federated access token, which (optionally) is exchanged again to act as a service account that has been granted access to the Shared Drive.

     subject token (a file the server keeps refreshed)  --STS-->  federated token  --IAM Credentials generateAccessToken-->  Drive access token

Configuration: the standard Google "external account credential configuration" JSON file (made with `gcloud iam workload-identity-pools create-cred-config ... --credential-source-file=...`),
referenced by SPINCO_GDRIVE_WIF_CONFIG_FILE (kept OUTSIDE the repository). It contains NO secret - only the pool audience, endpoints and the path of the subject-token file.
Only FILE-sourced subject tokens are supported: URL- and executable-sourced configurations are refused (an executable source would let a configuration file run programs).

Safety: the STS and IAM endpoints must be Google's own hosts (a tampered configuration cannot redirect the token exchange); nothing secret - subject token, federated
token, access token, response body - is ever logged, included in an exception message or shown by repr().

NOT verified against Google's real service by the project's tests (they use a fake transport): the first real exchange must be done with the company's IdP in staging.
"""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.services.drive.auth import (
    TRANSIENT_TOKEN_STATUSES,
    DriveAuthPermanentError,
    DriveAuthTransientError,
    SingleFlightToken,
    TokenSettings,
)

STS_HOSTS = frozenset({"sts.googleapis.com", "sts.mtls.googleapis.com"})
IAM_HOSTS = frozenset({"iamcredentials.googleapis.com", "iamcredentials.mtls.googleapis.com"})
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_AUDIENCE_RE = re.compile(r"^//iam\.googleapis\.com/(projects/\d+/locations/global/workloadIdentityPools/[A-Za-z0-9._-]+/providers/[A-Za-z0-9._-]+|locations/global/workforcePools/[A-Za-z0-9._-]+/providers/[A-Za-z0-9._-]+)$")
_MAX_SUBJECT_TOKEN_BYTES = 64 * 1024


def _safe_code(response: httpx.Response) -> str:
    """A short error code from a failed response (STS: `error`; IAM: `error.status`), reduced to harmless characters - never any other part of the body."""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        raw = str(error.get("status", "unknown")) if isinstance(error, dict) else str(error or "unknown")
    except (ValueError, AttributeError):
        raw = "unknown"
    return re.sub(r"[^A-Za-z0-9_ .-]", "", raw)[:60] or "unknown"


@dataclass(frozen=True)
class ExternalAccountConfig:
    audience: str
    subject_token_type: str
    token_url: str
    impersonation_url: str | None
    subject_token_file: Path
    subject_format: str  # "text" | "json"
    subject_field: str | None

    def __repr__(self) -> str:
        return "ExternalAccountConfig(<hidden>)"

    @classmethod
    def from_file(cls, path: Path) -> "ExternalAccountConfig":
        def refuse(message: str):
            raise DriveAuthPermanentError(message) from None

        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            refuse("The external-account configuration file could not be read.")
        if not isinstance(data, dict) or data.get("type") != "external_account":
            refuse("The configuration file is not an external-account (Workload Identity Federation) file.")
        audience, subject_type, token_url = data.get("audience"), data.get("subject_token_type"), data.get("token_url")
        if not (isinstance(audience, str) and _AUDIENCE_RE.match(audience)):
            refuse("The external-account audience is not a Google workload identity pool provider.")
        if not (isinstance(subject_type, str) and subject_type):
            refuse("The external-account configuration has no subject_token_type.")
        if not (isinstance(token_url, str) and urlparse(token_url).scheme == "https" and urlparse(token_url).hostname in STS_HOSTS):
            refuse("The token_url must be Google's Security Token Service (sts.googleapis.com) over HTTPS.")
        impersonation = data.get("service_account_impersonation_url")
        if impersonation is not None:
            parsed = urlparse(impersonation) if isinstance(impersonation, str) else None
            if not (parsed and parsed.scheme == "https" and parsed.hostname in IAM_HOSTS and parsed.path.endswith(":generateAccessToken")):
                refuse("The service_account_impersonation_url must be Google's IAM Credentials endpoint (generateAccessToken) over HTTPS.")
        source = data.get("credential_source")
        if not isinstance(source, dict) or set(source) - {"file", "format"} or not isinstance(source.get("file"), str) or not source["file"]:
            refuse("Only a file-sourced subject token is supported (credential_source.file); URL and executable sources are not.")
        fmt = source.get("format") or {"type": "text"}
        if not isinstance(fmt, dict) or fmt.get("type", "text") not in ("text", "json"):
            refuse("The subject-token format must be text or json.")
        field = fmt.get("subject_token_field_name")
        if fmt.get("type") == "json" and not (isinstance(field, str) and field):
            refuse("A json subject-token format needs subject_token_field_name.")
        return cls(audience, subject_type, token_url, impersonation, Path(source["file"]), fmt.get("type", "text"), field if fmt.get("type") == "json" else None)

    def read_subject_token(self) -> str:
        """Reads the subject token file afresh (an external process keeps it current). Never returns or logs its path in an error."""
        try:
            raw = self.subject_token_file.read_bytes()
        except OSError:
            raise DriveAuthPermanentError("The subject-token file could not be read.") from None
        if not raw or len(raw) > _MAX_SUBJECT_TOKEN_BYTES:
            raise DriveAuthPermanentError("The subject-token file is empty or too large.")
        text = raw.decode("utf-8-sig", errors="replace").strip()
        if self.subject_format == "json":
            try:
                value = json.loads(text).get(self.subject_field)
            except (ValueError, AttributeError):
                value = None
            text = value.strip() if isinstance(value, str) else ""
        if not text:
            raise DriveAuthPermanentError("The subject-token file contains no token.")
        return text


class ExternalAccountTokenProvider(SingleFlightToken):
    """Federated sign-in (see the module docstring). Same single-flight, bounded behaviour as the other providers (auth.py); `get_token()` / `invalidate()` are the interface."""

    def __init__(self, config: ExternalAccountConfig, scope: str, http: httpx.Client, clock=time.time, *, settings: TokenSettings | None = None, monotonic=time.monotonic):
        super().__init__(settings, clock, monotonic)
        self._config = config
        self._scope = scope
        self._http = http

    def __repr__(self) -> str:
        return "ExternalAccountTokenProvider(<hidden>)"

    def _post(self, url: str, budget, **kwargs) -> httpx.Response:
        remaining = budget()
        if remaining <= 0:
            raise DriveAuthTransientError("Google's token endpoint did not answer in time.")
        try:
            return self._http.post(url, timeout=self._settings.post_timeout(remaining), **kwargs)
        except httpx.HTTPError:  # timeouts and network errors alike: their text can carry request data, so none is kept
            raise DriveAuthTransientError("Could not reach Google's token service.") from None

    @staticmethod
    def _check(response: httpx.Response, what: str) -> dict:
        if response.status_code != 200:
            message = f"Google rejected the {what} ({_safe_code(response)})."
            if response.status_code in TRANSIENT_TOKEN_STATUSES or response.status_code >= 500:
                raise DriveAuthTransientError(message)
            raise DriveAuthPermanentError(message)
        try:
            body = response.json()
        except ValueError:
            raise DriveAuthTransientError("Google's token response was not understood.") from None
        if not isinstance(body, dict):
            raise DriveAuthTransientError("Google's token response was not understood.")
        return body

    def _refresh(self, budget) -> tuple[str, float]:
        cfg = self._config
        impersonating = cfg.impersonation_url is not None
        subject = cfg.read_subject_token()
        sts = self._post(
            cfg.token_url,
            budget,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "audience": cfg.audience,
                "scope": CLOUD_PLATFORM_SCOPE if impersonating else self._scope,
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "subject_token_type": cfg.subject_token_type,
                "subject_token": subject,
            },
        )
        body = self._check(sts, "identity-token exchange")
        federated = body.get("access_token")
        if not isinstance(federated, str) or not federated:
            raise DriveAuthTransientError("Google's token response was not understood.")
        if not impersonating:
            try:
                return federated, float(body.get("expires_in", 3600))
            except (TypeError, ValueError):
                raise DriveAuthTransientError("Google's token response was not understood.") from None

        iam = self._post(
            cfg.impersonation_url,
            budget,
            headers={"Authorization": f"Bearer {federated}"},
            json={"scope": [self._scope], "lifetime": "3600s"},
        )
        body = self._check(iam, "service-account impersonation")
        access = body.get("accessToken")
        if not isinstance(access, str) or not access:
            raise DriveAuthTransientError("Google's token response was not understood.")
        lifetime = 3600.0
        expire = body.get("expireTime")
        if isinstance(expire, str):
            try:
                lifetime = (datetime.fromisoformat(expire.replace("Z", "+00:00")) - datetime.fromtimestamp(self._clock(), tz=timezone.utc)).total_seconds()
            except ValueError:
                lifetime = 3600.0
        return access, lifetime
