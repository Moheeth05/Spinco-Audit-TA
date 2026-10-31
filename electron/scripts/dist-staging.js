"use strict";

/**
 * Staging build: the same pipeline as `npm run dist` (tests -> build info -> frontend -> backend -> electron-builder ->
 * verify -> update feed), but EVERYTHING it writes goes into one new, unique run folder outside the repository:
 *
 *   D:\SpincoTA-artifacts\release-staging\<YYYYMMDD-HHMMSS>-v<version>\
 *     build\          PyInstaller output (SPINCO_BUILD_DIR): dist_backend\, build\
 *     frontend-dist\  Vite output (--outDir)
 *     build-info\     build-info.json (SPINCO_BUILD_INFO_DIR)
 *     release\        electron-builder output: installers, latest.yml, win-unpacked\ (SPINCO_RELEASE_DIR)
 *     update-feed\    what release:stage would publish (SPINCO_UPDATE_FEED_DIR)
 *     tmp\            TEMP/TMP for the test run and SPINCO_TMP for the release verification
 *     logs\           one log per step
 *     electron-builder.staging.config.json, RUN.json
 *
 * It never touches D:\Bills\release (the official release output), never runs `clean`, never overwrites an existing
 * run folder (a name that is taken gets a -2, -3... suffix), reuses the existing resources\tesseract unchanged
 * (collect:tesseract is NOT run), and does not change the version. Electron's userData (%APPDATA%) is not involved.
 *
 * Usage:
 *   npm run dist:staging -- --dry-run     print the plan, create and run nothing
 *   npm run dist:staging                  do it
 *   npm run dist:staging -- --skip-tests  as above without the backend test suite
 * Setting SPINCO_STAGING_ROOT replaces the default root D:\SpincoTA-artifacts\release-staging (it must be outside the repo).
 */

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const REPO_ROOT = path.join(__dirname, "..", "..");
const DEFAULT_STAGING_ROOT = "D:\\SpincoTA-artifacts\\release-staging";

function pad(n, width = 2) {
  return String(n).padStart(width, "0");
}

function timestamp(now) {
  return (
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-` +
    `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  );
}

function isInside(parent, child) {
  const rel = path.relative(path.resolve(parent), path.resolve(child));
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

// Large artifacts stay out of the repository, and the official release folder is never a staging target.
function assertRootOutsideRepo(root, repoRoot = REPO_ROOT) {
  if (isInside(repoRoot, root)) {
    throw new Error(`staging root ${path.resolve(root)} is inside the repository ${path.resolve(repoRoot)}; use a folder outside it`);
  }
}

/**
 * Picks (and, with create:true, creates) a run folder that does not exist yet. mkdir without `recursive` fails with
 * EEXIST instead of reusing a folder, so two runs - even in the same second - can never share one.
 */
function allocateRunDir(root, version, { now = new Date(), create = true } = {}) {
  if (!/^[0-9A-Za-z][0-9A-Za-z.+-]*$/.test(String(version))) throw new Error(`unusable version "${version}"`);
  const base = `${timestamp(now)}-v${version}`;
  if (create) fs.mkdirSync(root, { recursive: true });
  for (let n = 1; n <= 99; n++) {
    const dir = path.join(root, n === 1 ? base : `${base}-${n}`);
    if (!create) {
      if (!fs.existsSync(dir)) return dir;
      continue;
    }
    try {
      fs.mkdirSync(dir);
      return dir;
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
    }
  }
  throw new Error(`no free run folder name for ${base} under ${root}`);
}

function layout(runDir) {
  return {
    runDir,
    buildDir: path.join(runDir, "build"),
    frontendDist: path.join(runDir, "frontend-dist"),
    buildInfoDir: path.join(runDir, "build-info"),
    releaseDir: path.join(runDir, "release"),
    feedDir: path.join(runDir, "update-feed"),
    tmpDir: path.join(runDir, "tmp"),
    logsDir: path.join(runDir, "logs"),
    builderConfig: path.join(runDir, "electron-builder.staging.config.json"),
  };
}

/** package.json's "build" block with the output folder and every extraResources source pointed at the run folder. */
function buildBuilderConfig(pkgBuild, L, repoRoot = REPO_ROOT) {
  const redirect = {
    "frontend/dist": L.frontendDist,
    "backend/dist_backend/spinco-backend": path.join(L.buildDir, "dist_backend", "spinco-backend"),
    "resources/tesseract": path.join(repoRoot, "resources", "tesseract"), // existing folder, used as it is
    "electron/generated/build-info.json": path.join(L.buildInfoDir, "build-info.json"),
  };
  const cfg = JSON.parse(JSON.stringify(pkgBuild));
  cfg.directories = { ...cfg.directories, output: L.releaseDir };
  cfg.extraResources = (cfg.extraResources || []).map((entry) => {
    const from = redirect[entry.from.replace(/\\/g, "/")];
    if (!from) throw new Error(`package.json build.extraResources has an entry this script does not know how to redirect: ${entry.from}`);
    return { ...entry, from };
  });
  return cfg;
}

function buildPlan({ runDir, skipTests = false, repoRoot = REPO_ROOT }) {
  const L = layout(runDir);
  const node = process.execPath;
  const script = (name) => path.join(repoRoot, "electron", "scripts", name);
  const stageEnv = {
    SPINCO_BUILD_DIR: L.buildDir,
    SPINCO_BUILD_INFO_DIR: L.buildInfoDir,
    SPINCO_RELEASE_DIR: L.releaseDir,
    SPINCO_FRONTEND_DIST: L.frontendDist,
    SPINCO_UPDATE_FEED_DIR: L.feedDir,
    SPINCO_TMP: L.tmpDir,
    TEMP: L.tmpDir,
    TMP: L.tmpDir,
  };
  const steps = [];
  if (!skipTests) steps.push({ id: "tests", label: "backend tests", cmd: node, args: [script("run-tests.js")], cwd: repoRoot });
  steps.push(
    { id: "build-info", label: "write build info", cmd: node, args: [script("write-build-info.js")], cwd: repoRoot },
    {
      id: "frontend",
      label: "frontend build (vite --outDir)",
      cmd: node,
      args: [path.join(repoRoot, "frontend", "node_modules", "vite", "bin", "vite.js"), "build", "--outDir", L.frontendDist, "--emptyOutDir"],
      cwd: path.join(repoRoot, "frontend"),
    },
    { id: "backend", label: "backend build (PyInstaller)", cmd: node, args: [script("build-backend.js")], cwd: repoRoot },
    {
      id: "electron-builder",
      label: "electron-builder",
      cmd: node,
      args: [path.join(repoRoot, "node_modules", "electron-builder", "cli.js"), "--win", "--config", L.builderConfig],
      cwd: repoRoot,
    },
    { id: "verify", label: "verify release", cmd: node, args: [script("verify-release.js")], cwd: repoRoot },
    { id: "feed", label: "stage update feed", cmd: node, args: [script("stage-release.js")], cwd: repoRoot }
  );
  return { layout: L, env: stageEnv, steps };
}

function parseArgs(argv) {
  const opts = { dryRun: false, skipTests: false, help: false };
  for (const a of argv) {
    if (a === "--dry-run") opts.dryRun = true;
    else if (a === "--skip-tests") opts.skipTests = true;
    else if (a === "--help" || a === "-h") opts.help = true;
    else throw new Error(`unknown argument "${a}" (use --dry-run, --skip-tests or --help)`);
  }
  return opts;
}

function printPlan(plan, root, dryRun) {
  const L = plan.layout;
  console.log(`[dist-staging] ${dryRun ? "DRY RUN - nothing is created, built or changed" : "run folder created"}`);
  console.log(`[dist-staging] staging root : ${root}`);
  console.log(`[dist-staging] run folder   : ${L.runDir}${dryRun ? "  (would be created; a -2, -3.. suffix is added if the name is taken)" : ""}`);
  console.log(`[dist-staging] official release folder (never touched): ${path.join(REPO_ROOT, "release")}`);
  console.log("[dist-staging] environment for every step:");
  for (const [k, v] of Object.entries(plan.env)) console.log(`    ${k}=${v}`);
  console.log("[dist-staging] steps:");
  plan.steps.forEach((s, i) => console.log(`  ${i + 1}. ${s.label}\n       ${[s.cmd, ...s.args].join(" ")}\n       (cwd ${s.cwd})`));
  console.log(`[dist-staging] electron-builder config will be written to ${L.builderConfig}`);
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) {
    console.log("Usage: npm run dist:staging -- [--dry-run] [--skip-tests]\nSee the header of electron/scripts/dist-staging.js.");
    return;
  }
  const pkg = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, "package.json"), "utf8"));
  const root = path.resolve(process.env.SPINCO_STAGING_ROOT || DEFAULT_STAGING_ROOT);
  assertRootOutsideRepo(root);

  if (!fs.existsSync(path.join(REPO_ROOT, "resources", "tesseract", "tesseract.exe"))) {
    // collect:tesseract is deliberately not part of this workflow, so the vendored runtime has to exist already.
    throw new Error(`resources\\tesseract\\tesseract.exe is missing; this workflow reuses it and does not rebuild it`);
  }

  if (opts.dryRun) {
    const runDir = allocateRunDir(root, pkg.version, { create: false });
    printPlan(buildPlan({ runDir, skipTests: opts.skipTests }), root, true);
    return;
  }

  const runDir = allocateRunDir(root, pkg.version);
  const plan = buildPlan({ runDir, skipTests: opts.skipTests });
  const L = plan.layout;
  for (const dir of [L.buildDir, L.frontendDist, L.buildInfoDir, L.releaseDir, L.feedDir, L.tmpDir, L.logsDir]) fs.mkdirSync(dir);
  fs.writeFileSync(L.builderConfig, JSON.stringify(buildBuilderConfig(pkg.build, L), null, 2));
  printPlan(plan, root, false);

  const manifest = { version: pkg.version, startedAt: new Date().toISOString(), runDir, steps: [] };
  const saveManifest = () => fs.writeFileSync(path.join(runDir, "RUN.json"), JSON.stringify(manifest, null, 2));
  saveManifest();

  const env = { ...process.env, ...plan.env };
  for (const [i, step] of plan.steps.entries()) {
    const logPath = path.join(L.logsDir, `${pad(i + 1)}-${step.id}.log`);
    const fd = fs.openSync(logPath, "w");
    console.log(`[dist-staging] ${i + 1}/${plan.steps.length} ${step.label} ...`);
    let result;
    try {
      result = spawnSync(step.cmd, step.args, { cwd: step.cwd, env, stdio: ["ignore", fd, fd] });
    } finally {
      fs.closeSync(fd);
    }
    const status = result.status === null ? 1 : result.status;
    manifest.steps.push({ id: step.id, status, log: logPath });
    saveManifest();
    if (status !== 0) {
      console.error(`[dist-staging] FAILED at "${step.label}" (exit ${status}). Log: ${logPath}`);
      console.error(`[dist-staging] Run folder kept for inspection: ${runDir}`);
      process.exit(status);
    }
  }
  manifest.finishedAt = new Date().toISOString();
  saveManifest();
  console.log(`[dist-staging] DONE. Output: ${L.releaseDir}`);
  console.log(`[dist-staging] Update-feed files: ${path.join(L.feedDir, "spinco-ta-audit")}`);
}

module.exports = { allocateRunDir, assertRootOutsideRepo, buildBuilderConfig, buildPlan, layout, parseArgs };

if (require.main === module) {
  try {
    main();
  } catch (err) {
    console.error(`[dist-staging] ${err.message}`);
    process.exit(1);
  }
}
