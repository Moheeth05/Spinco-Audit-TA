'use strict';

// SYNTHETIC PDFs in a temp folder laid out like the desktop app's documents folder. No real documents are read.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { ArchiveStore } = require('../src/store');
const { createSyncer, originalNameOf } = require('../src/sync');
const { buildConfig } = require('../src/config');
const { createServer } = require('../src/server');
const rules = require('../src/rules');

function makePdf(comment) {
  return Buffer.from(`%PDF-1.4\n% ${comment}\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Count 1 >>\nendobj\n3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\nstartxref\n0\n%%EOF\n`, 'latin1');
}
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'tasync-'));
function put(dir, rel, buf, mtime) {
  const f = path.join(dir, rel);
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, buf);
  if (mtime) fs.utimesSync(f, mtime, mtime);
  return f;
}
const old = new Date(Date.now() - 60_000); // well past the "still being written" window
const setup = () => {
  const src = tmp(), dir = tmp();
  const store = new ArchiveStore(dir);
  return { src, dir, store, syncer: createSyncer({ store, folder: src, settleMs: 1000 }) };
};
const ls = (d) => (fs.existsSync(d) ? fs.readdirSync(d) : []);
const appName = (buf, name) => `${rules.sha256(buf).slice(0, 12)}_${name}`; // how the desktop app names stored files

test('the app folder layout is kept: expense_reports/ and trip_reports/ decide the kind; the hash prefix is dropped from the name', async () => {
  const { src, dir, store, syncer } = setup();
  const e = makePdf('e'), t = makePdf('t'), u = makePdf('u');
  put(src, `expense_reports/${appName(e, 'ER-39832.pdf')}`, e, old);
  put(src, `trip_reports/${appName(t, 'TRIP-24935_Trip.pdf')}`, t, old);
  put(src, 'other/random_name.pdf', u, old);
  put(src, 'excel/Spinco_TA_Audit_Consolidated.xlsx', Buffer.from('PK'), old); // not a PDF: ignored
  const r = await syncer.runOnce();
  assert.deepEqual([r.scanned, r.stored, r.duplicates, r.rejected], [3, 3, 0, 0]);
  const docs = store.list();
  assert.deepEqual(docs.map((d) => [d.original_filename, d.kind]).sort(), [['ER-39832.pdf', 'expense_report'], ['TRIP-24935_Trip.pdf', 'trip_report'], ['random_name.pdf', 'unsorted']]);
  assert.equal(ls(path.join(dir, 'expense_reports')).length, 1);
  assert.equal(ls(path.join(dir, 'trip_reports')).length, 1);
  assert.equal(ls(path.join(dir, 'unsorted')).length, 1);
  assert.deepEqual(store.verify(), { checked: 3, problems: [] });
});

test('archived files are byte-for-byte copies and the source folder is never changed', async () => {
  const { src, dir, store, syncer } = setup();
  const buf = makePdf('copy');
  const f = put(src, `expense_reports/${appName(buf, 'ER-1.pdf')}`, buf, old);
  const before = fs.statSync(f).mtimeMs;
  await syncer.runOnce();
  assert.ok(fs.readFileSync(path.join(dir, store.list()[0] ? 'expense_reports' : '', ls(path.join(dir, 'expense_reports'))[0])).equals(buf));
  assert.ok(fs.existsSync(f));
  assert.equal(fs.statSync(f).mtimeMs, before);
  assert.deepEqual(ls(path.dirname(f)), [path.basename(f)]);
});

test('running again is idempotent: nothing stored twice and no duplicate records pile up', async () => {
  const { src, store, syncer } = setup();
  const buf = makePdf('idem');
  put(src, `expense_reports/${appName(buf, 'ER-2.pdf')}`, buf, old);
  await syncer.runOnce();
  for (let i = 0; i < 3; i++) {
    const r = await syncer.runOnce();
    assert.deepEqual([r.stored, r.duplicates, r.unchanged], [0, 0, 1]);
  }
  assert.equal(store.list().length, 1);
  assert.equal(store.list()[0].duplicate_count, 0);
  // a brand-new syncer (as after a server restart) re-reads the file once but still adds nothing
  const again = await createSyncer({ store, folder: src, settleMs: 1000 }).runOnce();
  assert.deepEqual([again.stored, again.duplicates], [0, 0]);
  assert.equal(store.list()[0].duplicate_count, 0);
});

test('duplicates in the source (same bytes, different names) are archived once and recorded once', async () => {
  const { src, store, syncer } = setup();
  const buf = makePdf('dup');
  put(src, `expense_reports/${appName(buf, 'ER-3.pdf')}`, buf, old);
  put(src, 'expense_reports/copy_of_it.pdf', buf, old);
  const r = await syncer.runOnce();
  assert.deepEqual([r.stored, r.duplicates], [1, 1]);
  assert.equal(store.list().length, 1);
  assert.equal(store.list()[0].duplicate_count, 1);
  await syncer.runOnce();
  assert.equal(store.list()[0].duplicate_count, 1);
});

test('the same bytes found first outside the app folders and then in expense_reports/ stay ONE document (moved, counted as reclassified)', async () => {
  const { src, dir, store, syncer } = setup();
  const buf = makePdf('moved');
  put(src, 'backup/copy_of_it.pdf', buf, old); // read first: kind unknown -> unsorted
  put(src, `expense_reports/${appName(buf, 'ER-3.pdf')}`, buf, old);
  const r = await syncer.runOnce();
  assert.deepEqual([r.stored, r.reclassified, r.duplicates], [1, 1, 0]);
  assert.equal(store.list().length, 1);
  assert.equal(store.list()[0].kind, 'expense_report');
  assert.equal(ls(path.join(dir, 'unsorted')).length, 0);
  assert.equal(ls(path.join(dir, 'expense_reports')).length, 1);
});

test('a file added later is picked up on the next pass; one still being written is skipped until it settles', async () => {
  const { src, store, syncer } = setup();
  await syncer.runOnce();
  assert.equal(store.list().length, 0);
  const a = makePdf('late');
  put(src, `trip_reports/${appName(a, 'TRIP-9.pdf')}`, a, old);
  const fresh = makePdf('fresh');
  const freshPath = put(src, `expense_reports/${appName(fresh, 'ER-9.pdf')}`, fresh); // modified just now
  const r = await syncer.runOnce();
  assert.deepEqual([r.stored, r.skippedRecent], [1, 1]);
  fs.utimesSync(freshPath, old, old); // the app finished writing
  const r2 = await syncer.runOnce();
  assert.equal(r2.stored, 1);
  assert.equal(store.list().length, 2);
});

test('a half-written / invalid PDF is rejected, not stored, and re-checked only when it changes', async () => {
  const { src, store, syncer } = setup();
  const f = put(src, 'expense_reports/abc_partial.pdf', Buffer.from('%PDF-1.4\n1 0 obj\n<< /Type /Page >>\n'), old); // no %%EOF yet
  const r = await syncer.runOnce();
  assert.deepEqual([r.stored, r.rejected], [0, 1]);
  assert.deepEqual((await syncer.runOnce()).rejected, 0); // unchanged: not re-evaluated
  fs.writeFileSync(f, makePdf('now complete'));
  fs.utimesSync(f, old, old);
  const r3 = await syncer.runOnce();
  assert.equal(r3.stored, 1);
  assert.equal(store.list().length, 1);
});

test('deleting a file from the source leaves the archived copy (the archive is a record, not a mirror)', async () => {
  const { src, dir, store, syncer } = setup();
  const buf = makePdf('gone');
  const f = put(src, `expense_reports/${appName(buf, 'ER-4.pdf')}`, buf, old);
  await syncer.runOnce();
  fs.unlinkSync(f);
  await syncer.runOnce();
  assert.equal(store.list().length, 1);
  assert.equal(ls(path.join(dir, 'expense_reports')).length, 1);
});

test('a missing sync folder is reported, not fatal, and is picked up when it appears', async () => {
  const dir = tmp();
  const store = new ArchiveStore(dir);
  const missing = path.join(tmp(), 'not-yet');
  const syncer = createSyncer({ store, folder: missing, settleMs: 1000 });
  await syncer.runOnce();
  assert.match(syncer.status().lastError, /not found/);
  const buf = makePdf('appears');
  put(missing, `expense_reports/${appName(buf, 'ER-5.pdf')}`, buf, old);
  await syncer.runOnce();
  assert.equal(syncer.status().lastError, null);
  assert.equal(store.list().length, 1);
});

test('uploads through the API and the sync share one store without losing either (no lost index entries)', async () => {
  const dir = tmp(), src = tmp();
  const cfg = { ...buildConfig({ STORAGE_DIR: dir, SYNC_FOLDER: src }), quiet: true, syncSettleMs: 0 };
  const { server, store, syncer } = createServer(cfg);
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  for (let i = 0; i < 5; i++) put(src, `expense_reports/${appName(makePdf(`s${i}`), `ER-${100 + i}.pdf`)}`, makePdf(`s${i}`), old);
  const up = (name, c) => { const f = new FormData(); f.append('files', new Blob([makePdf(c)]), name); return fetch(`${base}/api/uploads/trip-reports`, { method: 'POST', body: f }); };
  const [a, b, c, d, e] = await Promise.all([fetch(`${base}/api/sync`, { method: 'POST' }), up('TRIP-1.pdf', 'u1'), up('TRIP-2.pdf', 'u2'), up('TRIP-3.pdf', 'u3'), up('TRIP-4.pdf', 'u4')]);
  assert.equal(a.status, 200);
  assert.ok([b, c, d, e].every((r) => r.status === 200));
  assert.equal(store.list().length, 9); // 5 synced + 4 uploaded
  assert.deepEqual(new ArchiveStore(dir).verify(), { checked: 9, problems: [] }); // what is on disk is complete
  const st = await (await fetch(`${base}/api/sync`)).json();
  assert.equal(st.enabled, true);
  assert.equal(st.totals.stored, 5);
  await new Promise((r) => { server.closeAllConnections?.(); server.close(r); });
});

test('POST /api/sync says so when sync is not configured; SYNC_FOLDER may not overlap STORAGE_DIR', async () => {
  const cfg = { ...buildConfig({ STORAGE_DIR: tmp() }), quiet: true };
  const { server } = createServer(cfg);
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const r = await fetch(`http://127.0.0.1:${server.address().port}/api/sync`, { method: 'POST' });
  assert.equal(r.status, 400);
  await new Promise((rs) => { server.closeAllConnections?.(); server.close(rs); });
  const same = tmp();
  assert.throws(() => buildConfig({ STORAGE_DIR: same, SYNC_FOLDER: same }), /must not be the same folder/);
  assert.throws(() => buildConfig({ STORAGE_DIR: path.join(same, 'x'), SYNC_FOLDER: same }), /must not be the same folder/);
  assert.equal(originalNameOf('abcdef123456_ER-1.pdf', 'abcdef1234567890'), 'ER-1.pdf');
  assert.equal(originalNameOf('abcdef123456_ER-1.pdf', 'ffffffffffff0000'), 'abcdef123456_ER-1.pdf'); // not this file's own prefix
});
