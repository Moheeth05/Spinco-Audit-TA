'use strict';

/**
 * Folder sync: copies every PDF found in a source folder (typically the desktop app's own documents folder, which has
 * expense_reports/ and trip_reports/ inside) into the archive, sorted and de-duplicated by the ArchiveStore.
 *
 * It runs INSIDE the server process on purpose: the server owns index.json, and two processes writing it would overwrite
 * each other. The source folder is only READ; nothing in it is moved, changed or deleted. If a file is later removed from the
 * source (e.g. deleted in the app) the archived copy stays: the archive is a record, not a mirror.
 */

const fs = require('node:fs');
const path = require('node:path');
const rules = require('./rules');

const DIR_TO_KIND = { expense_reports: 'expense_report', trip_reports: 'trip_report' };

function* walk(dir) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) yield* walk(p);
    else if (e.isFile()) yield p;
  }
}

/** The app stores "<hash12>_<name>"; give the archive the real name when that prefix is this file's own hash prefix. */
function originalNameOf(fileName, hash) {
  const m = /^([0-9a-f]{12})_(.+)$/i.exec(fileName);
  return m && m[1].toLowerCase() === hash.slice(0, 12) ? m[2] : fileName;
}

function createSyncer({ store, folder, intervalMs = 30000, settleMs = 3000, log = () => {} }) {
  const seen = new Map(); // "path|size|mtime" already handled (stored, duplicate or rejected): not read again
  let timer = null;
  let running = null;
  const status = { folder, enabled: Boolean(folder), lastRun: null, lastError: null, runs: 0, totals: { stored: 0, duplicates: 0, rejected: 0, unchanged: 0 }, last: null };

  async function runOnce() {
    if (!folder) return null;
    if (running) return running; // never overlap two passes
    running = (async () => {
      const result = { scanned: 0, stored: 0, reclassified: 0, duplicates: 0, rejected: 0, unchanged: 0, skippedRecent: 0, errors: 0 };
      try {
        if (!fs.existsSync(folder)) throw new Error(`Sync folder not found: ${folder}`);
        for (const file of walk(folder)) {
          if (!file.toLowerCase().endsWith('.pdf')) continue; // the excel/ folder etc. are not upload candidates
          result.scanned++;
          let stat;
          try {
            stat = fs.statSync(file);
          } catch {
            continue;
          }
          const key = `${file}|${stat.size}|${stat.mtimeMs}`;
          if (seen.has(key)) {
            result.unchanged++;
            continue;
          }
          if (Date.now() - stat.mtimeMs < settleMs) {
            result.skippedRecent++; // possibly still being written by the app: look again next pass
            continue;
          }
          let buffer;
          try {
            buffer = fs.readFileSync(file);
          } catch {
            result.errors++; // locked or vanished: retried next pass
            continue;
          }
          const parent = path.basename(path.dirname(file));
          const kind = DIR_TO_KIND[parent] || null; // the app's own upload channel, when the folder layout shows it
          const item = await store.storeUpload({
            buffer, filename: originalNameOf(path.basename(file), rules.sha256(buffer)), kind, source: file,
          });
          seen.set(key, true);
          if (item.success && item.reclassified) result.reclassified++;
          else if (item.success) result.stored++;
          else if (item.error_message === 'File already exists.') (item.already_imported ? result.unchanged++ : result.duplicates++);
          else result.rejected++;
        }
        status.lastError = null;
      } catch (err) {
        status.lastError = err.message;
        result.errors++;
      }
      status.runs++;
      status.lastRun = new Date().toISOString();
      status.last = result;
      for (const k of ['stored', 'duplicates', 'rejected']) status.totals[k] += result[k];
      if (result.stored || result.duplicates || result.rejected || status.lastError) {
        log('sync', `stored=${result.stored} duplicates=${result.duplicates} rejected=${result.rejected}`, status.lastError ? `error=${status.lastError}` : '');
      }
      return result;
    })().finally(() => {
      running = null;
    });
    return running;
  }

  function start() {
    if (!folder || timer) return;
    runOnce();
    timer = setInterval(() => runOnce(), intervalMs);
    timer.unref();
  }
  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }
  return { runOnce, start, stop, status: () => ({ ...status, totals: { ...status.totals } }) };
}

module.exports = { createSyncer, originalNameOf };
