"use strict";

/**
 * Removes every build output directory before a production build, so a
 * fresh `npm run dist` can never accidentally package stale files left over
 * from a previous build (see docs/BUILD_AND_RELEASE.md - "win-unpacked was
 * rebuilt but the installer .exe next to it was left over from an older
 * build" was an actual incident this guards against).
 *
 * Deliberately narrow: only known build-output directories are removed.
 * Source, node_modules, the database, storage/, and backups/ are never
 * touched by this script.
 */

const fs = require("node:fs");
const path = require("node:path");

const REPO_ROOT = path.join(__dirname, "..", "..");

const TARGETS = [
  path.join(REPO_ROOT, "frontend", "dist"),
  path.join(REPO_ROOT, "backend", "dist_backend"),
  path.join(REPO_ROOT, "backend", "build"),
  path.join(REPO_ROOT, "release"),
  path.join(REPO_ROOT, "electron", "generated"),
];

// Safety guard: release/ holds the official release artifacts. A non-empty one is never deleted unless the caller
// deliberately passes --force (npm run clean -- --force). Checked BEFORE anything is removed, so a refusal deletes nothing.
const RELEASE_DIR = path.join(REPO_ROOT, "release");
const FORCE = process.argv.slice(2).includes("--force");

function isNonEmptyDir(dir) {
  try {
    return fs.readdirSync(dir).length > 0;
  } catch {
    return false; // missing (or not a directory): nothing to protect
  }
}

if (!FORCE && isNonEmptyDir(RELEASE_DIR)) {
  console.error(`[clean] REFUSED: ${RELEASE_DIR} is not empty. Nothing was deleted.`);
  console.error(`[clean] It holds release artifacts. To delete it on purpose run:  npm run clean -- --force`);
  process.exit(1);
}

for (const target of TARGETS) {
  if (fs.existsSync(target)) {
    fs.rmSync(target, { recursive: true, force: true });
    console.log(`[clean] removed ${target}`);
  }
}
