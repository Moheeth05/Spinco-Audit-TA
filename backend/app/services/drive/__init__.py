"""Google Drive central-storage integration (Phase 2 feasibility work; see docs/central-architecture/).

Nothing here is wired into the existing upload / pipeline code paths: those still use the local DocumentStorage. This package is
exercised through the /api/storage admin router (only when SPINCO_GDRIVE_ENABLED=true and credentials are configured), by the unit
tests (against a fake Drive) and by backend/drive_spike (against a real, temporary test folder).

Credentials exist only on the backend: a service-account key file OUTSIDE the repository, referenced by SPINCO_GDRIVE_CREDENTIALS_FILE.
They are never logged, never returned by any API and never sent to the frontend or Electron.
"""
