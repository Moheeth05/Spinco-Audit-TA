# Spinco PDF store (Node.js)

Stores uploaded PDFs in an archive folder, **sorted and de-duplicated with the same rules the Spinco TA Audit
backend uses on upload**. Zero dependencies, Node 18+.

```
node backend/pdf_store/cli.js ingest <sourceFolder> [--out <archiveFolder>] [--kind auto|expense|trip] [--dry-run] [--json]
node backend/pdf_store/cli.js list   [--out <archiveFolder>] [--kind expense|trip|unsorted]
node backend/pdf_store/cli.js verify [--out <archiveFolder>]
cd backend/pdf_store && npm test
```

Default archive: `backend/pdf_archive/` (git-ignored). The source folder is only **read**; archived files are byte-for-byte copies.
Use `--dry-run` first to see what would happen.

## Archive layout
```
pdf_archive/
  expense_reports/<sha256[0:12]>_<sanitised name>.pdf
  trip_reports/<sha256[0:12]>_<sanitised name>.pdf
  unsorted/...            kind could not be decided (never guessed)
  index.json              one entry per stored document + every duplicate seen + every rejected file
```
`index.json` entry: `sha256, kind, documentNumber (ER-/TRIP- from the filename), originalFilename, storedPath, sizeBytes, pageCount,
classifiedBy (override|content|filename|none), notes, archivedAt, sources, duplicates[]`.

## Rules taken from the software
| Rule | Software source | Here |
|---|---|---|
| Validation: `.pdf` name, not empty, <= 25 MB, `%PDF-` header, readable, not password-protected, >= 1 page; same messages | `services/pdf_validation.py` | `validatePdf` - rejected files are listed in `index.json` and never stored |
| Duplicate = same SHA-256 of the bytes **within the same kind**; first upload wins, later copies are not stored ("File already exists.") | `api/uploads.py` | oldest file (mtime, then path) is canonical; others recorded under `duplicates`; re-running is idempotent |
| Same bytes as an expense report and as a trip report are two documents | separate tables | same, when the kind is explicit (`--kind` or the source folder name) |
| Stored name `<hash[:12]>_<sanitised name>`; unsafe characters become `_` | `services/storage_backend.py`, `storage.py` | identical |
| Kind (sorting): the upload channel decides; content signals are the software's own lists; `ER-<n>` / `TRIP-<n>` filename is the secondary signal | `classifier.py`, `filename_ids.py` | `--kind` / source folder `expense_reports`/`trip_reports` > content signals > filename > `unsorted/` |
| Document number from the filename only | `filename_ids.py` | `ER-<digits>` / `TRIP-<digits>`, else null |
| Upload list newest first | `GET /api/uploads` | `list` command |

Pointing `ingest` at a storage root that already has `expense_reports/` and `trip_reports/` keeps each file's kind, and the
software's `<hash12>_` prefix is recognised and not doubled.

## Differences you should know
* **Content signals read embedded text only.** A scanned PDF (or one with CID fonts) has none; it is then sorted by filename or
  goes to `unsorted/`. The software OCRs such pages; this tool does not.
* **Page count and password check are structural** (page objects, `/Encrypt`), not a full PDF parse. A PDF protected with an
  *empty* password, which the software would unlock, is rejected here.
* **Report month grouping is not done here.** The software derives the month from the extracted expense dates (OCR/parsing in the
  Python pipeline); that cannot be reproduced reliably without it.
* If the same bytes are named differently and no upload channel is known, they are treated as ONE document (a name is only a guess).
