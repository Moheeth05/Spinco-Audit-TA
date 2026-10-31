"use strict";

/**
 * Stamps the build with what it actually is: the app version being built and
 * the exact git commit it was built from, written once at build time (never
 * at runtime - a packaged app has no .git directory to read from). Consumed
 * by electron/main.js (as an extraResource, see package.json) and surfaced
 * in Settings for release traceability - see Part 9 of the update-system
 * spec this was built against.
 *
 * Refuses to silently stamp "unknown" for the commit: if git isn't
 * available or the tree can't be inspected, that's a real build-environment
 * problem, not something to paper over with a placeholder that would then
 * be shipped to users as if it were real traceability data.
 */

const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const REPO_ROOT = path.join(__dirname, "..", "..");
// Optional: SPINCO_BUILD_INFO_DIR writes build-info.json there instead of electron/generated (staging workflow).
const OUT_DIR = process.env.SPINCO_BUILD_INFO_DIR
  ? path.resolve(process.env.SPINCO_BUILD_INFO_DIR)
  : path.join(REPO_ROOT, "electron", "generated");
const OUT_FILE = path.join(OUT_DIR, "build-info.json");

function git(args) {
  return execFileSync("git", args, { cwd: REPO_ROOT, encoding: "utf8" }).trim();
}

function main() {
  const pkg = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, "package.json"), "utf8"));

  let commit;
  let dirty = false;
  try {
    commit = git(["rev-parse", "--short", "HEAD"]);
    dirty = git(["status", "--porcelain"]).length > 0;
  } catch (err) {
    console.error(`[write-build-info] Could not read git commit: ${err.message}`);
    process.exit(1);
  }

  const buildInfo = {
    version: pkg.version,
    gitCommit: commit,
    // A build made from a working tree with uncommitted changes is real
    // (e.g. testing this exact pipeline before those changes are committed)
    // but must never be presented as if it traces to a clean, reproducible
    // commit - the dirty flag is how Settings/About tells the two apart.
    dirty,
    buildDate: new Date().toISOString(),
  };

  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.writeFileSync(OUT_FILE, JSON.stringify(buildInfo, null, 2));
  console.log(`[write-build-info] wrote ${OUT_FILE}`);
  console.log(`[write-build-info] ${JSON.stringify(buildInfo)}`);
}

main();
