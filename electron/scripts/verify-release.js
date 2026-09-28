"use strict";

/**
 * Post-build sanity check: proves the just-produced release/ artifacts
 * actually came from THIS build, not a stale one left over from earlier
 * (see electron/scripts/clean.js's header for the incident this whole
 * chain - clean -> write-build-info -> build -> verify-release - exists to
 * prevent). A green result here is what "the release is current" actually
 * means; nothing upstream of this script gets to claim that on its own.
 *
 * Checks, all against electron/generated/build-info.json's buildDate as the
 * single point of truth for "when did this build start":
 *   - build-info.json's version matches package.json's version
 *   - release/latest.yml's version matches package.json's version
 *   - the installer named in latest.yml exists in release/
 *   - frontend/dist, the backend exe, and the installer were all written
 *     AFTER the build started (i.e. are not left over from a prior build)
 */

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const yaml = require("js-yaml");

const REPO_ROOT = path.join(__dirname, "..", "..");
const RELEASE_DIR = path.join(REPO_ROOT, "release");
const UNPACKED_DIR = path.join(RELEASE_DIR, "win-unpacked");

function fail(msg) {
  console.error(`[verify-release] FAIL: ${msg}`);
  process.exitCode = 1;
}

function mtime(p) {
  return fs.statSync(p).mtime;
}

function sha256(p) {
  return crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex");
}

function reportCheck(label, ok, detail) {
  console.log(`  ${label}: ${ok ? "PASS" : "FAIL"}${detail ? ` (${detail})` : ""}`);
  if (!ok) process.exitCode = 1;
  return ok;
}

// Electron ships a handful of Chromium runtime DLLs (ffmpeg.dll among them,
// needed for <video>/<audio> decoding) alongside its own binary in
// node_modules/electron/dist/ - electron-builder is supposed to copy the
// entire dist/ into win-unpacked/ verbatim. This check proves that actually
// happened for THIS build, byte-for-byte, rather than trusting that it
// always does - see docs/RELEASE_AND_UPDATES.md for the incident this
// guards against ("Spinco TA Audit.exe - System Error: ffmpeg.dll was not
// found").
function verifyElectronRuntime() {
  console.log("\nElectron runtime verification:");
  const electronDist = path.join(REPO_ROOT, "node_modules", "electron", "dist");

  const ffmpegSrc = path.join(electronDist, "ffmpeg.dll");
  const ffmpegDst = path.join(UNPACKED_DIR, "ffmpeg.dll");
  if (!fs.existsSync(ffmpegSrc)) {
    reportCheck("ffmpeg.dll", false, `source missing at ${ffmpegSrc} - the electron package itself has no ffmpeg.dll; reinstall node_modules/electron`);
  } else if (!fs.existsSync(ffmpegDst)) {
    reportCheck("ffmpeg.dll", false, `missing from packaged output at ${ffmpegDst}`);
  } else {
    const same = sha256(ffmpegSrc) === sha256(ffmpegDst);
    reportCheck("ffmpeg.dll", same, same ? `byte-identical to node_modules/electron/dist/ffmpeg.dll` : "present but does NOT match Electron's own ffmpeg.dll - packaging corrupted it");
  }

  reportCheck("electron executable", fs.existsSync(path.join(UNPACKED_DIR, "Spinco TA Audit.exe")), "win-unpacked\\Spinco TA Audit.exe");
  reportCheck(
    "backend executable",
    fs.existsSync(path.join(UNPACKED_DIR, "resources", "backend", "spinco-backend.exe")),
    "win-unpacked\\resources\\backend\\spinco-backend.exe"
  );
  reportCheck(
    "frontend bundle",
    fs.existsSync(path.join(UNPACKED_DIR, "resources", "frontend", "index.html")),
    "win-unpacked\\resources\\frontend\\index.html"
  );
  reportCheck(
    "build-info",
    fs.existsSync(path.join(UNPACKED_DIR, "resources", "build-info.json")),
    "win-unpacked\\resources\\build-info.json (proves release traceability was actually packaged, not just generated on disk)"
  );
}

// Best-effort STATIC checks against the built frontend bundle's text
// content. These prove the compiled output contains the expected
// strings/wiring, which is necessary but not sufficient - they cannot
// observe rendered DOM order or click behavior. Real functional
// verification (live app, real clicks/API calls) is a separate manual/
// smoke-test step; this script only ever claims what it can actually check.
function verifyApplicationFeatures() {
  console.log("\nApplication feature verification (static build-artifact checks - not a substitute for live testing):");
  const assetsDir = path.join(REPO_ROOT, "frontend", "dist", "assets");
  if (!fs.existsSync(assetsDir)) {
    reportCheck("bundle present", false, `${assetsDir} does not exist - run the frontend build first`);
    return;
  }
  const jsFiles = fs.readdirSync(assetsDir).filter((f) => f.endsWith(".js"));
  const bundle = jsFiles.map((f) => fs.readFileSync(path.join(assetsDir, f), "utf8")).join("\n");

  // A naive /bell/i substring check false-positives on ARIA attributes and
  // ordinary English words (aria-labelledby, LabelList, "rebellion", ...) -
  // confirmed empirically against this exact bundle. lucide-react's Bell
  // icon component name gets minified away, but its SVG path `d` data is a
  // string literal that survives minification verbatim - check for that
  // specific, distinctive fragment instead (see
  // node_modules/lucide-react/dist/esm/icons/bell.mjs).
  reportCheck(
    "bell icon removed",
    !bundle.includes("3.262 15.326A1 1 0 0 0 4 17h16"),
    "no match for lucide-react's Bell icon SVG path data (tree-shaken out - Bell is not imported anywhere)"
  );
  reportCheck(
    "search functionality",
    bundle.includes("/search") && /search reports, employees/i.test(bundle),
    "real /api/search call + SearchBox placeholder text both present"
  );
  reportCheck(
    "Expense Reports column order",
    bundle.includes("File Name") && bundle.includes("Employee Name"),
    "both column headers present as separate strings (does not verify rendered DOM order - see live smoke test)"
  );
  reportCheck(
    "review/matched action",
    bundle.includes("/verify") && /confirm verification/i.test(bundle),
    "verify endpoint call + confirmation UI text both present"
  );
  reportCheck("account verification", bundle.includes("ACCOUNT VERIFIED"), "status string present in bundle");
}

function main() {
  const pkg = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, "package.json"), "utf8"));
  const buildInfoPath = path.join(REPO_ROOT, "electron", "generated", "build-info.json");

  if (!fs.existsSync(buildInfoPath)) {
    fail(`${buildInfoPath} does not exist - run "npm run write:build-info" (or the full build) first.`);
    return;
  }
  const buildInfo = JSON.parse(fs.readFileSync(buildInfoPath, "utf8"));
  const buildStarted = new Date(buildInfo.buildDate);

  if (buildInfo.version !== pkg.version) {
    fail(`build-info.json version (${buildInfo.version}) does not match package.json version (${pkg.version}).`);
  } else {
    console.log(`[verify-release] OK: build-info.json version matches package.json (${pkg.version})`);
  }
  console.log(`[verify-release] build commit=${buildInfo.gitCommit} dirty=${buildInfo.dirty} buildDate=${buildInfo.buildDate}`);

  const latestYmlPath = path.join(RELEASE_DIR, "latest.yml");
  if (!fs.existsSync(latestYmlPath)) {
    fail(`${latestYmlPath} does not exist - the installer build did not produce update metadata.`);
    return;
  }
  const meta = yaml.load(fs.readFileSync(latestYmlPath, "utf8"));
  if (meta.version !== pkg.version) {
    fail(`latest.yml version (${meta.version}) does not match package.json version (${pkg.version}).`);
  } else {
    console.log(`[verify-release] OK: latest.yml version matches package.json (${pkg.version})`);
  }

  const installerPath = path.join(RELEASE_DIR, meta.path || "");
  if (!meta.path || !fs.existsSync(installerPath)) {
    fail(`Installer named in latest.yml ("${meta.path}") does not exist in ${RELEASE_DIR}.`);
    return;
  }

  const checks = [
    ["frontend build", path.join(REPO_ROOT, "frontend", "dist", "index.html")],
    ["backend executable", path.join(REPO_ROOT, "backend", "dist_backend", "spinco-backend", "spinco-backend.exe")],
    ["installer", installerPath],
  ];
  for (const [label, filePath] of checks) {
    if (!fs.existsSync(filePath)) {
      fail(`${label} not found at ${filePath}.`);
      continue;
    }
    const written = mtime(filePath);
    if (written < buildStarted) {
      fail(`${label} (${filePath}) was last written at ${written.toISOString()}, BEFORE this build started (${buildInfo.buildDate}) - it is stale, not from this build.`);
    } else {
      console.log(`[verify-release] OK: ${label} written at ${written.toISOString()} (after build start)`);
    }
  }

  verifyElectronRuntime();
  verifyApplicationFeatures();

  if (process.exitCode === 1) {
    console.error("\n[verify-release] Release verification FAILED - do not publish these artifacts.");
  } else {
    console.log(`\n[verify-release] Release verification PASSED - version ${pkg.version}, commit ${buildInfo.gitCommit}${buildInfo.dirty ? " (dirty)" : ""}.`);
  }
}

main();
