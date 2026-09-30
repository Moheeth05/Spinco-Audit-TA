# Orphan Record Investigation — bill_match_candidates / trip_match_candidates

**Status: investigation complete, code fix implemented/tested/committed
(4d7defc), the DEV database's historical orphans have been cleaned (see "DEV
Historical Orphan Cleanup" below), and a DISPOSABLE COPY of the production
backup has also been cleaned as a controlled rehearsal (see "Production
Backup Historical Orphan Cleanup — Disposable Copy" below). The ORIGINAL
production database backup itself remains untouched - hash-verified
identical before and after every step - and the live production database
was never opened for modification. SQLite foreign-key enforcement was not
enabled anywhere.**

## Fix implemented

A new, small, reusable helper module -
`app/services/extraction/candidate_cleanup.py` - provides three scoped
cleanup functions (`delete_candidates_for_expense_ids`,
`delete_bill_match_candidates_for_bill_ids`,
`delete_trip_match_candidates_for_trip_report_ids`), each taking an explicit
list of ids and never issuing a broad/unscoped `DELETE`. Wired into all four
lifecycle paths identified above, always called *before* the corresponding
parent delete, with no `db.commit()` in between (so a rollback of the parent
operation rolls the cleanup back too, by construction - no extra code needed
for that guarantee):

| Lifecycle path | File : line | Fix |
|---|---|---|
| Document reprocessing (expense recreation) | `pipeline.py` (`_extract_expense_report`, before the `Expense` delete) | captures old expense ids, cleans both candidate tables by `expense_id` |
| Document reprocessing (embedded trip deleted) | `pipeline.py` (`elif existing_embedded:` branch) | cleans `TripMatchCandidate` by the embedded `TripReport`'s own id before deleting it |
| Bill rematching | `bill_pipeline.py` (before the `Bill` delete) | captures old bill ids, cleans `BillMatchCandidate` by `bill_id` |
| Document deletion | `documents.py::delete_document` | for an ExpenseReport, cleans both candidate tables by its expense/bill ids; for a TripReport, cleans `TripMatchCandidate` by its own id - all before `db.delete(row)`, since ORM cascade cannot reach either candidate table |

Trip rematching (`batta_pipeline.py`) was investigated and confirmed to
already correctly replace its own candidates for an unchanged expense set -
the actual `trip_report_id`/`expense_id` invalidation that orphaned rows
came from is upstream, in `pipeline.py`'s expense recreation and embedded-
trip deletion, both fixed above; no change was needed in `batta_pipeline.py`
itself (verified by dedicated test).

**Tests added:** `tests/test_match_candidate_cleanup.py`, 7 tests covering
A (document reprocessing), B (bill rematching), C (trip-candidate cleanup
scoping), D (document deletion, both ExpenseReport and TripReport, with
explicit unrelated-data preservation), E (rollback behavior), and F (a full
process → reprocess → rematch → reprocess → delete repeat cycle proving zero
new orphans accumulate). All use the real pipeline against real sample PDFs,
not mocks.

**A genuine lesson learned during test design**, worth recording: an early
version of tests A and B asserted that the *old* parent/candidate row ids no
longer existed after the fix ran - this is unreliable on SQLite, which
reuses rowids once a table's rows are all deleted (these models have no
`AUTOINCREMENT` keyword), so a reprocessed report's new rows can legitimately
receive the exact same ids as the old ones in a single-report test,
producing a false failure that looked like the fix wasn't working. The tests
were rewritten to upload a second, unrelated report (keeping tables
non-empty, avoiding the recycling) and to assert the invariant that actually
matters - zero orphaned rows anywhere (dialect-independent) plus the
unrelated report's own candidates provably untouched.

**Full regression result:** all pre-existing tests plus the 7 new ones pass
with zero regressions (see the task's final report for the exact count).

**Historical orphan rows confirmed untouched at the time this fix was
tested**: `PRAGMA foreign_key_check` against both the dev database and the
production database backup returned the exact same counts as the original
investigation (2,122 and 1,038 respectively) - this task's own new test data
is fully isolated in a temporary test database (see `tests/conftest.py`) and
never touched either file. **The dev database's orphans were subsequently
cleaned in a separate, later task - see "DEV Historical Orphan Cleanup"
below. The production backup's 1,038 orphaned rows remain untouched.**

## Executive summary

`bill_match_candidates` and `trip_match_candidates` contain a large number of
rows whose `bill_id`/`expense_id`/`trip_report_id` foreign-key values point at
`bills`/`expenses`/`trip_reports` rows that no longer exist. This is **not**
data corruption, a security issue, or anything touching a single incident —
it is a systematic, always-reproducible application bug: **every** code path
that wholesale-replaces `Expense`/`Bill` rows (document reprocessing, bill
rematch, trip rematch) or deletes a document deletes the *old* parent rows
but never deletes the match-candidate rows that referenced them, and document
deletion's ORM cascade cannot reach these tables at all because neither model
declares a `relationship()` to the other. The result: every reprocess/rematch/
delete action silently leaves behind a fresh batch of orphaned rows, forever,
with no code path in the entire codebase that ever cleans them up. This has
been happening since early in this database's history and fully accounts for
the row counts found.

**Category: (C) evidence of a transaction/cleanup bug** — confirmed by direct
code inspection, not inferred from row counts alone.

## Exact orphan counts

| | DEV database | Production DB backup |
|---|---|---|
| `bill_match_candidates` total rows | 1,170 | 756 |
| — orphaned (bill_id missing) | 1,008 | 501 |
| — orphaned (expense_id missing) | 1,008 | 501 |
| — orphaned on **both** simultaneously | 1,008 | 501 |
| — healthy | 162 | 255 |
| `trip_match_candidates` total rows | 103 | 54 |
| — orphaned (trip_report_id missing) | 21 | 0 |
| — orphaned (expense_id missing) | 85 | 36 |
| — orphaned on both | 21 | 0 |
| **Total orphaned rows** | **2,122** | **1,038** |

(Matches `PRAGMA foreign_key_check`'s totals exactly — both methods agree.)

## Schema / reference analysis

| Table | FK column | References | Index? | `ondelete=`? | `relationship()` declared anywhere? |
|---|---|---|---|---|---|
| `bill_match_candidates` | `expense_id` | `expenses.id` | yes | no | no |
| `bill_match_candidates` | `bill_id` | `bills.id` | yes (added Phase 1) | no | no |
| `trip_match_candidates` | `expense_id` | `expenses.id` | yes | no | no |
| `trip_match_candidates` | `trip_report_id` | `trip_reports.id` | yes (added Phase 1) | no | no |

Neither `BillMatchCandidate` nor `TripMatchCandidate` (`app/models/bill_match.py`,
`app/models/trip_match.py`) declares a single `relationship()` — confirmed by
direct inspection. This means:
- SQLAlchemy's ORM cascade (`cascade="all, delete-orphan"` on
  `ExpenseReport.expenses`/`ExpenseReport.bills` and `TripReport.legs`) has no
  path to these tables at all - they are structurally unreachable by ORM
  cascade regardless of what's configured elsewhere.
- No FK declares `ondelete=` (e.g. `ondelete="CASCADE"`), so there is no
  database-level cascade either.
- SQLite foreign-key enforcement being off is *why* the app has never raised
  an error creating these orphans (an enforced FK would reject the underlying
  bulk deletes outright, or reject the later dangling references) - it is not
  the cause of the orphaning itself.

`expenses`, `bills`, `trip_reports`, `expense_reports`, `trips` themselves
have no orphan issue of their own - the corruption is entirely contained to
the two match-candidate tables, which is consistent with them being the only
tables with zero relationship-graph coverage.

## Dev database findings

- Orphaned `bill_match_candidates` rows occupy a **contiguous low-id block**
  (ids 1-1101); every currently-healthy row has a **higher** id (1174-1335).
  This is the direct fingerprint of repeated wholesale-replace cycles: each
  reprocessing run creates a fresh batch of candidate rows with new,
  higher ids, while the previous run's rows are abandoned in place.
- Every orphaned `bill_match_candidates` row is orphaned on **both**
  `bill_id` and `expense_id` at once (1,008 = 1,008 = 1,008, exactly) - never
  just one or the other. This matches the confirmed mechanism exactly: a
  reprocessing run deletes and recreates Expense and Bill rows for the whole
  report *together*, so a stale candidate row's two FK values go bad at the
  same moment, not independently.
- 253 distinct missing `bill_id` values and 336 distinct missing `expense_id`
  values are referenced across the orphaned rows - consistent with *many*
  separate reprocessing cycles, not one event.
- `expenses.id`'s autoincrement high-water mark is 435, while only 68 `Expense`
  rows currently exist - roughly 367 expense rows have been created and then
  discarded over this database's lifetime, corroborating frequent reprocessing.
- Audit log evidence: 15 `DOCUMENT_REPROCESSED` events across the 4 expense
  reports (roughly 4 reprocessing cycles per report), spanning
  2026-09-22 through 2026-09-24 - directly correlating with the orphan growth.
- `trip_match_candidates`: 21 rows orphaned on `trip_report_id` (an embedded
  trip summary was deleted and re-created differently across reprocess runs -
  see Code paths, path (a)'s embedded-TripReport branch) and 85 orphaned on
  `expense_id` (same expense-recreation mechanism as bill matching).

## Production-backup findings

Identical pattern, smaller scale (fewer reprocessing cycles occurred):
- Same low-id-orphaned / high-id-healthy split (ids 1-501 orphaned, 574-828
  healthy) in `bill_match_candidates`.
- Same both-sides-simultaneously orphaning (501 = 501 = 501, exactly).
- 8 `DOCUMENT_REPROCESSED` events across 4 reports (~2 cycles/report) -
  proportionally fewer than dev, consistent with the smaller orphan count.
- `trip_match_candidates` here shows 0 rows orphaned on `trip_report_id` (no
  embedded-trip-summary deletion happened in this backup's history) and 36
  orphaned on `expense_id` only - a real difference in *history*, not in
  *mechanism*.

## Comparison between dev and production

**Same root cause, different amount of accumulated history.** The orphan
count scales almost exactly with how many times each document was
reprocessed (dev: more reprocessing cycles -> more orphans; production
backup: fewer cycles -> proportionally fewer orphans). No evidence of a
different bug, a one-off incident, or environment-specific behavior - this is
the same deterministic code defect firing every time, in both environments.

## Root cause, with evidence

Confirmed by direct inspection of every code path capable of invalidating an
`Expense`/`Bill`/`TripReport` id (file:line citations):

1. **Full document reprocessing** (`app/services/extraction/pipeline.py:165`) -
   `db.query(Expense).filter(...).delete()` wholesale-deletes all Expense rows
   for the report, then inserts brand-new rows with new autoincrement ids
   (lines 167-180). Nothing in this file ever references
   `BillMatchCandidate`/`TripMatchCandidate` (confirmed via whole-file search -
   zero matches for either symbol).
2. **Bill rematch** (`app/services/extraction/bill_pipeline.py:35,100-104`) -
   deletes all `Bill` rows for the report (line 35, no candidate cleanup at
   all, by any key). The *only* `BillMatchCandidate` delete in this file
   (lines 102-104) is filtered by `expense_id.in_([e.id for e in expenses])`
   where `expenses` is queried at line 100 - **after** pipeline.py's expense
   recreation already happened, so this is always the *current* expense ids.
   It can never match old orphaned ids; it is a functional no-op for cleanup.
3. **Trip/Batta rematch** (`app/services/extraction/batta_pipeline.py:103-105`) -
   identical shape: `TripMatchCandidate` deleted only by
   `expense_id.in_(current batta_expenses)`, never by `trip_report_id`.
4. **Document deletion** (`app/api/documents.py:365`, `db.delete(row)`) -
   SQLAlchemy's ORM cascade correctly removes the `ExpenseReport`'s `Expense`/
   `Bill` rows (`cascade="all, delete-orphan"` on both relationships,
   `expense_report.py:59-60`) or the `TripReport`'s `Trip` legs (`trip.py:56`),
   but since neither match-candidate model has a `relationship()` anywhere in
   the codebase (confirmed - both are plain FK columns only), cascade cannot
   reach them. No `ondelete=` exists at the schema level either.

**Every single code path that can invalidate an id leaves matching
match-candidate rows behind.** There is no path in this codebase, ORM-cascade
or explicit, that correctly cleans up `BillMatchCandidate`/`TripMatchCandidate`
rows referencing an id that just became invalid.

## Application code paths involved

`app/services/extraction/pipeline.py` (expense/trip recreation on reprocess),
`app/services/extraction/bill_pipeline.py` (bill deletion + the no-op
candidate cleanup), `app/services/extraction/batta_pipeline.py` (same shape
for trip candidates), `app/api/documents.py::delete_document` (cascade gap).

## Risk to current application

**Low, in practice, today.** Nothing in the current codebase reads
`bill_match_candidates`/`trip_match_candidates` rows without joining through
their parent (`expenses`/`bills`/`trip_reports`) - an orphaned row's `bill_id`/
`expense_id` simply won't match anything, so it's silently invisible to every
existing query path (none of which do a naive "list all rows" without a join/
filter by a currently-valid parent id, per the model inventory: these tables
have no `relationship()`, so nothing eagerly loads or displays them
independent of a live parent). The main real costs today are: unbounded
table growth (the tables only ever grow, never shrink) and the fact that this
is exactly what makes SQLite FK enforcement unsafe to turn on (§ Phase 1
report).

## Risk to future PostgreSQL migration

**Real and must be addressed before migration**, not after:
- PostgreSQL enforces foreign keys by default. A direct data copy of either
  table as-is would be rejected outright by the target schema's FK
  constraints (`bill_match_candidates.bill_id -> bills.id` etc.) unless the
  orphaned rows are excluded from the migrated data or the constraint is
  deferred/dropped, either of which needs a deliberate decision, not a
  silent default.
- The underlying code defect (four call sites, confirmed above) must be
  fixed as part of - or before - centralization, or the same orphan growth
  will resume immediately in the new environment, now against a database
  that *does* enforce FKs, meaning it would start throwing errors instead of
  silently accumulating debt (arguably a better failure mode, but one that
  needs the code fix in place first, or every reprocess/rematch/delete call
  in production would start failing).

## Recommended cleanup strategy (for the next, separately-approved phase)

1. **Fix the code defect first**, before any data cleanup - otherwise cleanup
   just gets reproduced by the next reprocess/rematch/delete call. Concretely:
   add an explicit delete of `BillMatchCandidate` filtered by `bill_id.in_(...)`
   alongside the existing `Bill` delete (bill_pipeline.py:35), and delete
   `BillMatchCandidate`/`TripMatchCandidate` by the *old* expense ids *before*
   pipeline.py:165's expense delete (not after, and not filtered by the
   soon-to-be-different new ids) - and add an explicit cleanup of both tables
   in `documents.py::delete_document`, since ORM cascade cannot reach them.
2. **Only then**, clean up the existing orphaned rows - a straightforward
   `DELETE ... WHERE NOT EXISTS (...)` per table, but run only after the code
   fix is deployed and only with an explicit backup taken first (same
   discipline already established elsewhere in this project).
3. Validate row counts and a full `PRAGMA foreign_key_check` return to zero
   before considering FK enforcement.

## What must NOT be deleted

- Any row in `expenses`, `bills`, `trip_reports`, `expense_reports`, `trips` -
  none of these are orphaned; the corruption is entirely contained to the two
  match-candidate tables.
- The *healthy* (non-orphaned) `bill_match_candidates`/`trip_match_candidates`
  rows - these are live, currently-referenced matching evidence still shown
  in the UI today.
- Nothing should be deleted in this phase at all - this document is
  investigation only.

## Proposed validation steps before any cleanup

1. Deploy and test the code fix (item 1 above) in isolation first, with
   dedicated regression tests proving a reprocess/rematch/delete cycle no
   longer leaves any new orphans (a `PRAGMA foreign_key_check` assertion of
   zero new violations after each operation is a natural test shape).
2. Re-run the exact investigation queries in this document against dev to
   confirm the orphan count stops growing under the fixed code.
3. Take a fresh, hash-verified backup of production immediately before any
   cleanup of existing orphaned rows.
4. Run the cleanup against a copy first, verify row counts and
   `PRAGMA foreign_key_check` returns zero, verify the application's existing
   regression suite and a manual UI smoke test still show correct bill/trip
   matching for every current document, before running it against the real
   production database.

## Recommendation for the next phase

Treat this as two separable, sequential pieces of approved work: (1) a small,
well-tested code fix closing the four cleanup gaps identified above - safe to
do immediately, independent of centralization or migration timing, and
directly prevents further orphan growth; (2) a separately-approved data
cleanup pass (dev first, then production only after explicit approval and a
fresh backup) once the code fix is verified. Do not combine these into one
change, and do not enable SQLite FK enforcement until both are complete.

## DEV Historical Orphan Cleanup

**Status: complete. DEV database only. The production database and the
production database backup were not touched by this step at all - the
backup's 1,038 historical orphaned rows remain exactly as before (the live
production database itself has never been directly queried for orphans in
this investigation - only a backup copy was, per the original safety
constraints - so no production orphan count is claimed here either way).**

### Backup

- Location: `D:\Bills\backups\dev-orphan-cleanup-20260925-164651\spinco_ta.db`
- SHA-256 (source and backup copy, verified identical):
  `FB9C649BEE75D92AC097AFFACE6926162284E50FFDD1F2EB3795AE4919A5B720`
- Source file size at backup time: 831,488 bytes

### Pre-cleanup counts (re-verified, exactly matching the original investigation)

| | Count |
|---|---|
| `bill_match_candidates` total rows | 1,170 |
| `bill_match_candidates` orphaned (union of bill_id-missing OR expense_id-missing) | 1,008 |
| `bill_match_candidates` healthy | 162 |
| `trip_match_candidates` total rows | 103 |
| `trip_match_candidates` orphaned (union of trip_report_id-missing OR expense_id-missing) | 85 |
| `trip_match_candidates` healthy | 18 |
| `PRAGMA foreign_key_check` total violations | 2,122 |

**Reconciling 2,122 (violations) with 1,093 (rows actually deleted):** every
orphaned `bill_match_candidates` row is broken on *both* `bill_id` and
`expense_id` simultaneously, so `PRAGMA foreign_key_check` counts each such
row twice (1,008 x 2 = 2,016 violations from 1,008 distinct rows). Of
`trip_match_candidates`' violations, the 21 rows broken on `trip_report_id`
are a subset of the 85 rows broken on `expense_id` (confirmed by direct
query before deleting anything), so the distinct-row union is 85, not 106.
2,016 + 85 + 21 = 2,122 violations, matching exactly; 1,008 + 85 = **1,093
distinct rows** were the actual deletion target - re-verified row-by-row
immediately before deletion that every targeted row genuinely had at least
one broken required parent reference (script asserted this and would have
refused to proceed otherwise).

### Cleanup executed

Single transaction, `DELETE ... WHERE id IN (<explicit id list>)` per table
(never a broad/unscoped delete):

- **Deleted from `bill_match_candidates`: 1,008**
- **Deleted from `trip_match_candidates`: 85**
- **Total deleted: 1,093**

### Post-cleanup counts

| | Count |
|---|---|
| `bill_match_candidates` remaining | 162 (all healthy) |
| `trip_match_candidates` remaining | 18 (all healthy) |
| **Remaining valid candidate rows** | **180** |
| `PRAGMA foreign_key_check` violations | **0** |
| `PRAGMA integrity_check` | **ok** |

**Unrelated tables - every row count unchanged, verified individually:**
`expense_reports` (4), `expenses` (68), `bills` (48), `trip_reports` (5),
`trips` (14), `exceptions` (42), `exception_verifications` (0),
`validation_results` (548), `source_evidence` (258), `audit_logs` (109),
`users` (2), `processing_logs` (100), `excel_reports` (1) - all identical
before and after.

### Lifecycle regression after cleanup (against the real dev database, not just the isolated test suite)

Directly exercised the fixed pipeline against an existing real report (id 1,
`ER-39832`) in the actual dev database: a full reprocess
(`run_extraction`), then an explicit bill-only rematch
(`bill_pipeline.extract_and_match_bills`), then an explicit trip/Batta
rematch (`batta_pipeline.match_batta_expenses`). Confirmed genuine (not a
silent no-op) by observing the report's `Expense` rows shift to a brand-new
id range (424-435, the table's new high-water mark) - proving a real
delete-and-recreate cycle ran. Orphan count before: 0. Orphan count after:
**0**. No new orphans were created.

### Full test suite

**342 passed, 0 failed** (backend regression suite; runs against its own
isolated temporary database per `tests/conftest.py`, unaffected by and not
affecting the dev database cleanup).

### Safety confirmations

- Production database (`%APPDATA%\Spinco TA Audit\database\spinco_ta.db`):
  untouched - `LastWriteTime` unchanged (`2026-09-25 09:20:14`, same as
  before this task and every prior task today).
- Production database backup
  (`D:\Bills\backups\dipak-cleanup-20260924-170758\database\spinco_ta.db`):
  untouched - `LastWriteTime` unchanged (`2026-09-24 17:06:21`). Its 1,038
  historical orphaned rows were never queried for deletion and remain
  exactly as found.
- NVIDIA OCR files: untouched - unchanged timestamps, no modifications.
- `backups/`: untouched except for the one new, explicitly-created backup
  described above (`dev-orphan-cleanup-20260925-164651/`) - no existing
  backup directory was modified or removed.
- SQLite foreign-key enforcement: not enabled, anywhere.
- No application code was changed as part of this cleanup task - it was
  data-only, against the dev database exclusively.

## Production Backup Historical Orphan Cleanup — Disposable Copy

**Status: complete. A DISPOSABLE COPY of the production backup was cleaned
as a controlled rehearsal of the same fix. The ORIGINAL production backup
and the live production database were never modified. This is explicitly
NOT a production cleanup - the disposable copy was never copied back
anywhere, never used to replace anything, and is not referenced by the
running application.**

### Original backup (untouched)

- Path: `D:\Bills\backups\dipak-cleanup-20260924-170758\database\spinco_ta.db`
- SHA-256 before this task: `67DC4C4333ACB205CD2D8EC54DE2EBBF92C6E850AD6FA4949DBC6606333004D6`
- SHA-256 after this task: `67DC4C4333ACB205CD2D8EC54DE2EBBF92C6E850AD6FA4949DBC6606333004D6`
- **Identical - confirmed unchanged.**

### Disposable copy

- Path: `D:\Bills\backups\production-orphan-cleanup-20260928-101912\spinco_ta.db`
- SHA-256 before cleanup: `67DC4C4333ACB205CD2D8EC54DE2EBBF92C6E850AD6FA4949DBC6606333004D6`
  (identical to the original at copy time, confirmed before any query ran)
- SHA-256 after cleanup: `D7394320AB652403A5A1E76D4774BCA95FC1B75DEF0FBDA2E72FC83F1CB5CB07`
  (expected to differ - this file's data was intentionally modified; the
  original it was copied from was not)

### Baseline (disposable copy, before cleanup)

| | Count |
|---|---|
| `bill_match_candidates` total rows | 756 |
| `bill_match_candidates` distinct orphan rows | 501 (all broken on **both** `bill_id` and `expense_id`) |
| `bill_match_candidates` healthy | 255 |
| `trip_match_candidates` total rows | 54 |
| `trip_match_candidates` distinct orphan rows | 36 (all broken on `expense_id` only; 0 on `trip_report_id`) |
| `trip_match_candidates` healthy | 18 |
| **Total distinct orphan rows** | **537** |
| `PRAGMA foreign_key_check` violations | **1,038** |
| `PRAGMA integrity_check` | ok |

Matched the expected baseline exactly (501 / 36 / 537 / 1,038) - no
discrepancy, so cleanup proceeded per the approved plan. Full table
inventory also recorded: users:3, employees:6, expense_reports:5,
expenses:99, bills:84, trip_reports:7, trips:32, validation_results:797,
exceptions:166, exception_verifications:0, source_evidence:272,
audit_logs:67, processing_logs:90, excel_reports:1, policy_master:29.

### Cleanup executed (disposable copy only)

Single transaction, `DELETE ... WHERE id IN (<explicit id list>)` per
table, after row-by-row re-verification that every targeted row genuinely
had a broken required parent reference (the script asserted the exact
target counts - 501 and 36 - before touching anything, and would have
aborted on any mismatch):

- **Deleted from `bill_match_candidates`: 501**
- **Deleted from `trip_match_candidates`: 36**
- **Total deleted: 537**

### Post-cleanup verification (disposable copy)

| | Count |
|---|---|
| `bill_match_candidates` remaining | 255 (all healthy) |
| `trip_match_candidates` remaining | 18 (all healthy) |
| **Remaining valid candidate rows** | **273** |
| `PRAGMA foreign_key_check` violations | **0** |
| `PRAGMA integrity_check` | **ok** |

**All 15 unrelated tables verified individually unchanged**: expense_reports
(5), expenses (99), bills (84), trip_reports (7), trips (32), exceptions
(166), exception_verifications (0), validation_results (797),
source_evidence (272), audit_logs (67), users (3), employees (6),
processing_logs (90), excel_reports (1), policy_master (29) - identical
before and after.

No `VACUUM`, no compaction - the file remains reversible via the untouched
original backup at any time.

### What was intentionally NOT done

Per explicit scope: no application-level regression tests were run against
this disposable copy (database-level integrity/FK checks were judged
sufficient and appropriate, consistent with keeping this task isolated from
application/production state); the cleaned disposable copy was not copied
back to the original backup location, not used to replace the live
production database, not migrated anywhere, and the application was never
configured to use it.

### Safety confirmations

- Live production database (`%APPDATA%\Spinco TA Audit\database\spinco_ta.db`):
  never opened for modification - only its filesystem metadata was read.
- Original production backup: hash-verified identical before and after (see above).
- NVIDIA OCR files: untouched.
- Application source code: untouched - this was a data-only operation
  against one disposable file.
- No PostgreSQL migration, no centralized-architecture change, no SQLite
  FK-enforcement change: none occurred.
- `backups/`: untouched except for the one new, explicitly-created
  disposable copy directory (`production-orphan-cleanup-20260928-101912/`).
- Nothing was committed.
