"""Independent LIVE test of the Drive integration signed in as a company USER (Google OAuth), using SYNTHETIC PDFs only.

    & "D:\\Bills\\backend\\.venv\\Scripts\\python.exe" backend\\drive_spike\\run_oauth_live.py --live --approve-writes --out-dir D:\\SpincoTA-artifacts\\test-runs\\<new-unique-run>\\oauth-live

Read-only check, no approval needed because nothing can be written (see "Status mode" below):

    & "D:\\Bills\\backend\\.venv\\Scripts\\python.exe" backend\\drive_spike\\run_oauth_live.py --status

Prerequisites (otherwise the run stops BLOCKED, before any Drive call, and says exactly what is missing):
    SPINCO_GDRIVE_AUTH_MODE=oauth_user
    SPINCO_GDRIVE_ROOT_FOLDER_ID            the root folder or Shared Drive id to use
    SPINCO_GDRIVE_OAUTH_CLIENT_FILE         the Desktop-client JSON (outside the repository)
    SPINCO_GDRIVE_OAUTH_TOKEN_FILE          written beforehand by  oauth_authorize.py --authorize

Status mode (`--status`): refreshes one access token, reads the metadata of the configured root (one GET) and its sharing summary (one GET), and
reports whether the root belongs to THE company Shared Drive (COMPANY_SHARED_DRIVE_ID below) or to a My Drive / another drive. It cannot write:
an httpx request hook refuses every request except a GET and the OAuth token endpoint's refresh POST, and the code path never reaches the upload,
create, move or archive functions. Nothing personal is requested from Drive (no e-mail addresses, no owner), and no token or secret is printed.

Safety:
  * `--live` does nothing unless `--approve-writes` AND `--require-shared-drive` are given: this run CREATES one new folder and a few synthetic files.
  * `--require-shared-drive` makes the run verify, BEFORE any write, that the configured root belongs to the company Shared Drive. A My Drive folder
    (including the old test folder), a different Shared Drive, a missing or unreadable root, a root this user cannot add children to, or anything
    that cannot be verified makes the run stop (exit 6) with every step BLOCKED and nothing written.
  * Everything is created inside ONE new sub-folder ("oauth-spike-[<label>-]<timestamp>"). Nothing that already existed is renamed, moved, shared or
    deleted. The ONLY file ever renamed or moved is the synthetic PDF this run uploaded itself (renamed once; moved between that folder's own Expense
    Reports / Archive sub-folders; archived again at the end as the reversible cleanup). The harness has no delete capability and no sharing capability.
  * `--run-label phase2-5` puts the label in every folder/file name and a visible "SYNTHETIC TEST DATA" banner inside the PDF.
  * Synthetic PDFs only. No token, secret or e-mail address is printed or written to the report; Drive ids are truncated.
  * Output goes under D:\\SpincoTA-artifacts\\test-runs\\ only and is never overwritten.

`--fake` runs the SAME steps against a fake Google (consent page, token endpoint) and a fake Drive. It validates the harness and is labelled FAKE in every
output: it is NOT evidence about Google's service.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_live_spike as base  # noqa: E402  (shares the Result type, synthetic PDF, environment check and failure classification)

BACKEND_DIR, REPO_ROOT, TEST_RUNS_ROOT, KIND = base.BACKEND_DIR, base.REPO_ROOT, base.TEST_RUNS_ROOT, base.KIND
Result, short = base.Result, base.short
REPORT_NAMES = ("oauth-report.json", "FAKE-oauth-report.json", "status-report.json", "FAKE-status-report.json")

# The ONLY Shared Drive this harness will write to. The guard compares the `driveId` Google reports for the configured root with this exact id.
COMPANY_SHARED_DRIVE_ID = "0AC6JZ6F0PQY-Uk9PVA"
EXIT_TARGET_REFUSED = 6
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT_FIELDS = "id,name,mimeType,driveId,capabilities(canAddChildren,canListChildren,canEdit,canTrash,canMoveItemWithinDrive,canDownload)"  # no owners, no e-mail
LIVE_MODES, STATUS_MODES = ("LIVE", "STATUS"), ("STATUS", "FAKE-STATUS")
# FAKE mode only: what the fake Drive pretends the configured root is (a test hook for the refusal paths; never used against Google).
FAKE_TARGETS = {
    "my-drive": ("ROOTFOLDER0000001", None),
    "shared-drive": (COMPANY_SHARED_DRIVE_ID, COMPANY_SHARED_DRIVE_ID),
    "other-shared-drive": ("0AOTHERDRIVE000001", "0AOTHERDRIVE000001"),
}

BLOCK_HELP = (
    "To enable the live run: (1) set SPINCO_GDRIVE_AUTH_MODE=oauth_user, SPINCO_GDRIVE_ROOT_FOLDER_ID, SPINCO_GDRIVE_OAUTH_CLIENT_FILE and "
    "SPINCO_GDRIVE_OAUTH_TOKEN_FILE; (2) sign in once with:  & \"D:\\Bills\\backend\\.venv\\Scripts\\python.exe\" backend\\drive_spike\\oauth_authorize.py --authorize  "
    "(3) check with --status; (4) re-run this script with --live --approve-writes --require-shared-drive."
)
DRIVE_FILE_NOTE = (
    "Scope: this run used the scope shown in test 1. `drive.file` only reaches files the app itself created (or that the user opened with it), so it cannot "
    "see a root folder that was created outside this OAuth client; the full `drive` scope is required to use that existing folder. `drive.file` would work "
    "only if the application created its own root folder - this run does not test that."
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
    if any((out / name).exists() for name in REPORT_NAMES):
        raise SystemExit("A report already exists in --out-dir; use a new unique run folder (nothing is ever overwritten).")
    out.mkdir(parents=True, exist_ok=True)
    return out


class ReadOnlyViolation(Exception):
    """A request that could change something was about to be sent while the harness was in read-only (status) mode."""


class ReadOnlyGuard:
    """An httpx request hook (it runs before ANY request leaves the client, whatever transport or proxy is used). In read-only mode it lets through
    GET requests and the OAuth token endpoint's POST (the access-token refresh) and refuses everything else before it is sent."""

    def __init__(self, token_uri: str):
        self.token_uri = token_uri
        self.reads = 0
        self.token_posts = 0
        self.blocked: list[str] = []

    def __call__(self, request) -> None:
        method = request.method.upper()
        if method == "GET":
            self.reads += 1
            return
        if method == "POST" and str(request.url).split("?")[0] == self.token_uri:
            self.token_posts += 1
            return
        self.blocked.append(method)
        raise ReadOnlyViolation(f"Read-only mode: a {method} request was refused before it was sent.")


class Context:
    def __init__(self, mode: str, out_dir: Path | None):
        self.mode, self.out_dir = mode, out_dir
        self.read_only = mode in STATUS_MODES
        self.guard: ReadOnlyGuard | None = None
        self.fake_target = "my-drive"
        self.target_check: dict | None = None
        self.oauth_client = None
        self.token_status: dict | None = None
        self.results: list[Result] = []
        self.notes: list[str] = [DRIVE_FILE_NOTE]
        self.blocked_reason: str | None = None
        self.blocked_kind = "credentials"
        self.environment: dict = {}
        self.config = self.http = self.client = self.provider = self.store = self.storage = self.folders = None
        self.run_name = self.run_folder_id = None
        self.token_file_bytes_before: bytes | None = None
        self.uploaded = None
        self.upload_content: bytes | None = None
        self.label = ""  # e.g. "phase2-5": makes every folder/file this run creates identifiable as that phase's synthetic test artifact


# -------------------------------------------------------------------------------------------------------------------------- build


def _simulated_browser(google):
    """FAKE mode only: plays the user's consent click and the browser's redirect to the loopback receiver."""
    import httpx
    from urllib.parse import parse_qs, urlparse

    def open_browser(url):
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        with httpx.Client(trust_env=False, timeout=10) as local:
            local.get(query["redirect_uri"], params={"code": google.issue_code(url), "state": query["state"]})

    return open_browser


def build(ctx: Context) -> bool:
    import httpx

    sys.path.insert(0, str(BACKEND_DIR))
    from app.services.drive import config as drive_config
    from app.services.drive.auth import DriveAuthError
    from app.services.drive.client import DriveClient, DriveError, UnguardedClientRefused
    from app.services.drive.oauth import OAuthClientConfig, OAuthTokenStore, authorize_interactively, inspect_token_file
    from app.services.drive.runtime import build_token_provider
    from app.services.drive.storage import DriveFolders, GoogleDriveStorage

    ctx.DriveError = DriveError
    if ctx.mode in LIVE_MODES:
        try:
            if drive_config.auth_mode() != drive_config.AUTH_OAUTH_USER:
                raise drive_config.DriveConfigError(f"{drive_config.AUTH_MODE_ENV} must be set to '{drive_config.AUTH_OAUTH_USER}' for this harness.")
            config = drive_config.load_config()
            ctx.store = OAuthTokenStore(config.oauth_token_file)
            ctx.oauth_client = OAuthClientConfig.from_file(config.oauth_client_file)
            status = inspect_token_file(ctx.store, ctx.oauth_client, config.scope)
        except (drive_config.DriveConfigError, DriveAuthError) as exc:
            ctx.blocked_reason = f"Configuration: {exc}"
            return False
        ctx.token_status = status
        if not (status["exists"] and status["valid"] and status["client_matches"] and status["scope_ok"]):
            ctx.blocked_reason = f"The OAuth token file is not ready ({status.get('problem') or 'missing, or for another client/scope'}). Authorize first."
            return False
        hooks = {}
        if ctx.read_only:
            ctx.guard = ReadOnlyGuard(ctx.oauth_client.token_uri)
            hooks = {"request": [ctx.guard]}
        http, sleep = httpx.Client(event_hooks=hooks), time.sleep
    else:
        sys.path.insert(0, str(BACKEND_DIR / "tests"))
        from drive_testkit import FakeGoogleOAuth, write_oauth_client_file
        from fake_drive import FakeDrive

        work = ctx.out_dir / "fake-work"
        work.mkdir(parents=True, exist_ok=True)
        root_id, drive_id = FAKE_TARGETS[ctx.fake_target]
        drive, google = FakeDrive(root_id, drive_id=drive_id), FakeGoogleOAuth()
        config = drive_config.DriveConfig(
            root_folder_id=next(iter(drive.files)), scope=drive_config.DEFAULT_SCOPE, auth_mode=drive_config.AUTH_OAUTH_USER, rate_limit_per_second=1000.0,
            oauth_client_file=write_oauth_client_file(work), oauth_token_file=work / "oauth-token.json",
        )
        ctx.oauth_client = OAuthClientConfig.from_file(config.oauth_client_file)
        hooks = {}
        if ctx.read_only:
            ctx.guard = ReadOnlyGuard(ctx.oauth_client.token_uri)
            hooks = {"request": [ctx.guard]}
        http, sleep = httpx.Client(transport=google.transport(drive), trust_env=False, event_hooks=hooks), (lambda s: None)
        ctx.store = OAuthTokenStore(config.oauth_token_file)
        authorize_interactively(ctx.oauth_client, ctx.store, config.scope, http=http, open_browser=_simulated_browser(google), timeout=10)
        ctx.fake = (drive, google)

    ctx.config, ctx.http = config, http
    ctx.token_file_bytes_before = config.oauth_token_file.read_bytes()
    ctx.provider = build_token_provider(config, http)
    try:
        # A harness is ISOLATED from the application's destination guard (it has its own --require-shared-drive check) and says so explicitly; the client
        # allows this opt-out ONLY when SPINCO_ENVIRONMENT is explicitly dev or staging (unset, production or anything else is refused), so this script
        # cannot write to a production Drive.
        ctx.client = DriveClient(config, ctx.provider, http, sleep=sleep, unguarded=True)
    except UnguardedClientRefused as exc:
        ctx.blocked_reason, ctx.blocked_kind = f"Not run: {exc}", "environment"
        return False
    try:
        ctx.provider.get_token()
    except DriveAuthError as exc:
        ctx.blocked_reason = f"Authentication failed: {exc}"
        return False
    ctx.DriveFolders, ctx.GoogleDriveStorage = DriveFolders, GoogleDriveStorage  # the storage itself is assembled in test 2, once the run folder exists
    return True


# ------------------------------------------------------------------------------------------------------------- target check and status


def _clean(text, limit: int = 60) -> str:
    """E-mail addresses and anything shaped like a token or key are masked before they can reach the console or a report."""
    from app.services.drive.client import safe_text

    return safe_text(text, limit)


def google_error(exc: Exception) -> dict:
    """What Google actually answered, for the report: the HTTP status, its machine reason, its status word and its (sanitised) message.
    Nothing here can hold a token, key, authorization header or e-mail address: every string passed through safe_text()."""
    return {
        "exception": type(exc).__name__, "http_status": getattr(exc, "status", None), "reason": _clean(getattr(exc, "reason", ""), 80),
        "google_status": _clean(getattr(exc, "google_status", ""), 40), "detail_reason": _clean(getattr(exc, "detail_reason", ""), 80),
        "message": _clean(getattr(exc, "detail", ""), 300),
    }


def _where(info: dict) -> str:
    if info["http_status"] is None:
        return info["exception"]
    return f"HTTP {info['http_status']}" + (f", reason {info['reason']}" if info["reason"] else "") + (f", {info['google_status']}" if info["google_status"] else "")


def inspect_target(ctx: Context, expected: str = COMPANY_SHARED_DRIVE_ID) -> dict:
    """READ-ONLY (one GET). Is the configured root inside THE company Shared Drive, and may this user add children to it?
    Never raises for a Drive problem: any failure to verify is returned as a refusal. `verified_for_writes` is the only thing the write guard trusts."""
    from app.services.drive.auth import DriveAuthError
    from app.services.drive.client import DriveError, DriveNotFound, DrivePermissionError

    root_id = ctx.config.root_folder_id
    check = {
        "configured_root": short(root_id), "expected_shared_drive": short(expected), "classification": "UNKNOWN",
        "matches_expected_shared_drive": None, "verified_for_writes": False, "refusal": None,
    }

    def refuse(reason: str) -> dict:
        check["refusal"] = reason
        return check

    try:
        root = ctx.client.get_raw(root_id, ROOT_FIELDS)
    except (DriveError, DriveAuthError, ValueError) as exc:
        check["google_error"] = info = google_error(exc)  # Google's own status/reason/message, so a refusal is never a mystery
        where = _where(info)
        if isinstance(exc, DriveNotFound):
            return refuse(f"The configured root was not found, or this account cannot see it ({where}; Drive answers 404 for both).")
        if isinstance(exc, DrivePermissionError):
            if info["http_status"] == 401:
                return refuse(f"Drive did not accept the sign-in for the configured root ({where}).")
            return refuse(f"This account is not allowed to read the configured root ({where}).")
        return refuse(f"The target could not be verified ({where}).")
    if not isinstance(root, dict) or not root.get("id"):
        return refuse("The target could not be verified (Drive returned no usable metadata).")

    caps = root.get("capabilities") if isinstance(root.get("capabilities"), dict) else {}
    drive_id = root.get("driveId") if isinstance(root.get("driveId"), str) and root.get("driveId") else None
    check.update(
        name=_clean(root.get("name", "")), is_folder=root.get("mimeType") == FOLDER_MIME, id_matches_request=root.get("id") == root_id,
        classification="SHARED DRIVE" if drive_id else "MY DRIVE", matches_expected_shared_drive=drive_id == expected if drive_id else False,
        root_is_the_shared_drive_itself=bool(drive_id) and drive_id == root_id, can_list_children=caps.get("canListChildren"),
        can_add_children=caps.get("canAddChildren"), can_edit=caps.get("canEdit"), can_trash=caps.get("canTrash"), can_move_within_drive=caps.get("canMoveItemWithinDrive"),
    )
    if drive_id is None:
        return refuse("The configured root is in a MY DRIVE (it has no driveId). Only the company Shared Drive is allowed.")
    if drive_id != expected:
        return refuse("The configured root belongs to a DIFFERENT Shared Drive than the company one.")
    if not check["is_folder"]:
        return refuse("The configured root is not a folder.")
    if not check["id_matches_request"]:
        return refuse("Drive returned a different item than the configured id.")
    if caps.get("canAddChildren") is not True:
        return refuse("The signed-in user cannot add items to the configured root (canAddChildren is not true).")
    check["verified_for_writes"] = True
    return check


def inspect_permissions(ctx: Context) -> dict:
    """READ-ONLY (one GET). Only the permission TYPE and ROLE are requested (never an e-mail address); the answer is a count per type:role."""
    from app.services.drive.client import DriveError

    try:
        permissions = ctx.client.list_permissions(ctx.config.root_folder_id)
    except (DriveError, ValueError) as exc:
        return {"available": False, "note": f"The sharing list could not be read ({type(exc).__name__}); managers of a Shared Drive can see it."}
    counts = Counter(f"{_clean(p.get('type', '?'), 30)}:{_clean(p.get('role', '?'), 30)}" for p in permissions if isinstance(p, dict))
    kinds = {str(p.get("type")) for p in permissions if isinstance(p, dict)}
    return {"available": True, "by_type_and_role": dict(sorted(counts.items())), "public_link": "anyone" in kinds, "domain_wide": "domain" in kinds}


def _probe(call) -> dict:
    from app.services.drive.auth import DriveAuthError
    from app.services.drive.client import DriveError

    try:
        return {"ok": True, "data": call()}
    except (DriveError, DriveAuthError, ValueError) as exc:
        return {"ok": False, **google_error(exc)}


def _likely_cause(about: dict, drive: dict) -> str:
    """A reading aid, not a verdict: which known situation the two probes match. The raw status/reason beside it is the evidence."""
    if not about["ok"]:
        text = " ".join(str(about.get(k, "")) for k in ("reason", "detail_reason", "message"))
        if "accessNotConfigured" in text or "SERVICE_DISABLED" in text or "has not been used" in text or "is disabled" in text:
            return "The Google Drive API appears to be switched off for the OAuth client's Google Cloud project: enable 'Google Drive API' there (APIs & Services > Library)."
        if "insufficientPermissions" in text or "SCOPE_INSUFFICIENT" in text:
            return "The stored authorization does not carry the Drive scope. Move the token file aside and run oauth_authorize.py --authorize again."
        return "Drive refused even a request that touches no file (about), so the problem is with the sign-in or the project, not with the Shared Drive. See the error beside it."
    if drive["ok"]:
        return "The id IS a Shared Drive this account can open (membership is fine); only reading it as a file was refused. See the error above; a folder inside the drive can be used as the root."
    if drive.get("http_status") == 404:
        return "Drive does not know this id as a Shared Drive for this account: the id is wrong, or the signed-in Google account is not a member of that Shared Drive (or is a different account than expected)."
    if drive.get("http_status") == 403:
        return "The Shared Drive is known but access to it is refused (see the reason): a membership, Workspace policy or admin setting may restrict this account."
    return "The probes did not narrow it down; see the errors."


def diagnose_root_failure(ctx: Context) -> dict:
    """READ-ONLY (two GETs), run only after the configured root could not be read. (1) `about`: does the Drive API answer at all for this sign-in?
    (2) `drives.get`: is the configured id a Shared Drive this account can open? Only booleans, status/reason and a few capability flags are kept."""
    about = _probe(ctx.client.get_about)
    drive = _probe(lambda: ctx.client.get_shared_drive(ctx.config.root_folder_id))
    if drive["ok"]:
        data = drive.pop("data")
        caps = data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}
        drive.update(name=_clean(data.get("name", "")), can_add_children=caps.get("canAddChildren"), can_list_children=caps.get("canListChildren"))
    about.pop("data", None)
    return {"drive_api_answers_for_this_sign_in": about, "configured_id_is_a_shared_drive_this_account_can_open": drive, "likely_cause": _likely_cause(about, drive)}


def apply_write_guard(ctx: Context, expected: str = COMPANY_SHARED_DRIVE_ID) -> bool:
    """The `--require-shared-drive` guard. Runs BEFORE any write; a False result leaves every step BLOCKED and nothing written."""
    ctx.target_check = inspect_target(ctx, expected)
    if ctx.target_check["verified_for_writes"]:
        return True
    ctx.blocked_reason = f"Shared Drive guard: {ctx.target_check['refusal']} Nothing was written."
    ctx.blocked_kind = "shared-drive"
    return False


def run_status(ctx: Context, expected: str = COMPANY_SHARED_DRIVE_ID) -> dict:
    """The read-only status report. Requests made: one token refresh (POST to the OAuth token endpoint) and two GETs on the configured root."""
    from app.services.drive.oauth import permission_problem

    ctx.target_check = target = inspect_target(ctx, expected)
    permissions = inspect_permissions(ctx) if target["classification"] != "UNKNOWN" else {"available": False, "note": "not requested: the target could not be read"}
    verified = target["verified_for_writes"]
    diagnosis = diagnose_root_failure(ctx) if target.get("google_error") else None  # only when Drive itself refused the root read
    banner ="READ-ONLY STATUS against the real configuration (OAuth, company user)" if ctx.mode == "STATUS" else "FAKE BACKEND - validates the status command only; NOT evidence about Google"
    return {
        "banner": banner,
        "authentication": {
            "auth_mode": ctx.config.auth_mode, "scope": ctx.config.scope, "access_token_obtained": bool(ctx.provider.get_token()),
            "token_file_permissions_acceptable": permission_problem(ctx.config.oauth_token_file) is None,
            "token_file_unchanged": ctx.config.oauth_token_file.read_bytes() == ctx.token_file_bytes_before,
        },
        "target": target,
        "permissions": permissions,
        "diagnosis": diagnosis,
        "requests": {"reads": ctx.guard.reads, "token_endpoint_posts": ctx.guard.token_posts, "refused_non_read_requests": len(ctx.guard.blocked)},
        "verdict": (
            "VERIFIED: the configured root is inside the company Shared Drive and this user may add items to it. Nothing was written."
            if verified
            else f"REFUSED: {target['refusal']} Nothing was written."
        ),
    }


# --------------------------------------------------------------------------------------------------------------------------- tests


def t01_sign_in_and_read_access(ctx: Context, r: Result):
    token = ctx.provider.get_token()
    about = ctx.client.get_about().get("storageQuota", {})  # storage numbers only; no name, no e-mail address is ever requested
    root = ctx.client.get_raw(ctx.config.root_folder_id, "id,mimeType,driveId,ownedByMe,capabilities(canAddChildren,canEdit,canTrash)")
    caps = root.get("capabilities", {})
    r.evidence.update(
        auth_mode=ctx.config.auth_mode, scope=ctx.config.scope, access_token_obtained=bool(token), storage_quota_reported=sorted(about),
        root_is_folder=root.get("mimeType") == "application/vnd.google-apps.folder", root_classification="SHARED DRIVE" if root.get("driveId") else "MY DRIVE",
        root_owned_by_signed_in_user=root.get("ownedByMe"), can_add_children=caps.get("canAddChildren"), can_edit=caps.get("canEdit"),
    )
    ok = r.evidence["root_is_folder"] and caps.get("canAddChildren") is not False
    r.status, r.detail = ("PASS", f"Signed in through the OAuth refresh token and reached the test root ({r.evidence['root_classification']}); the signed-in user may add children there.") if ok else ("FAIL", "The root is not a folder this user can add children to.")


def file_stem(ctx: Context) -> str:
    return f"{ctx.label.upper()}-SYNTHETIC" if ctx.label else "OAUTH-SPIKE"


def make_pdf(ctx: Context, marker: str) -> bytes:
    """The synthetic upload. Without a label it is the pre-existing blank synthetic page. With one, the page also CARRIES VISIBLE TEXT (a text annotation)
    and PDF metadata saying it is fictional test data, so nobody can mistake it for a company document."""
    if not ctx.label:
        return base.synthetic_pdf(marker)
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.annotations import FreeText

    banner = f"SYNTHETIC TEST DATA - {ctx.label.upper()} - FICTIONAL - NOT A REAL DOCUMENT"
    writer = PdfWriter()
    writer.add_blank_page(width=420 + (sum(map(ord, marker)) % 200), height=200)
    writer.add_annotation(page_number=0, annotation=FreeText(text=banner, rect=(10, 60, 400, 140), font_size="10pt"))
    writer.add_metadata({"/Title": f"synthetic-{ctx.label}-{marker}", "/Subject": "SYNTHETIC TEST DATA - fictional, not a real document"})
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def t02_create_run_folder(ctx: Context, r: Result):
    ctx.run_name = "oauth-spike-" + (f"{ctx.label}-" if ctx.label else "") + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    roles = {"spinco_role": "oauth-spike-run", **({"spinco_label": ctx.label} if ctx.label else {})}
    folder = ctx.client.create_folder(ctx.run_name, ctx.config.root_folder_id, file_id=ctx.client.generate_ids(1)[0], app_properties=roles)
    ctx.run_folder_id = folder.id
    ctx.folders = ctx.DriveFolders(ctx.client, folder.id)
    ctx.storage = ctx.GoogleDriveStorage(ctx.client, ctx.folders, temp_root=ctx.out_dir)
    r.evidence.update(run_folder=f"{ctx.run_name} ({short(folder.id)})", parent_is_test_root=folder.parents == (ctx.config.root_folder_id,))
    r.status, r.detail = ("PASS", "Created one new, uniquely named folder inside the test root; nothing existing was touched.") if r.evidence["parent_is_test_root"] else ("FAIL", "The new folder is not where it should be.")


def ensure_uploaded(ctx: Context):
    if ctx.storage is None:
        t02_create_run_folder(ctx, Result(0, "prerequisite"))
    if ctx.uploaded is None:
        content = make_pdf(ctx, "oauth-upload")
        ctx.upload_content = content
        ctx.uploaded = ctx.storage.upload(KIND, hashlib.sha256(content).hexdigest(), f"{file_stem(ctx)}-upload.pdf", content)
    return ctx.uploaded


def t03_upload_pdf(ctx: Context, r: Result):
    outcome = ensure_uploaded(ctx)
    in_folder = outcome.obj.parents == (ctx.folders.active(KIND),)
    r.evidence.update(file=short(outcome.obj.id), in_expected_folder=in_folder, size=outcome.obj.size, sha256_property_present=bool(outcome.obj.app_properties.get("sha256")), adopted=outcome.adopted)
    r.status, r.detail = ("PASS", "A synthetic PDF was uploaded (resumable) into 'Expense Reports' inside the OAuth spike folder.") if in_folder and r.evidence["sha256_property_present"] else ("FAIL", "Uploaded, but the file or its metadata is not where/what it should be.")


def t04_ownership_and_checksum(ctx: Context, r: Result):
    uploaded = ensure_uploaded(ctx)
    meta = ctx.client.get_raw(uploaded.obj.id, "id,ownedByMe,driveId,md5Checksum,size,capabilities(canTrash)")
    obj = ctx.storage.inspect_unchecked(uploaded.location)
    md5_ok = obj.md5 == hashlib.md5(ctx.upload_content).hexdigest() and obj.size == len(ctx.upload_content)
    r.evidence.update(owned_by_signed_in_user=meta.get("ownedByMe"), drive_type="SHARED DRIVE" if meta.get("driveId") else "MY DRIVE", md5_and_size_match=md5_ok)
    if not md5_ok:
        r.status, r.detail = "FAIL", "Drive's md5Checksum/size differ from the local values."
    elif meta.get("ownedByMe") is True:
        r.status, r.detail = "PASS", "md5 and size match, and the file is owned by the signed-in user - the storage-quota problem of a service account does not apply."
    else:
        r.status, r.detail = "PARTIAL", "md5 and size match, but ownership is not the signed-in user (Shared Drive, or the field was not reported); see evidence."


def t05_download_sha256_and_range(ctx: Context, r: Result):
    uploaded = ensure_uploaded(ctx)
    data = ctx.storage.retrieve(uploaded.location)
    same = hashlib.sha256(data).hexdigest() == hashlib.sha256(ctx.upload_content).hexdigest()
    with ctx.storage.open_stream(uploaded.location, expected_md5=hashlib.md5(ctx.upload_content).hexdigest(), expected_size=len(ctx.upload_content), range_header="bytes=0-9") as download:  # verified copy, cut here (3A.2-E.1)
        part = b"".join(download.iter_bytes())
        r.evidence.update(range_status=download.status, range_bytes_ok=part == ctx.upload_content[:10])
    r.evidence.update(sha256_uploaded=hashlib.sha256(ctx.upload_content).hexdigest(), sha256_downloaded=hashlib.sha256(data).hexdigest(), sha256_equal=same)
    r.status, r.detail = ("PASS", "The downloaded bytes have the same SHA-256 as the uploaded synthetic PDF; a Range request returned the expected bytes.") if same and r.evidence["range_bytes_ok"] else ("FAIL", "Round trip or Range mismatch.")


def t06_rename(ctx: Context, r: Result):
    """Renames ONLY the synthetic file this run uploaded (one PATCH of `name`), after checking it sits in this run's own folder."""
    uploaded = ensure_uploaded(ctx)
    fields = "id,name,md5Checksum,size,parents,appProperties,trashed"
    before = ctx.client.get_raw(uploaded.obj.id, fields)
    if before.get("parents") != [ctx.folders.active(KIND)]:
        r.status, r.detail = "FAIL", "Not renamed: the file is not in this run's own folder, so the rename could not be safely isolated."
        return
    new_name = f"{file_stem(ctx)}-renamed.pdf"
    ctx.client.update_file(uploaded.obj.id, name=new_name)
    after = ctx.client.get_raw(uploaded.obj.id, fields)
    content_intact = hashlib.sha256(ctx.storage.retrieve(uploaded.location)).hexdigest() == hashlib.sha256(ctx.upload_content).hexdigest()
    r.evidence.update(
        name_before=before.get("name"), name_after=after.get("name"), name_is_the_requested_one=after.get("name") == new_name and before.get("name") != new_name,
        md5_unchanged=after.get("md5Checksum") == before.get("md5Checksum"), size_unchanged=after.get("size") == before.get("size"),
        parent_unchanged=after.get("parents") == before.get("parents"), app_properties_unchanged=after.get("appProperties") == before.get("appProperties"),
        content_sha256_intact=content_intact, not_trashed=not after.get("trashed"),
    )
    checks = ("name_is_the_requested_one", "md5_unchanged", "size_unchanged", "parent_unchanged", "app_properties_unchanged", "content_sha256_intact", "not_trashed")
    r.status, r.detail = (
        ("PASS", "Renamed the synthetic file; its content, checksum, size, folder and metadata (including the sha256 property) are unchanged.")
        if all(r.evidence[k] for k in checks)
        else ("FAIL", "The rename changed something other than the name, or did not take effect; see evidence.")
    )


def t07_duplicate_upload(ctx: Context, r: Result):
    first = ensure_uploaded(ctx)
    again = ctx.storage.upload(KIND, hashlib.sha256(ctx.upload_content).hexdigest(), f"{file_stem(ctx)}-upload-copy.pdf", ctx.upload_content)
    r.evidence.update(same_file=again.obj.id == first.obj.id, deduplicated=again.deduplicated)
    r.status, r.detail = ("PASS", "Uploading identical content again created no second file.") if r.evidence["same_file"] and again.deduplicated else ("FAIL", "A duplicate was created or not detected.")


def t08_archive_and_restore(ctx: Context, r: Result):
    uploaded = ensure_uploaded(ctx)
    archived = ctx.storage.archive(uploaded.location, KIND)
    in_archive = archived.parents == (ctx.folders.archive(KIND),) and not archived.trashed
    restored = ctx.storage.restore(uploaded.location, KIND)
    back = restored.parents == (ctx.folders.active(KIND),)
    intact = hashlib.sha256(ctx.storage.retrieve(uploaded.location)).hexdigest() == hashlib.sha256(ctx.upload_content).hexdigest()
    r.evidence.update(moved_to_archive_folder=in_archive, restored_to_active_folder=back, content_intact_after_round_trip=intact, nothing_trashed_or_deleted=not archived.trashed and not restored.trashed)
    r.status, r.detail = ("PASS", "Archive and restore are folder moves inside the spike folder; the content is byte-identical afterwards; nothing was trashed or deleted.") if in_archive and back and intact else ("FAIL", "Archive/restore did not behave as designed.")


def t09_token_persistence_and_permissions(ctx: Context, r: Result):
    from app.services.drive.oauth import permission_problem
    from app.services.drive.runtime import build_token_provider

    second = build_token_provider(ctx.config, ctx.http)  # a brand-new provider, as after a restart: no cached token, no interaction
    token_ok = bool(second.get_token())
    problem = permission_problem(ctx.config.oauth_token_file)
    unchanged = ctx.config.oauth_token_file.read_bytes() == ctx.token_file_bytes_before
    r.evidence.update(new_provider_obtained_token_without_interaction=token_ok, token_file_permissions_acceptable=problem is None, token_file_unchanged_by_the_run=unchanged)
    if not token_ok or problem:
        r.status, r.detail = "FAIL", "A fresh provider could not sign in from the token file, or its permissions are not acceptable."
    else:
        r.status, r.detail = "PASS", "A new provider signed in from the stored refresh token alone; the token file's permissions are acceptable" + ("." if unchanged else " (Google rotated the refresh token and it was saved).")


def t10_expired_access_token_recovery(ctx: Context, r: Result):
    ctx.provider.invalidate()  # what the client does on a 401: drop the cached access token and sign in again
    ctx.storage.inspect_unchecked(ensure_uploaded(ctx).location)
    r.evidence["metadata_read_after_invalidate"] = True
    r.status, r.detail = "PASS", "After the cached access token was discarded, the next Drive call signed in again with the refresh token and succeeded."


def t11_cleanup_archive(ctx: Context, r: Result):
    """Reversible cleanup: moves the synthetic file into this run's own Archive folder. Nothing is trashed or deleted; the folder and file remain."""
    uploaded = ensure_uploaded(ctx)
    archived = ctx.storage.archive(uploaded.location, KIND)
    final = ctx.storage.inspect_unchecked(uploaded.location)
    in_archive = archived.parents == (ctx.folders.archive(KIND),) and not archived.trashed
    intact = final.md5 == hashlib.md5(ctx.upload_content).hexdigest() and final.size == len(ctx.upload_content)
    r.evidence.update(
        left_in_archive_folder=in_archive, not_trashed=not final.trashed, md5_and_size_intact=intact, final_name=final.name, run_folder=f"{ctx.run_name} ({short(ctx.run_folder_id)})",
        final_location="<run folder> / Archive / " + final.name,
    )
    r.status, r.detail = (
        ("PASS", "The synthetic file was archived (moved) into the run's Archive folder after verification; nothing was deleted or trashed and it is restorable.")
        if in_archive and intact and not final.trashed
        else ("FAIL", "The cleanup archive did not leave the file as expected; see evidence.")
    )


TESTS = [
    t01_sign_in_and_read_access, t02_create_run_folder, t03_upload_pdf, t04_ownership_and_checksum, t05_download_sha256_and_range, t06_rename,
    t07_duplicate_upload, t08_archive_and_restore, t09_token_persistence_and_permissions, t10_expired_access_token_recovery, t11_cleanup_archive,
]


# ------------------------------------------------------------------------------------------------------------------------- report


def write_reports(ctx: Context, started: datetime) -> Path:
    banner = "LIVE RUN against the real test folder (OAuth, company user)" if ctx.mode == "LIVE" else "FAKE BACKEND - validates the harness only; NOT evidence about Google"
    tag = "" if ctx.mode == "LIVE" else "FAKE-"
    counts: dict[str, int] = {}
    for result in ctx.results:
        counts[result.status] = counts.get(result.status, 0) + 1
    document = {
        "mode": ctx.mode, "banner": banner, "started_utc": started.isoformat(), "finished_utc": datetime.now(timezone.utc).isoformat(), "spike_folder": ctx.run_name,
        "environment": ctx.environment, "blocked_reason": ctx.blocked_reason, "help": BLOCK_HELP if ctx.blocked_reason and ctx.blocked_kind == "credentials" else None,
        "summary": counts, "results": [asdict(r) for r in ctx.results], "notes": ctx.notes, "target_check": ctx.target_check,
    }
    (ctx.out_dir / f"{tag}oauth-report.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    lines = [f"# Drive OAuth feasibility run - {ctx.mode}", "", f"**{banner}**", "", f"Summary: {counts}", ""]
    if ctx.environment:
        lines += [f"Interpreter: `{ctx.environment.get('interpreter')}` (Python {ctx.environment.get('python')}, project venv: {ctx.environment.get('is_project_venv')})", ""]
    if ctx.blocked_reason:
        lines += [f"**BLOCKED:** {ctx.blocked_reason}", ""] + ([BLOCK_HELP, ""] if ctx.blocked_kind == "credentials" else [])
    if ctx.target_check:
        lines += ["## Shared Drive guard (read-only check made before any write)", "```json", json.dumps(ctx.target_check, indent=2, default=str), "```", ""]
    lines += ["| # | Test | Status | Detail |", "|---|---|---|---|"]
    lines += [f"| {r.number} | {r.name} | {r.status} | {r.detail.replace('|', '/')} |" for r in ctx.results]
    lines += ["", "## Notes", ""] + [f"- {n}" for n in ctx.notes] + ["", "## Evidence", ""]
    for r in ctx.results:
        if r.evidence:
            lines += [f"### {r.number}. {r.name}", "```json", json.dumps(r.evidence, indent=2, default=str), "```"]
    (ctx.out_dir / f"{tag}oauth-report.md").write_text("\n".join(lines), encoding="utf-8")
    return ctx.out_dir / f"{tag}oauth-report.md"


def run_status_command(args) -> int:
    """`--status` / `--fake-status`: authenticate and look at the configured root. Read-only by construction (see ReadOnlyGuard)."""
    out_dir = validate_out_dir(args.out_dir) if args.out_dir else None
    ctx = Context("FAKE-STATUS" if args.fake_status else "STATUS", out_dir)
    ctx.fake_target = args.fake_target or "shared-drive"
    ctx.environment = base.check_environment()
    problem = base.environment_problem(ctx.environment)
    if problem:
        print(f"BLOCKED: {problem}")
        return 4
    if not build(ctx):
        print(f"BLOCKED: {ctx.blocked_reason}\nNothing was written. Sign in once with oauth_authorize.py --authorize, and check the SPINCO_GDRIVE_* settings listed in this script's --help.")
        return 3
    document = run_status(ctx)
    text = json.dumps(document, indent=2, default=str)
    print(text)
    if out_dir is not None:
        (out_dir / ("FAKE-status-report.json" if args.fake_status else "status-report.json")).write_text(text, encoding="utf-8")
    return 0 if document["target"]["verified_for_writes"] else EXIT_TARGET_REFUSED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="run against the REAL test folder as the authorized company user (WRITES: needs --approve-writes and --require-shared-drive)")
    mode.add_argument("--fake", action="store_true", help="validate the harness against a fake Google and a fake Drive")
    mode.add_argument("--status", action="store_true", help="READ-ONLY: sign in and report whether the configured root is the company Shared Drive; writes nothing")
    mode.add_argument("--fake-status", action="store_true", help="validate --status against a fake Google and a fake Drive")
    parser.add_argument("--approve-writes", action="store_true", help="REQUIRED with --live: you approve creating one folder and synthetic files in the target")
    parser.add_argument("--require-shared-drive", action="store_true", help=f"verify BEFORE any write that the root belongs to the company Shared Drive ({COMPANY_SHARED_DRIVE_ID}); REQUIRED with --live")
    parser.add_argument("--run-label", help="label for this run's artifacts, e.g. phase2-5: it is put in the folder and file names and in a visible banner inside the synthetic PDF")
    parser.add_argument("--fake-target", choices=sorted(FAKE_TARGETS), help="FAKE modes only: what the fake Drive pretends the root is (to exercise the refusal paths)")
    parser.add_argument("--out-dir", help="report folder under D:\\SpincoTA-artifacts\\test-runs\\ (required except for --status, where it is optional)")
    args = parser.parse_args(argv)
    status_mode = args.status or args.fake_status
    if args.run_label is not None and (status_mode or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,24}", args.run_label)):
        parser.error("--run-label is for write runs and may hold only lowercase letters, digits and '-' (at most 25 characters)")
    if args.fake_target and not (args.fake or args.fake_status):
        parser.error("--fake-target is only for --fake / --fake-status")
    if status_mode:
        if args.approve_writes or args.require_shared_drive:
            parser.error("--status is read-only: it takes neither --approve-writes nor --require-shared-drive")
        if args.fake_status and not args.out_dir:
            parser.error("--fake-status needs --out-dir")
        return run_status_command(args)
    if args.live and not args.approve_writes:
        print("REFUSED: --live creates a folder and synthetic files in the real target. Nothing was contacted or written. Re-run with --approve-writes once you have reviewed the implementation.")
        return 5
    if args.live and not args.require_shared_drive:
        print("REFUSED: --live must be run with --require-shared-drive, so the target is verified to be the company Shared Drive before anything is written. Nothing was contacted or written.")
        return 5
    if not args.out_dir:
        parser.error("--out-dir is required")
    out_dir = validate_out_dir(args.out_dir)
    started = datetime.now(timezone.utc)
    ctx = Context("LIVE" if args.live else "FAKE", out_dir)
    ctx.label = args.run_label or ""
    ctx.fake_target =args.fake_target or ("shared-drive" if args.require_shared_drive else "my-drive")
    ctx.environment = base.check_environment()
    problem = base.environment_problem(ctx.environment)
    if problem:
        ctx.blocked_reason, ctx.blocked_kind, ready = problem, "environment", False
    else:
        ready = build(ctx)
        if ready and args.require_shared_drive:
            ready = apply_write_guard(ctx)
    for number, func in enumerate(TESTS, start=1):
        result = Result(number, func.__name__[4:])
        if not ready:
            result.status, result.detail = "BLOCKED", ctx.blocked_reason or "not ready"
        else:
            try:
                func(ctx, result)
            except Exception as exc:  # noqa: BLE001 - recorded, never hidden; classify_failure keeps real defects as FAIL
                result.status, result.detail = base.classify_failure(exc)
        ctx.results.append(result)
    report = write_reports(ctx, started)
    counts = {s: sum(1 for r in ctx.results if r.status == s) for s in sorted({r.status for r in ctx.results})}
    print(f"{ctx.mode}: {counts}\nReport: {report}")
    if ctx.blocked_reason:
        print(f"BLOCKED: {ctx.blocked_reason}" + (f"\n{BLOCK_HELP}" if ctx.blocked_kind == "credentials" else ""))
        return {"environment": 4, "shared-drive": EXIT_TARGET_REFUSED}.get(ctx.blocked_kind, 3)
    return 0 if all(r.status in ("PASS", "PARTIAL") for r in ctx.results) else 1


if __name__ == "__main__":
    sys.exit(main())
