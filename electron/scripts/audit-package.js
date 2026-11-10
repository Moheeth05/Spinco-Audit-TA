"use strict";

/**
 * Packaging audit for a built (unpacked) Windows app: proves the installer content is what it should be, and - above all - that no credential, database or
 * document can have been packaged. Run after electron-builder:
 *
 *     node electron/scripts/audit-package.js <path to win-unpacked> [--json <report file>]
 *
 * It checks, and exits non-zero on the first category that fails:
 *   1. REQUIRED  the app archive (resources/app.asar) contains the shell files the remote (central server) mode needs, and the frontend build and backend are present;
 *   2. FORBIDDEN no database, no Google credential / token file, no environment file, no document store, no desktop-side server settings file anywhere in the app;
 *   3. CONTENT   no text or binary file contains a private key, a Google OAuth client secret / access / refresh token, or a path into the secrets folder.
 * It never prints a matched value, only the file and the rule that matched.
 */

const fs = require("node:fs");
const path = require("node:path");

const REQUIRED_IN_ASAR = [
  "electron/main.js",
  "electron/preload.js",
  "electron/updater.js",
  "electron/remote-config.js",
  "electron/connection-diagnostics.js",
  "electron/connection-error.html",
  "package.json",
];
const REQUIRED_ON_DISK = ["resources/app.asar", "resources/backend", "resources/frontend/index.html", "resources/build-info.json"];

// File NAMES that must never be inside the package.
const FORBIDDEN_NAMES = [
  [/\.(db|sqlite|sqlite3|db-wal|db-shm)$/i, "a database file"],
  [/^spinco_ta\.db/i, "the application database"],
  [/(^|[._-])(client[_-]?secret|oauth[_-]?token|service[_-]?account|credentials?)([._-].*)?\.json$/i, "a Google credential file"],
  [/^spinco-drive-/i, "a Spinco Drive credential file"],
  [/\.(pem|key|pfx|p12)$/i, "a key / certificate file"],
  [/^\.env(\..*)?$/i, "an environment file"],
  [/^central-backend\.json$/i, "a desktop server-settings file (settings belong on each PC, not in the installer)"],
];
// Whole folders that must never be inside the package.
const FORBIDDEN_DIRS = [/^(uat-data-test|experiments|storage|documents|excel)$/i, /^SPINCO-SECRETS$/i];

// Third-party packages legitimately ship PEM test fixtures / certificate bundles; those exact paths are not credentials.
const ALLOWED_PATH_FRAGMENTS = [
  /[\\/]certifi[\\/]/i,
  /[\\/]cryptography[\\/]/i,
  /[\\/]node_modules[\\/]/i,
  /[\\/]cacert\.pem$/i,
  /[\\/]tesseract[\\/]/i,
];

const CONTENT_RULES = [
  [/-----BEGIN (RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----/, "a PEM private key"],
  [/"private_key"\s*:\s*"-----BEGIN/, "a service-account private key"],
  [/GOCSPX-[A-Za-z0-9_-]{20,}/, "a Google OAuth client secret"],
  [/\bya29\.[A-Za-z0-9_-]{30,}/, "a Google access token"],
  [/\b1\/\/0[A-Za-z0-9_-]{30,}/, "a Google refresh token"],
  [/"refresh_token"\s*:\s*"[A-Za-z0-9_\/-]{20,}"/, "a refresh token"],
  [/SPINCO-SECRETS/i, "a path into the secrets folder"],
];
const SCAN_MAX_BYTES = 64 * 1024 * 1024;

function walk(dir, visit) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      visit(full, true);
      walk(full, visit);
    } else if (entry.isFile()) {
      visit(full, false);
    }
  }
}

function main() {
  const args = process.argv.slice(2);
  const jsonIdx = args.indexOf("--json");
  const jsonOut = jsonIdx >= 0 ? args[jsonIdx + 1] : null;
  const root = args.find((a, i) => !a.startsWith("--") && !(jsonIdx >= 0 && i === jsonIdx + 1));
  if (!root || !fs.existsSync(root)) {
    console.error("usage: node electron/scripts/audit-package.js <win-unpacked folder> [--json report.json]");
    process.exit(2);
  }
  const report = { root, required: [], forbidden: [], content: [], filesScanned: 0, bytesScanned: 0, skippedLarge: [] };
  let failed = false;

  // 1. required
  for (const rel of REQUIRED_ON_DISK) {
    const ok = fs.existsSync(path.join(root, rel));
    report.required.push({ item: rel, ok });
    if (!ok) failed = true;
  }
  let asarEntries = [];
  try {
    const asar = require("@electron/asar");
    asarEntries = asar.listPackage(path.join(root, "resources", "app.asar")).map((p) => p.replace(/\\/g, "/").replace(/^\//, ""));
  } catch (err) {
    console.error("[audit-package] could not read app.asar: " + err.message);
    failed = true;
  }
  for (const rel of REQUIRED_IN_ASAR) {
    const ok = asarEntries.includes(rel);
    report.required.push({ item: "app.asar:" + rel, ok });
    if (!ok) failed = true;
  }
  // Nothing from the asar's own list may be a forbidden name either.
  for (const entry of asarEntries) {
    const base = entry.split("/").pop();
    for (const [re, why] of FORBIDDEN_NAMES) if (re.test(base)) {
      report.forbidden.push({ file: "app.asar:" + entry, rule: why });
      failed = true;
    }
  }

  // 2 + 3. every file on disk
  walk(root, (full, isDir) => {
    const rel = path.relative(root, full);
    const base = path.basename(full);
    if (isDir) {
      if (FORBIDDEN_DIRS.some((re) => re.test(base)) && !ALLOWED_PATH_FRAGMENTS.some((re) => re.test(full + path.sep))) {
        report.forbidden.push({ file: rel + path.sep, rule: "a data / secrets folder" });
        failed = true;
      }
      return;
    }
    if (!ALLOWED_PATH_FRAGMENTS.some((re) => re.test(full))) {
      for (const [re, why] of FORBIDDEN_NAMES) {
        if (re.test(base)) {
          report.forbidden.push({ file: rel, rule: why });
          failed = true;
        }
      }
    }
    const size = fs.statSync(full).size;
    if (size === 0) return;
    if (size > SCAN_MAX_BYTES) {
      report.skippedLarge.push(`${rel} (${(size / 1048576).toFixed(0)} MB)`); // browser / runtime binaries; named in the report, not silently ignored
      return;
    }
    report.filesScanned += 1;
    report.bytesScanned += size;
    const text = fs.readFileSync(full).toString("latin1"); // byte-transparent: finds ASCII secrets inside executables and archives alike
    for (const [re, why] of CONTENT_RULES) {
      if (ALLOWED_PATH_FRAGMENTS.some((a) => a.test(full)) && why.includes("PEM")) continue;
      if (re.test(text)) {
        report.content.push({ file: rel, rule: why });
        failed = true;
      }
    }
  });

  console.log(`[audit-package] scanned ${report.filesScanned} files (${(report.bytesScanned / 1048576).toFixed(0)} MB) in ${root}`);
  for (const s of report.skippedLarge) console.log(`[audit-package] not content-scanned (over ${SCAN_MAX_BYTES / 1048576} MB): ${s}`);
  for (const r of report.required) console.log(`[audit-package] ${r.ok ? "OK     " : "MISSING"} ${r.item}`);
  for (const f of report.forbidden) console.log(`[audit-package] FORBIDDEN ${f.file}  (${f.rule})`);
  for (const c of report.content) console.log(`[audit-package] SECRET-PATTERN ${c.file}  (${c.rule})`);
  report.passed = !failed;
  if (jsonOut) fs.writeFileSync(jsonOut, JSON.stringify(report, null, 2));
  console.log(failed ? "[audit-package] FAILED" : "[audit-package] PASSED: required files present; no database, credential, token, key or document store in the package.");
  process.exit(failed ? 1 : 0);
}

main();
