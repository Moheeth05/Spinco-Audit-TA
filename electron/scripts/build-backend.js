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

function main() {
  if (!fs.existsSync(VENV_PYTHON)) {
    console.error(`[build-backend] Python venv not found at ${VENV_PYTHON}`);
    process.exit(1);
  }

  const result = spawnSync(
    VENV_PYTHON,
    ["-m", "PyInstaller", "spinco-backend.spec", "--distpath", "dist_backend", "--workpath", "build", "--noconfirm"],
    { cwd: BACKEND_DIR, stdio: "inherit" }
  );

  if (result.status !== 0) {
    console.error("[build-backend] PyInstaller build failed.");
    process.exit(result.status || 1);
  }

  const exePath = path.join(BACKEND_DIR, "dist_backend", "spinco-backend", "spinco-backend.exe");
  if (!fs.existsSync(exePath)) {
    console.error(`[build-backend] Expected output not found at ${exePath}`);
    process.exit(1);
  }

  console.log(`[build-backend] Built ${exePath}`);
}

main();
