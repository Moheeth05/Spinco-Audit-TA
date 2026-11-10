# Central backend: setup, security and moving to the server

Status: **staging / development.** Not approved for real accounts or real company documents until the checks in "Before real use" are done by IT.

## What "central" means here
One backend process (FastAPI + SQLite today) is the only place that talks to Google Drive and the only place that holds Google credentials. Every PC runs the Electron app in
**remote mode** (it opens a window on the backend's address; it starts no backend and no database of its own). Documents live in the company Shared Drive; the central database
holds, for each document: its record, Drive file id, SHA-256 and MD5, size, uploader, upload time (UTC, shown in IST), type, storage state and processing state.

## Modes (`SPINCO_CENTRAL_STORAGE_MODE`)
| mode | meaning |
|---|---|
| `off` (default) | unchanged behaviour: local files, local background tasks |
| `shadow` | local storage stays authoritative; every upload is also copied to Drive by a durable job; a Drive failure never fails the local upload |
| `serve` | Drive is authoritative: an upload succeeds only after the verified Drive copy exists; list / preview / download use the central record; a missing or unavailable central copy is an explicit error (404 / 410 / 502 / 503) - a local copy is **never** served instead |

A misspelt value is refused and treated as `off`. `shadow` and `serve` refuse to start on a database that does not already have the exact schema they need.

## Duplicates
Two uploads are an exact duplicate only when their bytes are identical (SHA-256). The same file name with different bytes is **not** a duplicate. A duplicate creates no second
document and no second Drive original; the attempt is recorded (who, when, file name, size, why, which canonical document) and appears in the duplicate history. With
`SPINCO_DUPLICATE_RETENTION=copy` the duplicate's bytes are also kept once in the Drive folder `Duplicates` (never touching the canonical file); the default `metadata` keeps only the record.

## Creating the staging database
The backend never creates or alters tables in the central modes. A NEW database is created on purpose, once:

    cd D:\Bills\backend
    .venv\Scripts\python.exe tools\init_central_staging_db.py --db D:\SpincoTA-artifacts\central-staging\<run>\central.db

It refuses an existing non-empty file and the development database. Point the backend at it with `SPINCO_DB_PATH`. Alembic revisions: `...8268b29c4fb1` -> `c3a9f0b17d42` -> `f1d0c9e84a27`.
(Known, separately tracked: the Alembic history alone does not build the complete current schema - 19 differences - so a new database is built from the models and stamped.)

## Running it on the development PC as the central backend (temporary)
    $env:SPINCO_DB_PATH = "D:\SpincoTA-artifacts\central-staging\<run>\central.db"
    $env:SPINCO_STORAGE_ROOT = "D:\SpincoTA-artifacts\central-staging\<run>\storage"
    $env:SPINCO_CENTRAL_STORAGE_MODE = "serve"
    $env:SPINCO_GDRIVE_* = ...            # see backend\.env.example (credentials stay on this machine, outside the repo)
    $env:SPINCO_ENVIRONMENT = "staging"
    $env:SPINCO_BIND_HOST = "0.0.0.0"     # only when other PCs must reach it
    $env:SPINCO_ALLOWED_HOSTS = "<this PC's host name>"
    $env:SPINCO_ADMIN_INITIAL_PASSWORD = "<explicit, 12+ characters>"
    $env:SPINCO_ALLOW_INSECURE_HTTP = "true"   # DEVELOPMENT / SYNTHETIC DOCUMENTS ONLY, until HTTPS is in place
    .venv\Scripts\python.exe run_server.py

* A backend bound to a network address **refuses to start** with the development default password (`ChangeMe123!`), with a short password, with an account that still has that
  default password, or over plain HTTP without the explicit acknowledgement above. The status page then shows administrators an "INSECURE ... synthetic documents only" warning.
* Windows Firewall (administrator PowerShell): `New-NetFirewallRule -DisplayName "Spinco TA central backend" -Direction Inbound -Protocol TCP -LocalPort 8001 -Profile Domain -Action Allow`
  (Domain profile only; remove the rule when the test ends). Use the host **name**, not an IP address, in `SPINCO_ALLOWED_HOSTS` and in the clients.
* Sign-in cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` when `SPINCO_COOKIE_SECURE=true` (set it once HTTPS is in front of the backend).

## Connecting a PC (Electron remote mode)
See `ELECTRON-REMOTE-MODE.md`: the backend address is read from the environment / a small settings file, so it can be changed without rebuilding the app. No credential is stored in the app.

## Background work
Extraction and the shadow copy run as durable jobs (table `processing_jobs`): they survive a restart, are claimed with a compare-and-swap lease, are kept alive by a heartbeat,
retry with back-off when Drive is unavailable, and end as FAILED (never losing the original PDF) when the problem is permanent. Administrators see them at
`GET /api/storage/jobs` and can re-queue one at `POST /api/storage/jobs/{id}/retry`; a failed Drive upload is retried at `POST /api/storage/records/{id}/retry`.
One worker thread processes jobs one at a time (SQLite allows one writer). `SPINCO_JOB_WORKER=off` disables it in a process.

## Crash recovery (decision M3)
A Drive upload claimed by a process that then dies is recovered by the retry machinery only after its lease expires - up to **30 minutes** later (a restart does not release it,
and an administrator's manual retry is also refused while the lease is live, by design). The document is not lost in that window: its bytes are kept in the spool and the record
is UPLOADING; it is uploaded when the lease ends. **Decision: the 30-minute lease is NOT shortened in this phase.** Evidence for and against: the new heartbeat (tests with a 0.9 s
lease and a 2.6 s upload on the real clock) keeps a live upload's lease alive, so a shorter lease would be technically possible; but a missed heartbeat (a locked database, a stalled
thread) would then let a second worker start the same upload, and there is no measurement against the real Drive of upload durations or database contention to size a shorter lease.
The cost of leaving it is a delay only after a crash. If IT wants a faster recovery, measure on staging first, then lower `lease_seconds` together with the heartbeat interval (lease/3).
Processing jobs (OCR / shadow copy) already use a 2-minute lease with a heartbeat, so a dead worker's job is recovered in about 2 minutes.

## Moving to the Windows Server
1. Install the backend (the same files) and Python/Tesseract as for the desktop build; create a service account that runs it. 2. Copy nothing from the development PC except
`backend\.env`-style settings (re-create the secrets files on the server). 3. Run `init_central_staging_db.py` for a new database **or** back up and migrate (Alembic) a copy first.
4. Put HTTPS in front (IIS ARR / nginx / the company standard) and set `SPINCO_COOKIE_SECURE=true`, `SPINCO_ALLOWED_HOSTS=<server name>`, `SPINCO_BIND_HOST=<interface>`.
5. Change each client's backend address (environment variable or settings file) - no rebuild. 6. Run `tools\central_staging_check.py` against staging, then the same checks on the
production drive with IT present.

## Before real use (not done - needs IT)
HTTPS with a company certificate; PostgreSQL (the code has not been run on it; known issue N1 and the 19-column schema gap must be closed first); backups and a restore rehearsal;
a firewall/VLAN review; Google Workspace security review of the service account / OAuth client; a load test against the real Shared Drive; authentication hardening review (password
policy, lock-out, session lifetime) beyond what this phase changed.
