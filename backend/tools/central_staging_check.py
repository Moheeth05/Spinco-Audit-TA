"""End-to-end CENTRAL STORAGE check against the STAGING Google Shared Drive (or, with --fake-drive, against the in-memory fake, to prove this script itself).

It runs the real application in-process on a NEW disposable database and a NEW storage folder, in `serve` mode, with SYNTHETIC PDFs only, and reports one
PASS / FAIL line per check. It never prints a credential, token, Drive id or file content. It never deletes or overwrites anything that existed before: it only
creates the synthetic files of this run (and archives / restores those, which is reversible).

SAFETY (all enforced before the first Drive call):
  * SPINCO_ENVIRONMENT must be `staging`, and the staging Shared Drive id (SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING) must be configured - the destination guard then
    refuses any write outside that Shared Drive. A production id is refused.
  * the work folder must be under D:\\SpincoTA-artifacts\\central-staging\\ (a new sub-folder per run).

How to run (PowerShell, from D:\\Bills\\backend; the credentials are read from the environment / files exactly as the backend reads them):
    $env:SPINCO_ENVIRONMENT = "staging"
    $env:SPINCO_GDRIVE_ENABLED = "true"
    $env:SPINCO_GDRIVE_ROOT_FOLDER_ID = "<staging test folder id>"
    $env:SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING = "<staging Shared Drive id>"
    $env:SPINCO_GDRIVE_CREDENTIALS_FILE = "D:\\SPINCO-SECRETS\\<service account key>.json"      # or the OAuth settings, see .env.example
    .venv\\Scripts\\python.exe tools\\central_staging_check.py --work D:\\SpincoTA-artifacts\\central-staging\\run-001

The result is also written to <work>\\central-staging-result.json (pass/fail + timings only).
"""

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from io import BytesIO
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
APPROVED_ROOT = Path(r"D:\SpincoTA-artifacts\central-staging")


def synthetic_pdf(marker: str) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200 + (sum(map(ord, marker)) % 400), height=200)
    writer.add_metadata({"/Title": f"synthetic-central-check-{marker}"})
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class Report:
    def __init__(self):
        self.rows = []

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.rows.append({"check": name, "result": "PASS" if passed else "FAIL", "detail": detail[:200]})
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail[:120]})" if detail and not passed else ""))
        return passed

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r["result"] == "FAIL")


def run(client, report: Report, drive_files_in) -> None:
    """`drive_files_in(folder_name) -> int` counts the files in a Drive folder (fake: from memory; real: not available -> None is passed and that check is skipped)."""
    token = secrets.token_hex(4)
    er, trip = synthetic_pdf(f"er-{token}"), synthetic_pdf(f"trip-{token}")

    def upload(kind, content, name):
        response = client.post(f"/api/uploads/{kind}", files=[("files", (name, content, "application/pdf"))])
        return response.status_code, (response.json()["results"][0] if response.status_code == 200 else {})

    status, first = upload("expense-reports", er, f"ER-9{token[:4]}1.pdf")
    report.check("1 upload an Expense Report: stored centrally before success", status == 200 and first.get("result") == "SUCCESS" and first.get("storage_state") == "STORED", str(first.get("result")))
    status, tr = upload("trip-reports", trip, f"TRIP-9{token[:4]}2_Trip_To_Check.pdf")
    report.check("2 upload a Trip Report", status == 200 and tr.get("result") == "SUCCESS" and tr.get("storage_state") == "STORED", str(tr.get("result")))

    records = client.get("/api/storage/records?state=all").json()
    sha_er = hashlib.sha256(er).hexdigest()
    report.check("3 central records exist (state STORED) for both documents", len([r for r in records if r["state"] == "STORED"]) == 2, f"{len(records)} records")
    detail = client.get(f"/api/documents/{first.get('document_id')}").json() if first else {}
    report.check("4 metadata: checksum, size, uploader, UTC time, IST time, type", detail.get("file_hash") == sha_er and detail.get("file_size") == len(er) and bool(detail.get("uploaded_by_name"))
                 and str(detail.get("uploaded_at", "")).endswith("+00:00") and str(detail.get("uploaded_at_ist", "")).endswith("+05:30") and detail.get("source_type") == "EXPENSE_REPORT")
    listed = {i["document_id"]: i for i in client.get("/api/uploads").json()}
    report.check("5 list shows both documents with storage state STORED", first.get("document_id") in listed and tr.get("document_id") in listed
                 and listed[first["document_id"]]["storage_state"] == "STORED")
    download = client.get(f"/api/documents/{first.get('document_id')}/file?download=true")
    report.check("6 download returns the exact bytes (checksum matches)", download.status_code == 200 and hashlib.sha256(download.content).hexdigest() == sha_er)
    preview = client.get(f"/api/documents/{first.get('document_id')}/file", headers={"Range": "bytes=0-9"})
    report.check("7 preview supports a Range request", preview.status_code == 206 and preview.content == er[:10])

    status, dup = upload("expense-reports", er, "renamed-duplicate.pdf")
    report.check("8 identical bytes under another name -> DUPLICATE, no second canonical document", dup.get("result") == "DUPLICATE" and dup.get("document_id") == first.get("document_id"))
    status, other = upload("expense-reports", synthetic_pdf(f"other-{token}"), f"ER-9{token[:4]}1.pdf")
    report.check("9 same file name, different bytes -> NOT a duplicate", other.get("result") == "SUCCESS" and other.get("document_id") != first.get("document_id"))
    history = client.get("/api/uploads/attempts?result=DUPLICATE").json()
    report.check("10 duplicate history lists the attempt with its canonical document", any(h["original_filename"] == "renamed-duplicate.pdf" and h["duplicate_of"]["document_id"] == first.get("document_id") for h in history))

    if drive_files_in is not None:
        report.check("11 one canonical Expense Report file per distinct bytes in Drive", drive_files_in("Expense Reports") == 2, str(drive_files_in("Expense Reports")))

    deleted = client.delete(f"/api/documents/{tr.get('document_id')}")
    after_delete = client.get(f"/api/documents/{tr.get('document_id')}/file")
    report.check("12 delete archives the central copy (reversible)", deleted.status_code == 204 and after_delete.status_code == 404)
    status, back = upload("trip-reports", trip, "restored-trip.pdf")
    report.check("13 re-uploading restores the archived central copy", back.get("result") == "SUCCESS" and back.get("storage_state") == "STORED")
    restored = client.get(f"/api/documents/{back.get('document_id')}/file")
    report.check("14 the restored document downloads with the same bytes", restored.status_code == 200 and restored.content == trip)

    generated = client.post("/api/excel-files/generate")
    report.check("16 Excel export: a verified copy is kept in the Drive folder 'Excel Exports'", generated.status_code == 200 and generated.json().get("central_copy") == "STORED",
                 str(generated.json().get("central_copy") if generated.status_code == 200 else generated.status_code))
    if drive_files_in is not None:
        report.check("17 exactly one Excel export file in Drive after one generation", drive_files_in("Excel Exports") == 1, str(drive_files_in("Excel Exports")))

    status_json = client.get("/api/central/status").json()
    report.check("15 status: serve mode, no security warnings expected on loopback", status_json.get("mode") == "serve" and status_json.get("storage_ready") is True, str(status_json.get("mode")))
    print("NOTE  not covered by this script (they need a person or a deliberate fault): external deletion of a Drive file, a Drive outage, an expired authorisation. "
          "See the manual checklist in the implementation report.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", required=True, help="a NEW folder under D:\\SpincoTA-artifacts\\central-staging\\")
    parser.add_argument("--fake-drive", action="store_true", help="use the in-memory fake Drive (proves this script; says nothing about Google)")
    args = parser.parse_args(argv)
    work = Path(args.work).resolve()
    try:
        work.relative_to(APPROVED_ROOT.resolve())
    except ValueError:
        print(f"Refused: the work folder must be under {APPROVED_ROOT}.", file=sys.stderr)
        return 2
    if work.exists() and any(work.iterdir()):
        print("Refused: the work folder is not empty; use a new one.", file=sys.stderr)
        return 2
    if not args.fake_drive:
        if os.environ.get("SPINCO_ENVIRONMENT", "").strip().lower() != "staging":
            print("Refused: SPINCO_ENVIRONMENT must be 'staging'.", file=sys.stderr)
            return 2
        if not os.environ.get("SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING"):
            print("Refused: SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING (the staging Shared Drive id) is not set.", file=sys.stderr)
            return 2
        if os.environ.get("SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION") and os.environ["SPINCO_GDRIVE_EXPECTED_DRIVE_ID_PRODUCTION"] == os.environ["SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING"]:
            print("Refused: the staging and production Shared Drive ids are the same.", file=sys.stderr)
            return 2
    work.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(BACKEND_DIR))
    sys.path.insert(0, str(BACKEND_DIR / "tests"))
    from tools.init_central_staging_db import main as init_db  # noqa: E402  (sets SPINCO_DB_PATH for this process)

    admin_password = "Chk-" + secrets.token_urlsafe(18)  # random, in memory only, never printed or stored
    os.environ.update({"SPINCO_STORAGE_ROOT": str(work / "storage"), "SPINCO_CENTRAL_STORAGE_MODE": "serve", "SPINCO_JOB_WORKER": "off", "TA_BACKEND_URL": "off",
                       "SPINCO_ADMIN_INITIAL_PASSWORD": admin_password, "SPINCO_GDRIVE_SPOOL_DIR": str(work / "spool")})
    if init_db(["--db", str(work / "central-check.db")]) != 0:
        return 2

    import tempfile

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import central_storage

    drive_files_in = None
    if args.fake_drive:
        os.environ["SPINCO_ENVIRONMENT"] = "dev"
        from drive_testkit import make_stack

        stack = make_stack(Path(tempfile.mkdtemp(prefix="central-check-", dir=work)))
        central_storage.manager = lambda: stack.manager

        def drive_files_in(folder_name):  # noqa: F811
            folders = {f["id"]: f["name"] for f in stack.fake.files.values() if f["mimeType"] == "application/vnd.google-apps.folder"}
            return sum(1 for f in stack.fake.files.values() if f["mimeType"] != "application/vnd.google-apps.folder" and any(folders.get(p) == folder_name for p in f["parents"]))

    report = Report()
    started = time.time()
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "intern@spincotech.com", "password": admin_password})
        if login.status_code != 200:
            print("Could not sign in to the disposable database.", file=sys.stderr)
            return 2
        csrf = login.json()["csrf_token"]
        original = client.request

        def with_csrf(method, url, **kwargs):
            if method.upper() not in ("GET", "HEAD", "OPTIONS"):
                kwargs["headers"] = {**(kwargs.get("headers") or {}), "X-CSRF-Token": csrf}
            return original(method, url, **kwargs)

        client.request = with_csrf
        run(client, report, drive_files_in)
    summary = {"mode": "fake-drive (proves the script only)" if args.fake_drive else "staging Shared Drive", "seconds": round(time.time() - started, 1), "failed": report.failed, "checks": report.rows}
    (work / "central-staging-result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n{len(report.rows) - report.failed} passed, {report.failed} failed  ->  {work / 'central-staging-result.json'}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
