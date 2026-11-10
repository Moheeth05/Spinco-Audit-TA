"""Creates ONE new, clearly named demo folder directly inside the STAGING Shared Drive (nothing existing is touched) and prints its folder id (not a secret).
Uses the staging OAuth sign-in kept in D:\\SPINCO-SECRETS. Staging only: the expected Shared Drive id is the staging one. Usage: python demo_drive_root.py <backend folder>"""
import os
import sys
import time

backend = sys.argv[1]
sys.path.insert(0, backend)
import httpx  # noqa: E402

from app.services.drive import config as drive_config  # noqa: E402
from app.services.drive.runtime import build_token_provider  # noqa: E402

DRIVE_ID = "0AC6JZ6F0PQY-Uk9PVA"  # the STAGING Shared Drive
env = {k: v for k, v in os.environ.items() if not k.startswith("SPINCO_")}
env.update(SPINCO_ENVIRONMENT="staging", SPINCO_GDRIVE_ENABLED="true", SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING=DRIVE_ID, SPINCO_GDRIVE_AUTH_MODE="oauth_user",
           SPINCO_GDRIVE_OAUTH_CLIENT_FILE=r"D:\SPINCO-SECRETS\spinco-drive-oauth-desktop.json", SPINCO_GDRIVE_OAUTH_TOKEN_FILE=r"D:\SPINCO-SECRETS\spinco-drive-oauth-token.json",
           SPINCO_GDRIVE_ROOT_FOLDER_ID=DRIVE_ID)
http = httpx.Client(timeout=60)
cfg = drive_config.load_runtime_config(env)
headers = {"Authorization": f"Bearer {build_token_provider(cfg, http).get_token()}"}
name = "SPINCO_DEMO_" + time.strftime("%Y%m%d_%H%M%S")
r = http.post("https://www.googleapis.com/drive/v3/files", headers=headers, params={"supportsAllDrives": "true", "fields": "id"},
              json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [DRIVE_ID]})
r.raise_for_status()
print(name + " " + r.json()["id"])
