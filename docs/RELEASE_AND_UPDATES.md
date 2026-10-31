# Release & Auto-Update Architecture

## Versioning

`package.json`'s `version` field is the single authoritative version number
for the whole application - it drives `app.getVersion()` (shown in
Settings), electron-builder's installer filename/metadata, and the
auto-update feed's `latest.yml`. Bump it with every release, following
semver (`1.1.0` -> `1.2.0` for a normal release, `1.1.0` -> `2.0.0` only for
a breaking change to the update mechanism or data format itself).

There is no separate "Electron package version" or "release version" to
keep in sync - they are all reads of this one field.

## Update flow (what a user sees)

1. App starts -> automatically checks the update feed in the background.
2. No update: nothing happens, no interruption.
3. Update found: Settings page shows "Update available", with an
   **Update Now** button. Nothing downloads until the user clicks it.
4. **Update Now** -> downloads the new installer, shows progress
   (percent + MB transferred), verifies its hash.
5. Once downloaded: **Restart & Install** button appears. Clicking it quits
   the app, silently runs the new installer, and relaunches.
6. **Check for Updates** is also available manually at any time from the
   same Settings card.

No update is ever forced - every download and install step requires an
explicit click.

## Production hosting: target architecture

The update feed's production home is the same future central Spinco server
described in `docs/CENTRALIZED_ARCHITECTURE.md` - but as a separate,
isolated static release area, never sharing a path, process, or credential
with the application API or its data:

```
https://<spinco-server>/updates/     <- THIS feed. Static files only.
    spinco-ta-audit/
        latest.yml
        Spinco TA Audit Setup <version>.exe
        Spinco TA Audit Setup <version>.exe.blockmap

https://<spinco-server>/api/         <- Central FastAPI application API,
                                         PostgreSQL, authentication,
                                         application data. Entirely separate
                                         concern - see
                                         docs/CENTRALIZED_ARCHITECTURE.md.
```

**`<spinco-server>` is not a real, provisioned hostname - do not treat it as
one.** `package.json`'s `build.publish.url` currently holds an explicit,
non-resolving placeholder (`https://spinco-central.example/updates/spinco-ta-audit/`
- `.example` is the domain IANA reserves specifically so it can never be
real, per RFC 2606) until the Spinco server is actually provisioned. At that
point exactly one value changes: this URL (see "Runtime configuration"
below for how to do that without a rebuild).

**Isolation guarantee, by construction, not just policy:** the `/updates/`
path only ever needs to be served as static files by whatever serves the
central server's other static assets (or a dedicated bucket/CDN in front of
it) - it never needs read or write access to PostgreSQL, the document
store, session/auth state, or any credential, because `stage-release.js`
(see below) only ever produces an installer, a `.blockmap`, and a
`latest.yml`. There is no code path by which a production database file,
employee/expense record, audit log, uploaded document, or credential could
end up published to `/updates/` - that content simply never exists in the
directory this mechanism stages or reads. The feed is also read-only for
every client application: nothing in `electron-updater`'s generic-provider
flow ever writes back to the feed host.

## Architecture

```
electron/preload.js   - context-isolated bridge exposing window.spincoApp
                         (getVersion, checkForUpdates, downloadUpdate,
                         installUpdate, onUpdateStatus) to the renderer
electron/updater.js    - electron-updater wiring: feed config, event
                         listeners, IPC handlers, electron-log logging to
                         <userData>/logs/updater.log
electron/main.js       - calls setupAutoUpdater()/checkForUpdatesOnStartup()
                         once the app window is ready (packaged mode only -
                         dev mode registers the same IPC handlers but never
                         touches electron-updater, since there is no
                         app-update.yml in a dev checkout)
frontend/src/lib/updater.js       - thin client wrapper around
                                     window.spincoApp, degrades to no-ops
                                     when not running inside Electron
frontend/src/pages/SettingsPage.jsx - "Application" card: Current Version,
                                        Latest Version, Status, and the
                                        Check/Update/Restart buttons
```

Update provider: `electron-updater`'s **generic** provider - a plain
static-file feed (an NSIS installer + its `.blockmap` + an auto-generated
`latest.yml`), no GitHub dependency, no credential embedded in the shipped
app. See `docs/CENTRALIZED_ARCHITECTURE.md` for why this was chosen over
GitHub Releases.

`autoDownload` and `autoInstallOnAppQuit` are both explicitly `false` -
checking is automatic, downloading and installing are always a deliberate
user action.

## Producing a release

```powershell
# 1. Bump the version
#    Edit package.json's "version" field (e.g. 1.1.0 -> 1.2.0)

# 2. Build, test, and verify - one command
cd D:\Bills
npm run dist
```

`npm run dist` runs the entire pipeline below, in order, and fails fast (no
installer is produced) if any step fails:

1. **`test:backend`** - the full backend test suite (`backend/tests`).
2. **`clean`** - removes `frontend/dist`, `backend/dist_backend`,
   `backend/build`, `release/`, and `electron/generated` so nothing from a
   previous build can leak into this one (see "Preventing stale releases"
   below - this exists because of a real incident).
3. **`write:build-info`** - stamps `electron/generated/build-info.json` with
   the `package.json` version, the current git commit
   (`git rev-parse --short HEAD`), whether the working tree is dirty, and
   the build timestamp. Packaged into the app as `build-info.json` and read
   via the `app:get-build-info` IPC call - shown in Settings next to the
   version (see "Release traceability" below).
4. **`build:frontend`** + **`build:backend`** + **`collect:tesseract`** -
   the React production build, the PyInstaller backend executable, and the
   vendored Tesseract runtime.
5. **`electron-builder --win`** - packages all of the above as
   `extraResources` and produces the NSIS installer + portable exe +
   `latest.yml` update metadata in `release/`.
6. **`verify:release`** - fails the build if the installer's version doesn't
   match `package.json`, or if any release artifact (frontend build,
   backend exe, installer) has a modification time older than this build's
   `write:build-info` timestamp. This is what actually proves the release is
   current, not stale - see below.

A release is never "the frontend changed but the backend didn't get
rebuilt" - step 4 always rebuilds and repackages both together into one
versioned bundle, so there is no way to produce a mismatched pair.

```powershell
# 3. Stage exactly the update-feed files (installer + .blockmap + latest.yml
#    - nothing else) into a local staging directory
npm run release:stage
#    -> D:\Bills\update-feed\spinco-ta-audit\ (override with
#       SPINCO_UPDATE_FEED_DIR)

# 4. Publish that staging directory's contents to the real update feed host,
#    under the central server's /updates/ path - kept structurally separate
#    from /api/ (the application server, database, and documents). Not
#    automated here since the Spinco server isn't provisioned yet; once it
#    is, this becomes a one-line sync/copy into its /updates/spinco-ta-audit/
#    location, e.g.:
#      rsync -av update-feed/spinco-ta-audit/ spinco-server:/var/www/updates/spinco-ta-audit/
#    or, if that path is instead backed by object storage:
#      aws s3 sync update-feed/spinco-ta-audit s3://<bucket>/updates/spinco-ta-audit/
#    Publish only the 3 files release:stage produced - nothing else from
#    release/ or anywhere in the repo.
```

`npm run dist:portable` builds only the portable target and skips
`test:backend`/`verify:release` - a quick local sanity build, never a release.

### Clean guard: `release/` is not deleted by accident

`npm run clean` (and therefore `npm run dist` / `prepackage`, which are
unchanged) **refuses** to run while `release/` is not empty: it prints
`[clean] REFUSED ...`, deletes nothing (not even the other build folders) and
exits with an error. To clean it deliberately, run
`npm run clean -- --force`, then `npm run dist`. The official release output
stays `D:\Bills\release`.

### Staging builds outside the repository: `npm run dist:staging`

A trial build that must not touch `release/` or put large files in the
repository. It runs the same steps as `dist` (backend tests, build info,
frontend, backend, electron-builder, verify, update-feed copy) but writes
everything into one **new** run folder
`D:\SpincoTA-artifacts\release-staging\<YYYYMMDD-HHMMSS>-v<version>\`
(`build\`, `frontend-dist\`, `build-info\`, `release\`, `update-feed\`,
`tmp\`, `logs\`, the generated electron-builder config and `RUN.json`). An
existing run folder is never reused (a `-2`, `-3` suffix is added), `clean` and
`collect:tesseract` are not run (the existing `resources\tesseract` is used as
it is), and the version is not changed.

```powershell
npm run dist:staging -- --dry-run     # print the plan; creates and builds nothing
npm run dist:staging                  # the staging build
npm run dist:staging -- --skip-tests  # without the backend test suite
```

Optional environment variables (all unset = the paths used by `npm run dist`;
the staging script sets them itself for each of its steps):

| Variable | Read by | Effect |
|---|---|---|
| `SPINCO_BUILD_DIR` | `build-backend.js`, `verify-release.js` | PyInstaller output goes to `<dir>\dist_backend`, its work files to `<dir>\build` |
| `SPINCO_BUILD_INFO_DIR` | `write-build-info.js`, `verify-release.js` | `build-info.json` is written to / read from this folder instead of `electron\generated` |
| `SPINCO_RELEASE_DIR` | `verify-release.js`, `stage-release.js` | electron-builder output folder to verify / take the installer and `latest.yml` from, instead of `release\` |
| `SPINCO_FRONTEND_DIST` | `verify-release.js` | built frontend folder to check, instead of `frontend\dist` |
| `SPINCO_TMP` | `verify-release.js` | folder for its throw-away database and storage, instead of the OS temp folder |
| `SPINCO_UPDATE_FEED_DIR` | `stage-release.js` | update-feed output folder (already existed) |
| `SPINCO_STAGING_ROOT` | `dist-staging.js` | replaces `D:\SpincoTA-artifacts\release-staging`; must be outside the repository |

For a manual frontend build elsewhere: `npm run build --prefix frontend -- --outDir <folder>`.
For test runs with scratch files off C:, set `TEMP` and `TMP` to a D: folder first.
Electron's `userData` stays in `%APPDATA%` and is not affected by any of this.

## Preventing stale releases

We previously had a build where `release/win-unpacked/` had been rebuilt but
`Spinco TA Audit Setup 1.0.0.exe` next to it was left over from an older
build - the installer end users actually run was stale even though the
unpacked directory looked current. `npm run dist`'s `clean` step (removes
every build-output directory before rebuilding) and `verify:release` step
(fails the build unless every artifact's timestamp is newer than this
build's start) together make that class of mistake structurally impossible:
there is no artifact left over to accidentally ship, and if one somehow were,
the build itself would refuse to report success.

## Pre-release check: stale catering / room-service-food evidence

Extraction stores catering and lodging room-service/food evidence on each
`bills` row when the bill is extracted, from the bill's FULL text:
`catering_amount`, `catering_mentioned`, `room_service_food_amount`,
`room_service_food_mentioned` (`models/bill.py`, `category_map.resolve_*`).
The detectors that write those values changed several times during
development (first-row-only room-service reading, tax rows read as the food
charge, an unrelated amount on the same or the next line read as catering,
`₹ 357.00` on its own line not read, and others). A stored value is trusted
over a fresh read of the capped `raw_text` for room-service/food whenever the
row carries the mention flag (a stored `NULL` amount is final) and for catering
when `raw_text` is at its 2,000-character cap or the current detector finds no
amount. **A report extracted by an older detector can therefore keep a stale
stored amount**, which can cause a wrong exclusion from the amount comparison,
a false AMOUNT MISMATCH, or a wrong amount in a review item.

### Status: exposure is UNKNOWN

- **No affected real database has been confirmed, and no real database has
  been checked for this issue. Exposure remains unknown: do not treat any
  database as safe, or as affected, on this page's word.** Nothing the
  repository's own files show points to an affected database, but that is not
  proof: other machines, copies, and installed builds' per-user data folders
  have not been checked.
- This is not a reason to run anything automatically (see "No automatic
  recovery"). It is a reason for each database owner to check their own data
  with the procedure below before a release, with the owner's authorization.

### 1. Is a database a candidate at all?

Everything in this step is read-only and must be done on a **verified backup
copy** (section 4), never on the live database.

1. Look at the `bills` table's columns (`PRAGMA table_info(bills);`).
2. **If neither `catering_mentioned` nor `room_service_food_mentioned` exists**,
   the database has not been migrated by code that stores this evidence. There
   is nothing of this kind to recover. Stop here.
3. **If both exist**, list the candidate reports:

   ```sql
   SELECT DISTINCT expense_report_id
   FROM bills
   WHERE catering_mentioned IS NOT NULL OR room_service_food_mentioned IS NOT NULL;
   ```

   Rows where both flags are `NULL` are legacy rows (extracted before the
   columns existed): the current detector runs on their `raw_text`, so they are
   not affected. A stored `0` / `False` is not `NULL` and still counts.
4. If only one of the two flag columns exists, the migration was incomplete:
   query only the column that exists, and escalate to the database owner.

This identifies **candidate records, not proven wrong amounts**: a candidate's
stored values may well be correct. It only says the evidence was written by code
that has these columns. It cannot tell an old detector's output from the current
detector's. (`document_id` values in the API look like `expense:<id>`.)

### 2. Recovery route and why it is not yet authorized

The intended recovery route is, one report at a time,
`POST /api/documents/{document_id}/rematch-bills`. It needs an authenticated
Auditor or Admin session and a CSRF token; **there is no button for it in the
UI**. It must not be run against any real database until the database owner
authorizes it and the failure modes below are understood.

- **The UI's "Reprocess" button is NOT the same operation.** It calls the full
  `POST /api/documents/{document_id}/reprocess`, which re-extracts the whole
  report and deletes and recreates its expense rows (new expense ids). **Do not
  use full reprocess as an alternative for this purpose.**
- **Full reprocess and manual verifications.** Manual verifications are keyed by
  `expense_id`, and a full reprocess gives the report's claims new expense ids.
  Before this was fixed, a disposable-database test (a second report loaded so
  SQLite could not reuse the freed ids) showed the verification staying on the OLD
  id and the regenerated exception coming back unresolved. A full reprocess now
  carries a verification forward (`extraction/verification_carryover.py`) only
  when the same claim is confidently matched: the same `s_no`, and an equal date,
  normalized category and claimed amount, on both the old and the new row, in the
  same report. The verification row itself is kept (same id, verifier, time, note,
  original severity and reason); only its expense id is updated, and a
  `VERIFICATION_CARRIED_OVER` audit event records old and new ids. In every other
  case (a changed or missing fact, no row with that `s_no`, an ambiguous match)
  the verification is left exactly as it was, the regenerated claim comes back
  open for an auditor, and a `VERIFICATION_NOT_CARRIED_OVER` audit event gives the
  reason and the old claim's facts. **Limits:** verifications already stranded by
  an earlier reprocess, before this fix, are not repaired (the old claim's facts
  no longer exist to match against), and real-database exposure remains unknown.
  This is a code fix, not a reason to use full reprocess for this purpose: it
  re-extracts the whole report, and `rematch-bills` stays the intended route.
  A carried-over verification is then re-applied only while the evidence behind
  the exception is unchanged (see "Manual verifications after an upgrade").
- **`rematch-bills` keeps the expense rows** (so verifications keep their expense
  ids) and, in the same disposable test, replaced the stale evidence and the
  wrong exceptions without touching a second report. Known failure modes:
  1. **The old bills are deleted and committed before re-extraction begins.** A
     crash, kill or error afterwards leaves the report with no bills and its
     claims pointing at bills that no longer exist. (A separate disposable-database
     test, `test_backup_simulated_failure_after_the_deletion_commit_and_restore`,
     simulates this failure by calling the extraction pipeline function directly,
     not through the API route.) The only recovery is the verified backup. Never
     interrupt a running request.
  2. **Validation, exception evaluation and workbook errors are swallowed** by
     the route. A successful HTTP response is NOT proof the recovery worked.
  3. It re-reads the stored PDF (re-running OCR on scanned pages), so it can be
     slow; a missing PDF returns 404 before anything is deleted.

### 3. Who must authorize, and when to stop

- The owner of the database (not the person running the release) must authorize
  the backup, the candidate query and each reprocessing request.
- Work on one report at a time. After each one, run the checks in section 5
  before starting the next.
- **If anything fails or looks wrong, stop.** Do not repeat the request, do not
  try the other reprocessing route, and do not edit the database by hand.
  Restore from the backup (section 4) and escalate.

### 4. Backup, verification and restore

Locations (from the source; do not guess others):

| | Dev (running from the repository) | Packaged app |
|---|---|---|
| Database | `backend\spinco_ta.db`, unless `SPINCO_DB_PATH` is set | `<userData>\database\spinco_ta.db` |
| Documents | `storage\` at the repository root, unless `SPINCO_STORAGE_ROOT` is set: `expense_reports\`, `trip_reports\`, `excel\` | `<userData>\documents\`: `expense_reports\`, `trip_reports\`, `excel\` |
| Workbook | `<documents>\excel\Spinco_TA_Audit_Consolidated.xlsx` | same, under `<userData>\documents\excel\` |
| Backend log | **unverified:** the source does not say where a dev backend's output goes; use wherever that process writes it | `<userData>\logs\backend.log` |

`<userData>` is Electron's per-user data folder on that machine, or the folder
passed with `--user-data-dir` (as `uat-launch.bat` does). This page does not
name it; confirm it on the machine itself.

Before any recovery:

1. **Get the database owner's authorization.**
2. **Stop the application and its backend completely.** SQLite uses its default
   rollback journal here (no WAL is configured in `database.py`), so a file copy
   is only consistent when nothing is writing. If a `spinco_ta.db-journal` file
   sits next to the database after everything is stopped, an earlier write was
   interrupted: do not proceed, escalate.
3. **Copy, to a folder outside the repository and outside `<userData>`:** the
   database file, the whole documents folder (`expense_reports`, `trip_reports`
   and the `excel` workbook, which the route regenerates and overwrites).
4. **Record the size and SHA-256 of every copied file next to the source's, and
   verify they are identical** (this repository's convention: a full file copy,
   hash-verified, outside the source directory; see
   `CENTRALIZED_ARCHITECTURE.md` section 22 and `ORPHAN_RECORD_INVESTIGATION.md`):

   ```powershell
   Get-FileHash -Algorithm SHA256 "<live db>", "<backup db>"
   (Get-Item "<live db>").Length, (Get-Item "<backup db>").Length
   ```
5. **Run the integrity check on the backup copy** and require `ok`:

   ```powershell
   python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" "<backup db>"
   ```
6. Write down, from the backup copy, per candidate report: the number of bills
   and expenses, each bill's `amount` and four evidence columns, each expense id,
   and the rows of `exception_verifications`. This is the "before" record for
   section 5. Do not start until the backup is verified.

Restore (if a recovery step fails or the checks in section 5 do not pass):

1. Stop the application and backend again.
2. **Keep the damaged files**: move them aside to a separate folder for
   investigation. Do not delete them.
3. Copy the backed-up database back to the live location, and the backed-up
   documents folder (including `excel`) back to the live documents location.
4. Verify the restored database's SHA-256 and size equal the recorded values and
   that `PRAGMA integrity_check` returns `ok`.
5. Start the application and repeat the "before" record from section 4 step 6:
   every count, amount, evidence column, expense id and verification must equal
   the recorded values.
6. **Escalate** with the kept damaged files. Do not retry recovery until the
   cause is understood.

### 5. After each recovery step, verify

An audit-log entry proves only that the request was received. **It does not
prove the recovery succeeded.** Check all of the following against the "before"
record, on the live data only after authorization:

- **Records:** the report has the expected bills (their ids are expected to be
  new) and the SAME expense ids as before.
- **Evidence:** each bill's `amount`, `catering_amount`, `catering_mentioned`,
  `room_service_food_amount` and `room_service_food_mentioned` are what the
  folio or ticket actually shows. A mention flag that was set should still be
  set when an amount cannot be read (amount `NULL`, flag `1`).
- **Exceptions:** the report's exceptions and review items are the expected ones
  (for example no AMOUNT MISMATCH that was only caused by a stale amount, and any
  catering or room-service/food review item shows the correct amount).
- **Manual verifications:** every row of `exception_verifications` for the report
  still points at an expense that exists, and the verified items are still
  resolved:

  ```sql
  SELECT v.id, v.expense_id FROM exception_verifications v
  LEFT JOIN expenses e ON e.id = v.expense_id
  WHERE v.expense_report_id = <id> AND v.expense_id IS NOT NULL AND e.id IS NULL;
  ```

  must return no rows.
- **Orphans:** must return `0`:

  ```sql
  SELECT COUNT(*) FROM bill_match_candidates
  WHERE bill_id NOT IN (SELECT id FROM bills) OR expense_id NOT IN (SELECT id FROM expenses);
  ```
- **Workbook:** the consolidated workbook was regenerated (its modified time
  moved) and its rows for the report agree with the application. The route
  ignores workbook errors, so look at the file.
- **Errors and audit:** read the backend log for errors during the request, and
  confirm a `BILL_REMATCHED` entry exists for the document in `audit_logs`.
  Neither of these replaces the checks above.

### 6. No automatic recovery

Nothing in the application reprocesses, migrates or repairs these rows by
itself, and none is planned here. No schema, resolver or endpoint change is made
for this check. It does not apply to a clean database or to disposable test
databases.

The rehearsal tests in `backend/tests/test_stale_evidence_recovery.py` exercise
this procedure on synthetic documents and throw-away databases only (stale
evidence repaired by `rematch-bills`, the full-reprocess behaviour above, a
backup / simulated mid-run failure / restore cycle, and the candidate query on
migrated, legacy and partially migrated schemas). They are evidence about the
code, not about any real database.

## Manual verifications after an upgrade

A manual verification ("Verify" on an exception or review item) is stored in
`exception_verifications`. Since the evidence-signature change, an approval is
re-applied (the exception shows RESOLVED, the expense VERIFIED) only while the
evidence it was given on is unchanged.

### What the signature compares

`exception_verifications.evidence_signature` is a SHA-256 hex digest of the typed
fields of the validation rows behind the exception: `check_type`, `result`,
`claimed_amount`, `bill_amount`, `policy_limit`, `expense_date`, `bill_date` and
`source_page`. Amounts are normalized to two decimals and dates to ISO, the rows
are sorted, and neither the wording (`remarks`) nor row ids are included. For a
review family such as BILL MATCH REVIEW REQUIRED the signature covers every row of
that family for the expense (for example Bill Amount, Bill Date and Category
Consistency together), and nothing from other families or from passing rows.

### What happens when the evidence changed

The verification row is kept exactly as it was (it is history, not deleted). The
exception comes back OPEN, so the expense is not VERIFIED and the report cannot
become ACCOUNT VERIFIED on its account, and this text is appended to its reason:

`[A previous manual verification no longer applies because the evidence changed; re-verify.]`

The suffix is rebuilt on every evaluation (it never repeats) and the reason is
capped at 1000 characters. For a BILL MATCH or TRIP MATCH REVIEW REQUIRED item the
expense's `bill_match_status` / `trip_match_status` is also taken back from MATCHED
to REVIEW_REQUIRED and the "Manually verified by auditor" prefix is removed from the
matching `..._match_reasons`, but only when that status was set by the manual
verification itself. A match the matcher confirmed is never changed. **Limit:** the
status before the approval is not stored, so a former MULTIPLE_CANDIDATES comes back
as REVIEW_REQUIRED (still unconfirmed; analytics and the workbook count it under
REVIEW_REQUIRED until matching is next re-run). Re-verifying updates the same row
(same id) with the current evidence and signature, and replaces the stored note only
if a new one is given; the replaced state is first kept in
`exception_verification_history` (below).

**Limit (MULTIPLE_CANDIDATES bill matches):** for such a claim the approval's flip to
MATCHED changes the Required Document Check from REVIEW_REQUIRED to PASS, so the next
real revalidation produces a BILL MATCH family with one row fewer. Its signature then
differs and the approval is (safely) not applied: the item shows OPEN with the note
above and the status is restored as REVIEW_REQUIRED. It does not flap, and a second
approval, given on rows that no longer depend on the flip, stays applied. Auditors
approving a MULTIPLE_CANDIDATES bill match should expect to confirm it once more
after the next revalidation. A REVIEW_REQUIRED bill match and a trip match (either
status) are stable across revalidation.

**A manual BILL MATCH approval never switches off a Shared Bill review.** The approval
flips `bill_match_status` to MATCHED but never sets the confirmed bill (`bill_id`);
validation treats "MATCHED without a confirmed bill" as unconfirmed when deciding
whether the separate Shared Bill review (`DUPLICATE BILL REVIEW REQUIRED`) applies, so
that review stays OPEN until it is verified on its own. A match the matcher confirmed
is unchanged: it is judged by the Duplicate Bill Check, as before.

### Upgrading an existing database

Startup adds the nullable `evidence_signature` column additively and changes no
row. Every approval made before this change is therefore **unsigned**. An unsigned
approval is applied only if its stored `original_reason` equals the exception's
current reason; when it is applied it is stamped with the current signature. If
the reason differs it is not applied and stays unsigned (and OPEN with the note
above) until an auditor verifies it again. Unsigned rows compare wording only.

Checks (read-only, run on a verified backup or with the owner's authorization):

```sql
SELECT COUNT(*) FROM exception_verifications WHERE evidence_signature IS NULL;

SELECT id, expense_report_id, expense_id, exception_type FROM exceptions
WHERE reason LIKE '%A previous manual verification no longer applies because the evidence changed; re-verify.%';
```

Before a release, compare the remark wording of the existing approvals in
`exception_verifications.original_reason` with the current reasons of the same
items. Wording that changed between versions makes an unsigned approval come back
open once. That is intended and safe, but the auditors should expect it.

### Limits of what the signature can see

- Shared Bill, Duplicate Bill and Payment Status reviews carry only `source_page`
  as typed evidence, so a change in the underlying bill is not detected by the
  signature for those types.
- Total Mismatch types only the extracted sum.
- Evidence that exists only in the remark text (not in a typed field) is not
  compared for signed approvals.
- Unsigned approvals compare the reason text only (above).

### Audit trail and notes

New `EXCEPTION_VERIFIED` audit events record the exception type, expense and
report ids, and `; note recorded` when a note was supplied. **They do not contain
the note text.** The note is kept on `exception_verifications.verification_note`
and is returned by the exceptions API (`verification_note`). Audit rows written
before this change are not migrated, rewritten or deleted, so **historical audit
rows still contain the notes they were written with.** Treat the audit log
accordingly.

### Verification history

When an auditor explicitly re-verifies an item that already has a verification, the
route first writes the superseded state to `exception_verification_history`, in the
same transaction as the update (a failed re-verification leaves no history row and an
unchanged verification). The live `exception_verifications` row keeps its id. A history
row holds: `verification_id` (the live row), the report / expense / exception type,
the superseded `original_severity`, `original_reason`, `evidence_signature`,
`verified_by_id`, `verified_at` and `verification_note`, plus `superseded_at` and
`superseded_by_id`. A re-verification with a blank note keeps the live note (as before)
and still writes a history row, because the other metadata is replaced. The first
verification writes none.

- **Access:** the table is append-only and written only by the verify route (which
  needs the manual-correction permission) and by report deletion (below). **No API
  endpoint reads it**; it is read with a database query by the database owner. It is not
  part of any API response, and existing rows are never rewritten by evaluation,
  reprocess or startup.
- **Migration:** nothing to run. Startup's `create_all()` creates the table on a
  database that lacks it, and leaves an existing table and its rows alone.
- **Deleting a report.** A live verification is keyed by (report id, expense id,
  exception type) and SQLite reuses freed ids, so a live verification left behind by
  a deleted report could be applied to a later report that receives the same ids. When
  a report is deleted its live verifications (expense-level and report-level) are
  therefore removed, and each one's last state is first written to the history table,
  in the same transaction as the deletion (a failed deletion changes nothing, and a
  retry retires each verification once). The `DOCUMENT_DELETED` audit event lists
  `retired_verification_ids=[...]` and never a note. Nothing relies on id uniqueness.
  Existing history rows are never changed. **Limits:** verifications orphaned by a
  report deleted BEFORE this change are not repaired; the history columns
  `verification_id` and `expense_report_id` are plain copies (not foreign keys, because
  the live row may be gone) and, with ids reused, identify a live row only at the time
  of `superseded_at` - compare timestamps, not ids, across a deletion. A deleted
  report's derived rows (`exceptions`, `validation_results`, `processing_logs`,
  `source_evidence`) are still not removed (as before). Validation and evaluation
  replace a report's `validation_results` and `exceptions` wholesale, so a report that
  reuses the id stops showing them at its first validation and evaluation; until then
  the exceptions API can show the old report's rows, including any RESOLVED one with
  its note. They are display copies: no approval is applied from them.
- **Check:**

  ```sql
  SELECT verification_id, verified_at, superseded_at, verification_note
  FROM exception_verification_history ORDER BY verification_id, id;

  -- live verifications left behind by a report deleted BEFORE this change (should be 0):
  SELECT v.id, v.expense_report_id, v.expense_id, v.exception_type
  FROM exception_verifications v
  LEFT JOIN expense_reports r ON r.id = v.expense_report_id
  WHERE r.id IS NULL;

  -- the case the query above cannot see: the deleted report's id was REUSED by a newer
  -- report, so the row now joins to it. An approval cannot predate the report it was
  -- made on, so an approval older than its report's upload was inherited (should be 0):
  SELECT v.id, v.expense_report_id, v.expense_id, v.exception_type, v.verified_at, r.uploaded_at
  FROM exception_verifications v
  JOIN expense_reports r ON r.id = v.expense_report_id
  WHERE v.verified_at < r.uploaded_at;
  ```

  Both are read-only `SELECT`s. The first finds only verifications whose report row is
  gone; it does not find one that has since been attached to a reused id (the second
  query), nor one whose expense is gone while its report exists (the "Manual
  verifications" query in section 5 above). Neither repairs anything. Remediation of
  any rows they return is a separate step that needs the database owner's
  authorization and a verified backup.

- **Limit:** verifications replaced before this table existed have no history; their
  earlier notes remain only in the historical audit rows.

## Release traceability

Every build's `electron/generated/build-info.json` (packaged as
`build-info.json`, read via `app:get-build-info`) records:

```json
{ "version": "1.2.0", "gitCommit": "7f3a21c", "dirty": false, "buildDate": "2026-09-28T10:00:00.000Z" }
```

Shown in Settings as a "Build" row alongside Current Version/Latest
Version/Status. `dirty: true` means the build was made from a working tree
with uncommitted changes - real for local testing, but never presented as
tracing to a clean, reproducible commit.

**Exactly what a real deployment publishes, every release:** the 3 files
`npm run release:stage` puts in `update-feed/spinco-ta-audit/` -
`latest.yml`, `Spinco TA Audit Setup <version>.exe`, and its `.blockmap` -
copied as-is into `/updates/spinco-ta-audit/` on the target host, replacing
the previous release's files there (the feed only ever serves the single
current version; `stage-release.js` already clears its own local staging
directory each run for the same reason). No server-side build step, no
database, no application code runs to serve this path - it is pure static
file hosting.

A release is never "the frontend changed but the backend didn't get
rebuilt" - step 3 always rebuilds and repackages both together into one
versioned bundle, so there is no way to publish a mismatched pair.

## Runtime configuration

`SPINCO_UPDATE_FEED_URL` (optional env var, set on the installed machine)
overrides the feed URL baked into the build at `package.json`'s
`build.publish.url` - this is the one, single place the feed URL is
configured; nothing else in the codebase hardcodes it. This lets the real
feed host be finalized, or changed later (e.g. if the central server's
`/updates/` path ever moves), without rebuilding/republishing the
application itself. Until the Spinco server is provisioned, the baked-in
default (`https://spinco-central.example/updates/spinco-ta-audit/` -
deliberately a non-resolving placeholder, not a claim that real hosting
exists) is what ships - the app degrades safely (update check fails
silently, logged to updater.log, app keeps working normally) exactly as it
would on a real network outage.

## What is NOT in this feed

Only the installer, its blockmap, and `latest.yml` are ever staged or
published (`electron/scripts/stage-release.js` copies exactly these three
file types and nothing else - see that script's own comments). The
production database, PostgreSQL data (once centralized), uploaded documents,
audit logs, employee data, session/auth state, and any API key/credential
are never in `release/` or `update-feed/` to begin with, and never touch the
`/updates/` path on the production host - so there is no path by which this
mechanism could expose them. That data lives exclusively behind `/api/` on
the central server, a structurally separate concern (see
`docs/CENTRALIZED_ARCHITECTURE.md`).
