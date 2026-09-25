# Centralized Data Architecture — Phase 2 Report

**Status: architecture assessment and plan only. Nothing in this document
has been implemented.** No model, no route, no database, and no production
data was changed to produce it. This report was built from a direct,
file-by-file inspection of the current codebase (not general knowledge) —
see the per-section notes for exactly what was read.

**Per the explicit instruction this was written under: STOP after this
report. Do not begin production migration or centralized-server
implementation until it is explicitly approved.**

---

## 1. Current database architecture

`backend/app/database.py` — bare SQLAlchemy, single SQLite file:

```python
DB_PATH = env SPINCO_DB_PATH or BACKEND_DIR / "spinco_ta.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
```

- No connection pooling configuration (SQLite doesn't need it the way
  Postgres does — this will need explicit `pool_size`/`max_overflow` tuning).
- **No Alembic, no migration framework of any kind.** Schema evolution is
  handled by a hand-rolled module, `app/services/db_migrations.py`, run on
  every FastAPI startup: idempotent `ALTER TABLE ... ADD COLUMN` for a
  hardcoded list of (table, column, ddl) tuples, followed by
  `Base.metadata.create_all()` (which only ever creates *missing* tables,
  never alters existing ones). **The DDL in `db_migrations.py` uses
  SQLite-specific literals** (`"BOOLEAN DEFAULT 1"`, `"DATETIME"`) that are
  not valid PostgreSQL syntax — this module cannot run against Postgres
  unmodified and its migration history would need to be absorbed into
  whatever real tool replaces it.
- Production database: `%APPDATA%\Spinco TA Audit\database\spinco_ta.db`
  (per install, per machine — confirmed no sharing of any kind).

## 2. Current SQLAlchemy model inventory

16 tables across 13 files, full column/FK/constraint inventory obtained by
reading every model file directly:

| Table | Notable columns | Relationships | Gaps found |
|---|---|---|---|
| `users` | email (unique, indexed), role, password_hash, is_active, must_change_password, last_login_at | none declared here | role has no CHECK constraint (Python-enforced only) |
| `auth_sessions` | user_id (FK, indexed), token_hash (unique, indexed), csrf_token, expires_at (indexed) | — | — |
| `employees` | employee_code (unique, indexed), name, email | — | **email not unique** (unlike users.email) |
| `policy_master` | category (unique, indexed), reimbursement_basis, limit_amount, document_required, claim_type, status, effective_date, policy_version | — | no CHECK on the enum-like string fields |
| `expense_reports` | er_id (indexed, **not unique**), file_hash (unique, indexed — dedupe key), employee_id (FK, **unindexed**), uploaded_by_id (FK, **unindexed**), overall_status | expenses (1:N cascade), bills (1:N cascade) | er_id business key not unique |
| `expenses` | expense_report_id (FK, indexed), s_no, bill_id (FK, **unindexed**), trip_report_id (FK, **unindexed**) | expense_report (N:1) | **no unique/composite constraint on (expense_report_id, s_no)** — line order can duplicate today |
| `bills` | expense_report_id (FK, indexed), matched_expense_id | expense_report (N:1) | **`matched_expense_id` is a plain int, not a declared ForeignKey at all** |
| `bill_match_candidates` | expense_id (FK, indexed), bill_id (FK, **unindexed**) | — (wholesale-replaced each match run) | — |
| `trip_reports` | trip_id (indexed, **not unique**), file_hash (unique, indexed), employee_id/source_expense_report_id/uploaded_by_id (FKs, all **unindexed**) | legs (1:N cascade) | trip_id business key not unique |
| `trips` (legs) | trip_report_id (FK, indexed), leg_no | trip_report (N:1) | **no unique/composite constraint on (trip_report_id, leg_no)** |
| `trip_match_candidates` | expense_id (FK, indexed), trip_report_id (FK, **unindexed**) | — (wholesale-replaced) | — |
| `validation_results` | expense_report_id (FK, indexed), expense_id (FK, indexed) | — (wholesale-replaced) | result has no CHECK constraint |
| `exceptions` | expense_report_id (FK, indexed), expense_id/validation_result_id/bill_id/trip_report_id/verified_by_id (FKs, all **unindexed**) | — (wholesale-replaced) | — |
| `exception_verifications` | expense_report_id (FK, indexed), expense_id (FK, indexed), verified_by_id (FK, **unindexed**) | — | docstring asserts logical key `(expense_report_id, expense_id, exception_type)` but **no UniqueConstraint enforces it** |
| `excel_reports` | report_month, file_path, last_updated | — | designed as a **singleton row** (`db.query(ExcelReport).first()`), uniqueness deliberately dropped in an earlier phase — nothing at the DB level stops a second row existing |
| `source_evidence` | document_type + document_ref_id (polymorphic pointer to either expense_reports.id or trip_reports.id) | — | **not a real FK by design** — can't trivially gain FK enforcement without a redesign |
| `processing_logs` / `audit_logs` | expense_report_id/trip_report_id/user_id (FKs, all **unindexed**) | — | — |

No `UniqueConstraint`, table-level `Index`, or `CheckConstraint` is used
*anywhere* in the codebase — every constraint present is a column-level
`unique=True`/`index=True` only.

**SQLite-specific findings:**
- No native JSON columns anywhere — every "structured" field (`reasons`,
  `details`, `remarks`, etc.) is plain string/text. No JSON-as-TEXT
  migration concern.
- No `func.strftime`/`julianday` or other SQLite-dialect query functions
  found anywhere.
- All primary keys are plain autoincrement `int` — translates cleanly to
  Postgres `SERIAL`/`IDENTITY`.
- Every `DateTime` column across all 16 tables uses the naive/UTC-by-convention
  pattern already identified and worked around at the API-serialization
  layer in this project's history (`UTCDateTime` in `app/schemas/common.py`)
  — Postgres's `TIMESTAMPTZ` would fix this at the storage layer itself, a
  genuine incidental benefit of migrating, not something this report treats
  as new.
- `db_migrations.py`'s raw `ALTER TABLE` DDL strings are the one place with
  real SQLite-dialect coupling requiring rewrite.

**Foreign keys with no index** (fine on SQLite's small scale, a real
performance concern once the DB is shared/larger): `expense_reports.employee_id`,
`expense_reports.uploaded_by_id`, `expenses.bill_id`, `expenses.trip_report_id`,
`bill_match_candidates.bill_id`, `trip_reports.source_expense_report_id`,
`trip_reports.employee_id`, `trip_reports.uploaded_by_id`,
`trip_match_candidates.trip_report_id`, `exceptions.expense_id`,
`exceptions.validation_result_id`, `exceptions.bill_id`,
`exceptions.trip_report_id`, `exceptions.verified_by_id`,
`exception_verifications.verified_by_id`, `processing_logs.expense_report_id`,
`processing_logs.trip_report_id`, `audit_logs.user_id`.

## 3. Current FastAPI API inventory

63 routes across 16 files (`app/api/*.py`), every one read and classified.
Full per-route table is in this session's working notes; summarized by
centralization-readiness bucket:

| Bucket | Count | Meaning |
|---|---|---|
| A — centralize as-is | 45 | pure DB read/write via the ORM, no local-machine assumption |
| B — auth/session/CORS rework needed | 4 | all of `auth.py` (login/logout/me/change-password) |
| C — local filesystem access | 8 | document file download, Excel sheets/download, document delete |
| D — concurrency risk (race or wholesale-replace with no locking) | 6 | exception verify, expense-report validate/revalidate, document rematch-trips, **user role/status change (real TOCTOU risk — see §10)** |
| E — slow synchronous work, request-timeout risk | 5 | both upload endpoints, document reprocess/extract-bills, Excel generate |

No route is purely "remove" on its own, but the Excel-workbook subsystem
(`generate`/`sheets`/`download`) is flagged for redesign — see §17.

## 4. Current filesystem/document-storage architecture

`app/services/storage.py` (29 lines, read in full):

```python
STORAGE_ROOT = env SPINCO_STORAGE_ROOT or <default local path>
EXPENSE_REPORTS_DIR = STORAGE_ROOT / "expense_reports"
TRIP_REPORTS_DIR = STORAGE_ROOT / "trip_reports"
EXCEL_DIR = STORAGE_ROOT / "excel"

def save_upload(directory, file_hash, original_filename, content) -> Path:
    stored_name = f"{file_hash[:12]}_{sanitize_filename(original_filename)}"
    (directory / stored_name).write_bytes(content)
    ...
```

- Filename scheme confirmed: first 12 hex chars of the file's own SHA-256
  content hash, prefixed to a sanitized original filename (`c967267ef0f4_ER-41115.pdf`
  pattern). The same full hash is the DB `file_hash` unique-index dedupe key.
- **No storage abstraction exists.** Every consumer (`uploads.py`,
  `documents.py`, `bill_pipeline.py`, `excel_generator.py`) uses raw
  `pathlib.Path` directly against `EXPENSE_REPORTS_DIR`/`TRIP_REPORTS_DIR`/
  `EXCEL_DIR`, or reads a stored path string (`row.source_path`,
  `ExcelReport.file_path`) straight off disk. There is no interface/protocol
  to swap for an S3 backend today — see §12 for the recommended design.

## 5. Current authentication/RBAC architecture

Read `app/api/auth.py`, `app/services/auth.py`, `app/services/permissions.py`,
`app/models/session.py`, `app/models/user.py` in full.

- **Sessions are already DB-backed** (`auth_sessions` table), not a
  self-contained JWT — deliberately chosen (per in-code docstrings) so
  sessions can be revoked server-side instantly. Cookie (`spinco_session`,
  HttpOnly, SameSite=Lax) holds only an opaque `secrets.token_urlsafe(32)`
  token; only its SHA-256 hash is persisted. 12-hour TTL.
- CSRF: synchronizer-token pattern, a `csrf_token` issued alongside the
  session, required as an `X-CSRF-Token` header on every non-safe request.
- Password hashing: Argon2id via `argon2-cffi`, production-strength params
  from env (time_cost=3, memory_cost=64MiB, parallelism=4 by default).
- RBAC: a central `ROLE_PERMISSIONS: dict[str, set[str]]` permission matrix
  (VIEWER / AUDITOR / ADMIN), enforced via a FastAPI dependency factory
  `require_permission(permission)` on every protected route — not a
  decorator, not scattered role checks.
- `SPINCO_COOKIE_SECURE` defaults to `"false"` (loopback-HTTP dev
  assumption); CORS in `main.py` is hardcoded to
  `["http://localhost:5200", "http://127.0.0.1:5200"]`.
- `SPINCO_ADMIN_INITIAL_PASSWORD` defaults to a hardcoded dev fallback
  (`ChangeMe123!` in `seed.py`, forces `must_change_password=True`) — should
  be removed/hardened before any shared deployment.

## 6. Current audit-log architecture

`AuditLog` model + `app/services/audit.py::log_action()`, the single writer.

- 19 action constants defined, 18 actually wired to a call site across 7 API
  files (`REPORT_VALIDATED` is dead code — defined, never called).
- **Confirmed no client-writable path**: `app/api/audit_logs.py` exposes
  exactly one route, `GET /api/audit-logs`. No API route can create, modify,
  or delete an `AuditLog` row.
- **Audit writes are not transactionally coupled to the action they log** —
  `log_action()` always does its own separate `db.commit()`, after the
  business action's own (already-committed) change. An action can
  structurally succeed with no audit row if the process fails between the
  two commits. No `SAVEPOINT`/single-transaction wrapping exists anywhere.

## 7. Current OCR/extraction processing architecture

Traced the full pipeline from `uploads.py` through completion.

- **Fully synchronous, in-request.** No threading, no `asyncio.create_task`,
  no FastAPI `BackgroundTasks` anywhere in the codebase (confirmed via
  whole-tree grep — the only `async def` in `app/` is a required ASGI
  middleware contract method, not business logic). Every route handler is
  plain `def`, dispatched to FastAPI's default sync threadpool — the only
  concurrency the app has today, and not something it opted into or manages.
- Pipeline per upload: PyMuPDF native text → Tesseract OCR fallback per
  weak page → NVIDIA Nemotron OCR v2 fallback (synchronous `httpx.post`,
  30s timeout) for pages Tesseract still reads poorly → regex field
  extraction → bill parsing/matching → BATTA/trip matching → policy
  validation → exception engine → full Excel workbook regeneration — all
  before the HTTP response returns.
- Rough latency: a native-text PDF is ~1-3s. A large scanned/photographed
  multi-page PDF with several weak OCR pages could plausibly block the
  request for 1-5+ minutes (each weak page can add up to a 30s NVIDIA
  timeout on the critical path), with no client-visible progress during
  that time. **This is the single biggest reason a background-job
  architecture matters for centralization — see §14.**

## 8. Current Excel-generation architecture

`app/services/excel_generator.py` — **one shared workbook, not per-report**
(`Spinco_TA_Audit_Consolidated.xlsx`, backed by a singleton `ExcelReport`
DB row). `generate_workbook()` always does a full rebuild from the DB, never
incremental, and is called synchronously after every single document's
extraction.

- **No locking of any kind** — no `threading.Lock`, no file lock, no
  atomic-write-then-rename. `wb.save(path)` writes directly to the final
  path. Two near-simultaneous uploads finishing extraction can both trigger
  a rebuild; a concurrent reader (the download endpoint) could serve a
  torn/partially-written file, and the singleton `ExcelReport` row update is
  a plain read-modify-write with no locking — last-writer-wins today.

## 9. SQLite-specific assumptions found

Consolidated from §§1-2: no JSON columns (non-issue), no SQLite-dialect
query functions in application code (non-issue), one real dialect-coupling
point (`db_migrations.py`'s raw DDL strings), and the already-known naive-
datetime storage behavior (already worked around, not a new finding).
`check_same_thread=False` in the engine config is SQLite-only and a no-op
to simply drop on Postgres.

## 10. Multi-user/concurrency risks

Directly observed, not theoretical:

1. **SQLite itself.** File-level writer serialization; under real concurrent
   load (multiple desktop clients hitting one shared backend) this is the
   most fundamental blocker, ahead of storage/API concerns — this is *why*
   Postgres is the right target, not an optional nice-to-have.
2. **Wholesale delete-then-reinsert with no locking**, on every (re)process/
   (re)match/(re)validate: `bill_match_candidates`, `trip_match_candidates`,
   `validation_results`, `exceptions`, `source_evidence`. Two workers
   concurrently reprocessing the same report could race.
3. **The shared Excel workbook file and its singleton DB row** (§8) — no
   locking at all today.
4. **A real TOCTOU race already exists** in `PUT /api/users/{id}/role` and
   `PUT /api/users/{id}/status`: the "last active admin" check-then-act isn't
   atomic. Two concurrent requests against different "last" admins could
   leave zero admins under a shared, multi-client server — this is a bug
   worth fixing regardless of centralization timing, since it's a real
   correctness gap, not a hypothetical one.
5. **No `with_for_update()`, no optimistic-concurrency version columns,
   anywhere** in the codebase (confirmed via grep) — every "modify" endpoint
   assumes it's the only writer.
6. Module-level in-process singletons (`extraction/shared.py`'s
   `ocr_engine`/`text_extractor`, `nvidia_ocr.py`'s unbounded in-memory
   cache) are safe as pure caches under multiple worker *processes* (each
   just redoes the work, no correctness break) but silently lose their
   "avoid repeated calls" benefit once there's more than one process/replica
   — worth knowing, not urgent.

## 11. Recommended PostgreSQL architecture

- SQLAlchemy's ORM layer is already dialect-agnostic for this codebase (no
  raw SQLite-syntax queries found in application code) — the swap is
  primarily: new connection URL, explicit pool sizing, and rewriting/
  replacing `db_migrations.py` with Alembic.
- Add the missing indexes on every unindexed FK listed in §2 as part of the
  first Postgres-targeted migration (cheap, unambiguous win, no behavior
  change).
- Add the missing unique constraints identified in §2 (`(expense_report_id,
  s_no)`, `(trip_report_id, leg_no)`, `(expense_report_id, expense_id,
  exception_type)` on exception_verifications, `employees.email`) — these
  are correctness fixes independent of Postgres, worth doing in the same
  pass since they require a real migration tool to do safely against live
  data anyway.
- Add a real `ForeignKey` for `bills.matched_expense_id` (currently a plain
  int) if the data is clean enough to support it — needs a data-quality
  check first (orphaned values would fail the constraint).
- `source_evidence`'s polymorphic `(document_type, document_ref_id)` pointer
  cannot become a normal FK without a redesign (e.g. two nullable FK columns,
  one per document type, with a CHECK that exactly one is set) — flagged for
  a deliberate decision, not silently left as-is or silently redesigned.

## 12. Recommended document-storage architecture

Introduce a small storage interface/protocol (e.g. `save(kind, file_hash,
filename, content) -> str (opaque location)`, `open_for_read(location) ->
bytes/stream`, `delete(location)`) that every current call site
(`uploads.py`, `documents.py`, `bill_pipeline.py`, `excel_generator.py`)
goes through instead of touching `pathlib.Path`/`EXPENSE_REPORTS_DIR`
directly. A local-filesystem implementation is the default (keeps current
behavior byte-for-byte); an S3-compatible implementation becomes a second,
swappable backend later — business logic never needs to know which one is
active. The API must control access (per `documents.py`'s existing
`GET /api/documents/{id}/file` pattern, already gated by
`VIEW_DOCUMENTS` permission) — never expose the storage backend directly to
clients, whether that's a local path or a bucket URL.

## 13. Recommended central API architecture

The existing `/api/...` route structure is already close to right — do not
invent a new URL scheme. Concretely:
- Buckets A (45 routes) need no change to centralize.
- Bucket B (auth) needs `SPINCO_COOKIE_SECURE=true`, `SameSite=None` (for
  genuine cross-origin desktop clients), and CORS origin allow-listing
  driven by config instead of hardcoded `localhost:5200`.
- Bucket C (filesystem-touching routes) route through the new storage
  interface (§12) instead of local `Path` objects.
- Bucket D (concurrency-risk routes) need explicit row/report-level locking
  or a `SELECT ... FOR UPDATE` before their wholesale-replace logic, and the
  last-admin TOCTOU (§10.4) needs a single atomic check-and-update.
- Bucket E (slow synchronous routes) move to the background-job pattern in
  §14.
- `main.py`'s catch-all static-file route (serving the built React SPA,
  `SPINCO_FRONTEND_DIST`) is an Electron-packaging convenience and should be
  disabled for the centralized server — the frontend is served separately
  there, which is also what makes the CORS/cookie work in §5/§13 necessary
  in the first place (today's same-origin design is *why* CORS has never
  needed to be solved).

## 14. Recommended background-processing architecture

The slow-work routes (§3 bucket E) are the real driver here. Given the
current scale (a single company's expense reports, not high-volume SaaS),
**a full job queue (Redis/Celery) is not justified yet** — it adds
operational surface (another service to run, monitor, and secure) for a
workload that doesn't need distributed workers. Recommended instead:
FastAPI's built-in `BackgroundTasks` (in-process, no new infrastructure) for
a first version: the upload endpoint creates the DB row with
`processing_status="QUEUED"` and returns immediately; a background task
performs the actual OCR/extraction/validation pipeline and updates the row
when done; the frontend polls (it already has a `processing_status` field
to poll against, per §2's `expense_reports`/`trip_reports` schema). This
solves the request-timeout risk with no new dependency. If the company
later needs multiple server processes/horizontal scaling, an in-process
`BackgroundTasks` approach doesn't survive a process restart mid-job — that
would be the actual trigger to introduce a real queue, not scale alone.

## 15. Recommended authentication/session architecture

Good news, not a redesign: sessions are already DB-backed (§5), so
horizontal scaling to multiple app server processes mainly needs the
database reachable from all of them — no sticky-session requirement is
inherent to the current design. Required changes are configuration, not
architecture: `SPINCO_COOKIE_SECURE=true`, explicit CORS allow-list,
`SameSite` reconsidered for the real deployment topology, and removing the
hardcoded dev-default admin password (§5) before shared use.

## 16. Recommended centralized audit-log architecture

Close the transactional gap found in §6: wrap the business action and its
`log_action()` call in the same database transaction (single commit) so an
audit row's existence is a reliable guarantee, not a best-effort side
effect. This is a real fix, not new scope — the audit table, its one-writer
design, and its read-only API surface are already exactly right for a
centralized deployment; only the atomicity gap needs closing.

## 17. Recommended deployment architecture

Central FastAPI server (Buckets A-D as one deployment) behind HTTPS/TLS
termination (reverse proxy or platform-provided), talking to a managed or
self-hosted PostgreSQL instance and the document-storage backend from §12.
The single-global-Excel-workbook model (§8) needs an explicit decision
before centralizing: either keep exactly one workbook (now genuinely
concurrent-write-prone across multiple users, needing real locking/queuing
of regenerate requests) or redesign it as per-report/on-demand generation —
this is a product decision (what should "the" Excel export mean once many
users share the data), not purely technical, and is flagged rather than
decided here.

## 18. Required environment variables/secrets (new, beyond the existing 17 `SPINCO_*` vars already in use)

- `SPINCO_DATABASE_URL` (Postgres connection string) — replacing/aliasing
  `SPINCO_DB_PATH`'s role, kept as a distinct name so a developer can never
  point a dev checkout at production by accident (see §19).
- Document-storage backend credentials (e.g. S3 access key/secret or
  connection config), if an object-store backend is chosen for §12.
- `SPINCO_COOKIE_SECURE=true`, `SPINCO_CORS_ALLOWED_ORIGINS` (new — CORS is
  currently hardcoded, not env-driven).
- Whatever secret-storage mechanism holds the above in production (a
  secrets manager, not a `.env` file — none exists today, see the earlier
  NVIDIA-integration work's finding that there is no dotenv loading in this
  codebase at all).

## 19. Required new dependencies

- A Postgres driver (`psycopg` or `psycopg2-binary`) — not yet a dependency.
- `alembic` — not yet a dependency; required before any live schema
  evolution against a shared database is safe.
- An object-storage SDK (e.g. `boto3` for S3-compatible storage) only if
  that backend is chosen for §12 — not required if local/NAS storage on the
  server is chosen instead.

## 20. Required source files to change (when this is actually implemented — none touched for this report)

`app/database.py` (connection config), `app/services/db_migrations.py`
(replaced by Alembic), `app/services/storage.py` (behind the new interface,
§12), `app/main.py` (CORS config, disable the SPA catch-all route for
server mode), `app/api/auth.py`/`app/services/auth.py` (cookie flags),
`app/services/audit.py` (transactional coupling, §16), the six bucket-D
routes (§3) for locking, the five bucket-E routes (§3) for background
processing, `electron/main.js` (stop spawning a local backend, point at the
central server — a later-phase change, not now).

## 21. Files that should remain unchanged

Every pure business-logic module operates on ORM objects/plain dicts
regardless of the backing database or deployment topology — none of these
need to change for centralization: `app/services/validation/checks.py`,
`app/services/extraction/bill_matcher.py`, `app/services/extraction/bill_parser.py`,
`app/services/exception_engine.py`, `app/services/category_map.py`,
`app/services/extraction/nvidia_ocr.py`/`nvidia_fallback.py` (both already
network-call modules, storage-agnostic), the entire OCR/extraction pipeline
logic itself (only its *invocation* — sync-in-request vs. background —
changes, per §14), and every Pydantic schema in `app/schemas/`.

## 22. Data migration strategy (design only — not executed)

```
Existing per-machine SQLite
      |
Validation  (row counts, referential integrity check, business-key sanity)
      |
Backup      (full file copy, hash-verified, outside the source directory —
             same discipline already used in this project's Dipak-cleanup work)
      |
Schema migration   (Alembic baseline applied to the target Postgres DB)
      |
Data migration      (per-machine, per-table, in FK-safe order: employees/users
                     first, then expense_reports/trip_reports, then their
                     dependents, matching the deletion-order discipline
                     already established in this project's cleanup scripts,
                     just inverted for inserts)
      |
Row counts           (source vs. destination, per table, per machine)
      |
Hash/checksum verification  (for document files moved to central storage)
      |
Business validation  (spot-check known records - the project already has a
                     precedent for this: ER-39832's ₹2,000 regression check)
      |
Controlled cutover   (one machine at a time, or a scheduled maintenance
                     window for all at once - a decision for whoever owns
                     the rollout, not assumed here)
```

**Critical, explicit requirement carried over from investigation history in
this project:** per-machine SQLite files have already been shown to
diverge in practice (this repo's own Dipak-cleanup incident). Migration
must treat each machine's data as a distinct, human-reviewed source — never
an automatic silent merge. No production record should disappear without
an explicit accounted-for decision (import / archive / discard).

## 23. Rollback strategy

Because the existing local SQLite install is never modified or deleted by
this plan (per the explicit safety requirement it was written under), the
simplest rollback is structural: keep every desktop install capable of
falling back to its local SQLite backend via configuration (an explicit
"centralized mode" flag, off by default) until the central deployment has
been running successfully for a defined trial period. The backup taken
during migration (§22) is the second line of defense — a full, hash-
verified copy of the pre-migration state, kept until the trial period ends.
Database-side, Postgres's own point-in-time-recovery/backups (standard
practice, not unique to this project) cover the central side once cutover
has happened.

## 24. Security risks

Beyond what's already covered in §§5,13,18: no rate limiting exists
anywhere in the current codebase (confirmed via grep) — worth adding before
a genuinely internet-reachable deployment, even an internal-company one.
File upload validation (25MB limit, PDF magic-byte + parse check, rejects
encrypted/corrupt/empty files) already exists and is solid — carries over
unchanged. No hardcoded production secrets were found anywhere in the
backend (one hardcoded *dev-only* fallback password, flagged in §5, should
be removed). Path-traversal protection for document access already exists
implicitly (documents are addressed by DB-row ID + permission check, not by
client-supplied filesystem paths) — carries over into the storage
abstraction in §12 as a requirement to preserve, not a new gap.

## 25. Performance/scaling considerations

The synchronous-OCR-in-request pattern (§7/§14) is the dominant performance
concern — not raw database throughput, which this application's actual
transaction volume (one company's expense/travel data, not high-frequency
trading) is unlikely to stress even a modest Postgres instance. The shared
Excel workbook (§8/§17) is the second-most-likely bottleneck once multiple
users are active concurrently. Neither requires exotic infrastructure to
solve — background tasks (§14) and either locking or a redesigned Excel
model (§17) address both without over-engineering for a scale this
application doesn't have.

## 26. Estimated implementation phases

1. **Prerequisite, storage/DB-agnostic:** introduce Alembic against the
   *existing* SQLite schema; add the missing indexes/constraints identified
   in §2/§11; fix the last-admin TOCTOU race (§10.4); close the audit-log
   transactional gap (§16). All of this is safe to do against the current
   local-SQLite architecture, independent of centralization timing, and
   directly improves correctness today.
2. **Storage abstraction (§12):** introduce the interface, local-filesystem
   implementation only at first — a pure refactor, behavior-identical.
3. **Background processing (§14):** move the five bucket-E routes to
   `BackgroundTasks` + polling — improves the current single-desktop app's
   own responsiveness too, not just a centralization prerequisite.
4. **Provision real infrastructure:** Postgres instance, document storage
   backend, HTTPS/TLS, the actual `<spinco-server>` host — this phase
   cannot proceed without real infrastructure decisions from whoever owns
   that (see §28).
5. **Central server deployment + auth/CORS rework (§13/§15).**
6. **Migration execution (§22)** — one machine at a time, explicit human
   review per machine, only after phases 1-5 are deployed and verified.
7. **Cutover + rollback-window monitoring (§23).**

## 27. Which parts can be implemented immediately

Phase 1 in full (Alembic baseline, missing indexes/constraints, the
last-admin race fix, audit transactional coupling) and Phase 2 (storage
abstraction, local-backend-only) — both are safe, local-only, behavior-
preserving changes that don't require any centralized infrastructure to
exist yet, and both directly improve the current single-desktop-install
architecture regardless of if/when centralization happens. **Not started
in this report per the explicit instruction not to change models yet.**

## 28. Which parts require actual server infrastructure

Phases 4-7: a provisioned Postgres instance, a document-storage backend, a
real `<spinco-server>` hostname with HTTPS, and a decision on where/how the
central FastAPI server is hosted and operated. None of this exists yet and
none of it should be invented or assumed — this report deliberately never
names a real hostname (matching the same placeholder discipline already
used for the update-feed URL in `docs/RELEASE_AND_UPDATES.md`).

## 29. Which parts require explicit production approval

Any actual data migration (§22), any change to the production SQLite
database on any employee machine, any decision to disable the per-machine
local-data fallback (§23), and the actual cutover moment. This report ends
here, as instructed — no migration, no schema change, no production-data
action has been taken.

## 30. Wholesale delete/reinsert and shared-file write locations (exact, Phase 1 hardening)

Per the approved Phase 1 scope, the processing pipeline itself was NOT
redesigned - these locations are documented here exactly, as requested, for
a later concurrency-redesign phase:

| Concern | File : line | What it does |
|---|---|---|
| Validation delete/reinsert | `app/services/validation/pipeline.py:82` | `db.query(ValidationResult).filter(expense_report_id==...).delete()` before rebuilding all check results for the report |
| Exception evaluation delete/reinsert | `app/services/exception_engine.py:161` | `db.query(ExceptionRecord).filter(expense_report_id==...).delete()` before rebuilding exceptions from the (just-rebuilt) ValidationResult rows read at line 165 |
| Bill matching delete/reinsert | `app/services/extraction/bill_pipeline.py:35` (bills themselves) and `:102` (`BillMatchCandidate`) | both replaced wholesale on every (re)match run |
| Trip/Batta matching delete/reinsert | `app/services/batta_pipeline.py:103` (`TripMatchCandidate`); `app/services/extraction/pipeline.py:165,217,313` (`Expense`/`Trip` leg rows on (re)extraction) | replaced wholesale on every (re)match/(re)extraction |
| Excel shared-file write | `app/services/excel_generator.py:558` (`wb.save(path)`) writing the single consolidated workbook, reading all `ExceptionRecord`/`ValidationResult` rows at lines 128-129 | no locking of any kind - see §8 |

None of these were changed in Phase 1 - fixing them is real pipeline
concurrency work (row/report-level locking or a queued-regeneration model
for the workbook), deliberately deferred to the phase that actually
introduces multi-user concurrent access, per this task's explicit scope.

## 31. Phase 1 + Phase 2 implementation record (this task)

Implemented (see the final task report for the full file list, test
results, and git status): Alembic migration infrastructure with a
stamp-safe baseline for existing databases; the three named constraints
(`(expense_report_id, s_no)`, `(trip_report_id, leg_no)`,
`bills.matched_expense_id` FK) plus every missing FK index identified in
§2 plus the `exception_verifications` identity uniqueness (flagged as a
gap in §2, added after confirming zero existing violations); the admin
TOCTOU fix via a single atomic guarded UPDATE (not row locking, which
SQLite doesn't support - see `app/api/users.py`); audit-log/business-action
atomicity at every one of the 13 call sites; and the Phase 2 storage
abstraction (`app/services/storage_backend.py`), wired into the upload,
document-download, and document-delete endpoints, with the OCR/Excel
pipeline's direct local-path usage deliberately left unchanged (§12/§26).
