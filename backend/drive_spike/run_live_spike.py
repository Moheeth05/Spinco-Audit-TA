"""Google Drive feasibility spike: the 16 tests of the Phase 1 plan, runnable against a REAL, TEMPORARY test folder.

    python backend/drive_spike/run_live_spike.py --live --approve-writes --out-dir D:/SpincoTA-artifacts/test-runs/<unique-run>/live

Required environment (when it is missing the run stops BLOCKED and prints exactly what to obtain; nothing is contacted):
    SPINCO_GDRIVE_ROOT_FOLDER_ID         the TEST root folder id
    SPINCO_GDRIVE_CREDENTIALS_FILE       a TEST service-account key file kept OUTSIDE the repository

Safety:
  * Live mode WRITES to Drive (folders, synthetic files, renames, moves), so `--live` does nothing unless `--approve-writes` is ALSO given: without it the run
    refuses (exit 5) before the output folder, the isolated database or any network client exists. The fake modes never need the flag.
  * Everything is created inside ONE new sub-folder ("spike-<timestamp>") of the test root; nothing
    that already exists is renamed, moved, shared or deleted. The harness has no delete capability at all (files are only trashed or
    moved, and the synthetic ones are left in the spike folder for inspection).
  * Synthetic PDFs only. Credentials are never printed or written to the report; Drive ids are truncated.
  * Output goes under D:\\SpincoTA-artifacts\\test-runs\\ only (never inside a repository) and is never overwritten.

`--fake` / `--fake-shared-drive` run the SAME tests against an in-memory fake Drive. That validates the harness itself and is labelled
FAKE in every output: it is NOT evidence about Google's real service.
"""

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
TEST_RUNS_ROOT = Path(r"D:\SpincoTA-artifacts\test-runs")
SYNTHETIC_ADMIN_PASSWORD = "Spike-Synthetic-Admin-Pw-1!"


@dataclass
class Result:
    number: int
    name: str
    status: str = "NOT_RUN"  # PASS | FAIL | PARTIAL | BLOCKED | NOT_APPLICABLE | NOT_RUN
    detail: str = ""
    evidence: dict = field(default_factory=dict)


def short(file_id: str | None) -> str:
    return f"{file_id[:6]}..." if file_id else "-"


def synthetic_pdf(marker: str) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200 + (sum(map(ord, marker)) % 400), height=200)
    writer.add_metadata({"/Title": f"synthetic-spike-{marker}"})
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------------------------------------------- environment

REQUIRED_MODULES = {"pypdf": "pypdf", "argon2": "argon2-cffi", "httpx": "httpx", "cryptography": "cryptography", "fastapi": "fastapi", "sqlalchemy": "sqlalchemy"}
PROJECT_PYTHON = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"


def check_environment() -> dict:
    """Which interpreter is running this, and does it have what the harness imports? (Nothing is ever installed.) A machine can have several
    Pythons on PATH - one with the app's libraries, others belonging to unrelated projects - and running under the wrong one used to surface as
    a dozen confusing test failures."""
    import importlib.metadata as metadata
    import importlib.util

    info = {"interpreter": sys.executable, "python": sys.version.split()[0], "project_venv": str(PROJECT_PYTHON), "is_project_venv": False, "packages": {}, "missing": []}
    try:
        info["is_project_venv"] = Path(sys.executable).resolve() == PROJECT_PYTHON.resolve()
    except OSError:
        pass
    for module, package in REQUIRED_MODULES.items():
        if importlib.util.find_spec(module) is None:
            info["missing"].append(package)
        else:
            try:
                info["packages"][package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                info["packages"][package] = "present"
    return info


def environment_problem(info: dict) -> str | None:
    if not info["missing"]:
        return None
    return (
        f"Wrong Python environment: this interpreter ({info['interpreter']}) lacks {', '.join(info['missing'])}. They are already listed in backend/requirements.txt and installed in the "
        f"project's own virtual environment, so nothing needs installing: run the harness with {info['project_venv']}. (Do NOT pip install into an interpreter that belongs to another project.)"
    )


def validate_out_dir(raw: str) -> Path:
    out = Path(raw).resolve()
    try:
        out.relative_to(TEST_RUNS_ROOT.resolve())
    except ValueError:
        raise SystemExit(f"--out-dir must be inside {TEST_RUNS_ROOT}")
    try:
        out.relative_to(REPO_ROOT)
        raise SystemExit("--out-dir must not be inside the repository")
    except ValueError:
        pass
    if (out / "report.json").exists() or (out / "FAKE-report.json").exists():
        raise SystemExit("A report already exists in --out-dir; use a new unique run folder (nothing is ever overwritten).")
    out.mkdir(parents=True, exist_ok=True)
    return out


class Context:
    """Everything the tests share. Built once; `mode` is LIVE, FAKE or FAKE-SHARED."""

    def __init__(self, mode: str, out_dir: Path):
        self.mode = mode
        self.out_dir = out_dir
        self.results: list[Result] = []
        self.notes: list[str] = []
        self.root_meta: dict = {}
        self.blocked_reason: str | None = None
        self.environment: dict = {}
        self.blocked_kind = "credentials"  # or "environment" / "approval": decides which instructions are shown
        self.writes_approved = False  # set by main() only when --approve-writes was given; build() refuses a LIVE run without it
        # State that later tests build on. Always initialised, and only ever obtained through ensure_*() below, so a failure in one
        # test is reported as THAT failure and can never surface as a confusing AttributeError in another.
        self.uploaded = None
        self.upload_content: bytes | None = None
        self.archived_record_id: int | None = None
        self.DocumentStorageRecord = None


def build(ctx: Context):
    """Imports the app (after the isolated environment is set), builds the client stack, signs in, creates the spike folder."""
    if ctx.mode == "LIVE" and not ctx.writes_approved:  # defence in depth: main() already refuses, this stops any other caller too
        ctx.blocked_reason, ctx.blocked_kind = "Live writes were not approved (--approve-writes). Nothing was contacted or written.", "approval"
        return False
    import httpx

    sys.path.insert(0, str(BACKEND_DIR))
    from app.database import Base, SessionLocal, engine
    from app.services.drive.auth import DriveAuthError, ServiceAccountCredentials, TokenProvider
    from app.services.drive.client import DriveClient, DriveError
    from app.services.drive.config import DriveConfigError, DriveConfig, load_config
    from app.services.drive.lifecycle import StorageManager
    from app.services.drive.storage import DriveFolders, GoogleDriveStorage

    class Injector(httpx.BaseTransport):
        """Wraps the real (or fake) transport so a test can inject failures and count calls. Injection is LOCAL: Google is not failing."""

        def __init__(self, inner):
            self.inner, self.calls, self.fail, self.block = inner, [], [], False

        def handle_request(self, request):
            self.calls.append((request.method, request.url.path))
            if self.block:
                raise httpx.ConnectError("offline (simulated locally)", request=request)
            if self.fail:
                status = self.fail.pop(0)
                return httpx.Response(status, json={"error": {"code": status, "message": "injected", "errors": [{"reason": "backendError"}]}})
            return self.inner.handle_request(request)

        def close(self):
            self.inner.close()

    Base.metadata.create_all(bind=engine)
    ctx.Injector, ctx.SessionLocal, ctx.DriveError = Injector, SessionLocal, DriveError
    ctx.sleeps = []

    if ctx.mode == "LIVE":
        try:
            config = load_config()
            credentials = ServiceAccountCredentials.from_file(config.credentials_file)
        except (DriveConfigError, DriveAuthError) as exc:
            ctx.blocked_reason = f"Configuration/credentials: {exc}"
            return False
        injector = Injector(httpx.HTTPTransport())
        sleep = time.sleep
        ctx.fake = None
    else:
        sys.path.insert(0, str(BACKEND_DIR / "tests"))
        from drive_testkit import rsa_key, write_service_account_file
        from fake_drive import FakeDrive

        work = ctx.out_dir / "fake-work"
        work.mkdir(parents=True, exist_ok=True)
        ctx.fake = FakeDrive(service_account_public_key=rsa_key().public_key(), drive_id="0SYNTHETICSHARED001" if ctx.mode == "FAKE-SHARED" else None)
        root = next(iter(ctx.fake.files))
        config = DriveConfig(root_folder_id=root, credentials_file=write_service_account_file(work), rate_limit_per_second=1000.0)
        credentials = ServiceAccountCredentials.from_file(config.credentials_file)
        injector = Injector(ctx.fake.transport())
        sleep = ctx.sleeps.append
    http = httpx.Client(transport=injector)
    tokens = TokenProvider(credentials, config.scope, http)
    client = DriveClient(config, tokens, http, sleep=sleep)
    ctx.config, ctx.injector, ctx.client, ctx.tokens = config, injector, client, tokens
    try:
        tokens.get_token()
    except DriveAuthError as exc:
        ctx.blocked_reason = f"Authentication failed: {exc}"
        return False

    try:
        run_name = "spike-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_folder = client.create_folder(run_name, config.root_folder_id, file_id=client.generate_ids(1)[0], app_properties={"spinco_role": "spike-run"})
    except DriveError as exc:
        ctx.blocked_reason = f"The service account can authenticate but cannot create a folder in the test root ({exc}). It needs Editor access (My Drive folder) or Content manager (Shared Drive)."
        return False
    ctx.run_folder_id, ctx.run_name = run_folder.id, run_name
    ctx.folders = DriveFolders(client, run_folder.id)
    ctx.storage = GoogleDriveStorage(client, ctx.folders, temp_root=ctx.out_dir)
    ctx.manager = StorageManager(ctx.storage, spool_dir=ctx.out_dir / "spool")
    return True


# --------------------------------------------------------------------------------------------------------------- the 16 tests

KIND = "expense_report"


def new_db(ctx):
    return ctx.SessionLocal()


def t01_auth_and_folder_access(ctx, r: Result):
    root = ctx.root_meta = ctx.client.get_raw(ctx.config.root_folder_id, "id,name,mimeType,driveId,ownedByMe,parents,capabilities(canAddChildren,canEdit,canTrash,canMoveItemWithinDrive,canListChildren)")
    caps = root.get("capabilities", {})
    classification = "SHARED DRIVE (item has a driveId)" if root.get("driveId") else "MY DRIVE folder (no driveId)"
    children = len(ctx.client.find_children(ctx.config.root_folder_id))
    public = "unknown (sharing settings not readable by this account)"
    try:
        permissions = ctx.client.list_permissions(ctx.config.root_folder_id)
        public = "YES - an 'anyone with the link' permission exists" if any(p.get("type") in ("anyone", "domain") for p in permissions) else "no anyone/domain permission found"
        r.evidence["permission_types"] = sorted({f"{p.get('type')}:{p.get('role')}" for p in permissions})
    except ctx.DriveError as exc:
        r.evidence["permissions_error"] = f"{type(exc).__name__} {getattr(exc, 'status', '')}"
    r.evidence.update(
        folder_name=root.get("name"), is_folder=root.get("mimeType") == "application/vnd.google-apps.folder", classification=classification,
        owned_by_service_account=root.get("ownedByMe"), can_add_children=caps.get("canAddChildren"), can_edit=caps.get("canEdit"),
        can_trash=caps.get("canTrash"), children_in_root_including_this_runs_spike_folder=children, publicly_shared=public, scope=ctx.config.scope,
        spike_folder=f"{ctx.run_name} ({short(ctx.run_folder_id)})",
    )
    ok = r.evidence["is_folder"] and caps.get("canAddChildren") is not False
    r.status, r.detail = ("PASS", f"Authenticated, reached the test root ({classification}) and created the spike folder inside it.") if ok else ("FAIL", "The root is not a folder the account can add children to.")


def ensure_uploaded(ctx):
    """The synthetic PDF that tests 2, 3, 4 and 16 examine. Uploaded once (a repeat is de-duplicated by hash). Later tests call this instead of
    assuming test 2 ran, so a genuine upload failure is reported as an upload failure."""
    if ctx.uploaded is None:
        content = synthetic_pdf("upload")
        outcome = ctx.storage.upload(KIND, hashlib.sha256(content).hexdigest(), "SPIKE-upload.pdf", content)
        ctx.upload_content, ctx.uploaded = content, outcome
    return ctx.uploaded


def t02_upload(ctx, r: Result):
    outcome = ensure_uploaded(ctx)
    in_folder = outcome.obj.parents == (ctx.folders.active(KIND),)
    r.evidence.update(file=short(outcome.obj.id), in_expected_folder=in_folder, app_properties=sorted(outcome.obj.app_properties), size=outcome.obj.size)
    r.status, r.detail = ("PASS", "A synthetic PDF was uploaded (resumable) into 'Expense Reports' inside the spike folder.") if in_folder and outcome.obj.app_properties.get("sha256") else ("FAIL", "Uploaded, but the file or its metadata is not where/what it should be.")


def t03_metadata_checksum(ctx, r: Result):
    obj = ctx.storage.info(ensure_uploaded(ctx).location)
    md5, size = hashlib.md5(ctx.upload_content).hexdigest(), len(ctx.upload_content)
    r.evidence.update(md5_matches=obj.md5 == md5, size_matches=obj.size == size, not_trashed=not obj.trashed)
    r.status, r.detail = ("PASS", "Drive's md5Checksum and size equal the locally computed values.") if obj.md5 == md5 and obj.size == size else ("FAIL", "Checksum or size mismatch.")


def t04_download_sha256(ctx, r: Result):
    uploaded = ensure_uploaded(ctx)
    data = ctx.storage.retrieve(uploaded.location)
    same = hashlib.sha256(data).hexdigest() == hashlib.sha256(ctx.upload_content).hexdigest()
    with ctx.storage.open_stream(uploaded.location, range_header="bytes=0-9") as download:
        part = b"".join(download.iter_bytes())
        r.evidence.update(range_status=download.status, range_bytes_ok=part == ctx.upload_content[:10])
    r.evidence["sha256_round_trip"] = same
    r.status, r.detail = ("PASS", "Downloaded bytes have the same SHA-256; a Range request returned the expected partial content.") if same and r.evidence["range_bytes_ok"] else ("FAIL", "Round trip or Range mismatch.")


def _record_for(ctx, db, marker):
    content = synthetic_pdf(marker)
    return ctx.manager.store(db, kind=KIND, content=content, original_filename=f"SPIKE-{marker}.pdf").record, content


def t05_rename_detection(ctx, r: Result):
    with new_db(ctx) as db:
        record, _ = _record_for(ctx, db, "rename")
        ctx.client.update_file(parse(ctx, record.location), name="renamed-outside-the-app.pdf")  # an "external" actor (same account)
        report = ctx.manager.reconcile(db)
        db.refresh(record)
        names = [e.event_type for e in __events(ctx, db, record)]
        r.evidence.update(renamed_detected=report.renamed_externally, state_after=record.state, events=names)
        r.status, r.detail = ("PASS", "A rename made outside the app was flagged (NAME_DRIFT) without changing state or data.") if "NAME_DRIFT" in names and record.state == "STORED" else ("FAIL", "The rename was not detected as designed.")
        r.evidence["limitation"] = "The rename was made by the same service account through the API, which emulates (but is not identical to) a human renaming in the Drive UI."


def t06_external_deletion(ctx, r: Result):
    with new_db(ctx) as db:
        record, _ = _record_for(ctx, db, "extdelete")
        ctx.client.update_file(parse(ctx, record.location), trashed=True)  # what a human 'Delete' does in Drive (moves to trash)
        report = ctx.manager.reconcile(db)
        db.refresh(record)
        r.evidence.update(missing_detected=report.missing_external, state_after=record.state)
        ok = record.state == "MISSING_EXTERNAL"
        r.status = "PARTIAL" if ok else "FAIL"
        r.detail = ("A file trashed in Drive was detected and marked MISSING_EXTERNAL; history untouched. " if ok else "Trashed file not detected. ") + "Permanent deletion by a human is NOT simulated: this system has no delete capability by design, and only a Shared Drive Manager can permanently delete there."


def ensure_archived(ctx):
    """A document that has been archived through the application (tests 7 and 8). Done once; test 8 calls this rather than assuming test 7 ran."""
    if ctx.archived_record_id is None:
        with new_db(ctx) as db:
            record, _ = _record_for(ctx, db, "archive")
            ctx.manager.archive(db, record.id, user_id=None)
            ctx.archived_record_id = record.id
    return ctx.archived_record_id


def t07_archive(ctx, r: Result):
    record_id = ensure_archived(ctx)
    with new_db(ctx) as db:
        record = db.get(ctx.DocumentStorageRecord, record_id)
        obj = ctx.storage.info(record.location)
        in_archive = obj.parents == (ctx.folders.archive(KIND),)
        r.evidence.update(state=record.state, in_archive_folder=in_archive, trashed=obj.trashed)
        r.status, r.detail = ("PASS", "Archive moved the file into 'Archive/Expense Reports' (not trashed) and set ARCHIVED.") if in_archive and not obj.trashed and record.state == "ARCHIVED" else ("FAIL", "Archive did not behave as designed.")


def t08_restore(ctx, r: Result):
    with new_db(ctx) as db:
        record = db.get(ctx.DocumentStorageRecord, ensure_archived(ctx))
        ctx.manager.restore(db, record.id, user_id=None)
        obj = ctx.storage.info(record.location)
        back = obj.parents == (ctx.folders.active(KIND),)
        r.evidence.update(state=record.state, back_in_active_folder=back)
        r.status, r.detail = ("PASS", "Restore moved the file back to the active folder and set STORED.") if back and record.state == "STORED" else ("FAIL", "Restore did not behave as designed.")


def t09_duplicates(ctx, r: Result):
    content = synthetic_pdf("duplicate")
    with new_db(ctx) as db:
        first = ctx.manager.store(db, kind=KIND, content=content, original_filename="SPIKE-dup.pdf")
        second = ctx.manager.store(db, kind=KIND, content=content, original_filename="SPIKE-dup-again.pdf")
    files = ctx.client.find_children(ctx.folders.active(KIND), app_property=("sha256", hashlib.sha256(content).hexdigest()))
    r.evidence.update(second_was_duplicate=second.duplicate, drive_files_with_that_hash=len(files))
    r.status, r.detail = ("PASS", "The same bytes produced exactly one Drive file and one record.") if second.duplicate and len(files) == 1 else ("FAIL", "A duplicate was created.")


def t10_concurrent_upload(ctx, r: Result):
    content = synthetic_pdf("concurrent")
    results, errors = [], []
    barrier = threading.Barrier(5)

    def go():
        db = new_db(ctx)
        try:
            barrier.wait(timeout=60)
            results.append(ctx.manager.store(db, kind=KIND, content=content, original_filename="SPIKE-concurrent.pdf"))
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
        finally:
            db.close()

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join(timeout=300) for t in threads]
    files = ctx.client.find_children(ctx.folders.active(KIND), app_property=("sha256", hashlib.sha256(content).hexdigest()))
    r.evidence.update(threads=5, completed=len(results), errors=errors, created_flags=sum(x.created for x in results), drive_files=len(files))
    r.status, r.detail = ("PASS", "Five simultaneous uploads of the same bytes produced exactly one Drive file and one created record.") if len(files) == 1 and not errors and sum(x.created for x in results) == 1 else ("FAIL", "Concurrency produced duplicates or errors.")


def t11_interrupted_upload(ctx, r: Result):
    from app.models import storage_record as sr
    from app.models.storage_record import DocumentStorageRecord
    from app.services.drive.storage import make_location

    parts = {}
    with new_db(ctx) as db:
        # (a) the file reached Drive but the process died before the database was updated
        content_a = synthetic_pdf("interrupted-a")
        reserved_a = ctx.storage.reserve_id()
        ctx.storage.upload(KIND, hashlib.sha256(content_a).hexdigest(), "SPIKE-a.pdf", content_a, reserved_id=reserved_a)
        rec_a = DocumentStorageRecord(kind=KIND, sha256=hashlib.sha256(content_a).hexdigest(), original_filename="SPIKE-a.pdf", size=len(content_a), md5=hashlib.md5(content_a).hexdigest(), location=make_location(reserved_a), state=sr.UPLOADING)
        # (b) nothing reached Drive; a spooled copy exists
        content_b = synthetic_pdf("interrupted-b")
        rec_b = DocumentStorageRecord(kind=KIND, sha256=hashlib.sha256(content_b).hexdigest(), original_filename="SPIKE-b.pdf", size=len(content_b), md5=hashlib.md5(content_b).hexdigest(), location=make_location(ctx.storage.reserve_id()), state=sr.UPLOADING)
        db.add_all([rec_a, rec_b])
        db.commit()
        ctx.manager._spool(rec_b, content_b)
        report = ctx.manager.reconcile(db, stale_after_seconds=0)
        db.refresh(rec_a), db.refresh(rec_b)
        parts.update(adopted=report.adopted_uploads, reuploaded=report.reuploaded, state_a=rec_a.state, state_b=rec_b.state)
        # (c) the question the design left open: what does REAL Drive answer when a file id is reused?
        try:
            ctx.client.upload_file(file_id=ctx.storage.info(rec_a.location).id, name="reuse.pdf", parent_id=ctx.folders.active(KIND), content=content_a)
            parts["id_reuse_observed"] = "Drive ACCEPTED a second create with the same id (no 409) - the adopt-on-409 path is not what happens; see report"
            parts["id_reuse_status"] = "accepted"
        except ctx.DriveError as exc:
            parts["id_reuse_observed"] = f"HTTP {exc.status} {exc.reason or ''}".strip()
            parts["id_reuse_status"] = exc.status
    r.evidence.update(parts)
    good = parts["state_a"] == "STORED" and parts["state_b"] == "STORED"
    ok_409 = parts.get("id_reuse_status") == 409
    r.status = "PASS" if good and ok_409 else ("PARTIAL" if good else "FAIL")
    r.detail = "Both interrupted-upload variants were recovered by reconciliation." + ("" if ok_409 else f" NOTE: reusing a file id answered {parts.get('id_reuse_observed')}, not 409 - the duplicate-protection assumption must be revisited.")


def t12_api_failures_and_retries(ctx, r: Result):
    before = len(ctx.injector.calls)
    sleeps_before = len(ctx.sleeps)
    ctx.injector.fail = [503, 503]
    obj = ctx.client.get_raw(ctx.config.root_folder_id, "id")
    attempts = len(ctx.injector.calls) - before
    ctx.injector.fail = [429]
    ctx.client.get_raw(ctx.config.root_folder_id, "id")
    r.evidence.update(attempts_for_two_503=attempts, call_succeeded=bool(obj.get("id")), backoff_sleeps_recorded=len(ctx.sleeps) - sleeps_before if ctx.fake else "real sleeps")
    r.evidence["limitation"] = "The 503/429 responses were INJECTED locally in front of the real transport; Google was not failing. This proves our retry/backoff logic end to end, not Google's failure behaviour."
    r.status = "PARTIAL" if attempts == 3 else "FAIL"
    r.detail = "Two injected 503s were retried with backoff and the third attempt succeeded; an injected 429 was retried too." if attempts == 3 else "Retry behaviour not as designed."


def t13_rbac(ctx, r: Result):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.seed import ADMIN_EMAIL  # the seeded administrator of THIS checkout (it differs between the original repo and the snapshot)
    from app.services.drive.runtime import get_storage_manager

    failures = []
    app.dependency_overrides[get_storage_manager] = lambda: ctx.manager
    try:
        with TestClient(app) as c:
            def login(email, password):
                c.cookies.clear()
                resp = c.post("/api/auth/login", json={"email": email, "password": password})
                assert resp.status_code == 200, resp.text
                c.headers["X-CSRF-Token"] = resp.json()["csrf_token"]

            login(ADMIN_EMAIL, SYNTHETIC_ADMIN_PASSWORD)
            for role in ("VIEWER", "AUDITOR"):
                assert c.post("/api/users", json={"name": role, "email": f"{role.lower()}.spike@example.com", "role": role, "password": "SyntheticPass123!"}).status_code == 201
            with new_db(ctx) as db:
                record, content = _record_for(ctx, db, "rbac")
            calls_before = len(ctx.injector.calls)
            c.cookies.clear()
            c.headers.pop("X-CSRF-Token", None)
            if c.get(f"/api/storage/records/{record.id}/file").status_code != 401:
                failures.append("anonymous stream not 401")
            if len(ctx.injector.calls) != calls_before:
                failures.append("an unauthenticated request reached Drive")
            login("viewer.spike@example.com", "SyntheticPass123!")
            if c.post(f"/api/storage/records/{record.id}/archive").status_code != 403:
                failures.append("viewer could archive")
            stream = c.get(f"/api/storage/records/{record.id}/file")
            if stream.status_code != 200 or stream.content != content:
                failures.append("viewer could not stream the exact bytes")
            login("auditor.spike@example.com", "SyntheticPass123!")
            if c.post(f"/api/storage/records/{record.id}/restore").status_code != 403:
                failures.append("auditor could restore")
            login(ADMIN_EMAIL, SYNTHETIC_ADMIN_PASSWORD)
            if c.post(f"/api/storage/records/{record.id}/archive").status_code != 200:
                failures.append("admin could not archive")
            login("viewer.spike@example.com", "SyntheticPass123!")
            if c.get(f"/api/storage/records/{record.id}/file").status_code != 410:
                failures.append("archived document still streamable")
            login(ADMIN_EMAIL, SYNTHETIC_ADMIN_PASSWORD)
            text = c.get("/api/storage/records?state=all").text + c.get("/api/storage/status").text
            if "gdrive:" in text or ctx.run_folder_id in text or "googleapis" in text:
                failures.append("a Drive reference leaked in a response")
    finally:
        app.dependency_overrides.pop(get_storage_manager, None)
    r.evidence["failures"] = failures
    r.status, r.detail = ("PASS", "RBAC holds with the real Drive manager: viewers/auditors cannot archive or restore, unauthenticated requests never reach Drive, archived documents cannot be streamed, no Drive ids leak.") if not failures else ("FAIL", "; ".join(failures))


def t14_offline(ctx, r: Result):
    from app.services.drive.client import DriveTransientError
    from app.services.drive.lifecycle import StorageUploadError

    content = synthetic_pdf("offline")
    ctx.storage.reserve_id()  # prime the id pool
    with new_db(ctx) as db:
        ctx.injector.block = True
        try:
            ctx.manager.store(db, kind=KIND, content=content, original_filename="SPIKE-offline.pdf")
            outcome = "UNEXPECTEDLY SUCCEEDED"
            record = None
        except StorageUploadError as exc:
            outcome, record = ("retryable error, record " + exc.record.state) if exc.record else "retryable error, nothing recorded", exc.record
        read_outcome = "n/a"
        if record is not None:
            try:
                ctx.storage.exists(record.location)
                read_outcome = "returned a value (WRONG: an outage must raise)"
            except DriveTransientError:
                read_outcome = "raised a retryable error (correct: an outage never reads as 'missing')"
        ctx.injector.block = False
        recovered = None
        if record is not None:
            record.created_at = datetime(2000, 1, 1)
            db.commit()
            report = ctx.manager.reconcile(db, stale_after_seconds=0)
            db.refresh(record)
            recovered = record.state
            r.evidence["reconciled_reuploads"] = report.reuploaded
    r.evidence.update(during_outage=outcome, read_during_outage=read_outcome, after_network_returned=recovered)
    r.evidence["limitation"] = "Connectivity loss was SIMULATED locally (all requests refused), not a real network outage."
    r.status = "PARTIAL" if recovered == "STORED" and "correct" in read_outcome else "FAIL"
    r.detail = "While offline the upload was held visibly as UPLOADING with its bytes kept, reads raised instead of reporting 'missing', and reconciliation completed it when the network returned."


def t15_change_tracking(ctx, r: Result):
    from app.services.drive.client import DriveChangeTokenExpired

    with new_db(ctx) as db:
        record, _ = _record_for(ctx, db, "changes")
        ctx.folders.ensure_structure()
        baseline = ctx.manager.sync_changes(db)
        token = ctx.client.get_start_page_token()
        ctx.client.update_file(parse(ctx, record.location), trashed=True)
        seen, state = False, None
        for _ in range(12):  # the change feed is eventually consistent: poll for up to ~60 s
            report = ctx.manager.sync_changes(db)
            db.refresh(record)
            if record.state == "MISSING_EXTERNAL":
                seen, state = True, record.state
                break
            time.sleep(0 if ctx.fake else 5)
    r.evidence.update(baseline_established=baseline.baseline, external_trash_seen_via_feed=seen, state=state)
    try:
        ctx.client.list_changes("1")
        r.evidence["very_old_token"] = "Drive ACCEPTED page token '1' (no expiry error)"
    except DriveChangeTokenExpired as exc:
        r.evidence["very_old_token"] = f"mapped to DriveChangeTokenExpired (HTTP {exc.status} {exc.reason})"
    except ctx.DriveError as exc:
        r.evidence["very_old_token"] = f"HTTP {exc.status} {exc.reason} - NOT mapped to token-expired: revisit the mapping"
    r.evidence["limitation"] = "A genuinely EXPIRED token cannot be forced on demand; a bogus/very old token's real response was recorded instead. Expiry recovery itself is covered by fake-backend tests only."
    r.status = "PARTIAL" if seen else "FAIL"
    r.detail = "An external trash was detected through the change feed" + ("." if seen else " - NOT observed within the polling window.")


def t16_drive_type_behaviour(ctx, r: Result):
    root = ctx.root_meta
    shared = bool(root.get("driveId"))
    created = ctx.client.get_raw(ensure_uploaded(ctx).obj.id, "id,ownedByMe,driveId,capabilities(canDelete,canTrash,canMoveItemWithinDrive,canEdit)")
    quota = {}
    try:
        quota = ctx.client.get_about().get("storageQuota", {})
    except ctx.DriveError as exc:
        quota = {"error": f"{type(exc).__name__} {getattr(exc, 'status', '')}"}
    r.evidence.update(
        classification="SHARED DRIVE" if shared else "MY DRIVE folder", created_file_owned_by_service_account=created.get("ownedByMe"),
        service_account_can_permanently_delete=created.get("capabilities", {}).get("canDelete"), service_account_can_trash=created.get("capabilities", {}).get("canTrash"),
        quota_limit=quota.get("limit"), quota_usage=quota.get("usage"),
    )
    if shared:
        r.status, r.detail = "PASS", "Shared Drive confirmed (items carry a driveId). Ownership, trash and delete capabilities of the service account were recorded."
    else:
        r.status = "PARTIAL"
        r.detail = ("The test root is an ordinary My Drive folder, NOT a Shared Drive. Shared-Drive-specific behaviour (organisation ownership, Content-manager role, drive-scoped change feed, "
                    "manager-only permanent delete) has NOT been tested. Implications are in the report: files created by the service account are owned by it and count against its own quota.")


TESTS = [
    (1, "Authentication and folder access", t01_auth_and_folder_access),
    (2, "Synthetic PDF upload", t02_upload),
    (3, "Metadata and checksum verification", t03_metadata_checksum),
    (4, "Download and SHA-256 comparison", t04_download_sha256),
    (5, "Rename detection", t05_rename_detection),
    (6, "External deletion detection", t06_external_deletion),
    (7, "Application archive", t07_archive),
    (8, "Restore from archive", t08_restore),
    (9, "Duplicate upload", t09_duplicates),
    (10, "Concurrent upload", t10_concurrent_upload),
    (11, "Interrupted upload recovery", t11_interrupted_upload),
    (12, "Temporary Drive API failure and retries", t12_api_failures_and_retries),
    (13, "Role-based access control", t13_rbac),
    (14, "Offline behaviour", t14_offline),
    (15, "Change tracking and expired page-token recovery", t15_change_tracking),
    (16, "Shared Drive / folder behaviour", t16_drive_type_behaviour),
]


def parse(ctx, location):
    from app.services.drive.storage import parse_location

    return parse_location(location)


def __events(ctx, db, record):
    from sqlalchemy import select

    from app.models.storage_record import StorageEvent

    return db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id)).scalars().all()


# --------------------------------------------------------------------------------------------------------------- reporting


BLOCK_HELP = (
    "To enable the live run: (1) in a company-OWNED Google Cloud TEST project enable the Google Drive API; (2) create a dedicated service account and a JSON key; "
    "(3) save the key OUTSIDE the repository, e.g. D:\\SPINCO-SECRETS\\spinco-drive-test-sa.json, with access limited to the account that runs the tests; "
    "(4) share the test folder with the service account's e-mail address as Editor (My Drive folder) or add it to the Shared Drive as Content manager; "
    "(5) set SPINCO_GDRIVE_ROOT_FOLDER_ID and SPINCO_GDRIVE_CREDENTIALS_FILE and re-run with --live --approve-writes."
)


def write_reports(ctx: Context, started: datetime) -> Path:
    banner = "LIVE RUN against the real test folder" if ctx.mode == "LIVE" else "FAKE BACKEND - validates the harness only; NOT evidence about Google Drive"
    tag = "" if ctx.mode == "LIVE" else "FAKE-"
    counts: dict[str, int] = {}
    for result in ctx.results:
        counts[result.status] = counts.get(result.status, 0) + 1
    document = {
        "mode": ctx.mode, "banner": banner, "started_utc": started.isoformat(), "finished_utc": datetime.now(timezone.utc).isoformat(),
        "spike_folder": getattr(ctx, "run_name", None), "environment": ctx.environment, "blocked_reason": ctx.blocked_reason, "help": BLOCK_HELP if ctx.blocked_reason and ctx.blocked_kind == "credentials" else None,
        "summary": counts, "results": [asdict(r) for r in ctx.results], "notes": ctx.notes,
    }
    (ctx.out_dir / f"{tag}report.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    env = ctx.environment
    lines = [f"# Drive feasibility spike - {ctx.mode}", "", f"**{banner}**", "", f"Summary: {counts}", ""]
    if env:
        lines += [f"Interpreter: `{env.get('interpreter')}` (Python {env.get('python')}, project venv: {env.get('is_project_venv')})", ""]
    if ctx.blocked_reason:
        lines += [f"**BLOCKED:** {ctx.blocked_reason}", ""] + ([BLOCK_HELP, ""] if ctx.blocked_kind == "credentials" else [])
    lines += ["| # | Test | Status | Detail |", "|---|---|---|---|"]
    for r in ctx.results:
        lines.append(f"| {r.number} | {r.name} | {r.status} | {r.detail.replace('|', '/')} |")
    lines += ["", "## Evidence", ""]
    for r in ctx.results:
        if r.evidence:
            lines += [f"### {r.number}. {r.name}", "```json", json.dumps(r.evidence, indent=2, default=str), "```"]
    (ctx.out_dir / f"{tag}report.md").write_text("\n".join(lines), encoding="utf-8")
    return ctx.out_dir / f"{tag}report.md"


QUOTA_REASONS = ("storageQuotaExceeded", "quotaExceeded")
QUOTA_NOTE = (
    "FINDING: Google refused to create a FILE with 'storageQuotaExceeded'. A service account has no storage quota of its own, so in an ordinary "
    "My Drive folder owned by a person it can create folders (which cost nothing - that is why test 1 passes) but not files. This is a "
    "configuration/architecture finding, not a code defect: documents must live in a Shared Drive (the service account added as Content manager, so "
    "the organisation owns the storage) or be created on behalf of a user. The tests that need an uploaded file are therefore BLOCKED, not failed."
)


def classify_failure(exc: Exception) -> tuple[str, str]:
    """(status, detail) for a test that raised. An environment problem or a Google storage-quota refusal is BLOCKED - an external condition, not a
    defect in the feature under test - and is always labelled as such. Anything else is a FAIL carrying only the error type and a short message."""
    if isinstance(exc, ImportError):
        return "BLOCKED", f"Environment: {type(exc).__name__}: {exc}"
    if getattr(exc, "reason", "") in QUOTA_REASONS:
        return "BLOCKED", f"Google refused the write ({getattr(exc, 'reason', '')}): storage quota - see the FINDING note in this report."
    return "FAIL", f"{type(exc).__name__}: {str(exc)[:200]}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="run against the REAL test folder (needs credentials)")
    mode.add_argument("--fake", action="store_true", help="validate the harness against an in-memory fake (My Drive style)")
    mode.add_argument("--fake-shared-drive", action="store_true", help="the same, with a fake Shared Drive")
    mode.add_argument("--check-env", action="store_true", help="only report which Python is running and whether it has what the harness needs (contacts nothing)")
    parser.add_argument("--out-dir")
    parser.add_argument("--approve-writes", action="store_true", help="REQUIRED with --live: you approve creating folders and synthetic files (and renaming/moving them) in the real test folder")
    args = parser.parse_args(argv)
    if args.approve_writes and not args.live:
        parser.error("--approve-writes only applies to --live (the fake modes write nothing to Drive)")
    if args.live and not args.approve_writes:
        # Refuse FIRST: before the output folder is created, the isolated environment is set, the app is imported or any client exists.
        print("REFUSED: --live writes to the real test folder (it creates folders and synthetic files, then renames and moves them). Nothing was contacted or written. Re-run with --approve-writes once you have reviewed the plan.")
        return 5
    if args.check_env:
        info = check_environment()
        print(json.dumps(info, indent=2))
        problem = environment_problem(info)
        print(problem or "Environment OK.")
        return 4 if problem else 0
    if not args.out_dir:
        parser.error("--out-dir is required")
    out_dir = validate_out_dir(args.out_dir)
    started = datetime.now(timezone.utc)

    # An isolated application environment: its own database and storage under the run folder, a synthetic admin password.
    for name in [k for k in os.environ if k.startswith(("SPINCO_ADMIN_", "SPINCO_SETUP_"))]:
        del os.environ[name]
    os.environ.update(
        {
            "SPINCO_DB_PATH": str(out_dir / "spike.db"), "SPINCO_STORAGE_ROOT": str(out_dir / "storage"), "SPINCO_ADMIN_INITIAL_PASSWORD": SYNTHETIC_ADMIN_PASSWORD,
            "SPINCO_ARGON2_TIME_COST": "1", "SPINCO_ARGON2_MEMORY_COST": "8", "SPINCO_ARGON2_PARALLELISM": "1", "TEMP": str(out_dir), "TMP": str(out_dir),
        }
    )
    ctx = Context("LIVE" if args.live else ("FAKE-SHARED" if args.fake_shared_drive else "FAKE"), out_dir)
    ctx.writes_approved = bool(args.live and args.approve_writes)
    ctx.environment = check_environment()
    problem = environment_problem(ctx.environment)
    if problem:
        ctx.blocked_reason, ctx.blocked_kind, ctx.ready = problem, "environment", False  # nothing is imported or contacted under the wrong interpreter
    else:
        ctx.ready = build(ctx)
    if ctx.ready:
        from app.models.storage_record import DocumentStorageRecord

        ctx.DocumentStorageRecord = DocumentStorageRecord
    for number, name, func in TESTS:
        result = Result(number, name)
        ctx.results.append(result)
        if not ctx.ready:
            result.status, result.detail = "BLOCKED", ctx.blocked_reason or "not ready"
            continue
        if number > 1 and ctx.results[0].status in ("FAIL", "BLOCKED"):
            result.status, result.detail = "BLOCKED", "Test 1 (authentication and folder access) did not pass."
            continue
        try:
            func(ctx, result)
        except Exception as exc:  # noqa: BLE001 - a crashing test is reported with the error type, never a secret
            result.status, result.detail = classify_failure(exc)
            if result.status == "BLOCKED" and "storage quota" in result.detail and QUOTA_NOTE not in ctx.notes:
                ctx.notes.append(QUOTA_NOTE)
    report_path = write_reports(ctx, started)
    for r in ctx.results:
        print(f"{r.number:>2}. {r.status:<15} {r.name}")
    print(f"\nreport: {report_path}")
    if ctx.blocked_reason:
        print(f"BLOCKED: {ctx.blocked_reason}" + (f"\n{BLOCK_HELP}" if ctx.blocked_kind == "credentials" else ""))
        return {"environment": 4, "approval": 5}.get(ctx.blocked_kind, 3)
    return 0 if all(r.status in ("PASS", "PARTIAL", "NOT_APPLICABLE") for r in ctx.results) else 1


if __name__ == "__main__":
    sys.exit(main())
