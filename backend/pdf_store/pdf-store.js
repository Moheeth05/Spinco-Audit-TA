'use strict';

/**
 * Node.js PDF archive: stores uploaded PDFs sorted and de-duplicated with the SAME rules the Spinco TA Audit
 * backend applies on upload (backend/app/api/uploads.py, services/pdf_validation.py, services/storage_backend.py,
 * services/filename_ids.py, services/extraction/classifier.py). Zero dependencies (Node 18+).
 *
 * Rules mirrored from the software
 *   validation  .pdf name, not empty, <= 25 MB, starts with "%PDF-", readable, not password-protected, >= 1 page
 *   duplicate   same SHA-256 of the file bytes within the same kind -> "File already exists." (first one wins, the
 *               later copy is NOT stored). Same bytes in the two kinds are two separate documents, as in the software.
 *   kind        expense_report | trip_report. The software's upload channel decides; here that is the --kind option or
 *               the source folder name (expense_reports/ , trip_reports/). Otherwise content signals decide (embedded
 *               text only), then the ER-<digits> / TRIP-<digits> filename prefix. No decision -> unsorted/.
 *   stored name "<first 12 hex of SHA-256>_<sanitised original filename>" (unsafe characters become "_")
 *   id          ER-<digits> / TRIP-<digits> read from the filename only (never guessed)
 *
 * Originals are only READ. Every archived file is a byte-for-byte COPY.
 */

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const zlib = require('node:zlib');

const MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024;
const PDF_MAGIC = Buffer.from('%PDF-', 'latin1');
const KIND_DIRS = { expense_report: 'expense_reports', trip_report: 'trip_reports', unsorted: 'unsorted' };
const DIR_TO_KIND = { expense_reports: 'expense_report', trip_reports: 'trip_report' };
const INDEX_FILE = 'index.json';
const UNSAFE_CHARS = /[^A-Za-z0-9._ \-()]+/g;

const ER_PATTERN = /\bER-(\d+)/i;
const TRIP_PATTERN = /\bTRIP-(\d+)/i;
const EXPENSE_REPORT_SIGNALS = [/\bExpense Report\b/i, /\bER-\d+\b/, /\bEXPENSE SUMMARY\b/i, /\bAmount to be Reimbursed\b/i, /\bReport Summary\b/i];
const TRIP_REPORT_SIGNALS = [/\bTRIP-\d+\b/, /\bTrip Summary\b/i, /\bTrip Number\b/i, /\bDomestic Trip\b/i, /\bBusiness Purpose\b/i, /\bApproval History\b/i];

// ---------------------------------------------------------------------------------------------- helpers

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function sanitizeFilename(name) {
  const base = String(name).split(/[\\/]/).pop();
  return base.replace(UNSAFE_CHARS, '_').trim() || 'document.pdf';
}

function extractDocumentNumber(filename, kind) {
  const m = (kind === 'expense_report' ? ER_PATTERN : kind === 'trip_report' ? TRIP_PATTERN : null)?.exec(filename);
  return m ? `${kind === 'expense_report' ? 'ER' : 'TRIP'}-${m[1]}` : null;
}

/** Every "stream ... endstream" body that inflates (FlateDecode); bad/other streams are skipped. */
function inflatedStreams(buf) {
  const out = [];
  let pos = 0;
  for (let guard = 0; guard < 100000; guard++) {
    const s = buf.indexOf('stream', pos, 'latin1');
    if (s === -1) break;
    let start = s + 6;
    if (buf[start] === 0x0d) start++;
    if (buf[start] === 0x0a) start++;
    const e = buf.indexOf('endstream', start, 'latin1');
    if (e === -1) break;
    pos = e + 9;
    try {
      out.push(zlib.inflateSync(buf.subarray(start, e), { maxOutputLength: 64 * 1024 * 1024 }));
    } catch {
      /* not a Flate stream (image, font, ...) */
    }
  }
  return out;
}

const PAGE_OBJECT = /\/Type\s*\/Page(?![A-Za-z])/g;
function countPages(buf) {
  let n = (buf.toString('latin1').match(PAGE_OBJECT) || []).length;
  if (n === 0) for (const s of inflatedStreams(buf)) n += (s.toString('latin1').match(PAGE_OBJECT) || []).length; // PDF 1.5 object streams
  return n;
}

/** Embedded text only (Tj / TJ strings). A scanned or CID-font PDF yields '' and is then sorted by filename. */
function extractText(buf) {
  const sources = [buf.toString('latin1'), ...inflatedStreams(buf).map((b) => b.toString('latin1'))];
  const pieces = [];
  const unescape = (s) => s.replace(/\\([nrtbf()\\])/g, (_, c) => ({ n: '\n', r: '\r', t: '\t', b: '\b', f: '\f' }[c] ?? c));
  for (const src of sources) {
    for (const m of src.matchAll(/\(((?:\\.|[^\\()])*)\)\s*Tj/g)) pieces.push(unescape(m[1]));
    for (const m of src.matchAll(/\[((?:\\.|[^\]])*)\]\s*TJ/g)) for (const p of m[1].matchAll(/\(((?:\\.|[^\\()])*)\)/g)) pieces.push(unescape(p[1]));
  }
  return pieces.join('\n');
}

// ---------------------------------------------------------------------------------------------- validation

/** Same checks, same user-facing messages, same order as services/pdf_validation.py. */
function validatePdf(filename, buf, { maxBytes = MAX_FILE_SIZE_BYTES } = {}) {
  const fail = (error) => ({ ok: false, error });
  if (!String(filename).toLowerCase().endsWith('.pdf')) return fail('Only PDF files are supported.');
  if (buf.length === 0) return fail('The uploaded file is empty.');
  if (buf.length > maxBytes) return fail(`File exceeds the ${Math.floor(maxBytes / (1024 * 1024))} MB upload limit.`);
  if (!buf.subarray(0, PDF_MAGIC.length).equals(PDF_MAGIC)) return fail('This file does not appear to be a valid PDF.');
  const tail = buf.subarray(Math.max(0, buf.length - 4096)).toString('latin1');
  if (!tail.includes('%%EOF')) return fail('The PDF could not be read - it may be corrupted.');
  if (/\/Encrypt\s*(?:\d+\s+\d+\s+R|<<)/.test(buf.toString('latin1'))) {
    return fail('This PDF is password-protected and cannot be processed. Remove the password and re-upload.');
  }
  const pageCount = countPages(buf);
  if (pageCount === 0) return fail('The PDF has no pages.');
  return { ok: true, pageCount };
}

// ---------------------------------------------------------------------------------------------- sorting

function countMatches(patterns, text) {
  return patterns.reduce((n, p) => n + (p.test(text) ? 1 : 0), 0);
}

/**
 * Decides the kind. Precedence: explicit override (upload channel) > decisive content signals > filename prefix.
 * Content signals use the software's own signal lists; a tie or no signal is never guessed.
 */
function classify({ buf, filename, override = null }) {
  if (override) return { kind: override, via: override === 'auto' ? 'none' : 'override', notes: [] };
  const text = extractText(buf);
  const e = countMatches(EXPENSE_REPORT_SIGNALS, text);
  const t = countMatches(TRIP_REPORT_SIGNALS, text);
  const content = e > t ? 'expense_report' : t > e ? 'trip_report' : null;
  const hasEr = ER_PATTERN.test(filename);
  const hasTrip = TRIP_PATTERN.test(filename);
  const byName = hasEr && !hasTrip ? 'expense_report' : hasTrip && !hasEr ? 'trip_report' : null;
  const notes = [];
  if (content) {
    if (byName && byName !== content) notes.push(`Content looks like ${content}, filename suggests ${byName}; content wins.`);
    return { kind: content, via: 'content', notes };
  }
  if (byName) return { kind: byName, via: 'filename', notes };
  return { kind: 'unsorted', via: 'none', notes: ['Kind could not be determined from content or filename.'] };
}

// ---------------------------------------------------------------------------------------------- index

function loadIndex(outDir) {
  const file = path.join(outDir, INDEX_FILE);
  if (!fs.existsSync(file)) return { version: 1, updatedAt: null, documents: [], rejected: [] };
  const idx = JSON.parse(fs.readFileSync(file, 'utf8'));
  idx.documents ||= [];
  idx.rejected ||= [];
  return idx;
}

function saveIndex(outDir, idx) {
  idx.updatedAt = new Date().toISOString();
  const file = path.join(outDir, INDEX_FILE);
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(idx, null, 2));
  fs.renameSync(tmp, file);
}

// ---------------------------------------------------------------------------------------------- scanning

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(p);
    else if (entry.isFile()) yield p;
  }
}

/** The software stores "<hash12>_<name>"; strip that prefix when it really is this file's own hash prefix. */
function originalNameOf(fileName, hash) {
  const m = /^([0-9a-f]{12})_(.+)$/i.exec(fileName);
  return m && m[1].toLowerCase() === hash.slice(0, 12) ? m[2] : fileName;
}

// ---------------------------------------------------------------------------------------------- ingest

/**
 * @param {string} sourceDir  folder to scan (recursively). Only read.
 * @param {object} opts       { outDir, kind: 'auto'|'expense'|'trip', dryRun, maxBytes }
 * @returns summary with per-file outcomes
 */
function ingest(sourceDir, opts = {}) {
  const outDir = path.resolve(opts.outDir || path.join(__dirname, '..', 'pdf_archive'));
  const src = path.resolve(sourceDir);
  if (!fs.existsSync(src) || !fs.statSync(src).isDirectory()) throw new Error(`Source folder not found: ${src}`);
  if (outDir === src || outDir.startsWith(src + path.sep) || src.startsWith(outDir + path.sep)) {
    throw new Error('The archive folder and the source folder must not contain each other.');
  }
  const forced = { expense: 'expense_report', trip: 'trip_report' }[opts.kind] || null;
  const dryRun = Boolean(opts.dryRun);
  const idx = loadIndex(outDir);
  const results = [];

  // First upload wins: oldest first, then by path, so a re-run picks the same canonical copy.
  const files = [...walk(src)].map((p) => ({ p, mtime: fs.statSync(p).mtimeMs })).sort((a, b) => a.mtime - b.mtime || a.p.localeCompare(b.p));

  for (const { p } of files) {
    const fileName = path.basename(p);
    if (!fileName.toLowerCase().endsWith('.pdf')) continue; // not an upload candidate: ignored silently
    const buf = fs.readFileSync(p);
    const check = validatePdf(fileName, buf, { maxBytes: opts.maxBytes });
    if (!check.ok) {
      const rec = { sourcePath: p, filename: fileName, reason: check.error, at: new Date().toISOString() };
      if (!idx.rejected.some((r) => r.sourcePath === p && r.reason === rec.reason)) idx.rejected.push(rec);
      results.push({ file: p, outcome: 'rejected', reason: check.error });
      continue;
    }
    const hash = sha256(buf);
    const originalFilename = originalNameOf(fileName, hash);
    const folderKind = DIR_TO_KIND[path.basename(path.dirname(p))] || null; // the software's upload channel
    const decision = classify({ buf, filename: originalFilename, override: forced || folderKind });
    const kind = decision.kind;

    let same = idx.documents.find((d) => d.sha256 === hash && d.kind === kind);
    if (!same && (decision.via === 'filename' || decision.via === 'none')) {
      // No upload channel and no content signal: the kind is only a guess from the filename, so identical bytes under
      // a different name are still ONE document. (An explicit --kind / source-folder kind keeps the software's
      // per-kind rule: the same bytes uploaded as an expense report and as a trip report are two documents.)
      same = idx.documents.find((d) => d.sha256 === hash && (kind === 'unsorted' || d.kind !== 'unsorted'));
    }
    if (same) {
      if (same.sources.includes(p) || same.duplicates.some((d) => d.sourcePath === p)) {
        results.push({ file: p, outcome: 'unchanged', kind, stored: same.storedPath }); // seen on an earlier run
        continue;
      }
      same.duplicates.push({ sourcePath: p, filename: originalFilename, seenAt: new Date().toISOString() }); // in memory; saved only when not a dry run
      results.push({ file: p, outcome: 'duplicate', duplicateOf: same.storedPath, kind });
      continue;
    }

    const parked = kind !== 'unsorted' ? idx.documents.find((d) => d.sha256 === hash && d.kind === 'unsorted') : null;
    const stored = `${hash.slice(0, 12)}_${sanitizeFilename(originalFilename)}`;
    const relPath = path.posix.join(KIND_DIRS[kind], stored);
    const target = path.join(outDir, KIND_DIRS[kind], stored);

    if (parked) {
      // Same bytes were parked in unsorted/ earlier; now a decisive kind is known: move it, keep one copy.
      if (!dryRun) {
        fs.mkdirSync(path.dirname(target), { recursive: true });
        fs.renameSync(path.join(outDir, parked.storedPath), target);
      }
      Object.assign(parked, { kind, storedPath: relPath, classifiedBy: decision.via, documentNumber: extractDocumentNumber(originalFilename, kind), notes: decision.notes });
      results.push({ file: p, outcome: 'reclassified', from: 'unsorted', kind, stored: relPath });
      continue;
    }

    if (!dryRun) {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      if (fs.existsSync(target)) {
        if (sha256(fs.readFileSync(target)) !== hash) throw new Error(`Refusing to overwrite a different file: ${target}`);
      } else {
        fs.copyFileSync(p, target, fs.constants.COPYFILE_EXCL);
      }
    }
    idx.documents.push({ // in memory for a dry run too, so later copies in the same run are classified correctly
      sha256: hash,
      kind,
      documentNumber: extractDocumentNumber(originalFilename, kind),
      originalFilename,
      storedPath: relPath,
      sizeBytes: buf.length,
      pageCount: check.pageCount,
      classifiedBy: decision.via,
      notes: decision.notes,
      archivedAt: new Date().toISOString(),
      sources: [p],
      duplicates: [],
    });
    results.push({ file: p, outcome: 'stored', kind, stored: relPath, via: decision.via });
  }

  if (!dryRun) {
    fs.mkdirSync(outDir, { recursive: true });
    saveIndex(outDir, idx);
  }
  const count = (o) => results.filter((r) => r.outcome === o).length;
  return {
    outDir, dryRun, results,
    summary: { stored: count('stored'), duplicates: count('duplicate'), unchanged: count('unchanged'), reclassified: count('reclassified'), rejected: count('rejected') },
  };
}

// ---------------------------------------------------------------------------------------------- list / verify

/** Newest first, like the software's upload list. */
function list(outDir, { kind } = {}) {
  const idx = loadIndex(path.resolve(outDir));
  const k = { expense: 'expense_report', trip: 'trip_report', unsorted: 'unsorted' }[kind] || null;
  return idx.documents.filter((d) => !k || d.kind === k).sort((a, b) => (a.archivedAt < b.archivedAt ? 1 : -1));
}

/** Re-hash every archived file against the index: reports missing or changed files. */
function verify(outDir) {
  const root = path.resolve(outDir);
  const idx = loadIndex(root);
  const problems = [];
  for (const d of idx.documents) {
    const f = path.join(root, d.storedPath);
    if (!fs.existsSync(f)) problems.push({ storedPath: d.storedPath, problem: 'missing' });
    else if (sha256(fs.readFileSync(f)) !== d.sha256) problems.push({ storedPath: d.storedPath, problem: 'content changed' });
  }
  return { checked: idx.documents.length, problems };
}

module.exports = {
  MAX_FILE_SIZE_BYTES, KIND_DIRS, sha256, sanitizeFilename, extractDocumentNumber, countPages, extractText,
  validatePdf, classify, ingest, list, verify, loadIndex,
};
