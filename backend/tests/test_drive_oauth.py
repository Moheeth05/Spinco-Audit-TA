"""User-OAuth sign-in for Drive (Phase 2.2): the installed-app loopback flow with PKCE, the refresh-token provider, the token file's
permissions, the environment-controlled provider selection. Everything is synthetic: a fake Google consent page / token endpoint and a fake Drive
(httpx.MockTransport). Only the loopback receiver uses a real socket, and only on 127.0.0.1. NO call is ever made to Google."""

import base64
import hashlib
import inspect
import json
import logging
import re
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.services.drive import config as drive_config
from app.services.drive import oauth
from app.services.drive.auth import DriveAuthError, TokenProvider
from app.services.drive.client import DriveClient
from app.services.drive.config import DriveConfigError, load_config
from app.services.drive.oauth import (
    OAuthClientConfig,
    OAuthTokenStore,
    StoredToken,
    UserOAuthTokenProvider,
    authorize_interactively,
    build_authorization_url,
    inspect_token_file,
    new_pkce,
    new_state,
    permission_problem,
)
from app.services.drive.runtime import build_token_provider
from app.services.drive.storage import DriveFolders, GoogleDriveStorage
from drive_testkit import SYNTHETIC_CLIENT_ID, SYNTHETIC_CLIENT_SECRET, FakeGoogleOAuth, pdf_bytes, write_oauth_client_file, write_service_account_file
from fake_drive import FakeDrive

DRIVE = "https://www.googleapis.com/auth/drive"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL behaviour")


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def env(tmp_path):
    fake = FakeGoogleOAuth()
    client = OAuthClientConfig.from_file(write_oauth_client_file(tmp_path))
    return SimpleNamespace(fake=fake, client=client, store=OAuthTokenStore(tmp_path / "oauth-token.json"), tmp=tmp_path)


def browser(fake, *, state=None, drop_state=False, error=None, probes=(), twice=False, record=None):
    """Plays the user and their browser: consent, then the redirect to the loopback receiver (optionally tampered with)."""
    record = record if record is not None else {}

    def open_browser(url):
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        redirect = query["redirect_uri"]
        record.update(url=url, redirect=redirect, query=query)
        with httpx.Client(trust_env=False, timeout=10) as local:  # a real socket, loopback only
            record["probes"] = [local.get(redirect.rstrip("/") + path, headers=headers).status_code for path, headers in probes]
            if error:
                params = {"state": query["state"], "error": error}
            else:
                params = {"code": fake.issue_code(url), "state": state or query["state"]}
            if drop_state:
                params.pop("state")
            record["status"] = local.get(redirect, params=params).status_code
            if twice:
                record["second_status"] = local.get(redirect, params=params).status_code

    return open_browser


def run_flow(env, open_browser, *, scope=DRIVE, timeout=10.0, client=None, **kwargs):
    with httpx.Client(transport=env.fake.transport(), trust_env=False) as http:
        return authorize_interactively(client or env.client, env.store, scope, http=http, open_browser=open_browser, timeout=timeout, **kwargs)


def provider(env, *, clock=None, scope=DRIVE, sleeps=None, client=None):
    http = httpx.Client(transport=env.fake.transport(), trust_env=False)
    return UserOAuthTokenProvider(client or env.client, env.store, scope, http, clock=clock or Clock(), sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


@pytest.fixture()
def authorized(env):
    run_flow(env, browser(env.fake))
    return env


# ------------------------------------------------------------------------------------------------------------------ client file


def test_a_desktop_client_file_is_read_and_its_secret_never_shown(tmp_path):
    client = OAuthClientConfig.from_file(write_oauth_client_file(tmp_path))
    assert client.client_id == SYNTHETIC_CLIENT_ID and client.token_uri == "https://oauth2.googleapis.com/token"
    assert SYNTHETIC_CLIENT_SECRET not in repr(client) and SYNTHETIC_CLIENT_SECRET not in str(client)


@pytest.mark.parametrize(
    "overrides",
    [
        {"section": "web"},  # a web-application client has fixed redirect URIs: unusable for a loopback flow
        {"secret": ""},
        {"client_id": "x"},
        {"token_uri": "https://evil.example/token"},  # the client secret would be sent there
        {"token_uri": "http://oauth2.googleapis.com/token"},
        {"auth_uri": "https://evil.example/auth"},
    ],
)
def test_unusable_or_suspicious_client_files_are_refused_without_echoing_anything(tmp_path, overrides):
    path = write_oauth_client_file(tmp_path, **overrides)
    with pytest.raises(DriveAuthError) as caught:
        OAuthClientConfig.from_file(path)
    assert SYNTHETIC_CLIENT_SECRET not in str(caught.value) and "evil.example" not in str(caught.value)


def test_a_garbage_client_file_is_refused(tmp_path):
    path = tmp_path / "client.json"
    path.write_text("not json SECRET-GARBAGE", encoding="utf-8")
    with pytest.raises(DriveAuthError) as caught:
        OAuthClientConfig.from_file(path)
    assert "SECRET-GARBAGE" not in str(caught.value)
    with pytest.raises(DriveAuthError):
        OAuthClientConfig.from_file(tmp_path / "missing.json")


# ------------------------------------------------------------------------------------------------------------------------- PKCE


def test_pkce_verifier_is_long_random_urlsafe_and_the_challenge_is_its_s256_hash():
    verifier, challenge = new_pkce()
    assert 43 <= len(verifier) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert challenge == expected and "=" not in challenge
    assert len({new_pkce()[0] for _ in range(50)}) == 50  # never repeats


def test_state_is_cryptographically_random_and_long():
    states = {new_state() for _ in range(200)}
    assert len(states) == 200 and all(len(s) >= 43 for s in states)  # 32 random bytes


def test_the_authorization_request_carries_pkce_state_loopback_redirect_and_offline_access(env):
    url = build_authorization_url(env.client, redirect_uri="http://127.0.0.1:5555/", scope=DRIVE, state="S", challenge="C", login_hint="a@example.test")
    parsed = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert parsed.hostname == "accounts.google.com" and parsed.scheme == "https"
    assert query["code_challenge"] == "C" and query["code_challenge_method"] == "S256" and query["state"] == "S"
    assert query["redirect_uri"] == "http://127.0.0.1:5555/" and query["response_type"] == "code" and query["access_type"] == "offline"
    assert query["scope"] == DRIVE and query["client_id"] == SYNTHETIC_CLIENT_ID
    assert "client_secret" not in query and SYNTHETIC_CLIENT_SECRET not in url  # the secret never goes through the browser


def test_the_token_exchange_proves_possession_of_the_pkce_verifier(authorized):
    record_form = authorized.fake.forms[0]
    assert record_form["grant_type"] == "authorization_code" and len(record_form["code_verifier"]) >= 43
    # A different verifier is refused by the (PKCE-enforcing) fake: an intercepted code is useless without the verifier.
    code = authorized.fake.issue_code(
        build_authorization_url(authorized.client, redirect_uri="http://127.0.0.1:5/", scope=DRIVE, state="s", challenge=new_pkce()[1])
    )
    with httpx.Client(transport=authorized.fake.transport(), trust_env=False) as http:
        with pytest.raises(DriveAuthError, match="invalid_grant"):
            oauth._token_request(
                authorized.client, http, {"grant_type": "authorization_code", "code": code, "redirect_uri": "http://127.0.0.1:5/", "code_verifier": new_pkce()[0]}, what="authorization code"
            )


# ------------------------------------------------------------------------------------------------------------- the authorization flow


def test_a_complete_authorization_writes_only_the_token_file_and_no_access_token(env):
    record = {}
    result = run_flow(env, browser(env.fake, record=record))
    assert result.token_file == env.store.path and result.scope == DRIVE and record["status"] == 200
    assert record["redirect"].startswith("http://127.0.0.1:") and record["redirect"].endswith("/")
    data = json.loads(env.store.path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "type", "client_id", "scope", "created_at", "refresh_token"}
    assert data["refresh_token"].startswith("fake-refresh-") and data["client_id"] == SYNTHETIC_CLIENT_ID
    assert "fake-token-oauth" not in env.store.path.read_text(encoding="utf-8") and SYNTHETIC_CLIENT_SECRET not in env.store.path.read_text(encoding="utf-8")
    assert [p.name for p in env.tmp.iterdir() if p.name.endswith(".tmp")] == []  # no stray temp file
    assert len(env.fake.forms) == 1  # exactly one exchange, and no Drive call at all


def test_a_state_mismatch_aborts_and_saves_nothing_and_never_contacts_the_token_endpoint(env):
    record = {}
    with pytest.raises(DriveAuthError, match="state mismatch"):
        run_flow(env, browser(env.fake, state="forged-state", record=record))
    assert record["status"] == 400 and not env.store.path.exists() and env.fake.forms == []


@pytest.mark.parametrize("drop", [True])
def test_a_missing_state_is_a_mismatch_too(env, drop):
    with pytest.raises(DriveAuthError, match="state mismatch"):
        run_flow(env, browser(env.fake, drop_state=drop))
    assert not env.store.path.exists() and env.fake.forms == []


def test_a_duplicated_state_parameter_is_a_mismatch(env):
    def open_browser(url):
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        with httpx.Client(trust_env=False) as local:
            local.get(query["redirect_uri"], params=[("state", query["state"]), ("state", query["state"]), ("code", "c")])

    with pytest.raises(DriveAuthError, match="state mismatch"):
        run_flow(env, open_browser)
    assert env.fake.forms == []


def test_a_denied_consent_is_reported_by_its_code_and_saves_nothing(env):
    with pytest.raises(DriveAuthError, match="access_denied"):
        run_flow(env, browser(env.fake, error="access_denied"))
    assert not env.store.path.exists() and env.fake.forms == []


def test_the_error_text_from_the_redirect_is_sanitised(env):
    with pytest.raises(DriveAuthError) as caught:
        run_flow(env, browser(env.fake, error="<script>alert(1)</script>SECRETTEXT"))
    assert str(caught.value) == "Google did not grant the authorization (scriptalertscriptsecrettext)."  # letters and underscores only: no markup, no text


def test_stray_and_wrongly_addressed_requests_do_not_end_or_poison_the_flow(env):
    record = {}
    run_flow(env, browser(env.fake, probes=[("/favicon.ico", {}), ("", {"Host": "evil.example"})], record=record))
    assert record["probes"] == [404, 400] and record["status"] == 200 and env.store.path.exists()


def test_a_redirect_can_be_used_only_once(env):
    record = {}
    run_flow(env, browser(env.fake, twice=True, record=record))
    assert record["status"] == 200 and record["second_status"] == 410 and len(env.fake.forms) == 1


def test_the_receiver_listens_on_the_loopback_interface_only_and_is_closed_afterwards(env):
    receiver = oauth._CallbackReceiver("state")
    try:
        assert receiver._httpd.server_address[0] == "127.0.0.1" and receiver.redirect_uri.startswith("http://127.0.0.1:")
    finally:
        receiver._httpd.server_close()
    record = {}
    run_flow(env, browser(env.fake, record=record))
    port = int(urlparse(record["redirect"]).port)
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=2):
        pass  # nothing is listening any more


def test_waiting_times_out_cleanly(env):
    started = time.monotonic()
    with pytest.raises(DriveAuthError, match="Timed out"):
        run_flow(env, lambda url: None, timeout=0.3)
    assert time.monotonic() - started < 5 and not env.store.path.exists()


def test_an_existing_token_file_is_never_overwritten_and_the_browser_is_not_even_opened(env):
    env.store.path.write_text("KEEP-ME", encoding="utf-8")
    opened = []
    with pytest.raises(DriveAuthError, match="never overwritten"):
        run_flow(env, lambda url: opened.append(url))
    assert env.store.path.read_text(encoding="utf-8") == "KEEP-ME" and opened == [] and env.fake.forms == []


def test_a_missing_token_folder_is_reported_before_anything_starts(env):
    env.store = OAuthTokenStore(env.tmp / "no-such-folder" / "token.json")
    with pytest.raises(DriveAuthError, match="folder"):
        run_flow(env, lambda url: pytest.fail("must not open a browser"))


def test_no_refresh_token_in_the_answer_means_nothing_is_saved(env, monkeypatch):
    original = env.fake._token

    def without_refresh(form):
        response = original(form)
        body = response.json()
        body.pop("refresh_token", None)
        return httpx.Response(200, json=body)

    monkeypatch.setattr(env.fake, "_token", without_refresh)
    with pytest.raises(DriveAuthError, match="refresh token"):
        run_flow(env, browser(env.fake))
    assert not env.store.path.exists()


def test_a_grant_narrower_than_requested_is_refused(env, monkeypatch):
    original = env.fake._token

    def narrower(form):
        response = original(form)
        return httpx.Response(200, json={**response.json(), "scope": "openid"})

    monkeypatch.setattr(env.fake, "_token", narrower)
    with pytest.raises(DriveAuthError, match="less access"):
        run_flow(env, browser(env.fake))
    assert not env.store.path.exists()


def test_invalid_client_credentials_are_reported_by_code_without_leaking_either_value(env):
    wrong = OAuthClientConfig(client_id=SYNTHETIC_CLIENT_ID, client_secret="WRONG-SECRET-VALUE")
    with pytest.raises(DriveAuthError, match="invalid_client") as caught:
        run_flow(env, browser(env.fake), client=wrong)
    assert "WRONG-SECRET-VALUE" not in str(caught.value) and SYNTHETIC_CLIENT_SECRET not in str(caught.value) and not env.store.path.exists()


def test_the_authorization_code_is_single_use(env):
    run_flow(env, browser(env.fake))
    code = next(iter(env.fake.forms))["code"]
    with httpx.Client(transport=env.fake.transport(), trust_env=False) as http, pytest.raises(DriveAuthError, match="invalid_grant"):
        oauth._token_request(env.client, http, {"grant_type": "authorization_code", "code": code, "redirect_uri": "http://127.0.0.1:1/", "code_verifier": "x" * 60}, what="authorization code")


# ----------------------------------------------------------------------------------------------------------------------- provider


def test_the_provider_refreshes_caches_and_refreshes_again_only_near_expiry(authorized):
    clock = Clock()
    p = provider(authorized, clock=clock)
    base = authorized.fake.access_issued  # the authorization exchange itself issued one
    first = p.get_token()
    assert first.startswith("fake-token-oauth-") and authorized.fake.access_issued == base + 1 and p.get_token() == first
    clock.now += 3600 - 121  # still outside the 120-second margin
    assert p.get_token() == first and authorized.fake.access_issued == base + 1
    clock.now += 2  # now inside the margin: refreshed
    assert p.get_token() != first and authorized.fake.access_issued == base + 2


def test_invalidate_forces_a_new_access_token(authorized):
    p = provider(authorized)
    base = authorized.fake.access_issued
    first = p.get_token()
    p.invalidate()
    assert p.get_token() != first and authorized.fake.access_issued == base + 2


def test_a_short_lived_token_is_refreshed_on_every_use(authorized):
    authorized.fake.expires_in = 60  # less than the safety margin
    p = provider(authorized)
    base = authorized.fake.access_issued
    p.get_token(), p.get_token()
    assert authorized.fake.access_issued == base + 2


def test_a_rotated_refresh_token_is_saved_and_used_next_time(env):
    env.fake.rotate = True
    run_flow(env, browser(env.fake))
    before = json.loads(env.store.path.read_text(encoding="utf-8"))["refresh_token"]
    p = provider(env)
    p.get_token()
    after = json.loads(env.store.path.read_text(encoding="utf-8"))["refresh_token"]
    assert after != before and permission_problem(env.store.path) is None
    p.invalidate()
    p.get_token()  # would fail with invalid_grant if the new token had not been persisted
    assert json.loads(env.store.path.read_text(encoding="utf-8"))["refresh_token"] != after


def test_an_unusual_non_ascii_rotated_token_is_handled_like_any_other(authorized, monkeypatch):
    original = authorized.fake._token

    def odd(form):
        response = original(form)
        return httpx.Response(200, json={**response.json(), "refresh_token": "réfresh-ünicode-token"})

    monkeypatch.setattr(authorized.fake, "_token", odd)
    assert provider(authorized).get_token()  # no TypeError from the comparison
    assert authorized.store.load().refresh_token == "réfresh-ünicode-token"


def test_a_revoked_authorization_says_so_and_how_to_fix_it(authorized):
    authorized.fake.revoke_all()
    with pytest.raises(DriveAuthError, match="invalid_grant") as caught:
        provider(authorized).get_token()
    assert "oauth_authorize.py --authorize" in str(caught.value) and "fake-refresh" not in str(caught.value)


def test_a_missing_token_file_needs_no_network_and_says_how_to_authorize(env):
    with pytest.raises(DriveAuthError, match="oauth_authorize.py --authorize"):
        provider(env).get_token()
    assert env.fake.forms == []


def test_a_token_from_a_different_oauth_client_is_refused(authorized):
    other = OAuthClientConfig(client_id="another-client-9999.apps.googleusercontent.com", client_secret="x" * 10)
    with pytest.raises(DriveAuthError, match="different OAuth client"):
        provider(authorized, client=other).get_token()
    assert len(authorized.fake.forms) == 1  # only the original authorization: no refresh was attempted


def test_scope_coverage_a_narrow_authorization_cannot_serve_a_wider_scope(env):
    run_flow(env, browser(env.fake), scope=DRIVE_FILE)
    assert provider(env, scope=DRIVE_FILE).get_token()
    with pytest.raises(DriveAuthError, match="does not cover"):
        provider(env, scope=DRIVE).get_token()


def test_the_full_drive_scope_covers_the_narrower_drive_file_scope(authorized):
    assert provider(authorized, scope=DRIVE_FILE).get_token()


def test_a_corrupt_token_file_is_refused_without_echoing_its_content(env):
    env.store.path.write_text("", encoding="utf-8")
    oauth.restrict_to_current_user(env.store.path)
    env.store.path.write_text("{not json REFRESH-SECRET-GARBAGE", encoding="utf-8")
    with pytest.raises(DriveAuthError) as caught:
        provider(env).get_token()
    assert "REFRESH-SECRET-GARBAGE" not in str(caught.value)
    env.store.path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    with pytest.raises(DriveAuthError, match="not valid"):
        provider(env).get_token()


def test_transient_token_endpoint_failures_are_retried_with_backoff_then_succeed(authorized):
    sleeps = []
    authorized.fake.fail_next(2, 503, "backend_error")
    assert provider(authorized, sleeps=sleeps).get_token() and sleeps == [1.0, 2.0]


def test_persistent_token_endpoint_failure_gives_up_with_a_secret_free_error(authorized):
    authorized.fake.fail_next(10, 503, "backend_error")
    with pytest.raises(DriveAuthError, match="backend_error") as caught:
        provider(authorized).get_token()
    assert "fake-refresh" not in str(caught.value)


def test_a_network_failure_is_reported_without_details(authorized):
    def broken(request):
        raise httpx.ConnectError("boom with SECRET-DETAIL", request=request)

    p = UserOAuthTokenProvider(authorized.client, authorized.store, DRIVE, httpx.Client(transport=httpx.MockTransport(broken)), sleep=lambda s: None)
    with pytest.raises(DriveAuthError) as caught:
        p.get_token()
    assert "SECRET-DETAIL" not in str(caught.value) and "Could not reach" in str(caught.value)


def test_a_permanent_client_error_is_not_retried(authorized):
    sleeps = []
    wrong = OAuthClientConfig(client_id=SYNTHETIC_CLIENT_ID, client_secret="WRONG-SECRET-VALUE")
    with pytest.raises(DriveAuthError, match="invalid_client"):
        provider(authorized, sleeps=sleeps, client=wrong).get_token()
    assert sleeps == []


def test_the_token_status_check_reports_without_secrets(authorized):
    status = inspect_token_file(authorized.store, authorized.client, DRIVE)
    assert status["exists"] and status["permissions_ok"] and status["valid"] and status["client_matches"] and status["scope_ok"]
    assert "fake-refresh" not in json.dumps(status)
    assert inspect_token_file(OAuthTokenStore(authorized.tmp / "none.json"), None, None)["exists"] is False


# --------------------------------------------------------------------------------------------------------------------- permissions


@windows_only
def test_the_token_file_is_created_with_an_acl_for_the_current_user_only(authorized):
    assert permission_problem(authorized.store.path) is None
    listing = oauth._run([oauth._system_tool("icacls.exe"), str(authorized.store.path)]).stdout
    me, _sid = oauth._windows_user()
    names = {n.strip().lower() for n in re.findall(r"^\s*(\S[^:\r\n]*?):\(", re.sub(re.escape(str(authorized.store.path)), "", listing, flags=re.I), flags=re.M)}
    assert me.lower() in names and names <= {me.lower(), "nt authority\\system", "builtin\\administrators", "owner rights"}, names
    assert not names & {"everyone", "builtin\\users", "nt authority\\authenticated users"}  # nobody else, and nothing inherited from a broad parent


def test_the_file_is_locked_down_before_the_secret_is_written(env, monkeypatch):
    seen = []
    real = oauth.restrict_to_current_user

    def spy(path):
        seen.append(Path(path).stat().st_size)  # at this moment the file must still be empty
        real(path)

    monkeypatch.setattr(oauth, "restrict_to_current_user", spy)
    env.store.save_new(StoredToken(refresh_token="r" * 20, client_id=SYNTHETIC_CLIENT_ID, scope=DRIVE, created_at="2026-01-01T00:00:00Z"))
    assert seen == [0]


@windows_only
def test_a_token_file_that_another_account_can_read_is_refused_before_it_is_read(authorized):
    oauth._run([oauth._system_tool("icacls.exe"), str(authorized.store.path), "/grant", "*S-1-1-0:(R)"])  # Everyone: read
    problem = permission_problem(authorized.store.path)
    assert problem and "other accounts" in problem
    with pytest.raises(DriveAuthError, match="other accounts"):
        provider(authorized).get_token()
    assert len(authorized.fake.forms) == 1  # the token was never even read, let alone sent
    status = inspect_token_file(authorized.store, authorized.client, DRIVE)
    assert status["permissions_ok"] is False and status["valid"] is None


def test_the_posix_branch_flags_group_and_world_access(tmp_path, monkeypatch):
    path = tmp_path / "t.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(oauth, "_IS_WINDOWS", False)
    if sys.platform == "win32":
        assert permission_problem(path)  # Windows reports 0666 for ordinary files, i.e. 'open'
    else:
        path.chmod(0o644)
        assert permission_problem(path)
        path.chmod(0o600)
        assert permission_problem(path) is None


def test_saving_never_overwrites_but_a_rotation_replaces_atomically(env):
    token = StoredToken(refresh_token="r" * 20, client_id=SYNTHETIC_CLIENT_ID, scope=DRIVE, created_at="2026-01-01T00:00:00Z")
    env.store.save_new(token)
    with pytest.raises(DriveAuthError, match="never overwritten"):
        env.store.save_new(StoredToken(refresh_token="s" * 20, client_id=SYNTHETIC_CLIENT_ID, scope=DRIVE, created_at="x"))
    assert env.store.load().refresh_token == "r" * 20
    env.store.replace(StoredToken(refresh_token="s" * 20, client_id=SYNTHETIC_CLIENT_ID, scope=DRIVE, created_at="x"))
    assert env.store.load().refresh_token == "s" * 20 and permission_problem(env.store.path) is None


# ---------------------------------------------------------------------------------------------------------------------- no leakage


def test_no_secret_reaches_logs_output_reprs_or_error_messages(env, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    record = {}
    run_flow(env, browser(env.fake, record=record), announce=print)
    verifier = env.fake.forms[0]["code_verifier"]
    p = provider(env)
    token = p.get_token()
    errors = []
    env.fake.revoke_all()
    p.invalidate()
    try:
        p.get_token()
    except DriveAuthError as exc:
        errors.append(str(exc))
    refresh = json.loads(env.store.path.read_text(encoding="utf-8"))["refresh_token"]
    secrets_to_hide = [SYNTHETIC_CLIENT_SECRET, "fake-code-1", refresh, token, verifier]
    # The simulated BROWSER's own request to the loopback receiver is logged by the test's httpx client (in real life the browser makes it, not
    # our code). Everything our code does is checked: it must have emitted no log record that mentions the redirect at all.
    ours = [r.getMessage() for r in caplog.records if "127.0.0.1" not in r.getMessage() and r.name.startswith(("app", "httpx"))]
    assert not [r for r in caplog.records if r.name.startswith("app")]  # our modules log nothing in this flow
    surfaces = ["\n".join(ours), capsys.readouterr().out, *errors, repr(p), repr(env.client), repr(env.store), repr(StoredToken(refresh, "c", "s", "t")), str(p), repr(oauth.OAuthTokenStore(env.store.path))]
    for secret in secrets_to_hide:
        for surface in surfaces:
            assert secret not in surface
    assert "state=" in record["url"]  # (the announced URL legitimately contains the one-time state, which is not a credential)


# --------------------------------------------------------------------------------------------------------------- provider selection


def oauth_env(tmp_path, **overrides):
    values = {
        "SPINCO_GDRIVE_ROOT_FOLDER_ID": "ROOTFOLDER0000001",
        "SPINCO_GDRIVE_AUTH_MODE": "oauth_user",
        "SPINCO_GDRIVE_OAUTH_CLIENT_FILE": str(write_oauth_client_file(tmp_path)),
        "SPINCO_GDRIVE_OAUTH_TOKEN_FILE": str(tmp_path / "oauth-token.json"),
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def test_the_service_account_is_the_default_even_when_oauth_files_are_configured(tmp_path):
    env = oauth_env(tmp_path, SPINCO_GDRIVE_AUTH_MODE=None, SPINCO_GDRIVE_CREDENTIALS_FILE=str(write_service_account_file(tmp_path)))
    config = load_config(env)
    assert config.auth_mode == "service_account" and config.credentials_file is not None
    assert isinstance(build_token_provider(config, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))), TokenProvider)


def test_explicit_service_account_mode_behaves_the_same(tmp_path):
    env = {"SPINCO_GDRIVE_ROOT_FOLDER_ID": "ROOTFOLDER0000001", "SPINCO_GDRIVE_AUTH_MODE": "Service_Account", "SPINCO_GDRIVE_CREDENTIALS_FILE": str(write_service_account_file(tmp_path))}
    assert isinstance(build_token_provider(load_config(env), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))), TokenProvider)


def test_oauth_mode_selects_the_user_provider_and_needs_no_service_account_key(tmp_path):
    config = load_config(oauth_env(tmp_path))
    assert config.auth_mode == "oauth_user" and config.credentials_file is None and config.oauth_token_file.name == "oauth-token.json"
    chosen = build_token_provider(config, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert isinstance(chosen, UserOAuthTokenProvider) and not isinstance(chosen, TokenProvider)


def test_an_unknown_mode_is_an_error_never_a_silent_fallback(tmp_path):
    with pytest.raises(DriveConfigError, match="AUTH_MODE"):
        load_config(oauth_env(tmp_path, SPINCO_GDRIVE_AUTH_MODE="oauth"))


@pytest.mark.parametrize("missing", ["SPINCO_GDRIVE_OAUTH_CLIENT_FILE", "SPINCO_GDRIVE_OAUTH_TOKEN_FILE"])
def test_oauth_mode_requires_both_files(tmp_path, missing):
    with pytest.raises(DriveConfigError, match=missing):
        load_config(oauth_env(tmp_path, **{missing: None}))


def test_oauth_files_inside_the_repository_are_refused(tmp_path):
    inside = str(Path(drive_config.__file__).resolve().parents[3] / "oauth-token.json")
    with pytest.raises(DriveConfigError, match="OUTSIDE the repository"):
        load_config(oauth_env(tmp_path, SPINCO_GDRIVE_OAUTH_TOKEN_FILE=inside))
    with pytest.raises(DriveConfigError, match="OUTSIDE the repository"):
        load_config(oauth_env(tmp_path, SPINCO_GDRIVE_OAUTH_CLIENT_FILE=str(Path(inside).with_name("client.json"))))


def test_a_missing_client_file_or_token_folder_is_reported(tmp_path):
    with pytest.raises(DriveConfigError, match="does not exist"):
        load_config(oauth_env(tmp_path, SPINCO_GDRIVE_OAUTH_CLIENT_FILE=str(tmp_path / "absent.json")))
    with pytest.raises(DriveConfigError, match="folder"):
        load_config(oauth_env(tmp_path, SPINCO_GDRIVE_OAUTH_TOKEN_FILE=str(tmp_path / "absent-dir" / "t.json")))


def test_the_drive_client_interface_is_unchanged():
    # Phase 3A.1.1: the client now OWNS its destination guard. Everything up to `rng` is unchanged; the keyword-only additions are the explicit
    # harness/fake opt-out (`unguarded`) and two test hooks for the guard. There is deliberately NO way to pass, swap or clear a guard.
    assert list(inspect.signature(DriveClient.__init__).parameters) == ["self", "config", "tokens", "http", "sleep", "monotonic", "rng", "unguarded", "guard_clock", "guard_max_depth"]
    for method in ("get_token", "invalidate"):
        assert callable(getattr(UserOAuthTokenProvider, method)) and callable(getattr(TokenProvider, method))


def test_a_whole_drive_stack_runs_on_oauth_tokens_including_the_401_refresh_path(tmp_path):
    drive, google = FakeDrive(), FakeGoogleOAuth()
    values = oauth_env(tmp_path)
    config = load_config(values)
    http = httpx.Client(transport=google.transport(drive), trust_env=False)
    client_config = OAuthClientConfig.from_file(config.oauth_client_file)
    authorize_interactively(client_config, OAuthTokenStore(config.oauth_token_file), config.scope, http=http, open_browser=browser(google), timeout=10)

    tokens = build_token_provider(config, http)
    client = DriveClient(config, tokens, http, sleep=lambda s: None, rng=lambda: 0.5, unguarded=True)  # a fake-backed provider test: not the destination guard
    storage = GoogleDriveStorage(client, DriveFolders(client, config.root_folder_id), temp_root=tmp_path)
    content = pdf_bytes("oauth")
    digest = hashlib.sha256(content).hexdigest()
    outcome = storage.upload("expense_report", digest, "SYNTHETIC-oauth.pdf", content)
    assert hashlib.sha256(storage.retrieve(outcome.location)).hexdigest() == digest
    assert drive.requests and all(r["auth"].startswith("Bearer fake-token-oauth-") for r in drive.requests)

    issued = google.access_issued
    drive.fail_next(1, 401, "authError")  # an expired / revoked access token: the client invalidates and signs in again
    assert storage.retrieve(outcome.location) == content
    assert google.access_issued == issued + 1
