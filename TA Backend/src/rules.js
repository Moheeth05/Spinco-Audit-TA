'use strict';

/**
 * Upload rules, taken from the Spinco TA Audit Python backend so this server behaves the same way:
 *   validation  services/pdf_validation.py   (same checks, same order, same messages)
 *   naming      services/storage.py + storage_backend.py   ("<sha256[0:12]>_<sanitised name>")
 *   ids         services/filename_ids.py     (ER-<digits> / TRIP-<digits> from the FILENAME only)
 *   sorting     services/extraction/classifier.py   (content signals, filename as the secondary signal)
 * Zero dependencies.
 */

const crypto = require('node:crypto');
const zlib = require('node:zlib');

const MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024; // the software's limit
const PDF_MAGIC = Buffer.from('%PDF-', 'latin1');
const KIND_DIRS = { expense_report: 'expense_reports', trip_report: 'trip_reports', unsorted: 'unsorted' };
const ID_PREFIX = { expense_report: 'expense', trip_report: 'trip', unsorted: 'unsorted' };
const UNSAFE_CHARS = /[^A-Za-z0-9._ \-()]+/g;

const ER_PATTERN = /\bER-(\d+)/i;
const TRIP_PATTERN = /\bTRIP-(\d+)/i;
const EXPENSE_REPORT_SIGNALS = [/\bExpense Report\b/i, /\bER-\d+\b/, /\bEXPENSE SUMMARY\b/i, /\bAmount to be Reimbursed\b/i, /\bReport Summary\b/i];
const TRIP_REPORT_SIGNALS = [/\bTRIP-\d+\b/, /\bTrip Summary\b/i, /\bTrip Number\b/i, /\bDomestic Trip\b/i, /\bBusiness Purpose\b/i, /\bApproval History\b/i];

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function sanitizeFilename(name) {
  const base = String(name).split(/[\\/]/).pop();
  return base.replace(UNSAFE_CHARS, '_').trim() || 'document.pdf';
}

function extractDocumentNumber(filename, kind) {
  const pattern = kind === 'expense_report' ? ER_PATTERN : kind === 'trip_report' ? TRIP_PATTERN : null;
  const m = pattern ? pattern.exec(filename) : null;
  return m ? `${kind === 'expense_report' ? 'ER' : 'TRIP'}-${m[1]}` : null;
}

/** Bodies of "stream ... endstream" blocks that inflate (FlateDecode). Others (images, fonts) are skipped. */
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
      /* not a Flate stream */
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

/** Same checks, same order, same user-facing messages as services/pdf_validation.py. */
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

/**
 * Decides the kind. Precedence: explicit upload channel (`override`) > decisive content signals > filename prefix.
 * A tie or no signal is never guessed: the file is "unsorted".
 */
function classify({ buf, filename, override = null }) {
  if (override) return { kind: override, via: 'override', notes: [] };
  const text = extractText(buf);
  const score = (patterns) => patterns.reduce((n, p) => n + (p.test(text) ? 1 : 0), 0);
  const e = score(EXPENSE_REPORT_SIGNALS);
  const t = score(TRIP_REPORT_SIGNALS);
  const content = e > t ? 'expense_report' : t > e ? 'trip_report' : null;
  const hasEr = ER_PATTERN.test(filename);
  const hasTrip = TRIP_PATTERN.test(filename);
  const byName = hasEr && !hasTrip ? 'expense_report' : hasTrip && !hasEr ? 'trip_report' : null;
  if (content) {
    const notes = byName && byName !== content ? [`Content looks like ${content}, filename suggests ${byName}; content wins.`] : [];
    return { kind: content, via: 'content', notes };
  }
  if (byName) return { kind: byName, via: 'filename', notes: [] };
  return { kind: 'unsorted', via: 'none', notes: ['Kind could not be determined from content or filename.'] };
}

module.exports = {
  MAX_FILE_SIZE_BYTES, KIND_DIRS, ID_PREFIX, sha256, sanitizeFilename, extractDocumentNumber, countPages, extractText, validatePdf, classify,
};
