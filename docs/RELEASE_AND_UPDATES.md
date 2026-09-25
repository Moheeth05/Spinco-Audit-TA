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

# 2. Run the full regression suite (backend)
cd backend
.\.venv\Scripts\python.exe -m pytest tests/ -q

# 3. Build the complete, versioned application bundle
cd ..
npm run dist
#    = npm run prepackage (frontend build + backend PyInstaller build +
#      Tesseract collection) + electron-builder --win (NSIS + portable)
#    Produces release/Spinco TA Audit Setup <version>.exe (+ .blockmap),
#    release/Spinco TA Audit <version>.exe (portable, NOT part of the
#    update feed), and release/latest.yml.

# 4. Stage exactly the update-feed files (installer + .blockmap + latest.yml
#    - nothing else) into a local staging directory
npm run release:stage
#    -> D:\Bills\update-feed\spinco-ta-audit\ (override with
#       SPINCO_UPDATE_FEED_DIR)

# 5. Publish that staging directory's contents to the real update feed host,
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
