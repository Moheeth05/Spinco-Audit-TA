"""Builds the Drive storage manager from the environment, once, for the /api/storage router. Disabled unless explicitly enabled."""

import logging
import os
import threading
from pathlib import Path

import httpx
from fastapi import HTTPException

from app.database import engine
from app.services.drive import config as drive_config
from app.services.drive import schema as drive_schema
from app.services.drive.auth import DriveAuthError, ServiceAccountCredentials, TokenProvider, TokenSettings
from app.services.drive.client import DriveClient
from app.services.drive.config import AUTH_EXTERNAL_ACCOUNT, AUTH_OAUTH_USER, DriveConfig, DriveConfigError
from app.services.drive.lifecycle import StorageManager
from app.services.drive.oauth import OAuthClientConfig, OAuthTokenStore, UserOAuthTokenProvider
from app.services.drive.storage import DriveFolders, GoogleDriveStorage
from app.services.drive.wif import ExternalAccountConfig, ExternalAccountTokenProvider

log = logging.getLogger("app.drive.runtime")
_lock = threading.Lock()
_manager: StorageManager | None = None


def build_token_provider(config: DriveConfig, http: httpx.Client):
    """The one place that chooses how the Drive client signs in. The service account is the default; user OAuth only when
    SPINCO_GDRIVE_AUTH_MODE=oauth_user (never in production); Workload Identity Federation when it is external_account. Both providers offer get_token() / invalidate(), which is all DriveClient uses."""
    settings = TokenSettings(
        deadline_seconds=config.token_deadline_seconds,
        timeout_seconds=config.token_timeout_seconds,
        wait_seconds=config.token_wait_seconds,
        failure_window_seconds=config.token_failure_window_seconds,
    )
    if config.auth_mode == AUTH_EXTERNAL_ACCOUNT:
        return ExternalAccountTokenProvider(ExternalAccountConfig.from_file(config.wif_config_file), config.scope, http, settings=settings)
    if config.auth_mode == AUTH_OAUTH_USER:
        client_config = OAuthClientConfig.from_file(config.oauth_client_file)
        return UserOAuthTokenProvider(client_config, OAuthTokenStore(config.oauth_token_file), config.scope, http, settings=settings)
    return TokenProvider(ServiceAccountCredentials.from_file(config.credentials_file), config.scope, http, settings=settings)


def build_manager(env=None, *, http: httpx.Client | None = None, spool_dir: Path | None = None) -> StorageManager:
    """Raises DriveConfigError / DriveAuthError (secret-free messages) when the configuration cannot be used.

    The returned manager can WRITE only through a client that carries the destination guard: no Drive write happens unless the configured
    destination is verified to be inside the explicitly configured, per-environment expected Shared Drive (see guard.py). The client BUILDS and
    OWNS that guard from the configuration (there is no way to install, swap or clear one), and it refuses to exist without an expected drive."""
    env = os.environ if env is None else env
    config = drive_config.load_runtime_config(env)  # environment + expected Shared Drive id are mandatory here
    client_http = http or httpx.Client()
    client = DriveClient(config, build_token_provider(config, client_http), client_http)  # never unguarded=True: that is for harnesses and fakes only
    if client.write_guard is None:  # cannot happen (the client refuses to exist unguarded without unguarded=True); checked anyway, loudly
        raise DriveConfigError("The Drive client has no destination guard; refusing to build the storage manager.")
    drive = GoogleDriveStorage(client, DriveFolders(client, config.root_folder_id))
    spool = spool_dir or (Path(env["SPINCO_GDRIVE_SPOOL_DIR"]) if env.get("SPINCO_GDRIVE_SPOOL_DIR") else None)
    return StorageManager(
        drive,
        spool_dir=spool,
        max_upload_attempts=config.max_upload_attempts,
        retry_backoff_seconds=config.upload_retry_backoff_seconds,
        retry_backoff_cap_seconds=config.upload_retry_backoff_cap_seconds,
    )


def check_schema_ready(*, detailed: bool = False) -> None:
    """503 unless the Drive tables are EXACTLY in the shape this code needs. Read-only: it inspects the database and never creates, alters or repairs
    anything - the schema is changed only by an operator applying the Alembic revision. Fails closed on any drift (and on an inspection that fails).
    `detailed` says whether the caller may be told what is wrong (the migration revision): only an ADMINISTRATOR, after authorisation. Everyone else gets
    a generic refusal; the details go to the server log."""
    report = drive_schema.schema_report(engine)
    if not report.ready:
        log.warning("Drive storage refused: database schema %s (%d difference(s)).", report.status, len(report.problems))
        raise HTTPException(status_code=503, detail=drive_schema.NOT_READY_TEXT if detailed else drive_schema.GENERIC_UNAVAILABLE_TEXT)


def schema_gate() -> None:
    """The gate for code that does not know who is asking (the manager dependency): always the generic answer. Authorisation of the request has already
    happened by the time this runs (the permission dependencies are declared before the manager). An administrator reads the reason from /status."""
    check_schema_ready(detailed=False)


def get_storage_manager() -> StorageManager:
    """FastAPI dependency. 503 (never a stack trace, never a credential) when Drive storage is off, misconfigured, or the database schema is not ready."""
    global _manager
    if not drive_config.is_enabled():
        raise HTTPException(status_code=503, detail="Drive storage is not enabled.")
    schema_gate()  # before anything is built or contacted: a database that cannot hold the retry state must not be written to
    first_use = False
    with _lock:  # held only to build and publish the manager: NEVER while waiting for the network
        if _manager is None:
            try:
                manager = build_manager()
            except (DriveConfigError, DriveAuthError) as exc:
                raise HTTPException(status_code=503, detail=f"Drive storage is not configured: {exc}") from None
            _manager = manager
            first_use = True
        current = _manager
    if first_use:
        # First use: verify the destination now and log the outcome. Outside the lock, so a slow or failing Drive cannot make every other request wait;
        # verification is single-flight inside the guard and never raises here (reads stay available; writes verify again for themselves).
        current.drive.client.write_guard.try_verify()
    return current


def reset_cache() -> None:
    global _manager
    with _lock:
        _manager = None
