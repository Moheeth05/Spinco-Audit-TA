# Centralized Data Architecture — Assessment & Migration Plan

**Status: assessment only. Not implemented. The application still uses
per-machine local SQLite exactly as before - nothing in this document
changes runtime behavior.**

## Current architecture (as of v1.1.0)

```
Electron (per employee machine)
  -> spawns a local FastAPI backend subprocess (loopback HTTP only)
  -> React frontend talks to that local backend over HTTP, same-origin
  -> FastAPI backend reads/writes SQLite at
       %APPDATA%\Spinco TA Audit\database\spinco_ta.db
  -> Documents stored on local disk at
       %APPDATA%\Spinco TA Audit\documents\
```

Every install is fully independent. There is no shared state between
machines, no network dependency at all once installed, and (confirmed
across this project's own data-cleanup work) each machine's SQLite file can
and does accumulate real, distinct production data.

Relevant existing properties:

- The frontend already talks to the backend exclusively through a REST API
  (`fetch`/`apiPost`/etc. against `/api/...`), never touching the database
  directly. This is the single biggest reason centralizing is *architecturally
  plausible without a rewrite* - the client/server boundary already exists;
  centralizing mostly means relocating the server, not restructuring the
  frontend.
- `backend/app/database.py` is a bare SQLAlchemy `create_engine(sqlite:///...)`
  - no SQLite-specific query syntax was found in scope for this task, so a
  swap to PostgreSQL's connection URL is plausible, but this was not
  exhaustively audited against every model/query in the codebase.
- **No migration framework exists** (no Alembic, no equivalent) - schema
  changes today are handled by dropping and recreating tables (see
  `tests/conftest.py`'s `Base.metadata.drop_all`/`create_all` pattern). This
  is fine for a single local SQLite file a developer controls, but is
  unworkable once many clients depend on one shared, always-on schema -
  **a real migration tool is a hard prerequisite for centralization**, not
  an optional nice-to-have.
- SQLite has no true timezone-aware column type (the root cause of the IST
  display bug fixed in this project's history) - PostgreSQL's
  `TIMESTAMPTZ` would fix this at the storage layer itself, a genuine
  incidental benefit of migrating.
- Document storage (`SPINCO_STORAGE_ROOT`) is local-filesystem-based with
  content-hash-prefixed filenames. This would need a centralized store
  (object storage, or a document-serving component) behind the same
  interface - the amount of rework here depends on how tightly the storage
  code is coupled to `pathlib.Path` local I/O, which needs its own
  dedicated review before estimating effort.
- Auth already uses argon2 password hashing + session cookies at the
  backend - the mechanism itself is reusable for a remote server, but CORS/
  cookie-domain/`Secure` flag handling (currently `SPINCO_COOKIE_SECURE=false`
  because everything is loopback HTTP) needs to change for a real HTTPS
  remote deployment.

## Target architecture

```
Electron Desktop App (per employee)
        |  HTTPS
        v
Central FastAPI Server (one deployment, not spawned by Electron)
        |
        +--> Central PostgreSQL Database
        |      users, employees, expense_reports, expenses, bills, trips,
        |      trip_reports, validation_results, exceptions,
        |      exception_verifications, audit_logs, policy_master,
        |      source_evidence, processing_logs, bill_match_candidates,
        |      trip_match_candidates
        |
        +--> Central Document Storage
               (object storage or a dedicated document-serving component -
               replaces the per-machine local documents/ folder)
```

Electron's role shrinks: it stops spawning a local backend subprocess
entirely and instead just loads the frontend pointed at the central
server's HTTPS URL. `electron/main.js`'s `startBackend()`/`waitForHealth()`/
`stopBackend()` machinery becomes dead code to remove at that point (not
removed now).

## Migration risks (why this is a plan, not this task's implementation)

1. **Existing per-machine data must be consolidated or deliberately
   retired.** This project's own history (root-caused a "resurrected test
   data" incident to a genuine local re-upload) is a direct demonstration
   that per-machine SQLite files already diverge in practice. A real
   migration needs an explicit, human-reviewed decision per machine: import
   its data into the central DB, archive it, or discard it - never an
   automatic silent merge.
2. **No offline mode today would be lost.** The app currently works with
   zero network dependency. A naive centralization makes the app unusable
   without connectivity. Whether that's acceptable (probably yes, for an
   office expense-audit tool) is a product decision, not a technical one -
   flagging it so it's made deliberately.
3. **No migration framework exists yet.** Introducing Alembic (or
   equivalent) and writing the first real baseline migration is
   prerequisite work, independent of the Postgres swap itself.
4. **Document storage migration is unscoped.** Moving from local disk to
   centralized storage needs its own review of how tightly `storage.py`
   (not audited in this task) couples to local filesystem paths.
5. **Auth/session/CORS rework** for a real multi-origin HTTPS deployment
   (currently everything assumes same-origin loopback HTTP).
6. **This is unrelated to, and does not block, the update architecture**
   built in this task - the self-hosted update feed distributes the
   *application binary*; this document is about where the app's *data*
   eventually lives. They can be deployed independently, and the update
   feed can later be hosted on the same infrastructure as the central
   server without any coupling between the two.

## Recommendation

Treat this as its own, separately-scoped project phase: (a) introduce a
migration framework first against the existing SQLite schema (lower risk,
immediately useful regardless of centralization timing), (b) audit
`storage.py` and every model/query for SQLite-specific behavior, (c) design
the per-machine data consolidation process explicitly with a human decision
point per machine, before writing any server-deployment code.
