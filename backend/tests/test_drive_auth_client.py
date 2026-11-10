"""Drive configuration, service-account sign-in and the REST client. FAKE-DRIVE tests: they validate OUR code (JWT signing, retry,
backoff, error mapping, hygiene) against a fake. They say nothing about how Google's real service behaves."""

import json
import logging
import time
from pathlib import Path

import httpx
import pytest

from app.services.drive import config as drive_config
from app.services.drive.auth import DriveAuthError, ServiceAccountCredentials, TokenProvider
from app.services.drive.client import (
    DriveChangeTokenExpired,
    DriveClient,
    DriveConflict,
    DriveError,
    DriveNotFound,
    DrivePermissionError,
    DriveQuotaError,
    DriveTransientError,
    _RateLimiter,
    escape_query,
)
from app.services.drive.config import DriveConfig, DriveConfigError, load_config

from conftest import REPO_ROOT
from drive_testkit import make_stack, private_pem, write_service_account_file

ROOT_ID = "1SyntheticRootFolderId_0001"


def _env(tmp_path, **overrides):
    creds = write_service_account_file(tmp_path)
    env = {"SPINCO_GDRIVE_ROOT_FOLDER_ID": ROOT_ID, "SPINCO_GDRIVE_CREDENTIALS_FILE": str(creds)}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


# ---------------------------------------------------------------------------------------------------------------- configuration


def test_a_complete_configuration_loads(tmp_path):
    cfg = load_config(_env(tmp_path))
    assert cfg.root_folder_id == ROOT_ID and cfg.credentials_file.is_file()
    assert cfg.scope == drive_config.DEFAULT_SCOPE and cfg.rate_limit_per_second == 10.0 and cfg.max_attempts == 6


@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"SPINCO_GDRIVE_ROOT_FOLDER_ID": None}, "SPINCO_GDRIVE_ROOT_FOLDER_ID"),
        ({"SPINCO_GDRIVE_ROOT_FOLDER_ID": "short"}, "SPINCO_GDRIVE_ROOT_FOLDER_ID"),
        ({"SPINCO_GDRIVE_ROOT_FOLDER_ID": "has spaces and !! chars"}, "SPINCO_GDRIVE_ROOT_FOLDER_ID"),
        ({"SPINCO_GDRIVE_CREDENTIALS_FILE": None}, "SPINCO_GDRIVE_CREDENTIALS_FILE"),
        ({"SPINCO_GDRIVE_CREDENTIALS_FILE": "Z:/definitely/not/here.json"}, "does not exist"),
        ({"SPINCO_GDRIVE_RATE_LIMIT_PER_SECOND": "fast"}, "must be a number"),
        ({"SPINCO_GDRIVE_RATE_LIMIT_PER_SECOND": "5000"}, "between"),
    ],
)
def test_bad_configuration_is_refused_with_a_precise_secret_free_message(tmp_path, overrides, needle):
    with pytest.raises(DriveConfigError) as exc:
        load_config(_env(tmp_path, **overrides))
    assert needle in str(exc.value)
    assert "BEGIN" not in str(exc.value)


def test_a_credentials_file_inside_the_repository_is_refused_even_if_it_does_not_exist():
    env = {"SPINCO_GDRIVE_ROOT_FOLDER_ID": ROOT_ID, "SPINCO_GDRIVE_CREDENTIALS_FILE": str(REPO_ROOT / "backend" / "sa-key.json")}
    with pytest.raises(DriveConfigError, match="OUTSIDE the repository"):
        load_config(env)


@pytest.mark.parametrize("value, expected", [("true", True), ("TRUE", True), ("1", True), ("yes", True), ("false", False), ("", False), ("off", False), (None, False)])
def test_drive_is_off_unless_explicitly_enabled(value, expected):
    env = {} if value is None else {"SPINCO_GDRIVE_ENABLED": value}
    assert drive_config.is_enabled(env) is expected


# ---------------------------------------------------------------------------------------------------------------- sign-in


def test_the_signed_jwt_is_accepted_and_the_token_is_cached(tmp_path):
    stack = make_stack(tmp_path)  # the fake verifies the RS256 signature with the matching public key
    first = stack.tokens.get_token()
    second = stack.tokens.get_token()
    assert first == second and stack.fake.token_requests == 1
    assert first.startswith("fake-token-")


def test_the_token_is_refreshed_shortly_before_it_expires(tmp_path):
    now = [time.time()]  # the fake checks the JWT's expiry against the real clock, so start from the real time
    stack = make_stack(tmp_path, clock=lambda: now[0])
    a = stack.tokens.get_token()
    now[0] += 3400  # expires_in=3600 minus the 120 s margin: still valid
    assert stack.tokens.get_token() == a and stack.fake.token_requests == 1
    now[0] += 100  # inside the margin: refreshed
    assert stack.tokens.get_token() != a and stack.fake.token_requests == 2


def test_a_wrong_key_is_rejected_with_only_a_google_error_code(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa

    stack = make_stack(tmp_path)
    stack.fake.public_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()  # a different key
    with pytest.raises(DriveAuthError) as exc:
        stack.tokens.get_token()
    assert "invalid_grant" in str(exc.value)
    assert "BEGIN" not in str(exc.value) and "eyJ" not in str(exc.value)


def test_an_unreachable_token_endpoint_is_an_auth_error_not_a_crash(tmp_path):
    stack = make_stack(tmp_path)
    stack.fake.drop_connection_next(1, match=lambda r: r.url.host == "oauth2.googleapis.com")
    with pytest.raises(DriveAuthError, match="token endpoint"):
        stack.tokens.get_token()


@pytest.mark.parametrize("content", ["not json at all", json.dumps({"type": "authorized_user"}), json.dumps({"type": "service_account"}), json.dumps({"type": "service_account", "client_email": 5, "private_key": 6})])
def test_a_bad_credentials_file_gives_one_generic_message(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content)
    with pytest.raises(DriveAuthError, match="not a valid service-account key file"):
        ServiceAccountCredentials.from_file(path)


def test_secrets_never_appear_in_reprs(tmp_path):
    stack = make_stack(tmp_path)
    credentials = ServiceAccountCredentials.from_file(stack.credentials_path)
    stack.tokens.get_token()
    pem_body = private_pem().splitlines()[1]
    for obj in (credentials, stack.tokens, stack.client):
        text = repr(obj)
        assert "BEGIN" not in text and pem_body not in text and "fake-token" not in text


def test_an_expired_token_is_refreshed_once_and_the_call_succeeds(tmp_path):
    stack = make_stack(tmp_path)
    stack.client.get_file(stack.config.root_folder_id)  # first token
    stack.fake.fail_next(1, 401, "authError", match=lambda r: r.url.host == "www.googleapis.com")
    obj = stack.client.get_file(stack.config.root_folder_id)
    assert obj.id == stack.config.root_folder_id and stack.fake.token_requests == 2


# ---------------------------------------------------------------------------------------------------------------- client: errors and retries


def test_not_found_and_permission_errors_are_mapped_and_not_retried(tmp_path):
    stack = make_stack(tmp_path)
    with pytest.raises(DriveNotFound):
        stack.client.get_file("doesnotexist12345")
    stack.fake.fail_next(1, 403, "insufficientFilePermissions")
    before = len([r for r in stack.fake.requests if r["path"].startswith("/drive")])
    with pytest.raises(DrivePermissionError) as exc:
        stack.client.get_file(stack.config.root_folder_id)
    assert not exc.value.retryable
    assert len([r for r in stack.fake.requests if r["path"].startswith("/drive")]) == before + 1  # exactly one attempt
    assert stack.sleeps == []


def test_quota_errors_are_not_retried(tmp_path):
    stack = make_stack(tmp_path)
    stack.fake.fail_next(1, 403, "storageQuotaExceeded")
    with pytest.raises(DriveQuotaError):
        stack.client.get_file(stack.config.root_folder_id)
    assert stack.sleeps == []


def test_transient_failures_are_retried_with_exponential_backoff_then_succeed(tmp_path):
    stack = make_stack(tmp_path)
    stack.fake.fail_next(3, 503)
    assert stack.client.get_file(stack.config.root_folder_id).id == stack.config.root_folder_id
    assert stack.sleeps == [0.75, 1.5, 3.0]  # base 1, 2, 4 seconds with the (injected) 0.5 jitter factor


def test_rate_limit_responses_are_retried_and_retry_after_is_honoured(tmp_path):
    stack = make_stack(tmp_path)
    stack.fake.fail_next(1, 429, "rateLimitExceeded", retry_after="2")
    stack.fake.fail_next(1, 403, "userRateLimitExceeded")
    assert stack.client.get_file(stack.config.root_folder_id).id
    assert stack.sleeps[0] == 2.0 and len(stack.sleeps) == 2


def test_retries_are_bounded_and_the_final_error_is_raised(tmp_path):
    stack = make_stack(tmp_path, max_attempts=3)
    stack.fake.fail_next(50, 503)
    with pytest.raises(DriveTransientError) as exc:
        stack.client.get_file(stack.config.root_folder_id)
    assert exc.value.retryable and exc.value.status == 503
    assert len(stack.sleeps) == 2  # slept between the three attempts, not after the last


def test_dropped_connections_and_timeouts_are_retried(tmp_path):
    stack = make_stack(tmp_path)
    stack.fake.drop_connection_next(1)
    stack.fake.drop_connection_next(1, exc=httpx.ReadTimeout)
    assert stack.client.get_file(stack.config.root_folder_id).id
    assert len(stack.sleeps) == 2


def test_a_persistent_outage_surfaces_as_a_retryable_error(tmp_path):
    stack = make_stack(tmp_path, max_attempts=2)
    stack.fake.drop_connection_next(100)
    with pytest.raises(DriveTransientError) as exc:
        stack.client.get_file(stack.config.root_folder_id)
    assert exc.value.retryable


def test_the_rate_limiter_spaces_requests():
    clock = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    limiter = _RateLimiter(2.0, lambda: clock[0], sleep)
    for _ in range(6):
        limiter.wait()
    assert sum(slept) == pytest.approx(2.0, abs=0.01)  # capacity 2 is free, the other four wait 0.5 s each


# ---------------------------------------------------------------------------------------------------------------- client: operations


def test_ids_are_generated_and_unique(tmp_path):
    stack = make_stack(tmp_path)
    ids = stack.client.generate_ids(5)
    assert len(set(ids)) == 5 and all(len(i) >= 10 for i in ids)


def test_upload_download_round_trip_and_a_repeated_id_conflicts(tmp_path):
    stack = make_stack(tmp_path)
    file_id = stack.client.generate_ids(1)[0]
    root = stack.config.root_folder_id
    obj = stack.client.upload_file(file_id=file_id, name="a.pdf", parent_id=root, content=b"%PDF-synthetic", app_properties={"sha256": "ab" * 32})
    assert obj.id == file_id and obj.size == 14 and obj.parents == (root,) and obj.app_properties == {"sha256": "ab" * 32}
    assert stack.client.download_bytes(file_id) == b"%PDF-synthetic"
    with pytest.raises(DriveConflict):
        stack.client.upload_file(file_id=file_id, name="a.pdf", parent_id=root, content=b"%PDF-synthetic")


def test_ranged_download_returns_partial_content(tmp_path):
    stack = make_stack(tmp_path)
    file_id = stack.client.generate_ids(1)[0]
    stack.client.upload_file(file_id=file_id, name="a.pdf", parent_id=stack.config.root_folder_id, content=b"0123456789")
    with stack.client.open_download(file_id, range_header="bytes=2-5") as download:
        assert download.status == 206 and b"".join(download.iter_bytes()) == b"2345"
        assert download.headers["content-range"] == "bytes 2-5/10"


def test_an_unexpected_upload_session_address_is_refused_and_no_token_is_sent_there(tmp_path):
    stack = make_stack(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.method == "POST":
            return httpx.Response(200, json={}, headers={"Location": "https://evil.example/upload?id=1"})
        return httpx.Response(200, json={"id": "x"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = DriveClient(stack.config, TokenProvider(ServiceAccountCredentials.from_file(stack.credentials_path), stack.config.scope, http), http, sleep=lambda s: None, unguarded=True)
    with pytest.raises(DriveError, match="unexpected upload session"):
        client.upload_file(file_id="fakeid000000000001", name="a.pdf", parent_id=stack.config.root_folder_id, content=b"x")
    assert not [c for c in calls if c.url.host == "evil.example"]  # nothing was ever sent to the foreign host


def test_listing_follows_pages_and_queries_are_escaped(tmp_path):
    stack = make_stack(tmp_path)
    root = stack.config.root_folder_id
    for i in range(5):
        stack.client.upload_file(file_id=stack.client.generate_ids(1)[0], name=f"it's {i}.pdf", parent_id=root, content=b"x" * (i + 1))
    names = [f.name for f in stack.client.list_files(f"'{root}' in parents and trashed = false", page_size=2)]
    assert len(names) == 5
    assert len(stack.client.find_children(root, name="it's 3.pdf")) == 1  # the quote is escaped, not injected
    assert escape_query("a'b\\c") == "a\\'b\\\\c"


def test_read_only_diagnostics_can_spot_a_public_link_and_report_quota(tmp_path):
    stack = make_stack(tmp_path)
    root = stack.config.root_folder_id
    assert [p["type"] for p in stack.client.list_permissions(root)] == ["user"]
    stack.fake.permissions[root] = [{"type": "user", "role": "writer"}, {"type": "anyone", "role": "reader"}]  # a public "anyone with the link"
    permissions = stack.client.list_permissions(root)
    assert any(p["type"] == "anyone" for p in permissions)
    assert set(permissions[0]) == {"type", "role"}  # never an e-mail address
    assert "limit" in stack.client.get_about()["storageQuota"]
    assert stack.client.get_raw(root, "id,capabilities")["id"] == root
    with pytest.raises(DriveNotFound):
        stack.client.list_permissions("doesnotexist12345")


def test_the_change_feed_and_an_expired_token(tmp_path):
    stack = make_stack(tmp_path)
    token = stack.client.get_start_page_token()
    file_id = stack.client.generate_ids(1)[0]
    stack.client.upload_file(file_id=file_id, name="a.pdf", parent_id=stack.config.root_folder_id, content=b"x")
    page = stack.client.list_changes(token)
    assert [c["fileId"] for c in page["changes"]] == [file_id] and page["newStartPageToken"]
    stack.fake.min_valid_change_token = 10_000
    with pytest.raises(DriveChangeTokenExpired):
        stack.client.list_changes(token)


# ---------------------------------------------------------------------------------------------------------------- hygiene


def test_no_credential_token_or_assertion_reaches_the_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    stack = make_stack(tmp_path)
    stack.fake.fail_next(2, 503)
    stack.client.get_file(stack.config.root_folder_id)
    file_id = stack.client.generate_ids(1)[0]
    stack.client.upload_file(file_id=file_id, name="a.pdf", parent_id=stack.config.root_folder_id, content=b"%PDF")
    stack.client.download_bytes(file_id)
    with pytest.raises(DriveNotFound):
        stack.client.get_file("doesnotexist12345")
    text = caplog.text
    assert "BEGIN" not in text and "Bearer" not in text and "fake-token" not in text and "eyJ" not in text and private_pem().splitlines()[1] not in text


def test_only_the_expected_http_methods_are_ever_used(tmp_path):
    stack = make_stack(tmp_path)
    file_id = stack.client.generate_ids(1)[0]
    stack.client.upload_file(file_id=file_id, name="a.pdf", parent_id=stack.config.root_folder_id, content=b"x")
    stack.client.update_file(file_id, name="b.pdf")
    stack.client.download_bytes(file_id)
    assert {r["method"] for r in stack.fake.requests} <= {"GET", "POST", "PATCH", "PUT"}  # never DELETE: the client cannot destroy files
    assert not hasattr(stack.client, "delete_file") and not hasattr(stack.client, "permanent_delete")


def test_the_client_module_never_calls_the_delete_endpoint():
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "drive" / "client.py").read_text(encoding="utf-8")
    assert '"DELETE"' not in source and "emptyTrash" not in source
