"""Drive configuration from environment variables. Credentials are referenced by FILE PATH only and never read into a log."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

ENABLED_ENV = "SPINCO_GDRIVE_ENABLED"
ROOT_FOLDER_ENV = "SPINCO_GDRIVE_ROOT_FOLDER_ID"
CREDENTIALS_FILE_ENV = "SPINCO_GDRIVE_CREDENTIALS_FILE"
SCOPE_ENV = "SPINCO_GDRIVE_SCOPE"
RATE_LIMIT_ENV = "SPINCO_GDRIVE_RATE_LIMIT_PER_SECOND"
# Authentication mode. The service account stays the default; user OAuth (a person's own authorization) must be asked for explicitly.
AUTH_MODE_ENV = "SPINCO_GDRIVE_AUTH_MODE"
OAUTH_CLIENT_FILE_ENV = "SPINCO_GDRIVE_OAUTH_CLIENT_FILE"
OAUTH_TOKEN_FILE_ENV = "SPINCO_GDRIVE_OAUTH_TOKEN_FILE"
AUTH_SERVICE_ACCOUNT = "service_account"
AUTH_OAUTH_USER = "oauth_user"

# The narrowest scope that can work with a folder a human created is a question for the live spike; `drive.file` normally cannot
# see such a folder, so the default is the full `drive` scope, limited in practice by what the service account has been shared on.
DEFAULT_SCOPE = "https://www.googleapis.com/auth/drive"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
_REPO_ROOT = Path(__file__).resolve().parents[4]


class DriveConfigError(Exception):
    """The Drive configuration is missing or unsafe. The message never contains a secret."""


@dataclass(frozen=True)
class DriveConfig:
    root_folder_id: str
    credentials_file: Path | None = None  # the service-account key; unused (None) in oauth_user mode
    scope: str = DEFAULT_SCOPE
    auth_mode: str = AUTH_SERVICE_ACCOUNT
    oauth_client_file: Path | None = None
    oauth_token_file: Path | None = None
    rate_limit_per_second: float = 10.0
    max_attempts: int = 6
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    transfer_timeout: float = 120.0
    api_base: str = "https://www.googleapis.com/drive/v3"
    upload_base: str = "https://www.googleapis.com/upload/drive/v3"


def is_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ENABLED_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def auth_mode(env=None) -> str:
    """`service_account` unless SPINCO_GDRIVE_AUTH_MODE explicitly says `oauth_user`. Anything else is an error, never a silent fallback."""
    env = os.environ if env is None else env
    value = str(env.get(AUTH_MODE_ENV, "")).strip().lower()
    if value in ("", AUTH_SERVICE_ACCOUNT):
        return AUTH_SERVICE_ACCOUNT
    if value == AUTH_OAUTH_USER:
        return AUTH_OAUTH_USER
    raise DriveConfigError(f"{AUTH_MODE_ENV} must be '{AUTH_SERVICE_ACCOUNT}' or '{AUTH_OAUTH_USER}'.")


def _outside_repo(raw_path: str, message: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    try:
        path.relative_to(_REPO_ROOT)
    except ValueError:
        return path
    raise DriveConfigError(message)


def load_config(env=None) -> DriveConfig:
    """Raises DriveConfigError (with a precise, secret-free instruction) unless everything required is present and safe."""
    env = os.environ if env is None else env
    root = str(env.get(ROOT_FOLDER_ENV, "")).strip()
    if not _ID_RE.match(root):
        raise DriveConfigError(f"{ROOT_FOLDER_ENV} must be set to the Drive folder id of the test root (letters, digits, '-' and '_').")
    mode = auth_mode(env)
    credentials_file = oauth_client_file = oauth_token_file = None
    if mode == AUTH_OAUTH_USER:
        raw_client, raw_token = str(env.get(OAUTH_CLIENT_FILE_ENV, "")).strip(), str(env.get(OAUTH_TOKEN_FILE_ENV, "")).strip()
        if not raw_client:
            raise DriveConfigError(f"{OAUTH_CLIENT_FILE_ENV} must point to the OAuth Desktop-client file (kept outside the repository).")
        if not raw_token:
            raise DriveConfigError(f"{OAUTH_TOKEN_FILE_ENV} must name the OAuth token file (kept outside the repository).")
        oauth_client_file = _outside_repo(raw_client, "The OAuth client file must be OUTSIDE the repository so it can never be committed.")
        oauth_token_file = _outside_repo(raw_token, "The OAuth token file must be OUTSIDE the repository so it can never be committed.")
        if not oauth_client_file.is_file():
            raise DriveConfigError(f"The OAuth client file named by {OAUTH_CLIENT_FILE_ENV} does not exist.")
        if not oauth_token_file.parent.is_dir():
            raise DriveConfigError(f"The folder of the file named by {OAUTH_TOKEN_FILE_ENV} does not exist.")
        # The token file itself may not exist yet: it is created by `oauth_authorize.py --authorize`.
    else:
        raw_path = str(env.get(CREDENTIALS_FILE_ENV, "")).strip()
        if not raw_path:
            raise DriveConfigError(f"{CREDENTIALS_FILE_ENV} must point to the test service-account key file (kept outside the repository).")
        credentials_file = _outside_repo(raw_path, "The credentials file must be OUTSIDE the repository so it can never be committed.")
        if not credentials_file.is_file():
            raise DriveConfigError(f"The credentials file named by {CREDENTIALS_FILE_ENV} does not exist.")
    try:
        rate = float(env.get(RATE_LIMIT_ENV, "10"))
    except ValueError:
        raise DriveConfigError(f"{RATE_LIMIT_ENV} must be a number.") from None
    if not 0.1 <= rate <= 100:
        raise DriveConfigError(f"{RATE_LIMIT_ENV} must be between 0.1 and 100.")
    return DriveConfig(
        root_folder_id=root,
        credentials_file=credentials_file,
        scope=str(env.get(SCOPE_ENV) or DEFAULT_SCOPE).strip(),
        auth_mode=mode,
        oauth_client_file=oauth_client_file,
        oauth_token_file=oauth_token_file,
        rate_limit_per_second=rate,
    )
