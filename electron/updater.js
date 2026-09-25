"use strict";

/**
 * Self-hosted auto-update wiring for the packaged app, using electron-updater's
 * "generic" provider - a plain static-file update feed (an installer .exe
 * plus an auto-generated latest.yml), no GitHub/credential dependency. See
 * electron/scripts/stage-release.js for how a release's files are produced,
 * and the repo's update-feed documentation for how they get published.
 *
 * Design choices, deliberately:
 *  - autoDownload/autoInstallOnAppQuit are both OFF. Checking for updates
 *    happens automatically on startup (and on demand), but downloading and
 *    installing are separate, explicit, user-triggered IPC calls - "do not
 *    force an update unless explicitly configured" from the spec this was
 *    built against.
 *  - The feed URL is resolved at runtime (SPINCO_UPDATE_FEED_URL), not
 *    hardcoded - electron-builder still bakes a default into app-update.yml
 *    at build time (see package.json's build.publish.url) so the app has a
 *    sane fallback, but the real deployed value should be set as a real
 *    environment variable on each install (mirrors how SPINCO_DB_PATH etc.
 *    already work in main.js) so the feed host can be finalized/changed
 *    without a full rebuild.
 *  - Every event is both logged (electron-log, to <userData>/logs/updater.log)
 *    and broadcast to the renderer via IPC, so the Settings page can show
 *    real status instead of a static "checking" spinner forever.
 *  - Disabled entirely in dev mode (app.isPackaged === false) - there is no
 *    app-update.yml in a dev checkout, and calling checkForUpdates() there
 *    throws.
 */

const { app, ipcMain } = require("electron");

let mainWindowRef = null;
let latestStatus = {
  state: "idle", // idle | checking | available | up-to-date | downloading | downloaded | error | unconfigured
  currentVersion: null,
  latestVersion: null,
  progress: null,
  error: null,
  checkedAt: null,
};
let ipcRegistered = false;
let autoUpdaterWired = false;
let autoUpdater = null;

function broadcast(patch) {
  latestStatus = { ...latestStatus, ...patch };
  if (mainWindowRef && !mainWindowRef.isDestroyed()) {
    mainWindowRef.webContents.send("updater:status", latestStatus);
  }
}

// Registers the app:get-version / updater:* IPC handlers unconditionally -
// safe to call in dev mode too, so the renderer never has to special-case
// "am I running packaged" itself, only "am I running inside Electron at
// all" (see preload.js). In dev mode there is no app-update.yml and no
// autoUpdater wiring, so these simply report "unconfigured".
function registerIpcHandlers({ mainWindow }) {
  mainWindowRef = mainWindow;
  latestStatus.currentVersion = app.getVersion();
  if (!app.isPackaged) {
    latestStatus.state = "unconfigured";
  }

  if (ipcRegistered) return;
  ipcRegistered = true;

  ipcMain.handle("app:get-version", () => app.getVersion());
  ipcMain.handle("updater:status", () => latestStatus);

  ipcMain.handle("updater:check", async () => {
    if (!autoUpdaterWired) return latestStatus;
    try {
      await autoUpdater.checkForUpdates();
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      broadcast({ state: "error", error: message });
    }
    return latestStatus;
  });

  ipcMain.handle("updater:download", async () => {
    if (!autoUpdaterWired || latestStatus.state !== "available") return latestStatus;
    try {
      await autoUpdater.downloadUpdate();
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      broadcast({ state: "error", error: message });
    }
    return latestStatus;
  });

  ipcMain.handle("updater:install", () => {
    if (!autoUpdaterWired || latestStatus.state !== "downloaded") return latestStatus;
    autoUpdater.quitAndInstall(false, true);
    return latestStatus;
  });
}

function setupAutoUpdater({ mainWindow, logFilePath, feedURLOverride }) {
  registerIpcHandlers({ mainWindow });
  if (!app.isPackaged) return; // no app-update.yml in a dev checkout

  const log = require("electron-log");
  ({ autoUpdater } = require("electron-updater"));

  log.transports.file.resolvePathFn = () => logFilePath;
  log.transports.file.level = "info";
  log.transports.console.level = false;
  autoUpdater.logger = log;

  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;

  if (feedURLOverride) {
    autoUpdater.setFeedURL({ provider: "generic", url: feedURLOverride, channel: "latest" });
    log.info(`[updater] feed URL overridden via SPINCO_UPDATE_FEED_URL: ${feedURLOverride}`);
  }

  if (autoUpdaterWired) return; // event listeners only need registering once
  autoUpdaterWired = true;
  latestStatus.state = "idle";

  autoUpdater.on("checking-for-update", () => {
    log.info("[updater] checking for update");
    broadcast({ state: "checking", error: null, checkedAt: new Date().toISOString() });
  });
  autoUpdater.on("update-available", (info) => {
    log.info(`[updater] update available: ${info.version}`);
    broadcast({ state: "available", latestVersion: info.version, error: null });
  });
  autoUpdater.on("update-not-available", (info) => {
    log.info(`[updater] no update available (latest published: ${info?.version ?? "unknown"})`);
    broadcast({ state: "up-to-date", latestVersion: info?.version ?? latestStatus.currentVersion, error: null });
  });
  autoUpdater.on("download-progress", (progress) => {
    broadcast({
      state: "downloading",
      progress: {
        percent: progress.percent,
        transferredBytes: progress.transferred,
        totalBytes: progress.total,
        bytesPerSecond: progress.bytesPerSecond,
      },
    });
  });
  autoUpdater.on("update-downloaded", (info) => {
    log.info(`[updater] update downloaded and verified: ${info.version}`);
    broadcast({ state: "downloaded", latestVersion: info.version, progress: null });
  });
  autoUpdater.on("error", (err) => {
    const message = err && err.message ? err.message : String(err);
    log.error(`[updater] error: ${message}`);
    broadcast({ state: "error", error: message });
  });
}

function checkForUpdatesOnStartup() {
  if (!autoUpdaterWired) return; // dev mode / not yet wired - nothing to check
  autoUpdater.checkForUpdates().catch((err) => {
    const message = err && err.message ? err.message : String(err);
    broadcast({ state: "error", error: message });
  });
}

module.exports = { setupAutoUpdater, checkForUpdatesOnStartup, getLatestStatus: () => latestStatus };
