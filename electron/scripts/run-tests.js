"use strict";

/**
 * Runs the backend test suite from backend/ (via backend/.venv), the same
 * way build-backend.js runs PyInstaller - so `npm run dist` fails fast on a
 * real test failure instead of packaging and shipping broken code. See
 * Part 7/10 of the update-system spec this was built against: "Run tests"
 * is step 1 of the production build sequence, not an optional extra.
 */

const path = require("node:path");
const fs = require("node:fs");
const { spawnSync } = require("node:child_process");

const BACKEND_DIR = path.join(__dirname, "..", "..", "backend");
const VENV_PYTHON = path.join(BACKEND_DIR, ".venv", "Scripts", "python.exe");

function main() {
  if (!fs.existsSync(VENV_PYTHON)) {
    console.error(`[run-tests] Python venv not found at ${VENV_PYTHON}`);
    process.exit(1);
  }

  const result = spawnSync(VENV_PYTHON, ["-m", "pytest", "tests", "-q"], { cwd: BACKEND_DIR, stdio: "inherit" });

  if (result.status !== 0) {
    console.error("[run-tests] Backend test suite failed.");
    process.exit(result.status || 1);
  }
  console.log("[run-tests] Backend test suite passed.");
}

main();
