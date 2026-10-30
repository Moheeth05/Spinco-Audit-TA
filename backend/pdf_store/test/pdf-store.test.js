'use strict';

// All PDFs here are SYNTHETIC, built in memory. No real documents are read.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const store = require('../pdf-store');

/** A minimal PDF. opts: pages, text (visible content), compress (page objects in a Flate object stream), encrypt, noEof, comment (makes bytes unique). */
function makePdf({ pages = 1, text = '', compress = false, encrypt = false, noEof = false, comment = '' } = {}) {
  const pageObjs = Array.from({ length: pages }, (_, i) => `${3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n`).join('');
  const content = text ? `4 0 obj\n<< /Length ${text.length + 30} >>\nstream\nBT /F1 12 Tf (${text}) Tj ET\nendstream\nendobj\n` : '';
  let body;
  if (compress) {
    const objstm = zlib.deflateSync(Buffer.from(Array.from({ length: pages }, () => '<< /Type /Page /Parent 2 0 R >>').join(' ')));
    body = `5 0 obj\n<< /Type /ObjStm /N ${pages} /Filter /FlateDecode /Length ${objstm.length} >>\nstream\n`;
    return Buffer.concat([
      Buffer.from(`%PDF-1.5\n% ${comment}\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Count ${pages} >>\nendobj\n${content}${body}`, 'latin1'),
      objstm,
      Buffer.from('\nendstream\nendobj\ntrailer\n<< /Root 1 0 R >>\nstartxref\n0\n%%EOF\n', 'latin1'),
    ]);
  }
  return Buffer.from(
    `%PDF-1.4\n% ${comment}\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Count ${pages} >>\nendobj\n${pageObjs}${content}` +
      `trailer\n<< /Root 1 0 R ${encrypt ? '/Encrypt 9 0 R' : ''} >>\nstartxref\n0\n${noEof ? '' : '%%EOF\n'}`,
    'latin1',
  );
}

function tmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'pdfstore-'));
}
function put(dir, rel, buf, mtime) {
  const f = path.join(dir, rel);
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, buf);
  if (mtime) fs.utimesSync(f, mtime, mtime);
  return f;
}
const ls = (dir) => (fs.existsSync(dir) ? fs.readdirSync(dir) : []);
const kindDirs = (out) => ({ e: ls(path.join(out, 'expense_reports')), t: ls(path.join(out, 'trip_reports')) });

// ------------------------------------------------------------------ validation (same rules and messages as the software)

test('validation: each rule rejects with the software message', () => {
  const ok = makePdf();
  assert.equal(store.validatePdf('a.txt', ok).error, 'Only PDF files are supported.');
  assert.equal(store.validatePdf('a.pdf', Buffer.alloc(0)).error, 'The uploaded file is empty.');
  assert.equal(store.validatePdf('a.pdf', ok, { maxBytes: 10 }).error, 'File exceeds the 0 MB upload limit.');
  assert.equal(store.validatePdf('a.pdf', Buffer.from('not a pdf')).error, 'This file does not appear to be a valid PDF.');
  assert.match(store.validatePdf('a.pdf', makePdf({ noEof: true })).error, /may be corrupted/);
  assert.match(store.validatePdf('a.pdf', makePdf({ encrypt: true })).error, /password-protected/);
  assert.equal(store.validatePdf('a.pdf', makePdf({ pages: 0 })).error, 'The PDF has no pages.');
  assert.deepEqual(store.validatePdf('A.PDF', ok), { ok: true, pageCount: 1 });
  assert.equal(store.MAX_FILE_SIZE_BYTES, 25 * 1024 * 1024);
});

test('page count: plain page objects and pages hidden in a compressed object stream', () => {
  assert.equal(store.countPages(makePdf({ pages: 3 })), 3);
  assert.equal(store.countPages(makePdf({ pages: 4, compress: true })), 4);
});

// ------------------------------------------------------------------ naming

test('stored name is <hash12>_<sanitised name>; ids come from the filename only', () => {
  assert.equal(store.sanitizeFilename('..\\x/ER-39832 (1).pdf'), 'ER-39832 (1).pdf');
  assert.equal(store.sanitizeFilename('a<b>c?.pdf'), 'a_b_c_.pdf');
  assert.equal(store.sanitizeFilename('   '), 'document.pdf');
  assert.equal(store.extractDocumentNumber('x ER-39832.pdf', 'expense_report'), 'ER-39832');
  assert.equal(store.extractDocumentNumber('TRIP-24935_Trip_To_Indore.pdf', 'trip_report'), 'TRIP-24935');
  assert.equal(store.extractDocumentNumber('report.pdf', 'expense_report'), null);
  assert.equal(store.extractDocumentNumber('ER-1.pdf', 'trip_report'), null);
});

// ------------------------------------------------------------------ sorting

test('sorting: filename prefix, content signals, override, upload folders, unsorted', () => {
  const src = tmp(), out = tmp();
  put(src, 'ER-39832.pdf', makePdf({ comment: '1' }));
  put(src, 'TRIP-24935_Trip_To_Indore.pdf', makePdf({ comment: '2' }));
  put(src, 'scan.pdf', makePdf({ comment: '3' })); // no filename id, no text
  put(src, 'misnamed.pdf', makePdf({ text: 'Trip Summary Trip Number TRIP-99', comment: '4' })); // content decides
  put(src, 'ER-1_but_trip.pdf', makePdf({ text: 'Domestic Trip Business Purpose', comment: '5' })); // content beats filename
  const r = store.ingest(src, { outDir: out });
  assert.deepEqual(r.summary, { stored: 5, duplicates: 0, unchanged: 0, reclassified: 0, rejected: 0 });
  const by = Object.fromEntries(store.list(out).map((d) => [d.originalFilename, d]));
  assert.equal(by['ER-39832.pdf'].kind, 'expense_report');
  assert.equal(by['ER-39832.pdf'].documentNumber, 'ER-39832');
  assert.equal(by['ER-39832.pdf'].classifiedBy, 'filename');
  assert.equal(by['TRIP-24935_Trip_To_Indore.pdf'].kind, 'trip_report');
  assert.equal(by['scan.pdf'].kind, 'unsorted');
  assert.equal(by['misnamed.pdf'].kind, 'trip_report');
  assert.equal(by['misnamed.pdf'].classifiedBy, 'content');
  assert.equal(by['ER-1_but_trip.pdf'].kind, 'trip_report');
  assert.match(by['ER-1_but_trip.pdf'].notes[0], /content wins/);
  for (const d of Object.values(by)) assert.match(d.storedPath.split('/')[1], new RegExp(`^${d.sha256.slice(0, 12)}_`));
  assert.ok(fs.existsSync(path.join(out, 'unsorted', by['scan.pdf'].storedPath.split('/')[1])));
});

test('sorting: --kind override and the software upload-folder layout', () => {
  const src = tmp(), out = tmp();
  put(src, 'x.pdf', makePdf({ comment: 'a' }));
  put(src, 'trip_reports/abc.pdf', makePdf({ comment: 'b' }));
  store.ingest(src, { outDir: out, kind: 'expense' });
  assert.deepEqual(store.list(out).map((d) => [d.originalFilename, d.kind, d.classifiedBy]).sort(), [['abc.pdf', 'expense_report', 'override'], ['x.pdf', 'expense_report', 'override']]);
  const out2 = tmp();
  store.ingest(src, { outDir: out2 }); // no override: the trip_reports/ folder is the upload channel
  assert.equal(store.list(out2).find((d) => d.originalFilename === 'abc.pdf').kind, 'trip_report');
});

test('an already hash-prefixed storage name is not prefixed twice', () => {
  const src = tmp(), out = tmp();
  const buf = makePdf({ comment: 'p' });
  const h = store.sha256(buf);
  put(src, `expense_reports/${h.slice(0, 12)}_ER-5.pdf`, buf);
  store.ingest(src, { outDir: out });
  const d = store.list(out)[0];
  assert.equal(d.originalFilename, 'ER-5.pdf');
  assert.equal(d.storedPath, `expense_reports/${h.slice(0, 12)}_ER-5.pdf`);
  assert.equal(d.documentNumber, 'ER-5');
});

// ------------------------------------------------------------------ duplicates

test('duplicates: same bytes under different names/folders store ONCE; the oldest copy is canonical and the rest are recorded', () => {
  const src = tmp(), out = tmp();
  const buf = makePdf({ comment: 'dup2' });
  put(src, 'ER-100.pdf', buf, new Date('2026-01-02'));
  put(src, 'dupe_one.pdf', buf, new Date('2026-01-03'));
  put(src, 'sub/dupe_two.pdf', buf, new Date('2026-01-04'));
  const r = store.ingest(src, { outDir: out });
  assert.deepEqual([r.summary.stored, r.summary.duplicates], [1, 2]);
  const [d] = store.list(out);
  assert.equal(d.originalFilename, 'ER-100.pdf'); // oldest wins
  assert.equal(d.duplicates.length, 2);
  assert.equal(kindDirs(out).e.length, 1);
});

test('duplicates: identical bytes never become two documents just because one copy has a filename id and the other does not', () => {
  const src = tmp(), out = tmp();
  const buf = makePdf({ comment: 'order' });
  put(src, 'scan_0001.pdf', buf, new Date('2026-03-01')); // oldest, no id -> unsorted first
  put(src, 'ER-55.pdf', buf, new Date('2026-03-02')); // later copy with an id -> the parked file moves to expense_reports
  const r = store.ingest(src, { outDir: out });
  assert.deepEqual([r.summary.stored, r.summary.reclassified], [1, 1]);
  assert.equal(store.list(out).length, 1);
  assert.equal(store.list(out)[0].kind, 'expense_report');
  assert.equal(ls(path.join(out, 'unsorted')).length, 0);
});

test('duplicates: a re-run is idempotent, and a new copy later is detected against the archive', () => {
  const src = tmp(), out = tmp();
  const buf = makePdf({ comment: 'idem' });
  put(src, 'ER-7.pdf', buf, new Date('2026-02-01'));
  store.ingest(src, { outDir: out });
  const again = store.ingest(src, { outDir: out });
  assert.deepEqual(again.summary, { stored: 0, duplicates: 0, unchanged: 1, reclassified: 0, rejected: 0 });
  assert.equal(store.list(out).length, 1);
  const src2 = tmp();
  put(src2, 'someone_elses_upload.pdf', buf);
  const later = store.ingest(src2, { outDir: out });
  assert.equal(later.summary.duplicates, 1);
  assert.equal(store.list(out)[0].duplicates.length, 1);
  assert.equal(kindDirs(out).e.length, 1);
});

test('duplicates: same NAME with different content are both kept (the hash prefix tells them apart)', () => {
  const src = tmp(), out = tmp();
  put(src, 'a/ER-9.pdf', makePdf({ comment: 'v1' }));
  put(src, 'b/ER-9.pdf', makePdf({ comment: 'v2' }));
  const r = store.ingest(src, { outDir: out });
  assert.equal(r.summary.stored, 2);
  assert.equal(new Set(kindDirs(out).e).size, 2);
});

test('duplicates: the same bytes in the two kinds are two documents, as in the software', () => {
  const src = tmp(), out = tmp();
  const buf = makePdf({ comment: 'both' });
  put(src, 'expense_reports/x.pdf', buf);
  put(src, 'trip_reports/x.pdf', buf);
  const r = store.ingest(src, { outDir: out });
  assert.equal(r.summary.stored, 2);
  assert.deepEqual([kindDirs(out).e.length, kindDirs(out).t.length], [1, 1]);
});

test('an unsorted file is MOVED (not copied twice) once a decisive kind is known', () => {
  const src = tmp(), out = tmp();
  const buf = makePdf({ comment: 'park' });
  put(src, 'scan.pdf', buf);
  store.ingest(src, { outDir: out });
  assert.equal(store.list(out)[0].kind, 'unsorted');
  const r = store.ingest(src, { outDir: out, kind: 'trip' });
  assert.equal(r.summary.reclassified, 1);
  assert.equal(store.list(out).length, 1);
  assert.equal(store.list(out)[0].kind, 'trip_report');
  assert.equal(fs.existsSync(path.join(out, 'unsorted')) ? fs.readdirSync(path.join(out, 'unsorted')).length : 0, 0);
  assert.equal(kindDirs(out).t.length, 1);
});

// ------------------------------------------------------------------ rejection, safety, verify

test('rejected files are recorded with the software message and never stored', () => {
  const src = tmp(), out = tmp();
  put(src, 'bad.pdf', Buffer.from('hello'));
  put(src, 'locked.pdf', makePdf({ encrypt: true }));
  put(src, 'notes.txt', Buffer.from('ignored silently'));
  const r = store.ingest(src, { outDir: out });
  assert.deepEqual(r.summary, { stored: 0, duplicates: 0, unchanged: 0, reclassified: 0, rejected: 2 });
  const idx = store.loadIndex(out);
  assert.equal(idx.rejected.length, 2);
  assert.equal(idx.documents.length, 0);
  store.ingest(src, { outDir: out });
  assert.equal(store.loadIndex(out).rejected.length, 2); // not re-listed on a re-run
});

test('originals are never modified; a dry run writes nothing', () => {
  const src = tmp(), out = path.join(tmp(), 'archive');
  const f = put(src, 'ER-3.pdf', makePdf({ comment: 'orig' }));
  const before = store.sha256(fs.readFileSync(f));
  put(src, 'copy_of_it.pdf', fs.readFileSync(f)); // same bytes: the dry run must already predict it as a duplicate
  const dry = store.ingest(src, { outDir: out, dryRun: true });
  assert.deepEqual([dry.summary.stored, dry.summary.duplicates], [1, 1]);
  assert.equal(fs.existsSync(out), false);
  store.ingest(src, { outDir: out });
  assert.equal(store.sha256(fs.readFileSync(f)), before);
  const d = store.list(out)[0];
  assert.equal(store.sha256(fs.readFileSync(path.join(out, d.storedPath))), before); // byte-for-byte copy
});

test('the archive and source folders may not contain each other', () => {
  const src = tmp();
  assert.throws(() => store.ingest(src, { outDir: path.join(src, 'archive') }), /must not contain each other/);
  assert.throws(() => store.ingest(path.join(src, 'missing'), { outDir: tmp() }), /not found/);
});

test('verify detects a missing or changed archived file; list is newest first', () => {
  const src = tmp(), out = tmp();
  put(src, 'ER-1.pdf', makePdf({ comment: 'v1' }), new Date('2026-01-01'));
  store.ingest(src, { outDir: out });
  put(src, 'TRIP-2.pdf', makePdf({ comment: 'v2' }), new Date('2026-01-02'));
  store.ingest(src, { outDir: out });
  const docs = store.list(out);
  assert.ok(docs[0].archivedAt >= docs[1].archivedAt);
  assert.deepEqual(store.verify(out), { checked: 2, problems: [] });
  fs.appendFileSync(path.join(out, docs[0].storedPath), 'tamper');
  fs.unlinkSync(path.join(out, docs[1].storedPath));
  assert.deepEqual(store.verify(out).problems.map((p) => p.problem).sort(), ['content changed', 'missing']);
});
