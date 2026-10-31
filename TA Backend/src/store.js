'use strict';

/**
 * File archive with a JSON index. All mutations run one at a time (a promise queue), so two simultaneous uploads of
 * the same PDF can never both be stored.
 *
 *   <dir>/expense_reports/<hash12>_<name>.pdf
 *   <dir>/trip_reports/...      <dir>/unsorted/...      <dir>/index.json
 */

const fs = require('node:fs');
const path = require('node:path');
const rules = require('./rules');

class ArchiveStore {
  constructor(dir, { maxFileBytes = rules.MAX_FILE_SIZE_BYTES } = {}) {
    this.dir = path.resolve(dir);
    this.maxFileBytes = maxFileBytes;
    this.indexFile = path.join(this.dir, 'index.json');
    fs.mkdirSync(this.dir, { recursive: true });
    this.idx = this._load();
    this._queue = Promise.resolve();
  }

  _load() {
    if (!fs.existsSync(this.indexFile)) return { version: 1, nextId: 1, updatedAt: null, documents: [], rejected: [] };
    const idx = JSON.parse(fs.readFileSync(this.indexFile, 'utf8'));
    idx.nextId ||= 1 + Math.max(0, ...(idx.documents || []).map((d) => d.id || 0));
    idx.documents ||= [];
    idx.rejected ||= [];
    return idx;
  }

  _save() {
    this.idx.updatedAt = new Date().toISOString();
    this.idx.rejected = this.idx.rejected.slice(-1000);
    const tmp = `${this.indexFile}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.idx, null, 2));
    fs.renameSync(tmp, this.indexFile);
  }

  _locked(fn) {
    const run = this._queue.then(fn);
    this._queue = run.catch(() => {});
    return run;
  }

  static documentId(d) {
    return `${rules.ID_PREFIX[d.kind]}:${d.id}`;
  }

  /** Public shape, matching the software's upload list / upload result fields. */
  static publicView(d) {
    return {
      document_id: ArchiveStore.documentId(d),
      source_type: { expense_report: 'EXPENSE_REPORT', trip_report: 'TRIP_REPORT', unsorted: 'UNSORTED' }[d.kind],
      kind: d.kind,
      filename: d.storedFilename,
      original_filename: d.originalFilename,
      document_number: d.documentNumber,
      file_size: d.sizeBytes,
      page_count: d.pageCount,
      sha256: d.sha256,
      classified_by: d.classifiedBy,
      notes: d.notes,
      processing_status: 'PROCESSED',
      uploaded_at: d.uploadedAt,
      duplicate_count: d.duplicates.length,
    };
  }

  /**
   * One upload, with the software's rules. `kind` is the upload channel: 'expense_report' | 'trip_report' | null (decide).
   * Returns an UploadResultItem-shaped object; never throws for a bad file.
   */
  storeUpload({ buffer, filename, kind = null, source = null }) {
    return this._locked(() => this._storeUpload({ buffer, filename: filename || 'document.pdf', kind, source }));
  }

  /** `source` (optional) names where the bytes came from (a folder sync passes the file path), so re-reading the same source is idempotent. */
  _storeUpload({ buffer, filename, kind, source }) {
    const check = rules.validatePdf(filename, buffer, { maxBytes: this.maxFileBytes });
    if (!check.ok) {
      this.idx.rejected.push({ filename, reason: check.error, at: new Date().toISOString() });
      this._save();
      return { filename, success: false, processing_status: 'FAILED', error_message: check.error };
    }
    const hash = rules.sha256(buffer);
    const decision = rules.classify({ buf: buffer, filename, override: kind });
    const finalKind = decision.kind;

    let same = this.idx.documents.find((d) => d.sha256 === hash && d.kind === finalKind);
    if (!same && decision.via !== 'override' && decision.via !== 'content') {
      // No upload channel and no content signal: the kind is only a filename guess, so identical bytes are ONE document.
      same = this.idx.documents.find((d) => d.sha256 === hash && (finalKind === 'unsorted' || d.kind !== 'unsorted'));
    }
    if (same) {
      same.sources ||= [];
      const seen = Boolean(source) && (same.sources.includes(source) || same.duplicates.some((d) => d.source === source));
      if (!seen) {
        same.duplicates.push({ filename, source, seenAt: new Date().toISOString() });
        this._save();
      }
      return {
        filename, success: false, document_id: ArchiveStore.documentId(same), processing_status: 'PROCESSED',
        error_message: 'File already exists.', already_imported: seen,
      };
    }

    const storedFilename = `${hash.slice(0, 12)}_${rules.sanitizeFilename(filename)}`;
    const relPath = path.posix.join(rules.KIND_DIRS[finalKind], storedFilename);
    const target = path.join(this.dir, rules.KIND_DIRS[finalKind], storedFilename);

    const parked = finalKind !== 'unsorted' ? this.idx.documents.find((d) => d.sha256 === hash && d.kind === 'unsorted') : null;
    try {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      if (parked) {
        fs.renameSync(path.join(this.dir, parked.storedPath), target); // same bytes were parked in unsorted: move, keep one copy
        Object.assign(parked, {
          kind: finalKind, storedFilename, storedPath: relPath, classifiedBy: decision.via,
          documentNumber: rules.extractDocumentNumber(filename, finalKind), notes: decision.notes,
        });
        parked.sources ||= [];
        if (source && !parked.sources.includes(source)) parked.sources.push(source);
        this._save();
        return { ...this._result(parked, check.pageCount), reclassified: true }; // one document, moved out of unsorted/
      }
      const tmp = `${target}.${process.pid}.part`;
      fs.writeFileSync(tmp, buffer);
      fs.renameSync(tmp, target);
    } catch {
      return { filename, success: false, processing_status: 'FAILED', error_message: 'The file could not be saved to storage.' };
    }
    const doc = {
      id: this.idx.nextId++,
      sha256: hash,
      kind: finalKind,
      documentNumber: rules.extractDocumentNumber(filename, finalKind),
      originalFilename: filename,
      storedFilename,
      storedPath: relPath,
      sizeBytes: buffer.length,
      pageCount: check.pageCount,
      classifiedBy: decision.via,
      notes: decision.notes,
      uploadedAt: new Date().toISOString(),
      sources: source ? [source] : [],
      duplicates: [],
    };
    this.idx.documents.push(doc);
    this._save();
    return this._result(doc, check.pageCount);
  }

  _result(d, pageCount) {
    return {
      filename: d.originalFilename, success: true, document_id: ArchiveStore.documentId(d), processing_status: 'PROCESSED',
      page_count: pageCount ?? d.pageCount, document_number: d.documentNumber, kind: d.kind, classified_by: d.classifiedBy,
    };
  }

  /** Newest first, like the software's upload list. */
  list({ kind = null } = {}) {
    return this.idx.documents
      .filter((d) => !kind || d.kind === kind)
      .sort((a, b) => (a.uploadedAt < b.uploadedAt ? 1 : a.uploadedAt > b.uploadedAt ? -1 : b.id - a.id))
      .map(ArchiveStore.publicView);
  }

  /** "expense:12", "trip:3", "unsorted:7" or just "12". Returns the raw entry or null. */
  find(documentId) {
    const m = /^(?:(?:expense|trip|unsorted):)?(\d+)$/.exec(String(documentId));
    return m ? this.idx.documents.find((d) => d.id === Number(m[1])) || null : null;
  }

  filePath(doc) {
    const p = path.resolve(this.dir, doc.storedPath);
    if (!p.startsWith(this.dir + path.sep)) throw new Error('Invalid stored path'); // never serve outside the archive
    return p;
  }

  remove(documentId) {
    return this._locked(() => {
      const doc = this.find(documentId);
      if (!doc) return false;
      try {
        fs.unlinkSync(this.filePath(doc));
      } catch {
        /* already gone */
      }
      this.idx.documents = this.idx.documents.filter((d) => d !== doc);
      this._save();
      return true;
    });
  }

  /** Re-hash every archived file against the index. */
  verify() {
    const problems = [];
    for (const d of this.idx.documents) {
      let status = 'ok';
      try {
        if (rules.sha256(fs.readFileSync(this.filePath(d))) !== d.sha256) status = 'content changed';
      } catch {
        status = 'missing';
      }
      if (status !== 'ok') problems.push({ document_id: ArchiveStore.documentId(d), stored_path: d.storedPath, problem: status });
    }
    return { checked: this.idx.documents.length, problems };
  }

  stats() {
    const by = { expense_report: 0, trip_report: 0, unsorted: 0 };
    for (const d of this.idx.documents) by[d.kind]++;
    return { documents: this.idx.documents.length, ...by, rejected_recorded: this.idx.rejected.length };
  }
}

module.exports = { ArchiveStore };
