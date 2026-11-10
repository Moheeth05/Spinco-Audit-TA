"""Drive configuration from environment variables. Credentials are referenced by FILE PATH only and never read into a log."""

import os
import re
from dataclasses import dataclass, replace
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
WIF_CONFIG_FILE_ENV = "SPINCO_GDRIVE_WIF_CONFIG_FILE"  # external_account: the Google "external account credential configuration" file (Workload Identity Federation)
AUTH_SERVICE_ACCOUNT = "service_account"
AUTH_OAUTH_USER = "oauth_user"
AUTH_EXTERNAL_ACCOUNT = "external_account"  # Workload Identity Federation: no key file; a short-lived identity token from the company identity provider is exchanged at Google
# Destination guard (Phase 3A.1). Every environment names the ONE Shared Drive it may write to, explicitly: there is no default, no fallback and no
# "the staging id will do". The expected id lives in an environment-SCOPED variable, so production cannot silently pick up the staging id.
ENVIRONMENT_ENV = "SPINCO_ENVIRONMENT"
EXPECTED_DRIVE_ENV_PREFIX = "SPINCO_GDRIVE_EXPECTED_DRIVE_ID_"  # + DEV | STAGING | PRODUCTION
UNSCOPED_EXPECTED_DRIVE_ENV = "SPINCO_GDRIVE_EXPECTED_DRIVE_ID"  # deliberately NOT supported (ambiguous between environments)
GUARD_TTL_ENV = "SPINCO_GDRIVE_GUARD_TTL_SECONDS"
GUARD_FAILURE_WINDOW_ENV = "SPINCO_GDRIVE_GUARD_FAILURE_WINDOW_SECONDS"  # how long writes are refused at once after the destination could not be verified (an outage)
ENVIRONMENTS = ("dev", "staging", "production")
DEFAULT_GUARD_TTL_SECONDS = 300.0
MIN_GUARD_TTL_SECONDS, MAX_GUARD_TTL_SECONDS = 10.0, 900.0
DEFAULT_GUARD_FAILURE_WINDOW_SECONDS = 15.0
MIN_GUARD_FAILURE_WINDOW_SECONDS, MAX_GUARD_FAILURE_WINDOW_SECONDS = 1.0, 120.0
DEFAULT_GUARD_VERIFY_ATTEMPTS = 2  # retries for a verification read: few, so an outage does not hold a caller for long
GUARD_VERIFY_DEADLINE_ENV = "SPINCO_GDRIVE_GUARD_VERIFY_DEADLINE_SECONDS"  # overall time limit of ONE destination verification (all its reads together)
DEFAULT_GUARD_VERIFY_DEADLINE_SECONDS = 15.0
MIN_GUARD_VERIFY_DEADLINE_SECONDS, MAX_GUARD_VERIFY_DEADLINE_SECONDS = 2.0, 60.0
DEFAULT_GUARD_VERIFY_MAX_BACKOFF_SECONDS = 2.0  # no verification read ever sleeps longer than this between attempts, whatever `Retry-After` says
DEFAULT_GUARD_WAITER_SECONDS = 1.5  # how long a writer waits behind a verification already in flight before it is refused instead of tying up its thread
# Pending uploads: a retryable failure is retried a bounded number of times, with a growing pause, and is then marked FAILED (never retried forever).
MAX_UPLOAD_ATTEMPTS_ENV = "SPINCO_GDRIVE_MAX_UPLOAD_ATTEMPTS"
DEFAULT_MAX_UPLOAD_ATTEMPTS = 10
MIN_MAX_UPLOAD_ATTEMPTS, MAX_MAX_UPLOAD_ATTEMPTS = 1, 100
UPLOAD_RETRY_BACKOFF_ENV = "SPINCO_GDRIVE_UPLOAD_RETRY_BACKOFF_SECONDS"  # the pause after the first failed attempt; it doubles each time up to the cap
DEFAULT_UPLOAD_RETRY_BACKOFF_SECONDS = 60.0
MIN_UPLOAD_RETRY_BACKOFF_SECONDS, MAX_UPLOAD_RETRY_BACKOFF_SECONDS = 1.0, 3600.0
DEFAULT_UPLOAD_RETRY_BACKOFF_CAP_SECONDS = 3600.0
# Token acquisition (Phase 3A.2-A): see auth.py. The deadline of one whole refresh can be LOWERED but never raised above 15 s.
TOKEN_DEADLINE_ENV = "SPINCO_GDRIVE_TOKEN_DEADLINE_SECONDS"
TOKEN_TIMEOUT_ENV = "SPINCO_GDRIVE_TOKEN_TIMEOUT_SECONDS"  # upper limit of the read phase of one token request
TOKEN_WAIT_ENV = "SPINCO_GDRIVE_TOKEN_WAIT_SECONDS"  # how long a thread without a usable token waits behind the thread that is refreshing
TOKEN_FAILURE_WINDOW_ENV = "SPINCO_GDRIVE_TOKEN_FAILURE_WINDOW_SECONDS"  # a failed transient refresh is remembered this long
DEFAULT_TOKEN_DEADLINE_SECONDS = 15.0
MIN_TOKEN_DEADLINE_SECONDS, MAX_TOKEN_DEADLINE_SECONDS = 2.0, 15.0
DEFAULT_TOKEN_TIMEOUT_SECONDS = 10.0
MIN_TOKEN_TIMEOUT_SECONDS, MAX_TOKEN_TIMEOUT_SECONDS = 1.0, 15.0
DEFAULT_TOKEN_WAIT_SECONDS = 2.0  # was 5 s: with no usable token a request thread waits this long at most behind the refresher
MIN_TOKEN_WAIT_SECONDS, MAX_TOKEN_WAIT_SECONDS = 0.5, 15.0
DEFAULT_TOKEN_FAILURE_WINDOW_SECONDS = 10.0
MIN_TOKEN_FAILURE_WINDOW_SECONDS, MAX_TOKEN_FAILURE_WINDOW_SECONDS = 1.0, 120.0

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
    wif_config_file: Path | None = None  # external_account mode only
    # Set by load_runtime_config() (what the application uses). The defaults keep direct construction (harnesses, fakes) working unchanged.
    environment: str = "dev"
    expected_drive_id: str | None = None
    guard_ttl_seconds: float = DEFAULT_GUARD_TTL_SECONDS
    guard_failure_window_seconds: float = DEFAULT_GUARD_FAILURE_WINDOW_SECONDS
    guard_verify_attempts: int = DEFAULT_GUARD_VERIFY_ATTEMPTS
    guard_verify_deadline_seconds: float = DEFAULT_GUARD_VERIFY_DEADLINE_SECONDS
    guard_verify_max_backoff_seconds: float = DEFAULT_GUARD_VERIFY_MAX_BACKOFF_SECONDS
    guard_waiter_seconds: float = DEFAULT_GUARD_WAITER_SECONDS
    max_upload_attempts: int = DEFAULT_MAX_UPLOAD_ATTEMPTS
    upload_retry_backoff_seconds: float = DEFAULT_UPLOAD_RETRY_BACKOFF_SECONDS
    upload_retry_backoff_cap_seconds: float = DEFAULT_UPLOAD_RETRY_BACKOFF_CAP_SECONDS
    token_deadline_seconds: float = DEFAULT_TOKEN_DEADLINE_SECONDS
    token_timeout_seconds: float = DEFAULT_TOKEN_TIMEOUT_SECONDS
    token_wait_seconds: float = DEFAULT_TOKEN_WAIT_SECONDS
    token_failure_window_seconds: float = DEFAULT_TOKEN_FAILURE_WINDOW_SECONDS
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
    if value == AUTH_EXTERNAL_ACCOUNT:
        return AUTH_EXTERNAL_ACCOUNT
    raise DriveConfigError(f"{AUTH_MODE_ENV} must be '{AUTH_SERVICE_ACCOUNT}', '{AUTH_OAUTH_USER}' or '{AUTH_EXTERNAL_ACCOUNT}'.")


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
    credentials_file = oauth_client_file = oauth_token_file = wif_config_file = None
    if mode == AUTH_EXTERNAL_ACCOUNT:
        raw_wif = str(env.get(WIF_CONFIG_FILE_ENV, "")).strip()
        if not raw_wif:
            raise DriveConfigError(f"{WIF_CONFIG_FILE_ENV} must point to the external-account (Workload Identity Federation) configuration file, kept outside the repository.")
        wif_config_file = _outside_repo(raw_wif, "The external-account configuration file must be OUTSIDE the repository.")
        if not wif_config_file.is_file():
            raise DriveConfigError(f"The file named by {WIF_CONFIG_FILE_ENV} does not exist.")
    elif mode == AUTH_OAUTH_USER:
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
        wif_config_file=wif_config_file,
        rate_limit_per_second=rate,
        token_deadline_seconds=_bounded(env, TOKEN_DEADLINE_ENV, DEFAULT_TOKEN_DEADLINE_SECONDS, MIN_TOKEN_DEADLINE_SECONDS, MAX_TOKEN_DEADLINE_SECONDS),
        token_timeout_seconds=_bounded(env, TOKEN_TIMEOUT_ENV, DEFAULT_TOKEN_TIMEOUT_SECONDS, MIN_TOKEN_TIMEOUT_SECONDS, MAX_TOKEN_TIMEOUT_SECONDS),
        token_wait_seconds=_bounded(env, TOKEN_WAIT_ENV, DEFAULT_TOKEN_WAIT_SECONDS, MIN_TOKEN_WAIT_SECONDS, MAX_TOKEN_WAIT_SECONDS),
        token_failure_window_seconds=_bounded(env, TOKEN_FAILURE_WINDOW_ENV, DEFAULT_TOKEN_FAILURE_WINDOW_SECONDS, MIN_TOKEN_FAILURE_WINDOW_SECONDS, MAX_TOKEN_FAILURE_WINDOW_SECONDS),
    )


def environment(env=None) -> str:
    """The deployment environment, which must be stated: there is no default, so a missing or misspelt value can never silently mean 'dev'."""
    env = os.environ if env is None else env
    value = str(env.get(ENVIRONMENT_ENV, "")).strip().lower()
    if value not in ENVIRONMENTS:
        raise DriveConfigError(f"{ENVIRONMENT_ENV} must be set explicitly to one of: {', '.join(ENVIRONMENTS)}.")
    return value


def expected_drive_id(active: str, env=None) -> str:
    """The Shared Drive id the ACTIVE environment may write to. Refuses every configuration that could redirect writes by accident."""
    env = os.environ if env is None else env
    if str(env.get(UNSCOPED_EXPECTED_DRIVE_ENV, "")).strip():
        raise DriveConfigError(
            f"{UNSCOPED_EXPECTED_DRIVE_ENV} is not supported: it does not say which environment it belongs to. "
            f"Set {EXPECTED_DRIVE_ENV_PREFIX}{active.upper()} for this environment."
        )
    configured: dict[str, str] = {}
    for name, raw in env.items():
        if not str(name).upper().startswith(EXPECTED_DRIVE_ENV_PREFIX):
            continue
        suffix = str(name)[len(EXPECTED_DRIVE_ENV_PREFIX):].lower()
        if suffix not in ENVIRONMENTS:  # a typo such as _PROD must not be silently ignored
            raise DriveConfigError(f"{name} is not a recognised setting: the suffix must be one of {', '.join(e.upper() for e in ENVIRONMENTS)}.")
        value = str(raw).strip()
        if not value:
            continue
        if not _ID_RE.match(value):
            raise DriveConfigError(f"{name} must be a Shared Drive id (letters, digits, '-' and '_').")
        configured[suffix] = value
    mine = configured.get(active)
    if not mine:
        raise DriveConfigError(f"{EXPECTED_DRIVE_ENV_PREFIX}{active.upper()} must be set to the Shared Drive id that the {active} environment may write to.")
    for other, other_id in configured.items():
        if other != active and other_id == mine:  # the classic copy-paste mistake: production pointed at the staging drive
            raise DriveConfigError(f"The expected Shared Drive id for {active} is identical to the one configured for {other}; each environment needs its own.")
    return mine


def guard_ttl_seconds(env=None) -> float:
    env = os.environ if env is None else env
    raw = str(env.get(GUARD_TTL_ENV, "")).strip()
    if not raw:
        return DEFAULT_GUARD_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise DriveConfigError(f"{GUARD_TTL_ENV} must be a number of seconds.") from None
    if not MIN_GUARD_TTL_SECONDS <= value <= MAX_GUARD_TTL_SECONDS:
        raise DriveConfigError(f"{GUARD_TTL_ENV} must be between {MIN_GUARD_TTL_SECONDS:g} and {MAX_GUARD_TTL_SECONDS:g} seconds.")
    return value


def guard_failure_window_seconds(env=None) -> float:
    env = os.environ if env is None else env
    raw = str(env.get(GUARD_FAILURE_WINDOW_ENV, "")).strip()
    if not raw:
        return DEFAULT_GUARD_FAILURE_WINDOW_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise DriveConfigError(f"{GUARD_FAILURE_WINDOW_ENV} must be a number of seconds.") from None
    if not MIN_GUARD_FAILURE_WINDOW_SECONDS <= value <= MAX_GUARD_FAILURE_WINDOW_SECONDS:
        raise DriveConfigError(f"{GUARD_FAILURE_WINDOW_ENV} must be between {MIN_GUARD_FAILURE_WINDOW_SECONDS:g} and {MAX_GUARD_FAILURE_WINDOW_SECONDS:g} seconds.")
    return value


def _bounded(env, name: str, default, low, high, *, whole: bool = False):
    """A numeric setting that must lie in [low, high]; blank = the default; anything else (including nan/inf) is refused."""
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw) if whole else float(raw)
    except ValueError:
        raise DriveConfigError(f"{name} must be a number.") from None
    if not low <= value <= high:
        raise DriveConfigError(f"{name} must be between {low:g} and {high:g}.")
    return value


def load_runtime_config(env=None) -> DriveConfig:
    """What the APPLICATION uses: load_config() plus the mandatory destination settings (environment, expected Shared Drive id, guard lifetime).
    Raises DriveConfigError - with a secret-free message - unless every one of them is explicit and consistent."""
    env = os.environ if env is None else env
    base = load_config(env)
    active = environment(env)
    if active == "production" and base.auth_mode == AUTH_OAUTH_USER:
        # A person's own OAuth authorisation is the TEST credential (it dies with that person's account and was granted for staging). Production signs in as a service
        # identity: Workload Identity Federation (external_account), or - where the organisation allows keys - a service account.
        raise DriveConfigError(f"{AUTH_MODE_ENV}=oauth_user is not allowed in the production environment; use external_account (Workload Identity Federation) or service_account.")
    return replace(
        base,
        environment=active,
        expected_drive_id=expected_drive_id(active, env),
        guard_ttl_seconds=guard_ttl_seconds(env),
        guard_failure_window_seconds=guard_failure_window_seconds(env),
        guard_verify_deadline_seconds=_bounded(env, GUARD_VERIFY_DEADLINE_ENV, DEFAULT_GUARD_VERIFY_DEADLINE_SECONDS, MIN_GUARD_VERIFY_DEADLINE_SECONDS, MAX_GUARD_VERIFY_DEADLINE_SECONDS),
        max_upload_attempts=_bounded(env, MAX_UPLOAD_ATTEMPTS_ENV, DEFAULT_MAX_UPLOAD_ATTEMPTS, MIN_MAX_UPLOAD_ATTEMPTS, MAX_MAX_UPLOAD_ATTEMPTS, whole=True),
        upload_retry_backoff_seconds=_bounded(env, UPLOAD_RETRY_BACKOFF_ENV, DEFAULT_UPLOAD_RETRY_BACKOFF_SECONDS, MIN_UPLOAD_RETRY_BACKOFF_SECONDS, MAX_UPLOAD_RETRY_BACKOFF_SECONDS),
    )
