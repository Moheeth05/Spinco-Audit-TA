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

# 5. Publish that staging directory's contents to the real update feed host
#    (not automated here - depends on final hosting choice, e.g.:
#      aws s3 sync update-feed/spinco-ta-audit s3://<bucket>/spinco-ta-audit/
#    or an rsync/scp to a company-owned server)
```

A release is never "the frontend changed but the backend didn't get
rebuilt" - step 3 always rebuilds and repackages both together into one
versioned bundle, so there is no way to publish a mismatched pair.

## Runtime configuration

`SPINCO_UPDATE_FEED_URL` (optional env var, set on the installed machine)
overrides the feed URL baked into the build at `package.json`'s
`build.publish.url` - this lets the real feed host be finalized or changed
without rebuilding/republishing the application itself. Until real hosting
is provisioned, the baked-in default (`https://updates.spincotech.internal/spinco-ta-audit/`)
is a deliberate placeholder that will not resolve - the app degrades safely
(update check fails silently, logged to updater.log, app keeps working
normally) exactly as it would on a real network outage.

## What is NOT in this feed

Only the installer, its blockmap, and `latest.yml` are ever staged or
published (`electron/scripts/stage-release.js` copies exactly these three
file types and nothing else - see that script's own comments). The
production database, uploaded documents, audit logs, employee data, and any
API key/credential are never in `release/` or `update-feed/` to begin with,
so there is no path by which this mechanism could expose them.
