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
const { spawn } = require("node:child_process");
const { setupAutoUpdater, checkForUpdatesOnStartup } = require("./updater");

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

let mainWindow = null;
let backendProcess = null;
let backendLogStream = null;

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
    ...process.env,
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

function waitForHealth(timeoutMs) {
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
      const req = http.get(HEALTH_URL, { timeout: 2000 }, (res) => {
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

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1200,
    minHeight: 700,
    show: false,
    title: "Spinco TA Audit",
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
    shell.openExternal(url);
    return { action: "deny" };
  });
}

function showStartupError(message) {
  dialog.showErrorBox(
    "Spinco TA Audit could not start",
    `${message}\n\nCheck the log file for details:\n${path.join(app.getPath("userData"), "logs", "backend.log")}`
  );
}

async function launch() {
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
