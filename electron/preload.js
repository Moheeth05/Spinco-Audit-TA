"use strict";

/**
 * Context-isolated bridge between the renderer (the React app, loaded from
 * the packaged backend's own origin) and the Electron main process. Exposes
 * exactly three things: the running app's version, its release-traceability
 * build info, and the auto-updater status/controls - nothing else. The
 * renderer has no other access to
 * Node/Electron APIs (contextIsolation: true, nodeIntegration: false,
 * sandbox: true - see main.js:createWindow).
 *
 * window.spincoApp is undefined when the page is loaded outside Electron
 * (e.g. the plain `npm run dev` web workflow) - every consumer must guard
 * for that rather than assume it exists.
 */

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("spincoApp", {
  getVersion: () => ipcRenderer.invoke("app:get-version"),
  getBuildInfo: () => ipcRenderer.invoke("app:get-build-info"),

  // Update lifecycle - see electron/updater.js for the actual autoUpdater
  // wiring this talks to. checkForUpdates/downloadUpdate/installUpdate are
  // three distinct, separately user-triggered steps (see updater.js's
  // autoDownload: false) - nothing here ever downloads or installs without
  // an explicit call from the renderer in response to a user action.
  checkForUpdates: () => ipcRenderer.invoke("updater:check"),
  downloadUpdate: () => ipcRenderer.invoke("updater:download"),
  installUpdate: () => ipcRenderer.invoke("updater:install"),
  getUpdateStatus: () => ipcRenderer.invoke("updater:status"),

  // Fires on every status change (checking/available/up-to-date/downloading/
  // downloaded/error) - returns an unsubscribe function.
  onUpdateStatus: (callback) => {
    const listener = (_event, status) => callback(status);
    ipcRenderer.on("updater:status", listener);
    return () => ipcRenderer.removeListener("updater:status", listener);
  },
});
