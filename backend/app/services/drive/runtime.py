"""Builds the Drive storage manager from the environment, once, for the /api/storage router. Disabled unless explicitly enabled."""

import os
import threading
from pathlib import Path

import httpx
from fastapi import HTTPException

from app.services.drive import config as drive_config
from app.services.drive.auth import DriveAuthError, ServiceAccountCredentials, TokenProvider
from app.services.drive.client import DriveClient
from app.services.drive.config import AUTH_OAUTH_USER, DriveConfig, DriveConfigError
from app.services.drive.lifecycle import StorageManager
from app.services.drive.oauth import OAuthClientConfig, OAuthTokenStore, UserOAuthTokenProvider
from app.services.drive.storage import DriveFolders, GoogleDriveStorage

_lock = threading.Lock()
_manager: StorageManager | None = None


def build_token_provider(config: DriveConfig, http: httpx.Client):
    """The one place that chooses how the Drive client signs in. The service account is the default; user OAuth only when
    SPINCO_GDRIVE_AUTH_MODE=oauth_user. Both providers offer get_token() / invalidate(), which is all DriveClient uses."""
    if config.auth_mode == AUTH_OAUTH_USER:
        client_config = OAuthClientConfig.from_file(config.oauth_client_file)
        return UserOAuthTokenProvider(client_config, OAuthTokenStore(config.oauth_token_file), config.scope, http)
    return TokenProvider(ServiceAccountCredentials.from_file(config.credentials_file), config.scope, http)


def build_manager(env=None, *, http: httpx.Client | None = None, spool_dir: Path | None = None) -> StorageManager:
    """Raises DriveConfigError / DriveAuthError (secret-free messages) when the configuration cannot be used."""
    env = os.environ if env is None else env
    config = drive_config.load_config(env)
    client_http = http or httpx.Client()
    client = DriveClient(config, build_token_provider(config, client_http), client_http)
    drive = GoogleDriveStorage(client, DriveFolders(client, config.root_folder_id))
    spool = spool_dir or (Path(env["SPINCO_GDRIVE_SPOOL_DIR"]) if env.get("SPINCO_GDRIVE_SPOOL_DIR") else None)
    return StorageManager(drive, spool_dir=spool)


def get_storage_manager() -> StorageManager:
    """FastAPI dependency. 503 (never a stack trace, never a credential) when Drive storage is off or misconfigured."""
    global _manager
    if not drive_config.is_enabled():
        raise HTTPException(status_code=503, detail="Drive storage is not enabled.")
    with _lock:
        if _manager is None:
            try:
                _manager = build_manager()
            except (DriveConfigError, DriveAuthError) as exc:
                raise HTTPException(status_code=503, detail=f"Drive storage is not configured: {exc}") from None
        return _manager


def reset_cache() -> None:
    global _manager
    with _lock:
        _manager = None
