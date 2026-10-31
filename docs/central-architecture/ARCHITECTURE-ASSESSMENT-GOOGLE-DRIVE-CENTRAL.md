# Spinco TA Audit - central multi-user architecture with Google Drive storage
## Inspection and plan (nothing implemented, no repository file changed)

Basis: read-only inspection of `D:\Bills` (backend, Electron, frontend, tests, the existing `docs/CENTRALIZED_ARCHITECTURE.md`)
and one unauthenticated request to the Drive link. No authentic document, `%APPDATA%` folder or real database was opened.
Things I could not verify from here are marked **[VERIFY]** and must be proven in the Phase 1 spike.

---

## 1. Phase 1 result - the Google Drive link

| Question | Finding |
|---|---|
| Reachable? | **Not verified.** The Google Drive connector in this environment is not authorised (it needs an interactive sign-in, which this session cannot do), so I could not list anything as an account holder. |
| What an unauthenticated request shows | The page title **"Spinco TA Audit"** and an empty-looking listing ("Drop files here"). That is the page shell; it does not prove the folder is empty, nor that it is private. |
| Shared Drive or normal folder? | **Cannot be told from the URL.** `/drive/folders/<id>` is used for both ordinary folders and the root of a Shared Drive. |
| Permissions? | Unknown. A signed-out viewer being shown the folder title *may* mean "Anyone with the link" sharing is on. **[VERIFY]** If it is, change it to "Restricted" now: this will hold confidential financial PDFs and link sharing must never be the security mechanism. |
| Structure / suitability | Not inspectable. |

What I need to finish Phase 1 (owner action, ~30 minutes):
1. In Drive, open the location and tell me: is it under **"Shared drives"** (left menu) or **"My Drive"**? Which Google account/organisation owns it?
2. Is the company on **Google Workspace** (Business Standard or higher)? Shared Drives are not available on free Gmail / personal accounts.
3. Create (or allow me to define) a **service account** in a company Google Cloud project, enable the Drive API, and **add the service account as a member of the Shared Drive** (role: Content manager, not Manager). I never need the key in this chat: it goes straight into the server's secret store.
4. Confirm sharing on the folder is "Restricted".

Suitability: a **Shared Drive is strongly preferred** (company-owned files, survive employee departures, no per-user quota, service accounts work properly). A folder in someone's personal "My Drive" is a poor home for company records (owned by one person; a service account uploading into it hits storage-quota limits **[VERIFY]**; ownership lost when that person leaves).

Proposed Shared Drive layout (names only, nothing created):
```
Spinco TA Audit (Shared Drive)
  Expense Reports/<YYYY-MM>/        original ER PDFs
  Trip Reports/<YYYY-MM>/
  Excel Exports/
  Processed/                        optional processed/derived archives
  Archive/                          soft-deleted / retired documents (never hard-delete from the app)
  _quarantine/                      files that failed the malware / validation gate
```

---

## 2. Audit of the existing application

### What exists today (verified in code)
* **Desktop shell**: Electron `main.js` **spawns a private local backend** (`spinco-backend.exe`, port 58732) and bundled Tesseract, with the DB and PDFs in `%APPDATA%\Spinco TA Audit\{database,documents,logs}`. Every PC has its own isolated copy of everything: no sharing of any kind.
* **Frontend**: React SPA, same-origin calls to a relative `/api` with a session cookie + CSRF header; PDFs are fetched from `/api/documents/<id>/file` and rendered with react-pdf.
* **Backend**: FastAPI 0.115, SQLAlchemy 2, **SQLite** (`sqlite:///...`, `check_same_thread=False`). 19 tables. Schema evolution = hand-rolled `db_migrations.py` (SQLite `ALTER TABLE`, run at every start). **Alembic is installed and has a baseline + one hardening revision, but is not what runs.**
* **Auth/RBAC**: DB-backed sessions (token hashed), CSRF synchroniser token, Argon2, roles VIEWER / AUDITOR / ADMIN with 19 permissions. RBAC is **global by role**: any user who can view documents can view *every* document. There is no per-report ownership or scoping (only an `uploaded_by_id` column).
* **Storage abstraction already half-built**: `services/storage_backend.py` defines `DocumentStorage` (save / retrieve / delete / exists / get_metadata) with `LocalFilesystemStorage`; one documented escape hatch `local_path()`.
* **Processing**: upload validates + stores + creates the row, then extraction runs in a FastAPI `BackgroundTasks` task inside the web process (the earlier doc saying "fully synchronous" is out of date). No job table, no retry, **lost on process restart**. OCR is local (PyMuPDF text, Tesseract fallback; hosted AI providers are forced off).
* **Duplicates**: SHA-256 `file_hash` unique per table; but the check is read-then-insert with **no `IntegrityError` handling**: two simultaneous identical uploads can produce a 500, not a clean "already exists".
* **Excel**: one shared consolidated workbook, regenerated after every document, written in place with no locking.
* **Important data-model fact**: *bills/receipts are not separate files.* They are **pages inside the Expense Report PDF** (`bills.page_number`). "Store individual bills in Drive" therefore means either (a) keep one Drive file per ER and keep page references (what exists, recommended), or (b) deliberately split pages into new files (a design change; not recommended initially).

### Every local-storage dependency (exact)
| Where | Dependency | Replacement |
|---|---|---|
| `api/uploads.py` | `storage.save()` writes `<hash12>_<name>` under `STORAGE_ROOT`; `source_path` = local path | Drive upload; store file id (see Section 4) |
| `models/expense_report.py`, `models/trip.py` | `source_path` (local path string) | keep column; new typed Drive columns; dual-read resolver |
| `api/documents.py` `GET /{id}/file` | `FileResponse(path=storage.local_path(...))` (HTTP Range for pdf.js) | stream from Drive through FastAPI with Range support, or a bounded local cache |
| `api/documents.py` extract-bills, reprocess, delete | `Path(row.source_path).exists()`, `storage.delete()` | `storage.exists/open`; delete becomes soft-delete + move to `Archive/` |
| `extraction/pipeline.py` (3 sites), `bill_pipeline.py`, `text_extraction.py` (4 `fitz.open(path)`) | OCR/extraction need a **real file path** (PyMuPDF, Tesseract) | worker downloads to a managed temp dir, processes, then cleans up |
| `services/excel_generator.py`, `api/excel_files.py` | `EXCEL_DIR / workbook.xlsx`, `ExcelReport.file_path`, `FileResponse` | generate to temp, upload to `Excel Exports/`, atomic row update; needs a locking decision |
| `electron/main.js` | creates `%APPDATA%` dirs, spawns local backend, passes `SPINCO_DB_PATH`/`SPINCO_STORAGE_ROOT` | becomes a thin client of the central server (Section 5) |
| `ta_backend_forward.py` / `TA Backend` (Node) | local second archive | retire or keep as optional; Drive replaces its purpose |
| Tests | SQLite temp DB, local temp storage via `conftest.py` | add a Postgres test target and a fake-Drive storage backend |

### What can be retained as-is
Business logic (extraction, parsing, matching, validation, exception engine, verification history/carry-over), the policy master, RBAC model (extended), the React UI, Argon2/session/CSRF design, audit-log table, the `/api/...` URL scheme, and the whole Python test suite (1,565 passing in the sandbox + 75 authentic-document tests). The pipeline stays Python/FastAPI: **no rewrite**.

### What must change
1. Database: SQLite -> PostgreSQL (+ Alembic as the only schema tool).
2. Storage: implement `GoogleDriveStorage` behind the existing interface, replace the `local_path()` escape hatch with "open as a local temp file" for the OCR code.
3. Processing: replace in-process `BackgroundTasks` with a **durable job table + worker(s)**.
4. Upload path: idempotent, race-safe duplicate handling.
5. Authorisation: add per-document/report access rules (if the company wants more than role-level access).
6. Client: Electron stops running its own backend.
7. Deployment: HTTPS, secrets, backups, monitoring on a server that is always on.

---

## 3. Target architecture (what I recommend)

```
Employee PCs (Electron shell, thin)  --HTTPS-->  Central FastAPI (stateless, N replicas possible)
                                                    |-- PostgreSQL (business data, sessions, audit, jobs, Drive file ids)
                                                    |-- Google Drive Shared Drive (PDFs, Excel exports)  via service account, backend only
                                                    '-- Worker process(es): OCR / extraction / matching / validation
```
* **FastAPI** stays the only door: authentication, authorisation, audit, Drive access. Electron never sees Google credentials; Drive file ids and links are never handed to the client (no public/shareable links).
* **PostgreSQL** holds everything except PDF bytes. Postgres is also the **job queue** (`SELECT ... FOR UPDATE SKIP LOCKED`): restart-safe, no Redis/Celery to operate, enough for a company's volume. (This supersedes the older doc's `BackgroundTasks` recommendation.)
* **Drive** holds the originals and Excel exports; Drive `appProperties` carries `sha256`, `report_id` and `kind` so an interrupted upload can be reconciled from either side.
* **Workers** use the same code image as the API, started as separate processes; they download the PDF to a temp directory, run the existing pipeline, write results, delete the temp file.

### Upload flow (idempotent)
1. Authenticated, CSRF-checked request; validate type/size/PDF structure (existing rules); compute SHA-256.
2. In **one DB transaction**: insert the report row (`UNIQUE(file_hash)`; on conflict return the existing document: the race becomes a clean "File already exists"), state `UPLOADING`, plus a job row. Commit.
3. Upload to Drive with a **pre-generated file id** (`files.generateIds`): a retried create with the same id is rejected by Drive instead of duplicating **[VERIFY]**. Verify Drive's `md5Checksum` against the bytes sent. Set `appProperties`.
4. Update the row: `drive_file_id`, folder id, md5, size, `STORED`; enqueue extraction (`QUEUED`).
5. A startup/periodic **reconciler** resolves rows left in `UPLOADING` (found in Drive by `appProperties.sha256` -> adopt; not found -> retry or mark `FAILED`) and Drive files with no row (orphans -> `_quarantine/` report).
6. Worker claims the job, downloads, extracts, validates, stores results, marks `PROCESSED`; failures retry with backoff, then `FAILED` with an error visible in the UI.

### Viewing / download
`GET /api/documents/{id}/file` checks the user's permission, then streams the PDF from Drive through FastAPI (Range supported so pdf.js still works). A small **bounded local cache** (LRU, size/TTL capped, cleaned at start-up) avoids re-downloading for every page view; it is explicitly the only permitted local copy.

### Data stored per document (new, replaces reliance on `source_path`)
`storage_backend` (`local` | `gdrive`), `drive_file_id`, `drive_folder_id`, `drive_md5`, `storage_state` (`UPLOADING` / `STORED` / `FAILED` / `ARCHIVED`), `uploaded_by_id`, `uploaded_at`, plus an optional `owner_scope`/department for access rules. Old rows keep their local `source_path` and keep working (resolver chooses by `storage_backend`), which is what makes a gradual, reversible migration possible.

---

## 4. Database / migration design (design only)
* SQLAlchemy is mostly dialect-neutral already; real SQLite couplings: `database.py` engine args, `db_migrations.py` DDL, and code that relied on **SQLite reusing freed integer ids** (the verification carry-over/history work); on Postgres ids are never reused, which is safer, but that code needs re-validation.
* Postgres **enforces foreign keys**, which SQLite here does not. The existing orphan-row queries (`docs/RELEASE_AND_UPDATES.md`) must be run on a copy first; any orphan blocks the constraint.
* Use **Alembic only**; retire `db_migrations.py`. Start from the existing baseline, add Postgres-specific revision(s): Drive columns, `processing_jobs`, `audit_logs` atomicity, missing unique constraints already identified in the older doc (`(expense_report_id, s_no)`, etc.), indexes.
* Data migration (separately approved, rehearsed on **disposable copies only**): export SQLite -> Postgres with row-count and checksum reconciliation per table; documents are **not** moved by SQL: a separate, resumable "upload legacy PDFs to Drive" tool verifies SHA-256 against `file_hash` and flips each row to `gdrive` only after a verified upload. Rollback = flip `storage_backend` back; the local files are never deleted by the migration.
* **Important organisational fact:** today there is **one SQLite database per employee PC**. Consolidating them is a real data-merge problem (duplicate employees, the same report uploaded on several PCs, id collisions, different policy versions), not just a copy. It needs its own plan and an owner decision on which database is the source of truth.

---

## 5. Electron / frontend
* **Recommended: Electron becomes a thin shell that opens the central server's HTTPS URL** (the server already knows how to serve the SPA bundle). Same-origin keeps the existing cookie + CSRF design working with no CORS rework, and the client always matches the server version. Remove the bundled backend and Tesseract from the installer (it shrinks by hundreds of MB); keep the auto-updater for the shell.
* Alternative (SPA bundled in Electron, API cross-origin): needs configurable API base URL, `SameSite=None; Secure` cookies, an explicit CORS allow-list, and CSRF re-validation. More moving parts; not recommended.
* Frontend changes are small: upload progress/"stored in Drive / processing" states, document viewer unchanged (it already fetches through the API), error surfaces for Drive outages, and admin screens for job status. Offline use is lost by design.

---

## 6. Security assessment
| Area | Position |
|---|---|
| Credentials | Service-account key only on the server, in a secret store (not in code, Electron, or `.env` in git); rotate; prefer key-less workload identity if hosted on Google Cloud. |
| Scope | Least privilege = the service account is a member of **only** the Shared Drive. `drive.file` scope is narrower but only sees files the app created, so a pre-made folder may be invisible to it; either have the app create its folders or use the `drive` scope restricted by membership **[VERIFY in the spike]**. |
| Sharing | No public links, no "anyone with the link", no ids exposed to clients; downloads always via an authorised API call. |
| Authorisation | Today global-by-role. If employees must not see each other's reports, add per-report/department scoping and test it (this is new work, not just configuration). |
| Transport | HTTPS only (reverse proxy), `SPINCO_COOKIE_SECURE=true`, HSTS. |
| Upload hardening | Keep type/size/structure checks; add malware scanning (e.g. ClamAV) **before** Drive upload; sanitised filenames (existing); quarantine folder. |
| Sessions/admin | Remove the dev default admin password (`ChangeMe123!`), force first-login change, fix the known "last active admin" race, rate-limit login. |
| Audit | Make business action + audit row one transaction (known gap); add Drive upload/download/delete events. |
| Deletion/retention | App never hard-deletes in Drive: soft-delete in DB and move to `Archive/`; Shared Drive "Content manager" cannot permanently delete; define retention. Note Drive version history/trash have limits **[VERIFY]**, so Drive is **not** a backup. |
| Backups | Postgres: scheduled dumps + point-in-time recovery, restore tested; Drive: Workspace admin retention/Vault if licensed, plus a periodic independent export. |
| Data egress | Company documents go to Google (by your decision, and to a company-owned tenant only). Hosted OCR/AI providers stay disabled; any enabling needs explicit company authorisation. |
| Availability | Server must not depend on any employee PC; Drive/API outages handled by retries + visible states. |

---

## 7. Phases, exit criteria and rough effort
(Effort is a one-developer estimate in working weeks; treat as +/-40 percent until the spike is done.)

| Phase | Content | Exit criterion | Weeks |
|---|---|---|---|
| 1 | **Drive/Workspace spike**: service account, Shared Drive access, upload/download/Range, `generateIds` idempotency, quota behaviour, scopes - synthetic PDFs only | all [VERIFY] items proven against the real tenant | 1 |
| 2 | Decisions (Section 8) + final design sign-off | written approval | 0.5 |
| 3 | `GoogleDriveStorage` + temp-file lifecycle + fake-Drive test double; dual-read resolver | pipeline tests pass on both backends | 2 |
| 4 | Postgres: Alembic revisions, engine/pool config, Postgres test target, orphan checks | existing suite green on Postgres (disposable DB) | 2-3 |
| 5 | Idempotent upload + `processing_jobs` + worker + reconciler | kill-and-restart and concurrency tests pass | 2 |
| 6 | AuthZ (ownership/scoping), audit atomicity, admin/session fixes | permission-matrix tests pass | 1-2 |
| 7 | Electron thin client + frontend states | desktop build talks to a test server over HTTPS | 1-2 |
| 8 | Multi-user, failure-injection and security testing; backup/restore drill | checklist signed off | 2 |
| 9 | Deployment (server, TLS, secrets, monitoring) + **separately approved** data migration rehearsal then cut-over | rehearsal reconciles to zero differences | 2-3 |
Total about **12-16 weeks** for one developer (less with two, since phases 3/4 and 6/7 can overlap).

---

## 8. Decisions I need from you before Phase 3
1. Is a **Google Workspace Shared Drive** available (edition), and may the service account be created in the company's Google Cloud project?
2. **Where will the central server run?** (company Windows/Linux server, a VPS, Google Cloud, Azure, ...). Container (Linux) is simplest for Postgres + Tesseract; the current build is Windows-only.
3. Should employees see **all** reports (as today) or only their own / their department's?
4. What happens to the **single shared Excel workbook** once many people upload? (keep one with locking, or per-report/on-demand export)
5. Which **existing database is the source of truth** for the merge of per-PC SQLite files, and who approves the migration window?
6. Retention period and who may archive/restore documents.
7. Written company approval for storing these financial PDFs in Google Drive.
8. Keep or retire the new Node "TA Backend" and the 1.2.8 forwarding feature (Drive replaces them).

## 9. Indicative running cost (verify current prices before budgeting)
* Google Workspace: Shared Drives require a paid Workspace plan (per-user licences for people using Drive; the service account itself needs no licence). Existing licences may already cover this.
* Server: one small VM (about 2-4 vCPU, 4-8 GB RAM; OCR is CPU-bound) - typically tens of USD per month in a public cloud, or nothing extra on existing company hardware.
* PostgreSQL: self-hosted on the same VM (no extra cost, you own backups) or a managed small instance (typically tens of USD per month).
* Backup storage, domain + TLS certificate (free via Let's Encrypt), monitoring: small.
* One-off: engineering effort (Section 7). No new per-user software licences are required by this design.

## 10. Risks (ranked)
1. Merging per-PC SQLite databases into one (data quality, duplicates, ids).
2. Drive API behaviour I could not test here (service-account quota/scopes, idempotent create, rate limits).
3. Removing offline operation and the local backend changes how every employee works (rollout plan needed).
4. Authorisation model: today everyone with a role sees everything.
5. Postgres FK enforcement exposing existing orphan rows.
6. OCR load concentrating on one server (size workers; jobs queue protects the API).

## 11. Protection rules honoured in this inspection
No code, config, version, Git state, release file, backup, `uat-data-test`, `experiments/node-extraction/fuel/`, `%APPDATA%` path, real database or authentic document was touched; nothing was built, packaged or sent anywhere except one unauthenticated read of the Drive page title.
