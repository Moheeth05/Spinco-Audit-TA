'use strict';

const http = require('node:http');
const fs = require('node:fs');
const crypto = require('node:crypto');
const { ArchiveStore } = require('./store');
const { createSyncer } = require('./sync');
const { parseMultipart, MultipartError } = require('./multipart');

const KIND_FROM_WORD = { expense: 'expense_report', trip: 'trip_report', auto: null, '': null };

function sendJson(res, status, body, extra = {}) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': data.length, ...extra });
  res.end(data);
}

function digest(s) {
  return crypto.createHash('sha256').update(s).digest();
}

/** Constant-time check of a presented key against every configured key. */
function keyAllowed(presented, keys) {
  if (!presented) return false;
  const p = digest(presented);
  return keys.map((k) => crypto.timingSafeEqual(p, digest(k))).some(Boolean);
}

function presentedKey(req) {
  const auth = req.headers.authorization || '';
  if (/^Bearer\s+/i.test(auth)) return auth.replace(/^Bearer\s+/i, '').trim();
  return (req.headers['x-api-key'] || '').toString().trim();
}

/** Reads the request body up to `limit` bytes; rejects with {status:413} as soon as it is exceeded. */
function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const declared = Number(req.headers['content-length']);
    if (Number.isFinite(declared) && declared > limit) return reject({ status: 413, message: 'The request is too large.' });
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > limit) {
        reject({ status: 413, message: 'The request is too large.' });
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', () => reject({ status: 400, message: 'The upload was interrupted.' }));
  });
}

function createServer(cfg, store = new ArchiveStore(cfg.storageDir, { maxFileBytes: cfg.maxFileBytes })) {
  const log = cfg.quiet ? () => {} : (...a) => console.log(new Date().toISOString(), ...a);
  const syncer = createSyncer({
    store, folder: cfg.syncFolder, intervalMs: cfg.syncIntervalMs, settleMs: cfg.syncSettleMs ?? 3000, log,
  });

  async function handleUpload(req, res, forcedKind, url) {
    const type = req.headers['content-type'] || '';
    if (!/^multipart\/form-data/i.test(type)) return sendJson(res, 415, { detail: 'Send the PDFs as multipart/form-data in the "files" field.' });
    const body = await readBody(req, cfg.maxRequestBytes);
    let parts;
    try {
      parts = parseMultipart(body, type);
    } catch (err) {
      if (err instanceof MultipartError) return sendJson(res, 400, { detail: err.message });
      throw err;
    }
    let kind = forcedKind;
    if (forcedKind === undefined) {
      const field = parts.find((p) => p.name === 'kind' && p.filename === null);
      const word = (field ? field.data.toString('utf8') : url.searchParams.get('kind') || '').trim().toLowerCase();
      if (!(word in KIND_FROM_WORD)) return sendJson(res, 400, { detail: 'kind must be expense, trip or auto.' });
      kind = KIND_FROM_WORD[word];
    }
    const files = parts.filter((p) => p.filename !== null && (p.name === 'files' || p.name === 'file'));
    if (files.length === 0) return sendJson(res, 400, { detail: 'No files were uploaded (field name "files").' });
    const results = [];
    for (const f of files) results.push(await store.storeUpload({ buffer: f.data, filename: f.filename || 'document.pdf', kind }));
    log('upload', `${files.length} file(s)`, `stored=${results.filter((r) => r.success).length}`);
    return sendJson(res, 200, { results });
  }

  async function route(req, res) {
    const url = new URL(req.url, 'http://localhost');
    const p = url.pathname.replace(/\/+$/, '') || '/';

    if (cfg.corsOrigin) {
      res.setHeader('Access-Control-Allow-Origin', cfg.corsOrigin);
      res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-API-Key');
      res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
      res.setHeader('Vary', 'Origin');
      if (req.method === 'OPTIONS') return res.writeHead(204).end();
    }
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Cache-Control', 'no-store');

    if (req.method === 'GET' && p === '/api/health') return sendJson(res, 200, { status: 'ok' });

    if (cfg.apiKeys.length > 0 && !keyAllowed(presentedKey(req), cfg.apiKeys)) {
      return sendJson(res, 401, { detail: 'A valid API key is required (Authorization: Bearer <key> or X-API-Key).' }, { 'WWW-Authenticate': 'Bearer' });
    }

    if (req.method === 'POST' && p === '/api/uploads/expense-reports') return handleUpload(req, res, 'expense_report', url);
    if (req.method === 'POST' && p === '/api/uploads/trip-reports') return handleUpload(req, res, 'trip_report', url);
    if (req.method === 'POST' && p === '/api/uploads') return handleUpload(req, res, undefined, url);

    if (req.method === 'GET' && p === '/api/uploads') {
      const kind = KIND_FROM_WORD[(url.searchParams.get('kind') || '').toLowerCase()] ?? (url.searchParams.get('kind') === 'unsorted' ? 'unsorted' : null);
      return sendJson(res, 200, store.list({ kind }));
    }
    if (req.method === 'GET' && p === '/api/sync') return sendJson(res, 200, syncer.status());
    if (req.method === 'POST' && p === '/api/sync') {
      if (!cfg.syncFolder) return sendJson(res, 400, { detail: 'Folder sync is not configured (set SYNC_FOLDER).' });
      return sendJson(res, 200, { result: await syncer.runOnce(), status: syncer.status() });
    }
    if (req.method === 'GET' && p === '/api/stats') return sendJson(res, 200, store.stats());
    if (req.method === 'GET' && p === '/api/verify') return sendJson(res, 200, store.verify());

    const m = /^\/api\/documents\/([^/]+)(\/file)?$/.exec(p);
    if (m) {
      const doc = store.find(decodeURIComponent(m[1]));
      if (!doc) return sendJson(res, 404, { detail: 'Document not found.' });
      if (req.method === 'GET' && m[2]) {
        let file;
        try {
          file = store.filePath(doc);
          const stat = fs.statSync(file);
          res.writeHead(200, {
            'Content-Type': 'application/pdf', 'Content-Length': stat.size,
            'Content-Disposition': `inline; filename="${doc.storedFilename.replace(/"/g, '')}"`,
          });
          return fs.createReadStream(file).pipe(res);
        } catch {
          return sendJson(res, 404, { detail: 'The stored file is missing.' });
        }
      }
      if (req.method === 'GET') return sendJson(res, 200, ArchiveStore.publicView(doc));
      if (req.method === 'DELETE') {
        await store.remove(m[1]);
        log('delete', m[1]);
        return res.writeHead(204).end();
      }
    }
    return sendJson(res, 404, { detail: 'Not found.' });
  }

  const server = http.createServer((req, res) => {
    route(req, res).catch((err) => {
      if (err && err.status) return sendJson(res, err.status, { detail: err.message });
      log('error', err && err.stack ? err.stack.split('\n')[0] : String(err)); // never echo internals to the client
      if (!res.headersSent) sendJson(res, 500, { detail: 'Internal server error.' });
      else res.end();
    });
  });
  server.requestTimeout = 5 * 60 * 1000;
  server.headersTimeout = 30 * 1000;
  server.keepAliveTimeout = 5 * 1000;
  return { server, store, syncer };
}

module.exports = { createServer, keyAllowed };
