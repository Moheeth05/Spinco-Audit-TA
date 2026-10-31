# TA Backend

Node.js upload server. It receives user-uploaded PDFs and stores them **sorted and de-duplicated with the same rules the
Spinco TA Audit software uses**. No dependencies: only Node.js 18 or newer.

## Run it on this computer (localhost)
```
cd "D:\Bills\TA Backend"
npm start            # or double-click start.cmd
```
It listens on **http://localhost:4000** and only this computer can reach it (no API key needed in this mode).
Check: open http://localhost:4000/api/health  ->  `{"status":"ok"}`. Stop with Ctrl+C.
PDFs and `index.json` are kept in `TA Backend\storage\` (git-ignored).

## Why uploads from the desktop app did not appear in `storage\`
The desktop app (Spinco TA Audit) and TA Backend are **two separate programs**. The app saves what you upload in its **own**
documents folder (`%APPDATA%\Spinco TA Audit\documents` for the installed/portable EXE, `D:\Bills\storage` for the dev servers) and
never sends it to TA Backend. TA Backend only stores files that are **uploaded to it** (its `/api/uploads` endpoints) or **synced** from a folder.

### Automatic copy from the app (Spinco TA Audit 1.2.8 and later)
From 1.2.8 the desktop app itself sends every upload it **accepts** to TA Backend, right after saving it, on the channel you chose
(expense report / trip report). Nothing to configure when TA Backend runs on the same computer (`http://127.0.0.1:4000`):
start TA Backend, upload in the app, and the file appears in `storage\` within seconds.
* It is best effort and in the background: if TA Backend is not running the upload works exactly as before (the folder sync below catches up later).
* Files the app refuses, or already has (duplicates), are not sent.
* Settings (environment variables of the app): `TA_BACKEND_URL` (default `http://127.0.0.1:4000`; `off` disables it),
  `TA_BACKEND_API_KEY` (needed when TA Backend runs with `TA_API_KEY`), `TA_BACKEND_TIMEOUT_SECONDS` (30).
* Earlier uploads are not re-sent; use the folder sync below once to bring them in.

### Folder sync (works with any app version)
Start TA Backend with `SYNC_FOLDER` pointing at the app's documents folder and it copies every PDF into `storage\`, sorted and
de-duplicated, then checks again every 30 seconds (new uploads appear within about a minute):
```
double-click start-with-sync.cmd         (uses %APPDATA%\Spinco TA Audit\documents; edit the file for another folder)
```
or put `SYNC_FOLDER=<folder>` in `.env`. You will see a line like `sync : copying PDFs from ...` at start-up, and
`GET http://localhost:4000/api/sync` shows what was copied; `POST /api/sync` copies right now.
* The app's folder is **only read**; nothing in it is moved, changed or deleted.
* `expense_reports\` and `trip_reports\` inside it decide the kind (the app's own upload channel); other folders are auto-sorted.
* The app's `<hash>_` filename prefix is removed, files already stored are skipped (same bytes = same document), and a file the app is
  still writing is picked up on the next pass.
* If a file is later deleted in the app, the copy in `storage\` stays (the archive is a record, not a mirror).
* **Restart TA Backend after updating it** (close its window / Ctrl+C, then start it again) so the new code is used.

## Run it on a server
1. Install Node.js 18+ (LTS) on the server and copy the whole `TA Backend` folder there (no `npm install` needed).
2. Create `.env` from `.env.example`:
   ```
   HOST=0.0.0.0
   PORT=4000
   TA_API_KEY=<a long random key>      node -e "console.log(require('crypto').randomBytes(24).toString('hex'))"
   STORAGE_DIR=D:\ta-data              (or /var/lib/ta-backend) - a folder outside the app folder is best
   ```
   The server **refuses to start** on a network address without `TA_API_KEY`, and every API route except `/api/health` then needs the key.
3. Start it: `npm start`. Keep it running as a service:
   * Windows: NSSM (`nssm install TABackend "C:\Program Files\nodejs\node.exe" "D:\TA Backend\index.js"`) or Task Scheduler "at startup".
   * Linux: a systemd unit (`ExecStart=/usr/bin/node /opt/ta-backend/index.js`, `WorkingDirectory=/opt/ta-backend`, `Restart=always`, a non-root `User=`) or `pm2 start index.js --name ta-backend`.
4. **Put HTTPS in front of it** (nginx, Caddy or IIS reverse proxy to `127.0.0.1:4000`). The API key travels in a header, so never
   expose plain HTTP on the internet. Set the proxy's upload limit to at least `MAX_REQUEST_MB` (nginx: `client_max_body_size 150m;`).
   With a proxy in front, keep `HOST=127.0.0.1` and let only the proxy reach it.
5. Open only the proxy's port (443) in the firewall. **Back up the `STORAGE_DIR` folder** (PDFs + `index.json`).

Settings (environment or `.env`): `HOST` (127.0.0.1), `PORT` (4000), `STORAGE_DIR` (storage), `TA_API_KEY`, `MAX_FILE_MB` (25),
`MAX_REQUEST_MB` (150), `CORS_ORIGIN` (only if a browser app on another origin calls it).

## API
Send the key as `Authorization: Bearer <key>` (or `X-API-Key`) when one is configured.

| Method and path | What it does |
|---|---|
| `POST /api/uploads/expense-reports` | upload one or more PDFs (multipart field `files`) as **expense reports** |
| `POST /api/uploads/trip-reports` | the same as **trip reports** |
| `POST /api/uploads` | upload and **sort automatically**; optional form field `kind` = `expense` / `trip` / `auto` |
| `GET /api/uploads[?kind=expense\|trip\|unsorted]` | list, newest first |
| `GET /api/documents/<id>` , `/file` | metadata, or the PDF itself (`id` like `expense:3`) |
| `DELETE /api/documents/<id>` | remove a document |
| `GET /api/stats` , `/api/verify` | counts, and a re-hash of every stored file against the index |
| `GET /api/sync` , `POST /api/sync` | folder-sync status, or copy new files from `SYNC_FOLDER` right now |
| `GET /api/health` | liveness (no key needed) |

Upload response (same fields as the software): `{"results":[{"filename","success","document_id","processing_status","page_count","document_number","error_message"}]}`
```
curl -H "Authorization: Bearer KEY" -F "files=@ER-39832.pdf" http://localhost:4000/api/uploads/expense-reports
curl -F "files=@a.pdf" -F "files=@b.pdf" http://localhost:4000/api/uploads          # automatic sorting
```

## The rules (taken from the software)
| Rule | Software source |
|---|---|
| Accepts only: `.pdf` name, not empty, <= 25 MB, starts with `%PDF-`, readable, not password-protected, >= 1 page. Same messages. | `services/pdf_validation.py` |
| **Duplicate** = same SHA-256 of the bytes within the same kind -> `"File already exists."`, nothing stored again. The same bytes uploaded as an expense report *and* as a trip report are two documents. | `api/uploads.py` |
| Stored as `<first 12 hex of SHA-256>_<sanitised name>` in `expense_reports/` or `trip_reports/` | `services/storage_backend.py` |
| **Sorting**: the upload endpoint (channel) decides; on `/api/uploads` the software's content signals decide, then the `ER-<n>` / `TRIP-<n>` filename, else `unsorted/` (never guessed) | `classifier.py`, `filename_ids.py` |
| Document number comes from the filename only | `filename_ids.py` |
| Upload list newest first | `GET /api/uploads` |

Extra, on purpose: identical bytes under a different name with no channel/content signal are ONE document (a name is only a guess);
a file parked in `unsorted/` moves to its kind when the same bytes arrive with a decisive kind; simultaneous uploads of the same
file store exactly one copy.

## Differences from the Python software
* Content signals read **embedded text only** (no OCR): a scanned PDF is sorted by filename or goes to `unsorted/`.
* Page count and the password check are structural, not a full PDF parse; a PDF locked with an *empty* password is rejected here.
* No extraction, bill matching or report-month grouping: this server stores and sorts files only.
* Uploads are buffered in memory (up to `MAX_REQUEST_MB`), single process, JSON index: right for one office's volume,
  not for thousands of uploads per minute. Use a reverse proxy for TLS and rate limiting.

## Tests
`npm test` (15 tests; real HTTP server on a free port, synthetic PDFs only).
