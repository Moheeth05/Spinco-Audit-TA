"use strict";

/**
 * Phase 11 - runs PyInstaller (via backend/.venv) to produce
 * backend/dist_backend/spinco-backend/spinco-backend.exe, the executable
 * electron/main.js spawns in the packaged app. See backend/spinco-backend.spec
 * for what gets bundled and why.
 *
 * Requires the backend venv to already have pyinstaller installed:
 *   backend\.venv\Scripts\pip install -r requirements-build.txt
 */

const path = require("node:path");
const fs = require("node:fs");
const { spawnSync } = require("node:child_process");

const BACKEND_DIR = path.join(__dirname, "..", "..", "backend");
const VENV_PYTHON = path.join(BACKEND_DIR, ".venv", "Scripts", "python.exe");

// Optional: SPINCO_BUILD_DIR redirects PyInstaller's output to <dir>\dist_backend and its work files to <dir>\build
// (used by the staging workflow, electron/scripts/dist-staging.js). Unset = backend/dist_backend and backend/build, as always.
const BUILD_DIR = process.env.SPINCO_BUILD_DIR ? path.resolve(process.env.SPINCO_BUILD_DIR) : null;
const DIST_PATH = BUILD_DIR ? path.join(BUILD_DIR, "dist_backend") : "dist_backend";
const WORK_PATH = BUILD_DIR ? path.join(BUILD_DIR, "build") : "build";

function main() {
  if (!fs.existsSync(VENV_PYTHON)) {
    console.error(`[build-backend] Python venv not found at ${VENV_PYTHON}`);
    process.exit(1);
  }

  const result = spawnSync(
    VENV_PYTHON,
    ["-m", "PyInstaller", "spinco-backend.spec", "--distpath", DIST_PATH, "--workpath", WORK_PATH, "--noconfirm"],
    { cwd: BACKEND_DIR, stdio: "inherit" }
  );

  if (result.status !== 0) {
    console.error("[build-backend] PyInstaller build failed.");
    process.exit(result.status || 1);
  }

  const exePath = path.resolve(BACKEND_DIR, DIST_PATH, "spinco-backend", "spinco-backend.exe");
  if (!fs.existsSync(exePath)) {
    console.error(`[build-backend] Expected output not found at ${exePath}`);
    process.exit(1);
  }

  console.log(`[build-backend] Built ${exePath}`);
}

main();
