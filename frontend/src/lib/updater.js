// Thin wrapper around window.spincoApp (exposed by electron/preload.js).
// undefined when this page is loaded outside Electron (e.g. `npm run dev`'s
// plain web workflow) - every export here degrades to a safe no-op/null in
// that case rather than throwing, so SettingsPage.jsx can render normally
// either way.
export function isElectron() {
  return typeof window !== 'undefined' && !!window.spincoApp
}

export async function getAppVersion() {
  if (!isElectron()) return null
  try {
    return await window.spincoApp.getVersion()
  } catch {
    return null
  }
}

export async function getBuildInfo() {
  if (!isElectron()) return null
  try {
    return await window.spincoApp.getBuildInfo()
  } catch {
    return null
  }
}

export async function getUpdateStatus() {
  if (!isElectron()) return null
  try {
    return await window.spincoApp.getUpdateStatus()
  } catch {
    return null
  }
}

export async function checkForUpdates() {
  if (!isElectron()) return null
  return window.spincoApp.checkForUpdates()
}

export async function downloadUpdate() {
  if (!isElectron()) return null
  return window.spincoApp.downloadUpdate()
}

export async function installUpdate() {
  if (!isElectron()) return null
  return window.spincoApp.installUpdate()
}

// Returns an unsubscribe function; no-op unsubscribe when not in Electron.
export function onUpdateStatus(callback) {
  if (!isElectron()) return () => {}
  return window.spincoApp.onUpdateStatus(callback)
}
