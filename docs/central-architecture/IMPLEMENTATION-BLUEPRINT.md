# Spinco TA Audit - Central Multi-User Platform: Implementation Blueprint

Status: **design only. Nothing has been implemented, migrated or uploaded.** Approved direction (from the owner): Electron + React
client; one centrally hosted FastAPI backend; PostgreSQL; Google Workspace Shared Drive for documents; central background workers.
This document is the detailed blueprint for that direction and is **not** approval to implement or migrate data.

Basis: read-only inspection of `D:\Bills` (code, tests, docs) plus the earlier assessment
(`ARCHITECTURE-ASSESSMENT-GOOGLE-DRIVE-CENTRAL.md`). No production database, `%APPDATA%` folder, authentic PDF, Google account or
credential was accessed or created. Statements about Google Drive behaviour, quotas and prices come from general knowledge, **not**
from this environment; they are marked **[VERIFY]** and must be proven in the Phase 1 spike or against current vendor pages.

Contents: 1 Architecture - 2 Database migration - 3 Google Drive - 4 Backend and processing - 5 Multi-user security - 6 Excel exports -
7 Existing functionality - 8 Infrastructure and costs - 9 Implementation plan - 10 Deliverables (diagrams, risk register, decision
register, checklists) - 11 Assumptions and gaps

---

# 1. Architecture

## 1.1 Facts about the current system that shape the design (verified in code)
* 19 tables; **67 API routes in 15 routers**; roles today: `VIEWER`, `AUDITOR`, `ADMIN` (19 permissions, **global by role**: no per-report access rules).
* Bills/receipts are **pages inside the expense-report PDF** (`bills.page_number`), not separate files.
* Extraction **never fills `department` or `location`** (no code writes them), so those columns are always empty today.
* The only human-entered business data is: user accounts/roles, **manual exception verifications** (+ history, bound to an evidence signature),
  and audit logs. Policy master is seeded and read-only through the API (one GET route). Everything else (expenses, bills, matches,
  validations, exceptions, evidence, Excel) is **derived from the PDFs** and can be regenerated.
* Processing runs in-process (`BackgroundTasks`), lost on restart; duplicate check is read-then-insert (race -> HTTP 500); one shared Excel file is rewritten in place.

## 1.2 Target architecture

```
                                  COMPANY NETWORK / INTERNET
  +--------------------+        HTTPS (TLS 1.2+)        +------------------------------------------------+
  | Employee PC        |  -----------------------------> |  Reverse proxy (TLS, size limit, rate limit)   |
  | Electron shell     |                                 +-----------------------+------------------------+
  | (thin: opens the   |                                                         |
  |  server URL)       |                                 +-----------------------v------------------------+
  +--------------------+                                 |  FastAPI API (stateless, 2+ processes)         |
                                                         |  - login/session/CSRF, RBAC + report scoping   |
   Employee PC ... (N)                                   |  - upload / download / list / export APIs      |
                                                         |  - audit writer, health, metrics               |
                                                         +---+------------------+-------------------+-----+
                                                             |                  |                   |
                                         SQL (pooled)        |        enqueue   |       Drive API   |  (backend only)
                                                             v                  v                   v
                                              +-----------------------+  +---------------+  +--------------------------+
                                              | PostgreSQL            |  | Worker(s)     |  | Google Workspace         |
                                              | business data, users, |<-| same code     |->| SHARED DRIVE             |
                                              | sessions, audit, jobs,|  | OCR/extract/  |  | originals, Excel exports |
                                              | Drive file ids        |  | match/validate|  | (private, no links)      |
                                              +-----------------------+  +-------+-------+  +--------------------------+
                                                                                 |
                                                                  temp work dir (per job, deleted after)

   Secrets (service-account key or workload identity, DB password, session keys) live in a secret store on the server side only.
```

### Component responsibilities
| Component | Owns | Must never |
|---|---|---|
| Electron shell | window, auto-update of the shell, opening the server URL | hold Google or DB credentials; run its own backend (central mode); store documents |
| React SPA (served by the API) | UI, calls relative `/api/...` (same origin) | see Drive ids/links; decide permissions |
| Reverse proxy | TLS, request-size limit, basic rate limiting, security headers | terminate auth |
| FastAPI API | authentication, authorisation, validation, audit, Drive access for uploads/downloads, job creation | do OCR inside a request; trust client-supplied ids |
| Worker | OCR, extraction, matching, validation, Excel generation; claims jobs from Postgres | accept network connections from clients |
| PostgreSQL | all business data, sessions, audit, job queue, Drive file ids/metadata | store PDF bytes |
| Shared Drive | original PDFs, Excel exports, archive | be the access-control mechanism (the app is), be public |

### Security boundaries
1. **Client <-> server**: untrusted client; TLS only; every request authenticated and authorised server-side; CSRF token on mutations (existing design kept, same-origin).
2. **Server <-> Google**: only the server holds Drive credentials; the service account sees only the one Shared Drive; no public/link sharing; no ids returned to the browser.
3. **Server <-> database**: separate DB roles: `app_rw` (no DDL), `migrator` (DDL, used only by Alembic), `audit_writer` insert-only on `audit_logs`, `readonly` for reporting/backup.
4. **Worker temp area**: private directory, 0700, per-job subfolder, deleted in `finally`, swept at start-up.
5. **Environments**: dev (SQLite/fake Drive allowed), staging (Postgres + a *separate* staging Shared Drive and service account, synthetic data only), production. Staging never gets production credentials or real documents.

### Key data flows
* **Upload**: client -> API (validate, hash) -> one DB transaction (document row + job) -> Drive upload (pre-generated id) -> row updated with Drive ids -> job `QUEUED` -> worker downloads to temp, processes, writes results, deletes temp -> status visible to all authorised users.
* **View/download**: client -> API (authorise by scope) -> API streams from Drive (Range supported) or bounded cache -> audit event recorded.
* **Export**: client -> API (filters) -> export job -> worker builds workbook from a consistent snapshot -> Drive `Excel Exports/` -> authorised download -> audit event.

---

# 2. Database migration: many per-PC SQLite databases -> one PostgreSQL

## 2.1 Principles
1. **The PDFs are the truth for extracted data.** Re-running the current central pipeline on each PDF regenerates every derived row, with today's (fixed) logic. Copying derived rows from many PCs would import different pipeline versions' mistakes.
2. **Human decisions are the only irreplaceable data**: user accounts/roles, manual verifications (+ history), audit logs. These are migrated explicitly, with key remapping and conflict handling.
3. **Per-PC databases are never modified.** All work is on read-only copies; nothing is deleted. Rollback is therefore "keep using the PCs".
4. **No automatic approvals.** A migrated verification is applied to a regenerated exception **only if its evidence signature still matches** (existing, tested mechanism); otherwise it is listed for re-review, never silently dropped or silently applied.

## 2.2 Table classification (all 19 tables)
| Class | Tables | Treatment |
|---|---|---|
| **A. Irreplaceable human data** | `users`, `exception_verifications`, `exception_verification_history`, `audit_logs` | migrate with remapping (2.5) |
| **B. Source documents** | `expense_reports`, `trip_reports` (rows hold `file_hash`, original filename, uploader, upload time) + the PDF files | PDFs uploaded to Drive; rows become central document rows with a provenance record per origin PC |
| **C. Derived / regenerable** | `expenses`, `bills`, `bill_match_candidates`, `trip_match_candidates`, `trips`, `validation_results`, `exceptions`, `source_evidence`, `processing_logs`, `excel_reports`, `employees` | **not copied**; regenerated by the central pipeline; legacy values kept only as a reconciliation baseline (2.7); `employees` enriched from the HR directory (5.3) |
| **D. Discard** | `auth_sessions` | never migrated (everyone logs in fresh) |
| **E. Reference** | `policy_master` | compare across PCs; one canonical set seeded centrally (2.5) |

## 2.3 Database inventory (owner-run; I did not and will not run it)
A small **read-only inventory tool** is specified (to be built in Phase 8; it opens each copied database with `mode=ro`, writes only aggregate/hash output, never document text, names, amounts or password hashes).

Per PC, collect:
| Item | Why |
|---|---|
| PC id, Windows user, app version, DB schema/migration state, DB file size and SHA-256 of the **copy** | identify the source and prove the copy is unchanged |
| Row counts of all 19 tables | sizing and later reconciliation |
| For each report: `file_hash`, size, `original_filename`, document id kind (ER/TRIP), `uploaded_at`, uploader email, extraction status | duplicate matrix |
| Users: email, role, active flag, created/last login (no password hashes) | user mapping |
| Verifications: count, verifier email, `evidence_signature` present or not | human-data sizing |
| Policy master: SHA-256 over canonical rows, `policy_version` | policy conflict detection |
| Last upload date, last audit event date | decide which PCs are still live |
| Where the PDFs live (folder path) and whether each referenced file still exists (existence + SHA-256 only) | missing-file list |

Output: one manifest per PC (JSON) + a combined **overlap matrix** (which hashes exist on which PCs). Procedure: freeze notice -> owner (or IT) copies each PC's database and documents folder to a controlled share -> tool runs on the copies.

## 2.4 Source-of-truth selection (no single database "wins")
| Data | Rule |
|---|---|
| Documents | **Union by SHA-256.** One central document per distinct hash per kind (expense/trip). |
| Users | The **central directory is authoritative** (HR/IT list, ideally Google Workspace accounts). Legacy users are mapped by email; the seeded default admin that exists on every PC is **not** migrated as a person (each PC has the same seeded email); passwords are never migrated; first login forces a new password (or SSO). |
| Verifications | Central identity = (document hash, expense line number, exception type). Applied only under the evidence-signature rule. |
| Audit logs | Append everything, tagged with origin PC; never edited. |
| Policy master | If every PC has identical rows, seed once. If they differ, **Finance decides** which version is canonical; others are logged. |
| Employees | Built from the HR directory by `employee_code`; codes seen in reports but absent from HR go to a review list. |

## 2.5 Duplicate identification and conflict resolution
| Situation | Detection | Resolution |
|---|---|---|
| Same PDF on several PCs | identical SHA-256 | one central document; every origin kept in `document_origins(pc_id, original_uploader_email, uploaded_at, legacy_id)`; central `uploaded_by` = earliest origin if that user exists, else the migration system user |
| Same ER/TRIP id, **different bytes** (re-downloaded or amended PDF) | same `er_id`/`trip_id`, different hash | **never merged automatically.** Both are migrated as separate documents, flagged `VERSION_CONFLICT`, and listed for Finance to choose the active version (the other is archived, not deleted) |
| Same report content under different names | identical hash | duplicate by hash (above) |
| Near duplicates (same employee/month/total, different hash) | heuristic report only | listed for review, never auto-merged |
| Same verification identity on several PCs | same (hash, line, type) | keep **all** as history; the live approval is the most recent whose evidence signature matches the regenerated evidence; if verifiers disagree, flag `CONFLICT` and leave the exception for re-review (conservative: not approved) |
| Verification with no signature (very old rows) | `evidence_signature IS NULL` | existing rule: applied only while `original_reason` equals the regenerated reason, else re-review |
| Verification whose evidence changed after regeneration | signature mismatch | not applied; written to `needs_re_review` with the old approval kept in history |
| User on several PCs | same email | one central user; roles reconciled by the central directory, not by "highest role wins" |
| Orphans in a source DB (SQLite does not enforce FKs) | pre-check queries (the orphan queries already documented in `docs/RELEASE_AND_UPDATES.md`) | orphan rows are reported and excluded; they are never imported |
| Clock/timezone | timestamps are stored naive in UTC by the app | confirm per PC that the app version stored UTC (it does in current versions); older versions listed in inventory |

## 2.6 Migration pipeline (all steps rehearsable on disposable copies)
```
 per-PC SQLite copies (read-only)         legacy PDF folders (read-only)
          |                                        |
          v                                        v
   [1 inventory + overlap matrix] -----> [2 dedupe plan (dry run report, owner reviews)]
                                                   |
                                                   v
                     [3 upload distinct PDFs to Drive: tagged batch id, verify md5 + sha256]
                                                   |
                                                   v
              [4 create central document rows + document_origins; storage_backend='gdrive']
                                                   |
                                                   v
                  [5 enqueue reprocess jobs: current central pipeline regenerates all derived data]
                                                   |
                       +---------------------------+---------------------------+
                       v                                                       v
   [6 reconcile regenerated results vs legacy baseline]        [7 import Class A: users (mapped), verifications
        (per document: line count, total, exception types)           (identity remap + signature gate), audit logs (tagged)]
                       |                                                       |
                       +---------------------------+---------------------------+
                                                   v
                         [8 validation suite + Finance spot audit + sign-off]
                                                   v
                         [9 freeze PCs read-only -> cut-over -> hypercare]
```
Idempotency: every step records a batch id and is re-runnable; a re-run never duplicates (hash and (origin PC, legacy id) keys). Steps 3-5 are resumable and rate-limited.

## 2.7 Reconciliation and validation (acceptance thresholds)
| Check | Pass condition |
|---|---|
| PDF integrity | 100% of distinct PDFs: SHA-256 in DB == hash of bytes in Drive; Drive md5 == recorded md5 |
| Coverage | every legacy report is either migrated, or on an explicit exclusion list with reason (missing file, orphan, rejected duplicate) |
| Row counts | central documents = distinct hashes per kind; `document_origins` = total legacy report rows migrated |
| Regenerated vs legacy extraction | per document compare: expense-line count, report total, set of exception types, overall status. Differences are **classified** (pipeline improvement / data issue / regression), not hidden; a difference not explained and approved by Finance blocks sign-off |
| Human data | 100% of verifications are either applied (signature match) or in `needs_re_review`; none lost: `applied + needs_re_review + conflicts == legacy count`; history rows preserved |
| Audit | audit rows imported == source rows (per PC); none altered |
| Integrity | zero FK violations (Postgres enforces them); zero NULL-identity duplicates; unique constraints hold |
| Spot audit | Finance reviews a stratified sample (minimum 30 reports or 5%, whichever is larger) against the original PDFs |
| Reproducibility | the whole pipeline re-run from scratch on the same inputs gives identical counts |

## 2.8 Rollback strategy
| Stage | Rollback |
|---|---|
| Before cut-over | nothing to undo: PCs are untouched; central system can be wiped and re-run |
| Drive | every migration upload carries `appProperties.migration_batch`; rollback = move that batch to `_quarantine/rolled-back-<batch>/` (no deletion) |
| Postgres | restore the pre-cut-over snapshot (taken immediately before go-live) |
| Clients | the previous **local-mode** Electron build stays available for the rollback window; PCs' databases remain as read-only archives |
| After cut-over | work done centrally since go-live cannot be merged back into per-PC databases. Therefore: **rollback window of 48 hours with a declared change freeze**; after that, problems are fixed forward. This is stated up front to users and approvers. |
Rollback triggers: any failed acceptance threshold above, unreconciled verification loss, data-corruption evidence, Drive access failure that cannot be recovered within the agreed time.

---

# 3. Google Drive

## 3.1 Shared Drive configuration
* One Shared Drive **"Spinco TA Audit"** for production, a separate **"Spinco TA Audit - STAGING"** for tests (separate service account too).
* Membership: the **service account only as "Content manager"** (can add/edit/move, cannot permanently delete or change membership); 2-3 Workspace administrators as "Manager". **Employees are not members**: they reach documents only through the app.
* Drive settings: external sharing off; "anyone with the link" off; members cannot be added outside the organisation; disable "allow people outside the shared drive to access files" **[VERIFY wording in the admin console]**; enable Drive audit logging; optionally DLP rules.
* Workspace requirement: an edition that includes Shared Drives (Business Standard or higher) **[VERIFY current editions]**; the service account itself needs no licence.

## 3.2 Service account and credentials
| Topic | Design |
|---|---|
| Identity | `svc-ta-audit-prod@<project>.iam.gserviceaccount.com` (and a staging equivalent) in a company-owned Google Cloud project with only the Drive API enabled |
| Authentication | **Prefer key-less** (workload identity / attached service account) when the server runs on Google Cloud. Otherwise a JSON key held **only** in a server secret store, never in git, Electron, or `.env` committed anywhere; rotation every 90 days; monitored for use |
| Scope | Option A `drive.file` (narrowest: the app sees only files it created; the app creates its own folders in the Shared Drive). Option B `drive` scope, restricted by the account's *membership* to one Shared Drive. Choose in the spike **[VERIFY: whether `drive.file` can see a folder created by a human]** |
| Not used | domain-wide delegation, personal accounts, OAuth refresh tokens of an individual employee |
| API usage | always `supportsAllDrives=true` (and `driveId`/`corpora=drive` for listings) |

## 3.3 Folder structure
```
Spinco TA Audit (Shared Drive)
  Expense Reports/<YYYY-MM of UPLOAD>/      original ER PDFs
  Trip Reports/<YYYY-MM of UPLOAD>/
  Excel Exports/<YYYY-MM>/
  Processed/                               derived archives, only if a need is approved
  Archive/<kind>/<YYYY-MM>/                soft-deleted / superseded documents
  _quarantine/                             failed validation/malware scan, rolled-back migration batches, orphans
```
Folders use the **upload month** (known at upload time), not the report month (only known after extraction). The report month stays in the database and is the filter used by the UI. Folder ids are cached in a `drive_folders` table (created on first use, found by `appProperties` on restart), so no path lookups on the hot path. A Shared Drive is limited in items and folder depth **[VERIFY: commonly cited 400,000 items per Shared Drive, 20 levels]**; one file per report keeps this far away (10,000 reports = ~10,000 items), but item count is monitored.

## 3.4 File naming
* Document: `<sha256[:12]>_<sanitised original filename>` (same convention the software already uses, so legacy and new files sort and read alike). Example `4c7ce71d98fe_ER-39832.pdf`.
* Export: `TA-Export_<YYYYMMDD-HHMMSS>_<requester-id>_<short-id>.xlsx`.
* Names are cosmetic. **Identity is the Drive file id plus `appProperties`**: `sha256`, `kind` (`expense`|`trip`|`export`), `doc_id` (central document id), `uploaded_by`, `migration_batch` (migrated files only). Property sizes are limited **[VERIFY]**; they hold only ids/hashes, never names of people or amounts.

## 3.5 Idempotent upload
```
1. API: validate, sha256, size.
2. TX1: INSERT document(file_hash UNIQUE, state='UPLOADING', drive_file_id=<pre-generated>) + job   -- race-safe:
        on unique violation -> return the existing document ("File already exists"), no Drive call.
   (the id comes from files.generateIds; stored BEFORE the upload so a crash leaves a traceable intent)
3. Drive: files.create with that id, resumable upload, appProperties, into the month folder.
        - success -> verify md5Checksum == local md5; mismatch -> delete? NO: move to _quarantine, state='FAILED'.
        - HTTP 409 (id already used) -> the earlier attempt succeeded: GET the file, compare md5/appProperties, ADOPT it.
        - timeout/5xx/network error -> retry with the SAME id (see 3.6).
4. TX2: state='STORED', drive_folder_id, drive_md5, size; job -> QUEUED.
```
**[VERIFY in spike]** that creating with a pre-generated id is rejected on a second attempt rather than creating a duplicate.
Fallback if not: before creating, search the folder for `appProperties has { key='sha256' and value='<hash>' }`.

## 3.6 Retry, recovery and reconciliation
| Condition | Behaviour |
|---|---|
| HTTP 408, 429, 500, 502, 503, 504, network error, `rateLimitExceeded` / `userRateLimitExceeded` (403) | exponential backoff with jitter: 1, 2, 4, 8, 16, 32 s (cap 60 s), max 6 attempts, honour `Retry-After`; the upload request itself returns `QUEUED/UPLOADING` quickly if the deadline passes, the **job** continues |
| 401 / token expiry | refresh once, then fail the job with an alert |
| 403 permission / `storageQuotaExceeded` / 404 on folder | **non-retryable**: job `FAILED`, alert to administrators (configuration problem) |
| Process crash mid-upload | row remains `UPLOADING`; the **reconciler** (runs at start-up and every 5 min) looks up the stored file id (or `sha256` property): found and md5 matches -> adopt (`STORED`); not found -> retry; older than the max age -> `FAILED` with message |
| Drive file with no database row (orphan) | reported to `_quarantine`/admin list, never auto-deleted |
| Database row whose Drive file is gone | document state `MISSING`; visible to admins; restore from Drive trash/Archive or re-upload from backup |
Downloads use `files.get?alt=media` with `Range`; failures use the same backoff; the md5 is re-checked when a file is cached.

## 3.7 Retention and archival
* The app **never hard-deletes** from Drive. "Delete document" = database soft-delete (`deleted_at`, `deleted_by`) + move to `Archive/`. Permanent deletion is an administrator + Workspace-admin action after the retention period, with a recorded approval.
* Retention period: **a Finance/legal decision (D-06)**. Accounting records are commonly kept for several years in India (often cited 6-8) **[confirm with Finance/legal]**.
* Drive trash and version history have time limits **[VERIFY]**: Drive is not a backup. Backup plan in 8.4 (Workspace Vault if licensed, plus a periodic independent export).
* Superseded documents (`VERSION_CONFLICT` losers) are archived, not deleted.

## 3.8 Quota and rate-limit handling
**[VERIFY all figures against the current Google documentation for the project]**: per-project and per-user request-rate limits (order of thousands per minute), a daily upload volume limit per account (commonly cited 750 GB/day), item-count limit per Shared Drive.
Design: a single **Drive client wrapper** with a token-bucket limiter (default 10 requests/second, configurable), concurrency cap (default 4 simultaneous transfers), backoff above, per-call timeout (30 s metadata, 120 s transfer), metrics (calls, errors by code, latency), and a **circuit breaker** (after N consecutive failures stop calling for 60 s and let jobs wait rather than burning quota). Expected volume (hundreds of reports/month, 1-5 MB each) is far below any limit; the migration (thousands of files) is throttled and run off-hours.

---

# 4. Backend and processing

## 4.1 Central FastAPI deployment
* One Linux container image (API + worker share it): FastAPI/uvicorn, PyMuPDF, Tesseract installed in the image (the current Windows-only PyInstaller bundle is not used on the server).
* Process model: reverse proxy -> `uvicorn` with 2-4 worker processes (stateless: sessions are in Postgres) ; **workers** are a separate service with the same image and `ROLE=worker`.
* Configuration: environment variables / secret store; **fail-fast at start-up** if mandatory settings are missing (DB URL, Drive config, session secret, `COOKIE_SECURE=true`, no default admin password).
* DB pool: `pool_size=10`, `max_overflow=20`, `pool_pre_ping=True`, `pool_recycle=1800`; statement timeout 30 s for API, longer for workers/export.
* Health: `/api/health` (liveness), `/api/health/ready` (DB reachable, Drive reachable, queue lag within bounds); Prometheus-style `/metrics` restricted to the internal network.
* Zero-downtime deploys: new image started, readiness passes, old drained; Alembic migrations run first with a backwards-compatible (expand/contract) discipline.

## 4.2 PostgreSQL job queue
```
processing_jobs(
  id bigserial PK, job_type text,            -- EXTRACT | REPROCESS | REMATCH_BILLS | REMATCH_TRIPS | VALIDATE | EXPORT | RECONCILE | MIGRATE_DOC
  document_kind text, document_id bigint,    -- target (nullable for EXPORT)
  idempotency_key text UNIQUE,               -- e.g. 'extract:<doc_id>:<pipeline_version>'; duplicate enqueue is a no-op
  state text,                                -- QUEUED | RUNNING | SUCCEEDED | FAILED | RETRY_WAIT | CANCELLED
  priority smallint default 100, attempts int, max_attempts int default 5,
  run_after timestamptz, locked_by text, locked_at timestamptz, heartbeat_at timestamptz,
  last_error text, payload jsonb, created_by bigint, created_at, updated_at, finished_at)
```
Claim (one statement, safe with many workers):
```sql
UPDATE processing_jobs SET state='RUNNING', locked_by=:w, locked_at=now(), heartbeat_at=now(), attempts=attempts+1
WHERE id = (SELECT id FROM processing_jobs
            WHERE state IN ('QUEUED','RETRY_WAIT') AND run_after <= now()
            ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```
* **Heartbeat** every 30 s while running; a **visibility timeout** (e.g. 10 min without heartbeat) returns the job to `RETRY_WAIT` (covers worker crash/restart).
* Retries: backoff 30 s, 2 min, 10 min, 1 h, 6 h up to `max_attempts`, then `FAILED` (visible in an admin screen, with a manual "retry" action that is audited). Non-retryable errors (corrupt PDF, validation) fail immediately.
* **Poison protection**: a job that crashes the worker process N times is parked as `FAILED` instead of looping.
* Status shown to users comes from the document row (`QUEUED`, `PROCESSING`, `PROCESSED`, `FAILED` - same fields the UI already polls) so the frontend barely changes.
* No Redis/Celery: Postgres is enough for this volume. Revisit only if throughput measurements say otherwise.

## 4.3 Worker architecture
* N worker processes (default = CPU cores - 1; OCR is CPU-bound); each runs one job at a time (pipeline is not thread-safe at the OCR level today).
* **Per-document serialisation**: `pg_advisory_xact_lock(hash(document))` around any job that rewrites a document's derived rows (today's wholesale delete-and-reinsert pattern: bill_match_candidates, trip_match_candidates, validation_results, exceptions, source_evidence). Two reprocess requests for one report therefore queue instead of racing.
* The pipeline code (extraction, parsing, matching, validation, verification carry-over) is reused unchanged; only its input changes from "a path on disk" to "a path in the job's temp directory".
* Pipeline version string recorded with the results (`pipeline_version`) so a later bulk reprocess can target only outdated documents.

## 4.4 OCR temporary-file lifecycle
```
job start  ->  create  WORK_DIR/<job_id>/   (mode 0700)
           ->  download the PDF from Drive (checksum verified)
           ->  run pipeline (reads the temp file)
           ->  commit results in ONE database transaction
finally    ->  delete WORK_DIR/<job_id>/ (always, including on failure/cancel)
start-up   ->  sweep: delete job dirs older than 2 h that have no RUNNING job
cap        ->  total temp usage limit (e.g. 10 GB); jobs wait when exceeded; alert at 80%
```
Viewing uses a **separate, bounded cache** (`VIEW_CACHE`, LRU, size cap e.g. 2 GB, TTL 24 h, md5-checked), the only permitted long-lived local copy; it is swept at start-up and is safe to delete at any time. Temp/cache contents are never logged; directories are on an encrypted volume.

## 4.5 Failure recovery and concurrency matrix
| Failure | Outcome |
|---|---|
| API crash during upload | reconciler adopts or retries (3.6); client sees the document as `UPLOADING`/`QUEUED` after refresh |
| Worker crash mid-job | heartbeat timeout -> `RETRY_WAIT`; temp dir swept; results were never half-committed (single transaction) |
| DB failover | pool reconnects (`pre_ping`); running jobs retry; no loss because state is in Postgres |
| Drive outage | circuit breaker; uploads accepted into `UPLOADING` and retried; extraction waits; users see a clear status |
| Duplicate simultaneous uploads | unique `file_hash` + conflict handling returns the same document (replaces today's 500) |
| Two users reprocess/verify the same report | advisory lock + optimistic version check (`expense_reports.row_version`); the second sees a "changed by someone else" message instead of silently overwriting |
| Two simultaneous admin role changes | single atomic `UPDATE ... WHERE (SELECT count(*) of other active admins) > 0` (fixes the known last-admin race) |

## 4.6 API security
HTTPS only, HSTS, `Secure; HttpOnly` cookies, SameSite=Lax (same-origin SPA keeps the existing cookie + CSRF design), CSP and `X-Content-Type-Options` headers, no CORS (same origin), login rate limit (e.g. 5 attempts/min per IP+account, progressive lock-out), upload limits (25 MB per file, per-user rate), server-side file-type and structure validation plus **malware scan before Drive upload** (ClamAV or equivalent; positives to `_quarantine`), sanitised filenames, parameterised SQL only (SQLAlchemy), secrets never in logs, error messages without internals, dependency pinning + vulnerability scanning in CI, audit event for every security-relevant action (5.5), session idle/absolute timeouts, optional SSO with the company Google Workspace (decision D-12) removing local passwords entirely.

---

# 5. Multi-user security

## 5.1 Roles
Existing: `ADMIN`, `AUDITOR`, `VIEWER`. Proposed (**decision D-04 confirms the real organisational roles**):
| Role | Purpose |
|---|---|
| EMPLOYEE | submits their own reports and sees only their own |
| AUDITOR | reviews reports in their scope, verifies review-required conditions, reprocesses |
| FINANCE | reads all reports in scope, exports Excel, sees audit history; **does not** change data or verify (unless decided) |
| ADMIN | manages users, scopes, system settings, archival/deletion approvals; does **not** verify claims by default (segregation of duties) |
| VIEWER (legacy) | kept as read-only until migration is complete, then mapped (VIEWER -> EMPLOYEE or FINANCE per person) |

## 5.2 Permission matrix (proposed; "scope" = the user's report scope, 5.3)
| Permission | EMPLOYEE | AUDITOR | FINANCE | ADMIN |
|---|---|---|---|---|
| Upload expense/trip report | own claims | in scope | no | in scope |
| View report, extraction, exceptions, bills | own | in scope | in scope | in scope |
| View/download the PDF | own | in scope | in scope | in scope |
| Reprocess / rematch / revalidate | no | in scope | no | in scope |
| Verify a review-required exception | no | in scope, **not own claim** | no | no (configurable) |
| Delete (soft) / archive document | no | no | no | yes, approval recorded |
| Dashboard / analytics / search | own data only | scope | scope | scope |
| Generate / download Excel export | own data, optional | in scope | in scope (all) | all |
| View audit history | own actions | scope-related events | scope-related events (read) | all |
| Manage users, roles, scopes, employee directory | no | no | no | yes |
| System settings, job administration | no | no | no | yes |
(Only the permissions that already exist in code are renamed/regrouped; new ones are listed in 7.3.) Segregation of duties: a user can never verify an exception on a report where they are the claimant, and an administrator who changes roles cannot also approve their own role change (two-person rule for ADMIN grants).

## 5.3 Report-level and department-level access
Today a report row has `employee_id`/`employee_code` (the claimant, extracted from the PDF), `uploaded_by_id`, and `department`/`location` columns that are **never filled**. Therefore:
1. **Employee directory** (new, authoritative, imported from HR): `employee_directory(employee_code PK, name, email, department, location, manager_email, active)`. `users.employee_code` links a login to a person.
2. **Scopes**: `user_scopes(user_id, scope_type IN ('ALL','DEPARTMENT','LOCATION','OWN'), value)`; EMPLOYEE implicitly `OWN`.
3. **Report ownership** is derived from the claimant's `employee_code` joined to the directory (department at the time of upload is snapshotted into the report row so later HR changes do not silently move history). Reports whose employee code is not in the directory fall into a **`UNASSIGNED` bucket visible only to ADMIN/FINANCE** and an alert, instead of becoming accidentally visible to everyone.
4. **Enforcement**: one central function `authorize(user, action, report)` and one SQL builder `scope_filter(user)` used by **every** listing/aggregate: expense/trip report lists, search, dashboard (5 routes), analytics (13 routes), exceptions, Excel export, document file route. Filtering only at the UI or after loading rows is not accepted: leakage through counts/aggregates/search is a standard failure, so each of these routes gets a permission-matrix test.
5. Default policy when in doubt: **deny**.

## 5.4 Document downloads
Same authorisation as the report; served only by the API (never a Drive link); `Content-Disposition` and `nosniff`; every download writes an audit event (user, document, time, IP, result); per-user rate limit and anomaly alert on bulk downloads; exports expire (6.4); optional watermark sheet with requester and time.

## 5.5 Audit history
* Append-only `audit_logs`: the application's DB role has INSERT only (no UPDATE/DELETE); optional hash chain per row to make tampering evident.
* The business change and its audit row are written in **one transaction** (closes today's gap where the audit write is a separate step).
* Events (adds to the current set): login success/failure/lock-out, logout, password change/reset, role/scope change, user create/disable, upload (accepted/duplicate/rejected), document view/download, reprocess, verify/re-verify (note text still not stored in the event), soft-delete/restore, export request/download, Drive reconciliation actions, job retry, configuration change, migration batch events.
* Each event stores: actor, action, entity type/id, document hash where relevant, outcome, IP, client version, timestamp (UTC), and for migrated events the origin PC.
* Retention: at least the document retention period (D-06); exported to cold storage after N months.

---

# 6. Excel exports

## 6.1 Why the current design cannot be centralised
One shared `Spinco_TA_Audit_Consolidated.xlsx`, rebuilt from the whole database after every document, written in place with no locking, tracked by a single `ExcelReport` row: concurrent users would overwrite each other, readers can receive a half-written file, and any user would export everyone's data.

## 6.2 Design: independent, timestamped, filterable export jobs
```
excel_exports(
  id bigserial PK, export_key uuid UNIQUE,         -- idempotency key from the client
  requested_by bigint, requested_at, scope_snapshot jsonb,   -- the exact scope that was applied
  filters jsonb,          -- months range, kind(expense/trip/both), status, exception types, department, employee_code, uploaded range
  state text,             -- QUEUED | RUNNING | READY | FAILED | EXPIRED
  as_of timestamptz,      -- the data snapshot time (also printed on the Summary sheet)
  filename text, drive_file_id text, sha256 text, size_bytes bigint,
  row_counts jsonb, generator_version text, finished_at, expires_at, downloaded_count int)
```
* `POST /api/excel-exports` (filters) -> job -> worker. The worker opens **one `REPEATABLE READ` read-only transaction** (consistent snapshot), applies `scope_filter` of the requester **plus** the filters, builds the workbook with the **existing generator logic** (same sheets: Expense Reports, Trip Reports, Expenses, Exceptions, Batta-Trip Matching, Summary), writes to a temp file, then renames atomically, uploads to `Excel Exports/<YYYY-MM>/`, verifies md5, marks `READY`.
* **No shared file is ever rewritten**: each export is its own immutable file, so concurrency is simply many independent jobs (bounded by worker slots).
* Idempotency: the same `export_key` returns the same export; an optional "reuse a READY export with identical filters and scope generated in the last N minutes".
* The Summary sheet states: requester, scope, filters, `as_of`, generator version, row counts (so a spreadsheet can always be traced to its inputs).

## 6.3 Export history
`GET /api/excel-exports` lists the caller's exports (FINANCE/ADMIN: all, filterable), with state, filters, time, size, row counts, downloads; re-download until expiry; admin can expire/archive; downloads and requests are audited (5.5).

## 6.4 Retention and safety
Exports hold confidential data: default expiry 30 days (D-09), then moved to `Archive/` and the DB row `EXPIRED`; export size/row limits with a clear error; optional per-user daily export cap; no export of data outside the requester's scope, ever.

## 6.5 Compatibility
`/api/excel-files/*` stays: `generate` creates an "ALL (within caller's scope)" export and returns the old response shape; `metadata`, `sheets`, `sheets/{name}` (the in-app preview) and `download` operate on the caller's most recent READY export. The UI "Excel Files" page keeps working and gains the history table.

---

# 7. Existing functionality: what happens to each piece

Rule: **nothing is removed or deprecated without (a) a compatibility layer, (b) a written date, (c) at least two releases of notice.** Response schemas only gain fields.

## 7.1 Components
| Component | Fate | Compatibility / sunset |
|---|---|---|
| **Node `TA Backend`** (archive server, folder sync) | Superseded by Drive. Keep **unchanged and running** through migration and the parallel-run (it is a second, independent copy of PDFs). Not part of the central path. | After sign-off and one clean release cycle: archive its code under `tools/` (not deleted), stop the service. Its sorting/duplicate rules were taken from the Python software, so no behaviour is lost. |
| **1.2.8 forwarding** (`ta_backend_forward`) | Kept; default **off** in central mode (`TA_BACKEND_URL=off`). In local-mode builds it keeps working as is. | Remove only after two central releases with no users on local mode. |
| **Local filesystem storage** (`LocalFilesystemStorage`, `source_path`) | **Kept as a permanent backend**: dev/test, legacy documents not yet migrated, rollback. `storage_backend` column selects (`local` or `gdrive`); resolver reads either. | Never removed while any row still says `local`. |
| **SQLite** | Supported for dev/test and local-mode builds until the Postgres test target is the CI default; **not** for production central mode. | `db_migrations.py` stays for local-mode builds; Alembic becomes the only tool for Postgres. |
| **Local-mode Electron build** (own backend + Tesseract) | Maintained as a parallel build line through the rollback window and pilot, then frozen. | Frozen build remains downloadable for emergencies. |
| **Local `%APPDATA%` data** | Untouched by migration; becomes a read-only archive per PC. | Cleanup is a separate, approved, much later step. |
| **Auto-updater / release folder / `latest.yml`** | Applies to the Electron shell only; server updates are deployed centrally. | Release-folder procedure unchanged. |
| **Excel consolidated workbook** | Replaced by export jobs with a compatibility endpoint (6.5). | Old endpoints stay. |

## 7.2 API compatibility (all 67 routes)
| Router (routes) | Change | Notes |
|---|---|---|
| `/api/auth` (4) | config: secure cookies, rate limit, optional SSO, `must_change_password` enforced | same request/response |
| `/api/uploads` (3) | **behaviour**: Drive upload, async states `UPLOADING/QUEUED`; idempotent duplicate response (replaces the 500) | response fields kept, new states additive |
| `/api/documents` (9) | `file` streams from Drive/cache; `extract`/`reprocess`/`rematch-*` enqueue jobs (return job/status); `DELETE` = soft-delete + archive | `204` kept; new `restore` route added |
| `/api/expense-reports` (9), `/api/trip-reports` (4), `/api/expenses` (3), `/api/exceptions` (1) | scope-filtered; verify adds segregation-of-duties and version check | same shapes |
| `/api/dashboard` (5), `/api/analytics` (13), `/api/search` (1) | **scope-filtered aggregates** (largest authorisation work item) | same shapes |
| `/api/excel-files` (6) | compatibility layer over export jobs (6.5) | same shapes |
| `/api/users` (6) | roles/scopes/directory link; atomic last-admin rule; two-person rule for ADMIN grants | additive fields |
| `/api/audit-logs` (1) | scope-aware; richer events | additive |
| `/api/policy-master` (1) | unchanged | |
| `/api/health` (1) | adds `/ready`, `/metrics` (internal) | |
New routes: `/api/excel-exports`, `/api/jobs` (admin), `/api/documents/{id}/restore`, `/api/employee-directory` (admin), `/api/users/{id}/scopes`.

## 7.3 Permissions that are new (the existing 19 are only regrouped)
`MANAGE_SCOPES`, `MANAGE_DIRECTORY`, `VIEW_ALL_AUDIT`, `RESTORE_DOCUMENTS`, `MANAGE_JOBS`, `EXPORT_ALL`, `APPROVE_DELETION`.

---

# 8. Infrastructure and costs

All prices are **indicative ranges for planning only, in USD per month, small-to-medium company scale; verify with current vendor calculators and quotes (and convert to INR at the then-current rate).** Nothing here is a quote.

## 8.1 Hosting options (company-approved choice = decision D-02)
| Option | Description | Pros | Cons | Indicative cost |
|---|---|---|---|---|
| A. **On-premises server** | existing/new company server (Linux VM or Windows + containers), Postgres on the same or a second machine | no cloud bill, data local, uses existing IT | you own patching, backups, power, uptime, remote access/VPN or public exposure; harder DR | hardware amortisation + IT time; software ~0 |
| B. **Single cloud VM, self-managed Postgres** | one VM (2-4 vCPU, 8 GB, 100 GB SSD) in a company cloud account | simple, cheap, flexible | you own DB ops/backups/patching; single point of failure unless a standby is added | VM 40-90 + disk/snapshots 5-20 |
| C. **Managed containers + managed Postgres** (e.g. Azure Container Apps + Azure Database for PostgreSQL; AWS ECS + RDS) | platform runs containers, vendor runs the DB (automated backups, PITR, patching) | least ops, built-in backups/HA options, scales | higher cost, vendor-specific setup | compute 30-100 + managed PG 30-120 + backup/egress 5-20 |
| D. **Google Cloud (aligned with Workspace)**: Cloud Run + Cloud SQL + Secret Manager | same as C on Google; the service account can be **key-less** via attached identity, tight IAM with the Shared Drive | no JSON key to protect, one vendor relationship, good fit for Drive | Cloud Run needs care for CPU-heavy OCR workers (use a VM or Cloud Run jobs for workers); cost similar to C | Cloud Run 20-80 + Cloud SQL 30-120 + workers 20-60 |
**Recommendation:** D (or C if the company standard is Azure/AWS) for a first deployment; B is the budget fallback; A only if IT can guarantee backups, patching, 24x7 reachability and a restore drill. Choose by company approval, data-residency requirements and IT capability, not by price alone.

## 8.2 Scenario totals (indicative, USD/month, excluding Workspace licences)
| Scale | Users | Reports/month | Likely setup | Estimate |
|---|---|---|---|---|
| Small | up to ~20 | up to ~300 | B or D minimal, 2 vCPU | **~80-200** |
| Medium | ~50-100 | ~1,000-3,000 | C/D with 1-2 workers, HA DB optional | **~200-500** |
Sizing of workers is unknown until measured: benchmark pages/minute for typical (native-text vs scanned) reports on the target VM in Phase 1/9 and size from the monthly page count with 3x headroom.

## 8.3 Google Workspace requirements
Shared Drives need an edition that includes them (Business Standard or higher) **[VERIFY]**; licences are per person using Drive (existing licences may already cover this); the service account needs none; a Google Cloud project (billing account) is needed for the Drive API/service account (the API itself is free of charge at this scale **[VERIFY]**); Vault/retention features depend on the edition.

## 8.4 Backups, monitoring, disaster recovery
| Area | Design |
|---|---|
| Postgres backup | nightly full + continuous WAL archiving / managed PITR; retention 35 days + monthly archives; **restore tested quarterly** into a scratch instance with a documented runbook |
| Targets | proposed **RPO <= 15 minutes, RTO <= 4 hours** (D-10) |
| Drive | Shared Drive is replicated by Google but is not a backup: Workspace retention rules/Vault if licensed + a **monthly independent export** of the Shared Drive to separate storage (checksummed) |
| Secrets/config | stored in the secret manager with versioning; infrastructure as code or a written rebuild runbook |
| Monitoring | uptime probe on `/api/health/ready`; alerts on: API 5xx rate, login failures spike, queue depth and **oldest queued job age**, failed jobs, Drive error/quota rate, DB connections/disk/replication lag, temp/cache disk use, certificate expiry, backup success/age |
| Logging | structured JSON logs, central retention 90 days, no document content or credentials in logs |
| DR scenarios | server loss (redeploy image + restore DB), DB loss (PITR), Drive outage (jobs wait, read from cache), accidental deletion (Archive + Drive trash + backup), credential compromise (rotate key, revoke, audit review) |

---

# 9. Implementation plan

Effort = developer-weeks for one developer; **revised total 22-26 dev-weeks (about 12-16 calendar weeks with two developers)**. This is larger than the first assessment's 12-16 weeks because this blueprint adds report/department authorisation, export jobs, migration tooling and a pilot. Treat as +/-40% until Phase 1 is done.

| # | Phase | Main tasks | Depends on | Acceptance criteria | Tests | Effort | Rollback condition |
|---|---|---|---|---|---|---|---|
| 0 | Prerequisites and decisions | answer decision register (10.4); company approval; Workspace/Shared Drive; Cloud project; hosting choice; HR directory source | - | all blocking decisions D-01..D-08 closed in writing | - | 1 | n/a |
| 1 | **Drive and infra spike** (synthetic only, staging Shared Drive) | service account, scopes, upload/download/Range, `generateIds` idempotency, quota/limit behaviour, md5 verification, OCR throughput benchmark | 0 | every [VERIFY] item resolved and written down; go/no-go for Drive | spike scripts, benchmark report | 1 | stop project phase if Drive cannot meet idempotency/quota needs |
| 2 | Storage layer | `GoogleDriveStorage` behind `DocumentStorage`; `open_local()` temp-file API replacing `local_path()`; temp/cache lifecycle; fake-Drive test double; dual-read resolver by `storage_backend` | 1 | whole existing pipeline passes on local **and** fake-Drive backends; no code opens `source_path` directly | existing 1,565-test suite + new storage tests; temp-leak test | 2 | feature flag off -> local backend, no schema change needed |
| 3 | PostgreSQL | Alembic revisions (Drive columns, `processing_jobs`, `document_origins`, `row_version`, constraints/indexes, directory/scope tables), engine/pool config, Postgres CI target, orphan pre-checks | 2 | full suite green on Postgres (disposable DB); migrations apply and downgrade cleanly; no SQLite DDL left in the central path | suite on both DBs; migration up/down tests; FK/constraint tests | 2.5 | revert Alembic revision; SQLite path untouched |
| 4 | Upload, jobs, workers | race-safe idempotent upload; Drive wrapper (limits/backoff/breaker); job table + claim/heartbeat/retry; worker service; reconciler; advisory locks; malware scan hook | 2, 3 | kill -9 API/worker at every step -> no loss, no duplicate; 20 concurrent uploads of the same file store one; temp dirs always cleaned | failure-injection, concurrency, soak tests | 3 | disable jobs flag -> in-process path (kept) |
| 5 | Identity, authorisation, audit | roles/scopes/directory; `authorize()` + `scope_filter()` on all 67 routes (aggregates, search, exports included); segregation of duties; atomic last-admin; audit atomicity + new events; session hardening; optional SSO | 3 | permission-matrix suite passes for every route x role x scope; no route returns out-of-scope data in rows, counts or search | generated matrix tests, negative tests, pen-test checklist | 3 | feature flag -> legacy role checks |
| 6 | Excel export jobs | `excel_exports`, worker generation from snapshot, Drive upload, history UI, compatibility endpoints | 4, 5 | 10 simultaneous exports all correct and independent; exports respect scope; old endpoints still work | concurrency tests, scope tests, golden workbook comparison | 1.5 | keep the old generator path behind a flag |
| 7 | Client | Electron thin shell (opens the server URL, keeps updater), frontend states (uploading/queued/failed), admin job screen, export history | 4, 6 | installer connects to staging over HTTPS; uploads/viewer/exports work; no local backend started in central mode | Playwright E2E (as used before), installer smoke test on a clean VM | 2 | previous local-mode build |
| 8 | Migration tooling and rehearsal | inventory tool, dedupe planner, legacy-PDF uploader, reprocess-and-reconcile, human-data importer, validation report; **rehearsals on disposable copies** | 3, 4, 5 | rehearsal run meets every threshold in 2.7 twice in a row; report approved by Finance | reconciliation suite, idempotent re-run test, rollback drill | 3 | rehearsal only: discard and repeat |
| 9 | Security, performance, DR | pen-test/threat review, dependency scan, load test at 2x expected, backup restore drill, failover drill, log review | 4-8 | no high/critical findings open; restore drill within RTO; load targets met | security checklist, k6/Locust, restore drill record | 2 | block go-live |
| 10 | Pilot, cut-over, hypercare | pilot group (3-5 users) on production; then production migration (approved window, freeze notice); 2 weeks hypercare | 8, 9 + explicit approval | thresholds met on the real migration; pilot sign-off; rollback window passed | production reconciliation, user acceptance | 2 | 2.8 rollback triggers within the 48 h window |

## 9.1 Dependency graph
```
0 -> 1 -> 2 -> 3 -> 4 -> 6 -> 7 -----+
              \      \-> 5 -----------+-> 9 -> 10
               \----------> 8 -------+
(2 and 3 can overlap; 5 and 4 can run in parallel with two developers; 8 starts when 3-5 are stable)
```

## 9.2 Test strategy
* Keep the existing suite as the regression net (it must pass on both SQLite and Postgres); the 75 authentic-document tests are run **only** by the owner in the authorised isolated workflow already in place.
* New layers: storage contract tests (local, fake Drive, real staging Drive), job-queue concurrency and crash tests, upload idempotency tests, permission-matrix tests generated from the route table, export concurrency tests, migration rehearsal reconciliation, load/soak, security checks.
* Synthetic documents only outside the owner-run steps; real documents never enter staging.

## 9.3 Go/no-go gates
G1 after Phase 1 (Drive feasible) - G2 after Phase 5 (authorisation proven) - G3 after Phase 8 (migration rehearsed twice) - G4 after Phase 9 (security/DR) - G5 explicit written approval for the production migration.

---

# 10. Deliverables

## 10.1 Architecture diagram
See section 1.2.

## 10.2 Database migration diagram
See section 2.6.

## 10.3 Risk register
| ID | Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R-01 | Merging per-PC databases loses or duplicates data | M | H | PDFs as truth, regenerate derived data, hash dedupe, reconciliation thresholds, rehearsals | Migration lead |
| R-02 | Lost manual verifications | M | H | identity+signature import, `needs_re_review` list, nothing dropped, history kept | Migration lead |
| R-03 | Drive behaviour differs from assumptions (quota, idempotent create, scopes) | M | H | Phase 1 spike with go/no-go | Backend lead |
| R-04 | Out-of-scope data exposed through aggregates/search/exports | M | H | `scope_filter` everywhere, generated matrix tests, default deny | Security lead |
| R-05 | Service-account key leak | L | H | key-less identity or secret store, rotation, least-privilege membership, alerts | Infra lead |
| R-06 | Department data missing (never extracted) | H | M | HR directory import; `UNASSIGNED` bucket | Finance/HR |
| R-07 | Server/DB outage stops all users (no offline mode) | M | H | HA options, monitoring, RPO/RTO, runbooks, local-mode build kept for the window | Infra lead |
| R-08 | OCR load saturates the server | M | M | job queue isolation, worker sizing from benchmark, priorities | Backend lead |
| R-09 | Postgres FK enforcement exposes legacy orphan rows | M | M | orphan pre-checks, exclusion list | Migration lead |
| R-10 | Different pipeline versions on PCs give different legacy results | H | M | regenerate centrally; classify differences instead of trusting legacy | Migration lead |
| R-11 | Timeline slips (scope grew vs first estimate) | M | M | phase gates, parallel work, +/-40% stated, scope control | Project owner |
| R-12 | Users resist the change / confusion at cut-over | M | M | pilot, training notes, freeze notice, hypercare | Project owner |
| R-13 | Retention/legal requirements not met | L | H | D-06 decided with Finance/legal before go-live; archive-not-delete | Finance/legal |
| R-14 | Drive/Google outage | L | M | circuit breaker, queued uploads, cache, status messages | Backend lead |
| R-15 | Cut-over rollback loses post-go-live work | M | M | 48 h window with change freeze, then forward-fix; stated to approvers | Project owner |
| R-16 | Costs exceed expectation | L | L | indicative ranges now, measured sizing in Phase 1/9, budget gate G1 | Project owner |

## 10.4 Decision register (all OPEN)
| ID | Decision | Options | Recommendation | Needed by |
|---|---|---|---|---|
| D-01 | Google Workspace edition and Shared Drive availability; service account allowed? | edition upgrade / existing | confirm existing edition includes Shared Drives | Phase 0 |
| D-02 | Where does the central server run? | on-prem / VM / managed / Google Cloud | Google Cloud (D) or the company's standard cloud; on-prem only with proven backup/DR | Phase 0 |
| D-03 | Company approval to store financial PDFs in Google Drive | yes / no | written approval before Phase 1 uploads even synthetic data to a company tenant | Phase 0 |
| D-04 | Real roles and who holds them (employee / auditor / finance / admin) and their rights | proposed matrix 5.2 | confirm or amend the matrix | Phase 5 design |
| D-05 | Access scope: all reports vs own vs department | ALL / DEPARTMENT / OWN per role | default deny; auditors by department; finance all | Phase 5 design |
| D-06 | Retention period and who may delete/restore | years / approval | Finance + legal decide; archive, never hard-delete without approval | before go-live |
| D-07 | Source of the employee directory | HR export / Google Workspace directory / manual | HR export refreshed monthly | Phase 5 |
| D-08 | Policy master conflicts across PCs: which version is canonical | Finance decides | decided from the inventory | Phase 8 |
| D-09 | Export retention and size limits | days / rows | 30 days; limits agreed with Finance | Phase 6 |
| D-10 | RPO/RTO targets | e.g. 15 min / 4 h | adopt, then price it | Phase 0 |
| D-11 | Keep or retire Node TA Backend and forwarding | retire after sign-off / keep as secondary | retire after one clean release cycle | after Phase 10 |
| D-12 | Sign-in method | local passwords / Google SSO (company domain) | Google SSO for central mode; keep local for break-glass admin | Phase 5 |
| D-13 | Which PCs' data is in scope and the freeze window | list / dates | from the inventory | Phase 8 |
| D-14 | Bills as separate files? | keep pages inside the ER PDF / split pages into files | keep as today (no split) | Phase 2 |
| D-15 | Segregation of duties: may ADMIN verify claims? | yes / no | no by default | Phase 5 |
| D-16 | Malware scanning product | ClamAV / vendor | ClamAV in the upload path | Phase 4 |

## 10.5 Testing and deployment checklist
**Before any implementation**
- [ ] Decisions D-01..D-05, D-10 closed in writing; company approval (D-03) recorded
- [ ] Staging Shared Drive + staging service account created by the owner (not by me); no production credentials in staging
- [ ] Sizing benchmark on the target server

**Per phase gate**
- [ ] Acceptance criteria of the phase met and evidenced
- [ ] Existing test suite green (SQLite, and Postgres from Phase 3); no test weakened
- [ ] New tests added for every new rule (storage, jobs, permissions, exports, migration)
- [ ] Security review of the phase's changes; no secrets in the repo
- [ ] Rollback condition for the phase written and tested

**Pre-production**
- [ ] Rehearsal migration run twice with all thresholds met (2.7); Finance sign-off
- [ ] Penetration/threat review closed (no high/critical)
- [ ] Load test at 2x expected volume passed; backup **restore drill** within RTO
- [ ] Monitoring, alerts and on-call contacts live; runbooks written (deploy, rollback, restore, key rotation)
- [ ] TLS valid, cookies `Secure`, default admin removed, first-login password change enforced
- [ ] Shared Drive sharing settings audited (no link sharing, no external members)
- [ ] Pilot group completed; user instructions and freeze notice sent

**Cut-over day**
- [ ] Explicit written approval (G5); change freeze declared
- [ ] Postgres snapshot + Drive batch tag taken; PCs set to read-only
- [ ] Migration executed; every threshold re-checked on production; Finance spot audit
- [ ] Smoke test by pilot users; go/no-go decision recorded
- [ ] Rollback window (48 h) started; hypercare roster active

**After**
- [ ] 2-week hypercare closed; incident log reviewed
- [ ] Retention/archival job schedule verified; first restore drill scheduled
- [ ] Sunset notices for Node TA Backend / forwarding issued per 7.1 (not before)

---

# 11. Assumptions and gaps
* Google Drive facts (idempotent create with generated ids, scope behaviour, quotas, item limits, property size limits, edition requirements) and all prices are **unverified** here; Phase 1 and current vendor pages decide.
* The unauthenticated check of the supplied Drive link showed only a title; whether it is a Shared Drive, and its permissions, are still unknown.
* No per-PC database was inventoried; the number of PCs, overlap and data volume are unknown, so migration effort (Phase 8) is an estimate.
* Legal/retention requirements are placeholders until Finance/legal answer D-06.
* This blueprint changes **nothing**: no source, database, Git state, version, build, credential, upload or publication.
