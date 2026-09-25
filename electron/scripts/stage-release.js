"use strict";

/**
 * Copies exactly the files an electron-updater "generic" feed needs - the
 * NSIS installer, its .blockmap (enables differential/delta downloads), and
 * the auto-generated latest.yml metadata - from electron-builder's local
 * `release/` output into a staging "update feed" directory. That directory
 * is what actually gets published (rsync/scp/aws s3 sync/etc.) to wherever
 * the feed is really hosted; this script never uploads anywhere itself.
 *
 * Deliberately narrow: only these three file types are ever copied. Nothing
 * else in `release/` (win-unpacked/, builder-debug.yml, the portable .exe,
 * which auto-update doesn't apply to) is included, and nothing from
 * anywhere else in the repo - so it is structurally impossible for this
 * script to leak the production database, documents, or any credential into
 * a directory that's meant to end up served publicly.
 *
 * Output layout mirrors the "channel" config in package.json's
 * build.publish (channel: "latest"), and is namespaced under the product so
 * this can later sit alongside the centralized Spinco server's other static
 * assets without colliding with anything: <feed dir>/spinco-ta-audit/...
 *
 * Usage: npm run release:stage
 * Override the output location with SPINCO_UPDATE_FEED_DIR (defaults to
 * D:\Bills\update-feed - itself gitignored, see .gitignore).
 */

const fs = require("node:fs");
const path = require("node:path");
const yaml = require("js-yaml");

const REPO_ROOT = path.join(__dirname, "..", "..");
const RELEASE_DIR = path.join(REPO_ROOT, "release");
const FEED_ROOT = process.env.SPINCO_UPDATE_FEED_DIR || path.join(REPO_ROOT, "update-feed");
const FEED_DIR = path.join(FEED_ROOT, "spinco-ta-audit");

function main() {
  const latestYmlPath = path.join(RELEASE_DIR, "latest.yml");
  if (!fs.existsSync(latestYmlPath)) {
    console.error(`[stage-release] ${latestYmlPath} does not exist - run "npm run dist" first.`);
    process.exit(1);
  }

  // latest.yml is the source of truth for which installer is "the" current
  // release - never a filename glob, which would also match every stale
  // installer left over from a previous build in the same release/
  // directory (confirmed this bug empirically: an old 1.0.0 Setup.exe from
  // a prior build was still sitting in release/ and a naive Setup*.exe glob
  // pulled it into the feed alongside the real 1.1.0 one).
  const meta = yaml.load(fs.readFileSync(latestYmlPath, "utf8"));
  const installerName = meta.path;
  if (!installerName) {
    console.error(`[stage-release] latest.yml has no "path" field - cannot determine the current installer.`);
    process.exit(1);
  }

  const wanted = ["latest.yml", installerName, `${installerName}.blockmap`].filter((name) =>
    fs.existsSync(path.join(RELEASE_DIR, name))
  );
  const missing = ["latest.yml", installerName].filter((name) => !fs.existsSync(path.join(RELEASE_DIR, name)));
  if (missing.length > 0) {
    console.error(`[stage-release] Missing required file(s) in ${RELEASE_DIR}: ${missing.join(", ")}`);
    process.exit(1);
  }

  // Clear the feed dir first so a stale previous version's installer can
  // never linger alongside the new one.
  fs.rmSync(FEED_DIR, { recursive: true, force: true });
  fs.mkdirSync(FEED_DIR, { recursive: true });

  console.log(`[stage-release] Staging release ${meta.version} into ${FEED_DIR}:`);
  for (const name of wanted) {
    const src = path.join(RELEASE_DIR, name);
    const dest = path.join(FEED_DIR, name);
    fs.copyFileSync(src, dest);
    const size = fs.statSync(dest).size;
    console.log(`  ${name}  (${(size / (1024 * 1024)).toFixed(2)} MB)`);
  }

  console.log(`\n[stage-release] Done. Publish this directory's contents to the real update feed host:`);
  console.log(`  ${FEED_DIR}`);
  console.log(`\n[stage-release] Never copy anything else from release/ (win-unpacked/, the portable`);
  console.log(`[stage-release] exe, builder-debug.yml, or any stale prior-version installer) into a`);
  console.log(`[stage-release] publicly-served location.`);
}

main();
