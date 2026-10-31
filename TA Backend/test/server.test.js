'use strict';

// All PDFs are SYNTHETIC, built in memory. A real HTTP server is started on a free port per test.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { buildConfig } = require('../src/config');
const { createServer, keyAllowed } = require('../src/server');
const { parseMultipart, MultipartError } = require('../src/multipart');
const rules = require('../src/rules');

function makePdf({ pages = 1, text = '', encrypt = false, noEof = false, comment = '' } = {}) {
  const pageObjs = Array.from({ length: pages }, (_, i) => `${3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n`).join('');
  const content = text ? `4 0 obj\n<< /Length ${text.length + 30} >>\nstream\nBT /F1 12 Tf (${text}) Tj ET\nendstream\nendobj\n` : '';
  return Buffer.from(
    `%PDF-1.4\n% ${comment}\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Count ${pages} >>\nendobj\n${pageObjs}${content}` +
      `trailer\n<< /Root 1 0 R ${encrypt ? '/Encrypt 9 0 R' : ''} >>\nstartxref\n0\n${noEof ? '' : '%%EOF\n'}`,
    'latin1',
  );
}

async function start(env = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tabackend-'));
  const cfg = { ...buildConfig({ ...env, STORAGE_DIR: dir }), quiet: true };
  const { server, store } = createServer(cfg);
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { base, dir, store, server, close: () => new Promise((r) => { server.closeAllConnections?.(); server.close(r); }) };
}

function form(files, fields = {}) {
  const f = new FormData();
  for (const [k, v] of Object.entries(fields)) f.append(k, v);
  for (const { name, buf } of files) f.append('files', new Blob([buf], { type: 'application/pdf' }), name);
  return f;
}
const post = (base, p, files, fields, headers = {}) => fetch(base + p, { method: 'POST', body: form(files, fields), headers });
const ls = (d) => (fs.existsSync(d) ? fs.readdirSync(d) : []);

test('health is open; the server answers JSON and sets nosniff', async () => {
  const s = await start();
  const r = await fetch(`${s.base}/api/health`);
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { status: 'ok' });
  assert.equal(r.headers.get('x-content-type-options'), 'nosniff');
  await s.close();
});

test('uploads to the expense channel: stored as <hash12>_<name>, id from the filename, software-shaped result', async () => {
  const s = await start();
  const pdf = makePdf({ pages: 2, comment: 'e1' });
  const r = await post(s.base, '/api/uploads/expense-reports', [{ name: 'ER-39832.pdf', buf: pdf }]);
  assert.equal(r.status, 200);
  const [item] = (await r.json()).results;
  assert.equal(item.success, true);
  assert.equal(item.processing_status, 'PROCESSED');
  assert.equal(item.page_count, 2);
  assert.equal(item.document_number, 'ER-39832');
  assert.match(item.document_id, /^expense:\d+$/);
  const stored = ls(path.join(s.dir, 'expense_reports'));
  assert.deepEqual(stored, [`${rules.sha256(pdf).slice(0, 12)}_ER-39832.pdf`]);
  assert.ok(fs.readFileSync(path.join(s.dir, 'expense_reports', stored[0])).equals(pdf)); // byte-for-byte
  await s.close();
});

test('duplicates: the software message, nothing stored twice, per-kind rule kept', async () => {
  const s = await start();
  const pdf = makePdf({ comment: 'dup' });
  await post(s.base, '/api/uploads/expense-reports', [{ name: 'ER-1.pdf', buf: pdf }]);
  const again = await (await post(s.base, '/api/uploads/expense-reports', [{ name: 'renamed.pdf', buf: pdf }])).json();
  assert.equal(again.results[0].success, false);
  assert.equal(again.results[0].error_message, 'File already exists.');
  assert.match(again.results[0].document_id, /^expense:\d+$/);
  assert.equal(ls(path.join(s.dir, 'expense_reports')).length, 1);
  // same bytes on the TRIP channel are a different document, as in the software
  const trip = await (await post(s.base, '/api/uploads/trip-reports', [{ name: 'TRIP-5.pdf', buf: pdf }])).json();
  assert.equal(trip.results[0].success, true);
  assert.equal(ls(path.join(s.dir, 'trip_reports')).length, 1);
  assert.equal(s.store.list().find((d) => d.kind === 'expense_report').duplicate_count, 1);
  await s.close();
});

test('duplicates: parallel uploads of the same file store exactly one copy', async () => {
  const s = await start();
  const pdf = makePdf({ comment: 'race' });
  const rs = await Promise.all(Array.from({ length: 8 }, (_, i) => post(s.base, '/api/uploads/expense-reports', [{ name: `copy${i}.pdf`, buf: pdf }]).then((r) => r.json())));
  assert.equal(rs.filter((r) => r.results[0].success).length, 1);
  assert.equal(rs.filter((r) => r.results[0].error_message === 'File already exists.').length, 7);
  assert.equal(ls(path.join(s.dir, 'expense_reports')).length, 1);
  await s.close();
});

test('invalid files are refused with the software messages and never stored', async () => {
  const s = await start();
  const r = await (await post(s.base, '/api/uploads/expense-reports', [
    { name: 'notes.txt', buf: makePdf() },
    { name: 'empty.pdf', buf: Buffer.alloc(0) },
    { name: 'fake.pdf', buf: Buffer.from('hello') },
    { name: 'cut.pdf', buf: makePdf({ noEof: true }) },
    { name: 'locked.pdf', buf: makePdf({ encrypt: true }) },
    { name: 'blank.pdf', buf: makePdf({ pages: 0 }) },
  ])).json();
  assert.deepEqual(r.results.map((x) => x.error_message), [
    'Only PDF files are supported.',
    'The uploaded file is empty.',
    'This file does not appear to be a valid PDF.',
    'The PDF could not be read - it may be corrupted.',
    'This PDF is password-protected and cannot be processed. Remove the password and re-upload.',
    'The PDF has no pages.',
  ]);
  assert.ok(r.results.every((x) => x.success === false && x.processing_status === 'FAILED'));
  assert.equal(s.store.list().length, 0);
  await s.close();
});

test('the per-file size limit follows MAX_FILE_MB', async () => {
  const s = await start({ MAX_FILE_MB: '1', MAX_REQUEST_MB: '5' });
  const big = Buffer.concat([makePdf({ comment: 'big' }), Buffer.alloc(1.2 * 1024 * 1024, 0x20)]);
  const r = await (await post(s.base, '/api/uploads/expense-reports', [{ name: 'big.pdf', buf: big }])).json();
  assert.equal(r.results[0].error_message, 'File exceeds the 1 MB upload limit.');
  await s.close();
});

test('a request larger than MAX_REQUEST_MB is cut off with 413', async () => {
  const s = await start({ MAX_FILE_MB: '1', MAX_REQUEST_MB: '2' });
  const r = await post(s.base, '/api/uploads/expense-reports', [{ name: 'a.pdf', buf: Buffer.alloc(3 * 1024 * 1024, 0x41) }]);
  assert.equal(r.status, 413);
  await s.close();
});

test('automatic sorting on /api/uploads: content, then filename, else unsorted; "kind" field overrides', async () => {
  const s = await start();
  const r = await (await post(s.base, '/api/uploads', [
    { name: 'ER-39832.pdf', buf: makePdf({ comment: 'a' }) },
    { name: 'TRIP-24935_Trip_To_Indore.pdf', buf: makePdf({ comment: 'b' }) },
    { name: 'misnamed.pdf', buf: makePdf({ text: 'Trip Summary Trip Number TRIP-9', comment: 'c' }) },
    { name: 'scan.pdf', buf: makePdf({ comment: 'd' }) },
  ])).json();
  assert.deepEqual(r.results.map((x) => [x.kind, x.classified_by]), [['expense_report', 'filename'], ['trip_report', 'filename'], ['trip_report', 'content'], ['unsorted', 'none']]);
  assert.equal(ls(path.join(s.dir, 'unsorted')).length, 1);
  const forced = await (await post(s.base, '/api/uploads', [{ name: 'scan2.pdf', buf: makePdf({ comment: 'e' }) }], { kind: 'expense' })).json();
  assert.deepEqual([forced.results[0].kind, forced.results[0].classified_by], ['expense_report', 'override']);
  const bad = await post(s.base, '/api/uploads', [{ name: 'x.pdf', buf: makePdf() }], { kind: 'nonsense' });
  assert.equal(bad.status, 400);
  await s.close();
});

test('identical bytes named differently are ONE document, and an unsorted file moves once its kind is known', async () => {
  const s = await start();
  const pdf = makePdf({ comment: 'park' });
  const first = await (await post(s.base, '/api/uploads', [{ name: 'scan_001.pdf', buf: pdf }])).json();
  assert.equal(first.results[0].kind, 'unsorted');
  const second = await (await post(s.base, '/api/uploads', [{ name: 'ER-77.pdf', buf: pdf }])).json();
  assert.equal(second.results[0].success, true);
  assert.equal(second.results[0].kind, 'expense_report');
  assert.equal(s.store.list().length, 1);
  assert.equal(ls(path.join(s.dir, 'unsorted')).length, 0);
  assert.equal(ls(path.join(s.dir, 'expense_reports')).length, 1);
  const third = await (await post(s.base, '/api/uploads', [{ name: 'other_name.pdf', buf: pdf }])).json();
  assert.equal(third.results[0].error_message, 'File already exists.');
  await s.close();
});

test('list is newest first and filterable; metadata, file download and delete work', async () => {
  const s = await start();
  const a = makePdf({ comment: 'a' });
  await post(s.base, '/api/uploads/expense-reports', [{ name: 'ER-1.pdf', buf: a }]);
  await new Promise((r) => setTimeout(r, 15));
  await post(s.base, '/api/uploads/trip-reports', [{ name: 'TRIP-2.pdf', buf: makePdf({ comment: 'b' }) }]);
  const all = await (await fetch(`${s.base}/api/uploads`)).json();
  assert.deepEqual(all.map((d) => d.document_number), ['TRIP-2', 'ER-1']);
  assert.deepEqual((await (await fetch(`${s.base}/api/uploads?kind=expense`)).json()).map((d) => d.document_number), ['ER-1']);
  const id = all[1].document_id;
  const meta = await (await fetch(`${s.base}/api/documents/${id}`)).json();
  assert.equal(meta.sha256, rules.sha256(a));
  const file = await fetch(`${s.base}/api/documents/${id}/file`);
  assert.equal(file.headers.get('content-type'), 'application/pdf');
  assert.ok(Buffer.from(await file.arrayBuffer()).equals(a));
  assert.equal((await fetch(`${s.base}/api/documents/${id}`, { method: 'DELETE' })).status, 204);
  assert.equal((await fetch(`${s.base}/api/documents/${id}`)).status, 404);
  assert.equal(ls(path.join(s.dir, 'expense_reports')).length, 0);
  assert.equal((await fetch(`${s.base}/api/documents/..%2F..%2Fx`)).status, 404); // no path access through ids
  await s.close();
});

test('the archive survives a restart: ids continue and old files are still recognised as duplicates', async () => {
  const s = await start();
  const pdf = makePdf({ comment: 'persist' });
  await post(s.base, '/api/uploads/expense-reports', [{ name: 'ER-1.pdf', buf: pdf }]);
  await s.close();
  const { ArchiveStore } = require('../src/store');
  const store2 = new ArchiveStore(s.dir);
  assert.equal(store2.list().length, 1);
  const dup = await store2.storeUpload({ buffer: pdf, filename: 'again.pdf', kind: 'expense_report' });
  assert.equal(dup.error_message, 'File already exists.');
  const next = await store2.storeUpload({ buffer: makePdf({ comment: 'second' }), filename: 'ER-2.pdf', kind: 'expense_report' });
  assert.match(next.document_id, /^expense:2$/);
  assert.deepEqual(store2.verify(), { checked: 2, problems: [] });
});

test('with an API key every route but /api/health needs it (Bearer or X-API-Key); wrong keys are refused', async () => {
  const key = 'k'.repeat(24);
  const s = await start({ TA_API_KEY: key });
  assert.equal((await fetch(`${s.base}/api/health`)).status, 200);
  assert.equal((await fetch(`${s.base}/api/uploads`)).status, 401);
  assert.equal((await fetch(`${s.base}/api/uploads`, { headers: { Authorization: `Bearer ${'x'.repeat(24)}` } })).status, 401);
  assert.equal((await fetch(`${s.base}/api/uploads`, { headers: { Authorization: `Bearer ${key}` } })).status, 200);
  assert.equal((await fetch(`${s.base}/api/uploads`, { headers: { 'X-API-Key': key } })).status, 200);
  const noKeyUpload = await post(s.base, '/api/uploads/expense-reports', [{ name: 'ER-1.pdf', buf: makePdf() }]);
  assert.equal(noKeyUpload.status, 401);
  assert.equal(s.store.list().length, 0);
  assert.equal(keyAllowed('a', ['b']), false);
  assert.equal(keyAllowed('', ['b']), false);
  await s.close();
});

test('configuration refuses an open server on a network address and short keys', () => {
  assert.throws(() => buildConfig({ HOST: '0.0.0.0' }), /without TA_API_KEY/);
  assert.throws(() => buildConfig({ HOST: '0.0.0.0', TA_API_KEY: 'short' }), /at least 16/);
  assert.equal(buildConfig({ HOST: '0.0.0.0', TA_API_KEY: 'k'.repeat(20) }).local, false);
  assert.equal(buildConfig({}).host, '127.0.0.1');
  assert.equal(buildConfig({}).port, 4000);
  assert.throws(() => buildConfig({ PORT: 'abc' }), /PORT must be/);
  assert.throws(() => buildConfig({ MAX_FILE_MB: '50', MAX_REQUEST_MB: '10' }), /must not exceed/);
});

test('multipart parser: binary-safe bodies and malformed input', () => {
  const bin = Buffer.from([0, 255, 13, 10, 45, 45, 1, 2]); // includes CRLF and dashes inside the data
  const boundary = 'XyZ123';
  const body = Buffer.concat([
    Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="kind"\r\n\r\nauto\r\n--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="a b.pdf"\r\nContent-Type: application/pdf\r\n\r\n`),
    bin,
    Buffer.from(`\r\n--${boundary}--\r\n`),
  ]);
  const parts = parseMultipart(body, `multipart/form-data; boundary=${boundary}`);
  assert.equal(parts.length, 2);
  assert.equal(parts[0].filename, null);
  assert.equal(parts[0].data.toString(), 'auto');
  assert.equal(parts[1].filename, 'a b.pdf');
  assert.ok(parts[1].data.equals(bin));
  assert.throws(() => parseMultipart(Buffer.from('junk'), 'multipart/form-data; boundary=XyZ123'), MultipartError);
  assert.throws(() => parseMultipart(body, 'multipart/form-data'), /boundary/);
});

test('non-multipart uploads get 415 and unknown routes 404; no stack traces leak', async () => {
  const s = await start();
  const r = await fetch(`${s.base}/api/uploads/expense-reports`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  assert.equal(r.status, 415);
  assert.equal((await fetch(`${s.base}/nope`)).status, 404);
  const empty = await fetch(`${s.base}/api/uploads/expense-reports`, { method: 'POST', body: new FormData() });
  assert.equal(empty.status, 400);
  await s.close();
});
