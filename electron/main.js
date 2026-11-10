"use strict";

/**
 * Phase 11 - Electron desktop shell.
 *
 * Architecture: Electron starts the packaged FastAPI backend as a plain
 * subprocess, waits for it to report healthy, then points a BrowserWindow at
 * that same localhost server (which also serves the built React app - see
 * app/main.py's SPINCO_FRONTEND_DIST handling). Same-origin frontend+API
 * means the existing cookie/CSRF session design works completely unchanged;
 * Electron itself never touches business logic, the database, or the API.
 *
 * Dev mode (`npm run electron:dev`, i.e. app.isPackaged === false) does NOT
 * spawn anything - it loads http://localhost:5200 directly, on the
 * assumption the developer already has `uvicorn` and `vite` running exactly
 * as documented for the existing web-app dev workflow. This keeps the
 * desktop shell from doubling as a second, divergent way to run the dev
 * backend.
 */

const { app, BrowserWindow, dialog, shell, ipcMain } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const { spawn } = require("node:child_process");
const { setupAutoUpdater, checkForUpdatesOnStartup } = require("./updater");
const remoteConfig = require("./remote-config");
const diagnostics = require("./connection-diagnostics");
const lanDiscovery = require("./lan-discovery");

// No Google Drive credential path or central-storage switch may ever reach a
// client-side process, in either connection mode. Removing them from this
// process's own environment covers every child the shell (or a library such as
// electron-updater) could spawn; startBackend() also sanitises its explicit
// copy as defence in depth.
remoteConfig.scrubEnvInPlace(process.env);

const DEV_FRONTEND_URL = "http://localhost:5200";
// Deliberately NOT 8001 - that's the existing dev workflow's port (see
// module docstring above), and a developer with a dev `uvicorn` instance
// still running would otherwise have the packaged app's own backend fail to
// bind while the readiness check happily gets a 200 from the WRONG (dev,
// frontend-less) server, silently loading a "Not Found" JSON blob instead of
// the app. Confirmed exactly this failure mode while diagnosing a UAT
// report - the packaging code itself was correct throughout; a port
// collision was masking it. Picked from the dynamic/private port range
// (49152-65535) specifically to avoid well-known-port collisions too.
const BACKEND_PORT = process.env.SPINCO_BACKEND_PORT || "58732";
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;
const HEALTH_URL = `${BACKEND_URL}/api/health`;
const READINESS_TIMEOUT_MS = 45_000;
const READINESS_POLL_MS = 350;

// Remote (central server) mode: how long the app keeps retrying before it shows the
// connection-problem page. The server is already running (unlike the local backend,
// which has to start), so this is much shorter than READINESS_TIMEOUT_MS.
const REMOTE_CONNECT_TIMEOUT_MS = 20_000;
const CONNECTION_ERROR_PAGE = path.join(__dirname, "connection-error.html");
// Machine-wide settings IT can deploy to every PC (lower priority than the per-user file).
const MACHINE_CONFIG_DIR = process.env.PROGRAMDATA ? path.join(process.env.PROGRAMDATA, "Spinco TA Audit") : null;

let mainWindow = null;
let backendProcess = null;
let backendLogStream = null;
// What the renderer may learn about the shell's connection (never a full URL, path, cookie or credential).
let connectionInfo = { mode: "local" };
// The last failed connection attempt, shown by electron/connection-error.html.
let connectionState = null;
let activeRemoteConfig = null;
let insecureWarningShown = false;

// ---------- Single instance lock ----------
// Prevents a second Electron process (and therefore a second backend
// subprocess) from ever starting alongside this one.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

// ---------- Per-user application data directories ----------
// Everything mutable (database, uploaded PDFs, generated Excel workbooks,
// logs) lives under Electron's per-user userData directory
// (%APPDATA%\Spinco TA Audit on Windows), never inside the installation
// directory - so an installer upgrade can safely replace program files
// without touching real user data.
function ensureDataDirs() {
  const userData = app.getPath("userData");
  const dirs = {
    root: userData,
    database: path.join(userData, "database"),
    documents: path.join(userData, "documents"),
    logs: path.join(userData, "logs"),
  };
  for (const dir of Object.values(dirs)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  // Match storage.py's expected subfolders under STORAGE_ROOT so nothing
  // inside the backend needs to know these paths were created by Electron.
  for (const sub of ["expense_reports", "trip_reports", "excel"]) {
    fs.mkdirSync(path.join(dirs.documents, sub), { recursive: true });
  }
  return dirs;
}

function resourcesPath(...segments) {
  // In a packaged app, extraResources land in process.resourcesPath
  // (…\resources\). In dev this file's own directory stands in for it so
  // `npm run electron:dev` can still be pointed at a locally-built backend
  // for manual desktop-shell testing without a full package step.
  const base = app.isPackaged ? process.resourcesPath : path.join(__dirname, "..");
  return path.join(base, ...segments);
}

function backendExePath() {
  return resourcesPath("backend", "spinco-backend.exe");
}

function tesseractDirPath() {
  return resourcesPath("tesseract");
}

function frontendDistPath() {
  return resourcesPath("frontend");
}

// Release traceability (Part 9 of the spec this was built against): a
// packaged app has no .git directory to read from, so the commit it was
// built from has to be stamped in at build time (see
// electron/scripts/write-build-info.js) and read back here. Missing in dev
// mode (no packaging step has run) - callers get null, never a fabricated
// placeholder that would misrepresent what commit is actually running.
let cachedBuildInfo;
function getBuildInfo() {
  if (cachedBuildInfo !== undefined) return cachedBuildInfo;
  try {
    cachedBuildInfo = JSON.parse(fs.readFileSync(resourcesPath("build-info.json"), "utf8"));
  } catch {
    cachedBuildInfo = null;
  }
  return cachedBuildInfo;
}

ipcMain.handle("app:get-build-info", () => getBuildInfo());

function startBackend(dirs) {
  const exePath = backendExePath();
  if (!fs.existsSync(exePath)) {
    throw new Error(`Backend executable not found at ${exePath}. Run "npm run prepackage" first.`);
  }

  const logFile = path.join(dirs.logs, "backend.log");
  backendLogStream = fs.createWriteStream(logFile, { flags: "a" });
  backendLogStream.write(`\n----- launch ${new Date().toISOString()} -----\n`);

  const frontendDist = frontendDistPath();
  const frontendIndexExists = fs.existsSync(path.join(frontendDist, "index.html"));
  // Logged permanently (not just while debugging) - "backend started but the
  // UI is blank/404" is exactly the class of failure this line exists to
  // diagnose without needing a rebuild to add logging after the fact.
  backendLogStream.write(
    `resourcesPath=${process.resourcesPath}\n` +
      `frontendDist=${frontendDist} (index.html exists: ${frontendIndexExists})\n`
  );

  const env = {
    // SPINCO_GDRIVE_* and SPINCO_CENTRAL_STORAGE_MODE are stripped so the
    // packaged local backend always runs with central storage off.
    ...remoteConfig.sanitizeChildEnv(process.env),
    SPINCO_DB_PATH: path.join(dirs.database, "spinco_ta.db"),
    SPINCO_STORAGE_ROOT: dirs.documents,
    SPINCO_BACKEND_PORT: BACKEND_PORT,
    SPINCO_FRONTEND_DIST: frontendDist,
    // Cookies are Secure=false because the backend is only ever reached at
    // http://127.0.0.1 (loopback) here, never over a real network - see
    // app/api/auth.py's own SPINCO_COOKIE_SECURE handling for why this is
    // safe and how a real HTTPS deployment would flip it.
    SPINCO_COOKIE_SECURE: "false",
  };
  if (fs.existsSync(path.join(tesseractDirPath(), "tesseract.exe"))) {
    env.SPINCO_TESSERACT_DIR = tesseractDirPath();
  }
  // Hosted AI providers (NVIDIA OCR/text/classifier, DeepSeek) are always OFF
  // in the packaged app. `...process.env` above would otherwise hand the
  // backend whatever provider variables happen to exist in the launching
  // shell or machine environment, so a developer's leftover
  // SPINCO_*_ENABLED=true could silently start sending company documents to
  // an external service. Forced here rather than merely unset so that no
  // inherited value, in either direction, can enable them. Enabling a
  // provider in a packaged build needs a deliberate, separate change (with
  // secure credential storage) - see backend/.env.example.
  env.SPINCO_NVIDIA_ENABLED = "false";
  env.SPINCO_DEEPSEEK_ENABLED = "false";

  const child = spawn(exePath, [], {
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.pipe(backendLogStream, { end: false });
  child.stderr.pipe(backendLogStream, { end: false });
  return child;
}

function waitForHealth(timeoutMs, healthUrl = HEALTH_URL) {
  const httpClient = healthUrl.startsWith("https:") ? https : http;
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    let backendExitedEarly = false;

    function onEarlyExit(code) {
      backendExitedEarly = true;
      reject(new Error(`Backend process exited before becoming ready (code ${code}). See backend.log.`));
    }
    if (backendProcess) backendProcess.once("exit", onEarlyExit);

    function poll() {
      if (backendExitedEarly) return;
      if (Date.now() > deadline) {
        if (backendProcess) backendProcess.off("exit", onEarlyExit);
        reject(new Error("Backend did not become ready in time."));
        return;
      }
      const req = httpClient.get(healthUrl, { timeout: 2000 }, (res) => {
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => {
          // Not just status 200 - some other process could already own this
          // port and happily answer any GET with a 200. Confirm it's
          // actually our own /api/health (see app/api/health.py) before
          // treating the backend as ready.
          let isOurBackend = false;
          try {
            isOurBackend = res.statusCode === 200 && JSON.parse(body).status === "ok";
          } catch {
            isOurBackend = false;
          }
          if (isOurBackend) {
            if (backendProcess) backendProcess.off("exit", onEarlyExit);
            resolve();
          } else {
            setTimeout(poll, READINESS_POLL_MS);
          }
        });
      });
      req.on("error", () => setTimeout(poll, READINESS_POLL_MS));
      req.on("timeout", () => {
        req.destroy();
        setTimeout(poll, READINESS_POLL_MS);
      });
    }
    poll();
  });
}

function stopBackend() {
  if (backendProcess && !backendProcess.killed) {
    try {
      backendProcess.kill();
    } catch {
      // best-effort - the process may have already exited
    }
  }
  backendProcess = null;
  if (backendLogStream) {
    backendLogStream.end();
    backendLogStream = null;
  }
}

function isHttpUrl(url) {
  try {
    const p = new URL(url).protocol;
    return p === "http:" || p === "https:";
  } catch {
    return false;
  }
}

// The window title makes the environment unmistakable: a remote window always carries a tag.
function remoteTitle(remote) {
  const baseTitle = "Spinco TA Audit";
  if (!remote) return baseTitle;
  if (remote.insecure) return `${baseTitle} [DEVELOPMENT - INSECURE - SYNTHETIC DATA ONLY]`;
  const tags = { staging: "STAGING", development: "DEVELOPMENT", production: "PRODUCTION" };
  return `${baseTitle} [${tags[remote.environment] || "CENTRAL SERVER"}]`;
}

// `remote` is null in local mode (behaviour unchanged) or
// { origin, insecure, environment } in remote central mode (origin is null when
// the configuration is invalid: nothing may be navigated to then).
function createWindow(remote = null) {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1200,
    minHeight: 700,
    show: false,
    title: remoteTitle(remote),
    icon: path.join(__dirname, "build", "icon.ico"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  // Any link the app tries to open in a new window (e.g. target="_blank")
  // opens in the user's real browser instead of a second Electron window -
  // this app never needs a second window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    // Remote mode: only http(s) URLs may be handed to the OS (a custom-scheme
    // URL from a remote page must never reach shell.openExternal). Local mode
    // keeps the original behaviour.
    if (!remote || isHttpUrl(url)) shell.openExternal(url);
    return { action: "deny" };
  });

  if (remote) {
    // Restrict in-app navigation to the configured central origin; anything
    // else opens in the user's browser (http/https only) or is dropped.
    const guardNavigation = (event, url) => {
      let origin = null;
      try {
        origin = new URL(url).origin;
      } catch {
        origin = null;
      }
      if (origin === remote.origin) return;
      event.preventDefault();
      if (isHttpUrl(url)) shell.openExternal(url);
    };
    mainWindow.webContents.on("will-navigate", guardNavigation);
    mainWindow.webContents.on("will-redirect", guardNavigation);
    // Keep the environment marker visible: don't let the page's own <title> replace it.
    mainWindow.on("page-title-updated", (event) => event.preventDefault());
  }
}

function showStartupError(message) {
  dialog.showErrorBox(
    "Spinco TA Audit could not start",
    `${message}\n\nCheck the log file for details:\n${path.join(app.getPath("userData"), "logs", "backend.log")}`
  );
}

// Remote mode never starts updater.js (no feed URL, no autoUpdater wiring), but
// the renderer's Settings page still invokes these channels, so register inert
// handlers that report "unconfigured". Guarded because launch() can run again
// (macOS "activate") and ipcMain.handle throws on duplicate registration.
let remoteIpcRegistered = false;
function registerInertUpdaterHandlers() {
  if (remoteIpcRegistered) return;
  remoteIpcRegistered = true;
  const status = () => ({
    state: "unconfigured",
    currentVersion: app.getVersion(),
    latestVersion: null,
    progress: null,
    error: null,
    checkedAt: null,
  });
  ipcMain.handle("app:get-version", () => app.getVersion());
  ipcMain.handle("updater:status", status);
  ipcMain.handle("updater:check", status);
  ipcMain.handle("updater:download", status);
  ipcMain.handle("updater:install", status);
}

function appendRemoteLog(message) {
  // Host-only, never full URLs or file contents.
  try {
    const logsDir = path.join(app.getPath("userData"), "logs");
    fs.mkdirSync(logsDir, { recursive: true });
    fs.appendFileSync(path.join(logsDir, "remote-mode.log"), `${new Date().toISOString()} ${message}\n`);
  } catch {
    // logging is best-effort
  }
}

// ---------- Remote mode: connection flow, diagnostics page and its IPC ----------

function isConnectionErrorSender(event) {
  // Only the shell's OWN local error page may use these channels - never the central server's page.
  try {
    const url = new URL((event.senderFrame && event.senderFrame.url) || event.sender.getURL());
    return url.protocol === "file:" && decodeURIComponent(url.pathname).toLowerCase().endsWith("/connection-error.html");
  } catch {
    return false;
  }
}

function showConnectionError(result, cfg) {
  const { ok, category, title, message, advice, host, httpStatus, attempts } = result;
  connectionState = {
    ok,
    category,
    title,
    message,
    advice,
    host: host || (cfg && cfg.host) || "",
    httpStatus: httpStatus || null,
    attempts: attempts || 1,
    source: cfg ? cfg.source : null,
    environment: cfg ? cfg.environment || null : null,
    insecure: !!(cfg && cfg.insecure),
    allowManual: category === diagnostics.CATEGORY.NO_HOST_FOUND, // LAN demo: the error page then offers a manual host address
    checkedAt: new Date().toISOString(),
  };
  connectionInfo = { mode: "remote", connected: false, host: connectionState.host, environment: connectionState.environment, insecure: connectionState.insecure };
  appendRemoteLog(`connection problem: category=${category} host=${connectionState.host || "-"} attempts=${connectionState.attempts}`);
  if (mainWindow) mainWindow.loadFile(CONNECTION_ERROR_PAGE);
}

function resolveMode() {
  return remoteConfig.resolveBackendMode({
    env: process.env,
    userDataDir: app.getPath("userData"),
    machineDataDir: MACHINE_CONFIG_DIR,
    demoDefaultsPath: app.isPackaged ? path.join(process.resourcesPath, "demo-defaults.json") : null, // present only in the LAN-demo build
    readFile: (p) => fs.readFileSync(p, "utf8"),
  });
}

function loadingPage(text) {
  return `data:text/html,${encodeURIComponent(
    `<body style="background:#0a1628;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">${text}</body>`
  )}`;
}

let reconnectHandlerInstalled = false;

// One connection attempt (with retries for up to REMOTE_CONNECT_TIMEOUT_MS). On failure the diagnostics page is shown;
// the app NEVER falls back to local mode.
async function attemptConnection(cfg) {
  if (!mainWindow) return;
  mainWindow.loadURL(loadingPage("Connecting to the Spinco TA Audit central server..."));
  const result = await diagnostics.waitForBackend(cfg.url, {
    totalMs: REMOTE_CONNECT_TIMEOUT_MS,
    onAttempt: (r, n) => appendRemoteLog(`health attempt ${n}: ${r.category} host=${r.host}`),
  });
  if (!result.ok) {
    showConnectionError(result, cfg);
    return;
  }
  appendRemoteLog(`health check ok for host=${cfg.host}`);
  connectionInfo = { mode: "remote", connected: true, host: cfg.host, environment: cfg.environment || null, insecure: cfg.insecure };
  connectionState = null;
  if (!mainWindow) return;

  if (!reconnectHandlerInstalled) {
    reconnectHandlerInstalled = true;
    // The server was reachable at start-up but a later load (reload, navigation) failed: say WHY instead of Chromium's raw error page.
    mainWindow.webContents.on("did-fail-load", async (_event, errorCode, _desc, validatedURL, isMainFrame) => {
      if (!isMainFrame || errorCode === -3 /* ERR_ABORTED */ || !activeRemoteConfig) return;
      let origin = null;
      try {
        origin = new URL(validatedURL).origin;
      } catch {
        return;
      }
      if (origin !== activeRemoteConfig.url) return;
      const probe = await diagnostics.probeBackend(activeRemoteConfig.url, { timeoutMs: 5000 });
      showConnectionError(probe.ok ? diagnostics.describe(diagnostics.CATEGORY.BACKEND_UNAVAILABLE, activeRemoteConfig.host) : probe, activeRemoteConfig);
    });
  }
  mainWindow.loadURL(cfg.url);
}

async function launchRemote(cfg) {
  // Remote central mode: no local backend, no local database, no documents
  // folder. Only the logs directory may be created (for remote-mode.log).
  registerInertUpdaterHandlers();
  activeRemoteConfig = cfg;
  appendRemoteLog(`remote mode: host=${cfg.host} source=${cfg.source} environment=${cfg.environment || "unspecified"} insecure=${cfg.insecure}`);

  createWindow({ origin: cfg.url, insecure: cfg.insecure, environment: cfg.environment });

  if (cfg.insecure && !insecureWarningShown) {
    insecureWarningShown = true;
    // ONE non-blocking warning: no parent window and not awaited, so it never
    // blocks startup.
    dialog.showMessageBox({
      type: "warning",
      title: "Development-only connection",
      message: "Insecure (non-HTTPS) central backend",
      detail:
        `This app is connected to ${cfg.host} over plain HTTP. This is for development only ` +
        "and must be used with synthetic data. Do not enter real company data or credentials.",
      buttons: ["OK"],
    });
  }

  await attemptConnection(cfg);
}

// ---------- LAN demo: automatic discovery of the demo host (mDNS), with a manual-address fallback ----------
// The window's navigation guard reads `.origin` at call time, so it can be set once a host is found.
const demoGuard = { origin: null, insecure: true, environment: "staging" };
const DEMO_HOST_FILE = () => path.join(app.getPath("userData"), "demo-host.json"); // the manual fallback only; never the company settings file

function savedDemoHost() {
  try {
    const parsed = lanDiscovery.parseManualAddress(JSON.parse(fs.readFileSync(DEMO_HOST_FILE(), "utf8")).address);
    return parsed.ok ? parsed : null;
  } catch {
    return null;
  }
}

async function connectToDemoHost(url, host, source) {
  demoGuard.origin = url;
  const cfg = { mode: "remote", url, host, source, insecure: true, environment: "staging" };
  activeRemoteConfig = cfg;
  appendRemoteLog(`demo host: host=${host} source=${source}`);
  await attemptConnection(cfg);
}

async function discoverAndConnect() {
  if (!mainWindow) return;
  mainWindow.loadURL(loadingPage("Searching for the Spinco demo host on this network..."));
  let hosts = [];
  try {
    hosts = await lanDiscovery.discoverDemoHosts({ timeoutMs: 8000 });
  } catch (err) {
    appendRemoteLog(`discovery error: ${err && err.code ? err.code : "unknown"}`);
  }
  appendRemoteLog(`discovery: ${hosts.length} verified host(s)`);
  if (hosts.length > 0) {
    await connectToDemoHost(hosts[0].url, hosts[0].host, "discovery");
    return;
  }
  const saved = savedDemoHost(); // a host the participant typed earlier
  if (saved) {
    const probe = await diagnostics.probeBackend(saved.url, { timeoutMs: 3000 });
    if (probe.ok) {
      await connectToDemoHost(saved.url, saved.host, "manual");
      return;
    }
  }
  showConnectionError(diagnostics.describe(diagnostics.CATEGORY.NO_HOST_FOUND, ""), { source: "discovery", insecure: true, environment: "staging", host: "" });
}

async function launchDiscover() {
  registerInertUpdaterHandlers();
  appendRemoteLog("LAN demo mode: searching for a demo host");
  createWindow(demoGuard);
  if (!insecureWarningShown) {
    insecureWarningShown = true;
    dialog.showMessageBox({
      type: "warning",
      title: "LAN demo connection",
      message: "Demo connection (plain HTTP on the local network)",
      detail: "This app is looking for the Spinco demo host on your Wi-Fi. Use SYNTHETIC documents only and your own demo account. Do not enter real company data or passwords you use elsewhere.",
      buttons: ["OK"],
    });
  }
  await discoverAndConnect();
}

// The configuration is unusable: show it in the diagnostics page (with a way to open the settings folder) - never start local mode.
function launchInvalidConfig(reason) {
  registerInertUpdaterHandlers();
  appendRemoteLog("invalid central server configuration");
  createWindow({ origin: null, insecure: false, environment: null });
  showConnectionError(diagnostics.invalidConfigResult(reason), null);
}

let connectionIpcRegistered = false;
function registerShellIpc() {
  if (connectionIpcRegistered) return;
  connectionIpcRegistered = true;

  // Which server this window talks to (safe for any page: no URL, path or credential). The React app uses it for the connection/environment banner.
  ipcMain.handle("app:get-connection-info", () => connectionInfo);

  // The channels below belong to the shell's own error page only.
  ipcMain.handle("connection:get-state", (event) => (isConnectionErrorSender(event) ? connectionState : null));
  ipcMain.handle("connection:retry", async (event) => {
    if (!isConnectionErrorSender(event)) return false;
    // Re-read the configuration first: an administrator may have just corrected it.
    const mode = resolveMode();
    if (mode.mode === "invalid") {
      showConnectionError(diagnostics.invalidConfigResult(mode.reason), null);
      return true;
    }
    if (mode.mode === "discover") {
      await discoverAndConnect();
      return true;
    }
    if (mode.mode !== "remote") {
      showConnectionError(
        diagnostics.invalidConfigResult("No central server is configured any more. Close and reopen the app to use local mode."),
        null
      );
      return true;
    }
    activeRemoteConfig = mode;
    await attemptConnection(mode);
    return true;
  });
  // LAN demo manual fallback: the participant types the host address shown on the host laptop. Validated (private/local address, host:port only), probed, remembered.
  ipcMain.handle("connection:manual-connect", async (event, address) => {
    if (!isConnectionErrorSender(event)) return { ok: false, reason: "Not allowed." };
    const parsed = lanDiscovery.parseManualAddress(address);
    if (!parsed.ok) return { ok: false, reason: parsed.reason };
    if (mainWindow) mainWindow.loadURL(loadingPage("Connecting to " + parsed.host + "..."));
    const probe = await diagnostics.probeBackend(parsed.url, { timeoutMs: 5000 });
    if (!probe.ok) {
      showConnectionError(probe, { source: "manual", insecure: true, environment: "staging", host: parsed.host });
      return { ok: false, reason: probe.message };
    }
    try {
      fs.mkdirSync(app.getPath("userData"), { recursive: true });
      fs.writeFileSync(DEMO_HOST_FILE(), JSON.stringify({ address: parsed.host }));
    } catch {
      // remembering is best effort
    }
    await connectToDemoHost(parsed.url, parsed.host, "manual");
    return { ok: true };
  });
  ipcMain.handle("connection:open-settings-folder", (event) => {
    if (!isConnectionErrorSender(event)) return false;
    const dir = app.getPath("userData");
    fs.mkdirSync(dir, { recursive: true });
    shell.openPath(dir);
    return true;
  });
  ipcMain.handle("connection:quit", (event) => {
    if (!isConnectionErrorSender(event)) return false;
    app.quit();
    return true;
  });
}
async function launch() {
  registerShellIpc();
  const mode = resolveMode();

  if (mode.mode === "invalid") {
    // The user clearly tried to configure remote mode: never fall back to
    // local mode (and its separate local database) silently. The reason is
    // shown on the diagnostics page, which can open the settings folder.
    launchInvalidConfig(mode.reason);
    return;
  }

  if (mode.mode === "remote") {
    await launchRemote(mode);
    return;
  }

  if (mode.mode === "discover") {
    await launchDiscover(); // LAN demo: find the host automatically - still never a local backend or database
    return;
  }

  const dirs = ensureDataDirs();

  if (!app.isPackaged) {
    createWindow();
    mainWindow.loadURL(DEV_FRONTEND_URL);
    // Registers app:get-version/updater:* IPC handlers so the Settings page
    // works in the web-dev workflow too (reporting "unconfigured" - there is
    // no app-update.yml in a dev checkout, so no real update check happens).
    setupAutoUpdater({ mainWindow, logFilePath: path.join(dirs.logs, "updater.log"), feedURLOverride: null });
    return;
  }

  try {
    backendProcess = startBackend(dirs);
  } catch (err) {
    showStartupError(err.message);
    app.quit();
    return;
  }

  createWindow();
  mainWindow.loadURL(
    `data:text/html,${encodeURIComponent(
      "<body style=\"background:#0a1628;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0\">Starting Spinco TA Audit...</body>"
    )}`
  );

  try {
    await waitForHealth(READINESS_TIMEOUT_MS);
  } catch (err) {
    showStartupError(err.message);
    stopBackend();
    app.quit();
    return;
  }

  if (mainWindow) {
    mainWindow.loadURL(BACKEND_URL);
  }

  // Self-hosted update feed (see electron/updater.js). The feed URL baked
  // into the build (package.json's build.publish.url) is a placeholder
  // until real hosting is provisioned - SPINCO_UPDATE_FEED_URL overrides it
  // at runtime without requiring a rebuild.
  setupAutoUpdater({
    mainWindow,
    logFilePath: path.join(dirs.logs, "updater.log"),
    feedURLOverride: process.env.SPINCO_UPDATE_FEED_URL || null,
  });
  checkForUpdatesOnStartup();
}

app.whenReady().then(() => {
  if (gotLock) launch();
});

app.on("window-all-closed", () => {
  stopBackend();
  app.quit();
});

app.on("before-quit", () => {
  stopBackend();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) launch();
});
