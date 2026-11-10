"""Phase 3A.2-A - token acquisition hardening.

Root cause being fixed: both token providers held their lock for the WHOLE refresh - the file checks (on Windows two helper processes, 30 s limit
each), then up to three token requests with 20 s timeouts and 1 s + 2 s pauses (about 63 s for the user-OAuth provider, 20 s for the service
account). Every thread that needed a token queued on that lock, whether or not the cached token was still valid.

Now: one thread refreshes at a time, within a hard 15 s deadline (retries and pauses included); every other thread keeps using a still-valid token
or - when none is valid - waits a few seconds at most and fails with a RETRYABLE error; a failed refresh is remembered for a short window; transient
and permanent failures are different exception types, and a transient one never marks an upload FAILED.

Everything runs against mocked token endpoints (httpx.MockTransport). No call is ever made to Google; no real credential or token file is used."""

import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import storage_record as sr
from app.models.storage_record import StorageEvent
from app.services.drive import oauth
from app.services.drive.auth import (
    MAX_TOKEN_DEADLINE_SECONDS,
    DriveAuthError,
    DriveAuthPermanentError,
    DriveAuthTransientError,
    ServiceAccountCredentials,
    TokenProvider,
    TokenSettings,
)
from app.services.drive.config import DriveConfigError, load_config
from app.services.drive.lifecycle import StorageUploadError
from app.services.drive.oauth import OAuthClientConfig, OAuthTokenStore, StoredToken, UserOAuthTokenProvider
from app.services.drive.runtime import build_token_provider

from drive_testkit import (
    SYNTHETIC_CLIENT_SECRET,
    FakeGoogleOAuth,
    make_stack,
    pdf_bytes,
    write_oauth_client_file,
    write_service_account_file,
)

DRIVE = "https://www.googleapis.com/auth/drive"
KIND = "expense_report"
REFRESH = "fake-refresh-for-token-tests-1"


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


class SimTime:
    """Simulated time: no test below waits for real. `sleep` advances it, and so can a scripted endpoint (a 'slow' token endpoint)."""

    def __init__(self):
        self.wall, self.mono, self.sleeps = 1_800_000_000.0, 1000.0, []

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class Endpoint:
    """A scripted token endpoint. `script` holds one step per request (a callable taking the request and returning a response, raising, blocking or
    advancing simulated time; returning None means 'answer normally'); once it is empty every request is answered with a fresh token."""

    def __init__(self, expires_in: int = 3600):
        self.expires_in = expires_in
        self.script: list = []
        self.calls = 0
        self.issued = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.timeouts: list[dict] = []
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.timeouts.append(dict(request.extensions.get("timeout", {})))
            step = self.script.pop(0) if self.script else None
        try:
            if step is not None:
                answer = step(request)
                if answer is not None:
                    return answer
            with self._lock:
                self.issued += 1
                number = self.issued
            return httpx.Response(200, json={"access_token": f"fake-token-{number}", "expires_in": self.expires_in, "token_type": "Bearer"})
        finally:
            with self._lock:
                self.in_flight -= 1


def fail(status: int, error: str = "backend_error"):
    return lambda request: httpx.Response(status, json={"error": error})


def timeout(request):
    raise httpx.ReadTimeout("read timed out - SECRET-DETAIL-FROM-THE-EXCEPTION", request=request)


def slow(sim: SimTime, seconds: float, then):
    """The endpoint 'takes' `seconds` of simulated time before it answers (or fails) with `then`."""

    def step(request):
        sim.advance(seconds)
        return then(request)

    return step


def block(entered: threading.Event, gate: threading.Event):
    def step(request):
        entered.set()
        assert gate.wait(timeout=20), "the test never released the blocked token request"
        return None

    return step


def service_account(tmp_path, endpoint, *, sim=None, wall=None, settings=None):
    http = httpx.Client(transport=httpx.MockTransport(endpoint.handler))
    options = {"settings": settings}
    if sim is not None:
        options.update(clock=lambda: sim.wall, monotonic=lambda: sim.mono)
    elif wall is not None:
        options["clock"] = wall
    credentials = ServiceAccountCredentials.from_file(write_service_account_file(tmp_path))
    return TokenProvider(credentials, DRIVE, http, **options)


def user_oauth(tmp_path, endpoint, *, sim=None, wall=None, settings=None, transport=None):
    client = OAuthClientConfig.from_file(write_oauth_client_file(tmp_path))
    store = OAuthTokenStore(tmp_path / "oauth-token.json")
    if not store.exists():
        store.save_new(StoredToken(refresh_token=REFRESH, client_id=client.client_id, scope=DRIVE, created_at="2026-01-01T00:00:00Z"))
    http = httpx.Client(transport=transport or httpx.MockTransport(endpoint.handler))
    options = {"settings": settings}
    if sim is not None:
        options.update(clock=lambda: sim.wall, monotonic=lambda: sim.mono, sleep=sim.sleep)
    else:
        options["sleep"] = lambda s: None
        if wall is not None:
            options["clock"] = wall
    return UserOAuthTokenProvider(client, store, DRIVE, http, **options), store


KINDS = ["service_account", "user_oauth"]


def make_provider(kind, tmp_path, endpoint, **options):
    return service_account(tmp_path, endpoint, **options) if kind == "service_account" else user_oauth(tmp_path, endpoint, **options)[0]


# ====================================================================================================================== refresh basics


@pytest.mark.parametrize("kind", KINDS)
def test_a_successful_refresh_is_cached_and_not_repeated(tmp_path, kind):
    endpoint = Endpoint()
    provider = make_provider(kind, tmp_path, endpoint)
    first = provider.get_token()
    assert first == "fake-token-1" and provider.get_token() == first and endpoint.calls == 1


@pytest.mark.parametrize("kind", KINDS)
def test_an_expired_access_token_is_refreshed_and_the_margins_are_unchanged(tmp_path, kind):
    sim, endpoint = SimTime(), Endpoint()
    provider = make_provider(kind, tmp_path, endpoint, sim=sim)
    first = provider.get_token()
    sim.advance(3600 - 121)  # still outside the 120 s soft margin: no refresh
    assert provider.get_token() == first and endpoint.calls == 1
    sim.advance(2)  # inside the margin: refreshed
    second = provider.get_token()
    assert second != first and endpoint.calls == 2
    sim.advance(4000)  # long expired: refreshed again
    assert provider.get_token() not in (first, second) and endpoint.calls == 3


@pytest.mark.parametrize("kind", KINDS)
def test_invalidate_forces_a_new_token_and_clears_a_remembered_failure(tmp_path, kind):
    sim, endpoint = SimTime(), Endpoint()
    provider = make_provider(kind, tmp_path, endpoint, sim=sim)
    first = provider.get_token()
    provider.invalidate()
    assert provider.get_token() != first and endpoint.calls == 2
    provider.invalidate()
    endpoint.script = [fail(503)] * 10
    with pytest.raises(DriveAuthTransientError):
        provider.get_token()
    calls = endpoint.calls
    endpoint.script = []
    with pytest.raises(DriveAuthTransientError):  # remembered
        provider.get_token()
    assert endpoint.calls == calls
    provider.invalidate()  # the caller learned something new (a 401): the remembered failure is forgotten, the endpoint is asked again
    assert provider.get_token() and endpoint.calls == calls + 1


# ====================================================================================================================== rejection and classification


@pytest.mark.parametrize("kind", KINDS)
def test_a_rejected_refresh_token_is_permanent_and_remembered(tmp_path, kind):
    sim, endpoint = SimTime(), Endpoint()
    endpoint.script = [fail(400, "invalid_grant")]
    provider = make_provider(kind, tmp_path, endpoint, sim=sim)
    with pytest.raises(DriveAuthPermanentError, match="invalid_grant") as caught:
        provider.get_token()
    assert not isinstance(caught.value, DriveAuthTransientError) and isinstance(caught.value, DriveAuthError)
    calls = endpoint.calls
    with pytest.raises(DriveAuthPermanentError):  # remembered: not asked again for the permanent window
        provider.get_token()
    assert endpoint.calls == calls
    sim.advance(61)  # the window has passed and the account has been re-authorised
    assert provider.get_token() and endpoint.calls == calls + 1


CLASSIFICATION = [
    (400, "invalid_grant", DriveAuthPermanentError),
    (401, "invalid_client", DriveAuthPermanentError),
    (403, "access_denied", DriveAuthPermanentError),
    (429, "rate_limit_exceeded", DriveAuthTransientError),
    (500, "backend_error", DriveAuthTransientError),
    (503, "backend_error", DriveAuthTransientError),
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("status", "error", "expected"), CLASSIFICATION)
def test_google_answers_are_classified_as_transient_or_permanent(tmp_path, kind, status, error, expected):
    endpoint = Endpoint()
    endpoint.script = [fail(status, error)] * 10
    provider = make_provider(kind, tmp_path, endpoint, sim=SimTime())
    with pytest.raises(expected) as caught:
        provider.get_token()
    assert type(caught.value) is expected


@pytest.mark.parametrize("kind", KINDS)
def test_an_unreadable_success_answer_is_transient(tmp_path, kind):
    endpoint = Endpoint()
    endpoint.script = [lambda r: httpx.Response(200, content=b"<html>proxy page</html>")] * 3
    with pytest.raises(DriveAuthTransientError, match="not understood"):
        make_provider(kind, tmp_path, endpoint, sim=SimTime()).get_token()
    endpoint.script = [lambda r: httpx.Response(200, json={"expires_in": 3600})] * 3
    with pytest.raises(DriveAuthTransientError):
        make_provider(kind, tmp_path, endpoint, sim=SimTime()).get_token()


def test_configuration_problems_are_permanent(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(DriveAuthPermanentError):
        ServiceAccountCredentials.from_file(bad)
    broken = ServiceAccountCredentials(client_email="a@b.example", private_key_pem="not a key")
    with pytest.raises(DriveAuthPermanentError):
        TokenProvider(broken, DRIVE, httpx.Client(transport=httpx.MockTransport(Endpoint().handler))).get_token()
    client = OAuthClientConfig.from_file(write_oauth_client_file(tmp_path))
    missing = UserOAuthTokenProvider(client, OAuthTokenStore(tmp_path / "none.json"), DRIVE, httpx.Client(transport=httpx.MockTransport(Endpoint().handler)))
    with pytest.raises(DriveAuthPermanentError, match="authorization"):
        missing.get_token()
    provider, store = user_oauth(tmp_path, Endpoint())
    other = UserOAuthTokenProvider(OAuthClientConfig(client_id="another-client-9999.apps.googleusercontent.com", client_secret="x" * 10), store, DRIVE, provider._http)
    with pytest.raises(DriveAuthPermanentError, match="different OAuth client"):
        other.get_token()
    narrow = UserOAuthTokenProvider(client, store, "https://www.googleapis.com/auth/drive.metadata.readonly", provider._http)
    with pytest.raises(DriveAuthPermanentError, match="does not cover"):
        narrow.get_token()
    monkeypatch.setattr(oauth, "_IS_WINDOWS", False)  # the POSIX rule on any platform: a file others can open is refused (and needs a person)
    monkeypatch.setattr(oauth, "_permission_check", lambda path, subject, timeout: ("accessible to other users", False))
    with pytest.raises(DriveAuthPermanentError, match="other users"):
        provider.invalidate() or provider.get_token()


def test_a_permission_check_that_times_out_is_transient_not_permanent(tmp_path, monkeypatch):
    provider, store = user_oauth(tmp_path, Endpoint())
    monkeypatch.setattr(oauth, "_IS_WINDOWS", True)

    def hang(args, timeout=30.0):
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(oauth, "_run", hang)
    monkeypatch.setattr(oauth, "_windows_user", lambda timeout=30.0: hang(["whoami"], timeout))
    with pytest.raises(DriveAuthTransientError, match="could not be read"):
        provider.get_token()


# ====================================================================================================================== timeouts, retries, deadline


@pytest.mark.parametrize("kind", KINDS)
def test_a_token_endpoint_timeout_is_transient_and_leaks_nothing(tmp_path, kind):
    endpoint = Endpoint()
    endpoint.script = [timeout] * 5
    provider = make_provider(kind, tmp_path, endpoint, sim=SimTime())
    with pytest.raises(DriveAuthTransientError, match="Could not reach the Google token endpoint") as caught:
        provider.get_token()
    assert "SECRET-DETAIL" not in str(caught.value) and caught.value.__cause__ is None


def test_the_oauth_provider_retries_transient_failures_with_backoff_then_succeeds(tmp_path):
    sim, endpoint = SimTime(), Endpoint()
    endpoint.script = [fail(503), timeout]
    provider = user_oauth(tmp_path, endpoint, sim=sim)[0]
    assert provider.get_token() == "fake-token-1" and sim.sleeps == [1.0, 2.0] and endpoint.calls == 3  # unchanged retry behaviour, now inside a deadline


def test_repeated_transient_failures_end_as_one_transient_error_and_are_remembered(tmp_path):
    sim, endpoint = SimTime(), Endpoint()
    endpoint.script = [fail(503)] * 10
    provider = user_oauth(tmp_path, endpoint, sim=sim)[0]
    with pytest.raises(DriveAuthTransientError, match="backend_error"):
        provider.get_token()
    assert endpoint.calls == 3  # three attempts, then it gives up
    for _ in range(50):  # every later caller fails at once, without a request, for the failure window
        with pytest.raises(DriveAuthTransientError):
            provider.get_token()
    assert endpoint.calls == 3
    sim.advance(11)  # the window (10 s) has passed
    endpoint.script = []
    assert provider.get_token() and endpoint.calls == 4


def test_the_whole_refresh_fits_the_15_second_deadline_retry_pauses_included(tmp_path):
    sim, endpoint = SimTime(), Endpoint()
    endpoint.script = [slow(sim, 6.0, fail(503))] * 5  # every request 'takes' 6 s and then fails
    provider = user_oauth(tmp_path, endpoint, sim=sim)[0]
    started = sim.mono
    with pytest.raises(DriveAuthTransientError):
        provider.get_token()
    elapsed = sim.mono - started
    # request 1 (6 s) + pause 1 s + request 2 (6 s) = 13 s; the 2 s pause that would follow does not fit in the 2 s left, so it is not started
    assert elapsed <= MAX_TOKEN_DEADLINE_SECONDS and endpoint.calls == 2 and sim.sleeps == [1.0]


def test_nothing_is_attempted_once_the_deadline_is_spent(tmp_path):
    sim, endpoint = SimTime(), Endpoint()
    endpoint.script = [slow(sim, 20.0, timeout)] * 5  # the first request alone overruns the deadline
    provider = user_oauth(tmp_path, endpoint, sim=sim)[0]
    with pytest.raises(DriveAuthTransientError):
        provider.get_token()
    assert endpoint.calls == 1 and sim.sleeps == []  # no second request, no pause


def test_the_service_account_refresh_respects_the_deadline_too(tmp_path):
    sim, endpoint = SimTime(), Endpoint()
    endpoint.script = [slow(sim, 20.0, timeout)]
    provider = service_account(tmp_path, endpoint, sim=sim)
    with pytest.raises(DriveAuthTransientError):
        provider.get_token()
    assert endpoint.calls == 1


@pytest.mark.parametrize("kind", KINDS)
def test_every_request_gets_timeouts_that_add_up_to_less_than_the_time_left(tmp_path, kind):
    sim, endpoint = SimTime(), Endpoint()
    endpoint.script = [slow(sim, 5.0, fail(503))]
    provider = make_provider(kind, tmp_path, endpoint, sim=sim)
    try:
        provider.get_token()  # the user-OAuth provider retries and succeeds; the service-account provider makes one attempt and fails
    except DriveAuthTransientError:
        assert kind == "service_account"
    assert len(endpoint.timeouts) >= 1
    first = endpoint.timeouts[0]
    assert sum(first.values()) <= MAX_TOKEN_DEADLINE_SECONDS and all(v > 0 for v in first.values())
    assert first["read"] <= 10.0 and first["connect"] <= 5.0  # never more than the configured limits either
    if kind == "user_oauth":
        second = endpoint.timeouts[1]  # after 5 s + a 1 s pause only 9 s are left: the second request is limited accordingly
        assert sum(second.values()) <= 9.0 + 1e-6


def test_the_file_checks_are_inside_the_deadline_with_a_small_limit_each(tmp_path, monkeypatch):
    seen = []
    real = oauth._permission_check
    provider = user_oauth(tmp_path, Endpoint())[0]  # (creating the token file runs its own check; only the refresh's checks are of interest)

    def spy(path, subject, timeout):
        seen.append(timeout)
        return real(path, subject, timeout)

    monkeypatch.setattr(oauth, "_permission_check", spy)
    provider.get_token()
    assert seen and max(seen) <= 5.0 and min(seen) >= 0.5  # each helper process is limited (3 s at the full 15 s budget), not 30 s


def test_the_deadline_can_be_lowered_but_never_raised_above_15_seconds():
    with pytest.raises(ValueError):
        TokenSettings(deadline_seconds=16.0)
    with pytest.raises(ValueError):
        TokenSettings(deadline_seconds=0)
    assert TokenSettings(deadline_seconds=3.0).deadline_seconds == 3.0 and TokenSettings().deadline_seconds == 15.0


def test_the_token_settings_come_from_the_configuration_and_are_bounded(tmp_path):
    base = {"SPINCO_GDRIVE_ROOT_FOLDER_ID": "ROOTFOLDER0000001", "SPINCO_GDRIVE_CREDENTIALS_FILE": str(write_service_account_file(tmp_path))}
    default = load_config(base)
    assert (default.token_deadline_seconds, default.token_timeout_seconds, default.token_wait_seconds, default.token_failure_window_seconds) == (15.0, 10.0, 2.0, 10.0)  # 3A.2-A.1: wait 5 s -> 2 s
    tuned = load_config({**base, "SPINCO_GDRIVE_TOKEN_DEADLINE_SECONDS": "8", "SPINCO_GDRIVE_TOKEN_WAIT_SECONDS": "2"})
    assert tuned.token_deadline_seconds == 8.0 and tuned.token_wait_seconds == 2.0
    for name, bad in [("DEADLINE_SECONDS", "16"), ("DEADLINE_SECONDS", "1"), ("DEADLINE_SECONDS", "abc"), ("TIMEOUT_SECONDS", "0"), ("WAIT_SECONDS", "100"), ("FAILURE_WINDOW_SECONDS", "0")]:
        with pytest.raises(DriveConfigError):
            load_config({**base, f"SPINCO_GDRIVE_TOKEN_{name}": bad})
    provider = build_token_provider(tuned, httpx.Client(transport=httpx.MockTransport(Endpoint().handler)))
    assert provider._settings.deadline_seconds == 8.0 and provider._settings.wait_seconds == 2.0  # the runtime really applies them


# ====================================================================================================================== concurrency


@pytest.mark.parametrize("kind", KINDS)
def test_forty_threads_without_a_token_cause_one_request_and_none_waits_for_long(tmp_path, kind):
    endpoint, entered, gate = Endpoint(), threading.Event(), threading.Event()
    endpoint.script = [block(entered, gate)]  # the token endpoint "hangs" until released
    provider = make_provider(kind, tmp_path, endpoint, settings=TokenSettings(wait_seconds=0.3))
    refreshed = []
    refresher = threading.Thread(target=lambda: refreshed.append(provider.get_token()))
    refresher.start()
    assert entered.wait(timeout=10)
    durations, errors = [], []

    def caller():
        began = time.monotonic()
        try:
            provider.get_token()
        except DriveAuthTransientError as exc:
            errors.append(str(exc))
        durations.append(time.monotonic() - began)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=40) as pool:
        for future in [pool.submit(caller) for _ in range(40)]:
            future.result(timeout=15)
    elapsed = time.monotonic() - started
    # All 40 callers are already finished while the refresher is STILL stuck: no request thread is held for the length of the hang.
    assert refresher.is_alive() and len(errors) == 40 and max(durations) < 2.0 and elapsed < 5.0
    assert endpoint.calls == 1 and endpoint.max_in_flight == 1  # one request, not forty
    gate.set()
    refresher.join(timeout=10)
    assert refreshed == ["fake-token-1"] and provider.get_token() == "fake-token-1" and endpoint.calls == 1


@pytest.mark.parametrize("kind", KINDS)
def test_callers_that_wait_long_enough_all_receive_the_one_refreshed_token(tmp_path, kind):
    endpoint, entered, gate = Endpoint(), threading.Event(), threading.Event()
    endpoint.script = [block(entered, gate)]
    provider = make_provider(kind, tmp_path, endpoint, settings=TokenSettings(wait_seconds=10.0))
    results = []
    threads = [threading.Thread(target=lambda: results.append(provider.get_token())) for _ in range(25)]
    threads[0].start()
    assert entered.wait(timeout=10)
    for thread in threads[1:]:
        thread.start()
    time.sleep(0.3)
    gate.set()
    for thread in threads:
        thread.join(timeout=15)
    assert results == ["fake-token-1"] * 25 and endpoint.calls == 1


@pytest.mark.parametrize("kind", KINDS)
def test_other_threads_keep_using_a_still_valid_token_while_one_refreshes(tmp_path, kind):
    wall = SimpleNamespace(now=1_800_000_000.0)
    endpoint = Endpoint()
    provider = make_provider(kind, tmp_path, endpoint, wall=lambda: wall.now)
    old = provider.get_token()
    wall.now += 3600 - 60  # inside the soft margin (a refresh is due) but still before the hard margin (the token still works)
    entered, gate = threading.Event(), threading.Event()
    endpoint.script = [block(entered, gate)]
    refreshed = []
    refresher = threading.Thread(target=lambda: refreshed.append(provider.get_token()))
    refresher.start()
    assert entered.wait(timeout=10)
    durations = []

    def caller():
        began = time.monotonic()
        durations.append((provider.get_token(), time.monotonic() - began))

    with ThreadPoolExecutor(max_workers=30) as pool:
        for future in [pool.submit(caller) for _ in range(30)]:
            future.result(timeout=10)
    assert refresher.is_alive() and {token for token, _ in durations} == {old} and max(d for _, d in durations) < 1.0  # nobody waited for the refresh
    assert endpoint.calls == 2 and endpoint.max_in_flight == 1
    gate.set()
    refresher.join(timeout=10)
    assert refreshed == ["fake-token-2"] and provider.get_token() == "fake-token-2"


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_refresh_inside_the_margin_keeps_serving_the_old_token_until_it_really_expires(tmp_path, kind):
    sim, endpoint = SimTime(), Endpoint()
    provider = make_provider(kind, tmp_path, endpoint, sim=sim)
    old = provider.get_token()
    sim.advance(3600 - 100)  # soft margin passed, token still usable for 70 s more
    endpoint.script = [fail(503)] * 30
    assert provider.get_token() == old  # the refresh failed (one attempt series) but the caller never notices
    calls = endpoint.calls
    assert provider.get_token() == old and endpoint.calls == calls  # and the failure is remembered: no hammering
    sim.advance(75)  # now past the hard margin: the old token is no longer handed out
    with pytest.raises(DriveAuthTransientError):
        provider.get_token()


@pytest.mark.parametrize("kind", KINDS)
def test_no_valid_token_fails_promptly_and_safely(tmp_path, kind):
    sim, endpoint = SimTime(), Endpoint()
    endpoint.script = [fail(503)] * 5
    provider = make_provider(kind, tmp_path, endpoint, sim=sim)
    with pytest.raises(DriveAuthTransientError):
        provider.get_token()
    calls = endpoint.calls
    with pytest.raises(DriveAuthTransientError):  # a second caller does not even ask Google
        provider.get_token()
    assert endpoint.calls == calls
    with pytest.raises(DriveAuthTransientError):  # nor does a caller that may not wait at all
        provider.get_token(max_wait=0)
    assert endpoint.calls == calls


def test_max_wait_zero_never_waits_behind_a_refresh(tmp_path):
    endpoint, entered, gate = Endpoint(), threading.Event(), threading.Event()
    endpoint.script = [block(entered, gate)]
    provider = service_account(tmp_path, endpoint)
    refresher = threading.Thread(target=provider.get_token)
    refresher.start()
    assert entered.wait(timeout=10)
    began = time.monotonic()
    with pytest.raises(DriveAuthTransientError, match="already in progress"):
        provider.get_token(max_wait=0)
    assert time.monotonic() - began < 0.5
    gate.set()
    refresher.join(timeout=10)


def test_a_bounded_drive_read_shares_its_own_budget_with_the_token_wait(tmp_path):
    stack = make_stack(tmp_path)
    stack.tokens._settings = TokenSettings(wait_seconds=5.0)
    gate, entered = threading.Event(), threading.Event()

    def hold(request):
        if request.url.host == "oauth2.googleapis.com":
            entered.set()
            gate.wait(timeout=20)
        return False

    stack.fake.faults.append({"times": 1, "kind": "none", "match": hold})  # the token endpoint hangs for the first request only
    refresher = threading.Thread(target=stack.tokens.get_token)
    refresher.start()
    assert entered.wait(timeout=10)
    began = time.monotonic()
    with pytest.raises(DriveAuthTransientError):
        stack.client.get_raw(stack.config.root_folder_id, "id", attempts=1, budget=0.4)  # a verification-style read with a 0.4 s budget
    assert time.monotonic() - began < 2.0  # not the 5 s token wait: the read's own budget governed it
    gate.set()
    refresher.join(timeout=10)


def test_the_user_oauth_refresh_token_is_rotated_exactly_once_under_concurrent_callers(tmp_path):
    fake = FakeGoogleOAuth(rotate=True)
    fake.refresh_tokens[REFRESH] = DRIVE
    provider, store = user_oauth(tmp_path, None, transport=fake.transport(), settings=TokenSettings(wait_seconds=15.0))
    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        for future in [pool.submit(provider.get_token) for _ in range(20)]:
            results.append(future.result(timeout=20))
    assert len(fake.forms) == 1 and len(set(results)) == 1  # one refresh for twenty callers
    assert store.load().refresh_token != REFRESH and fake.refresh_issued == 1  # rotated once, and the new token was persisted
    provider.invalidate()
    assert provider.get_token() and len(fake.forms) == 2  # the persisted token works for the next refresh


# ====================================================================================================================== uploads


def test_a_transient_sign_in_failure_does_not_fail_an_upload(tmp_path, db):
    now = [time.time()]
    later = [0.0]  # the storage manager's own clock (3A.2-B: a deferred upload waits for its schedule, so the test moves time forward)
    stack = make_stack(tmp_path, clock=lambda: now[0], now=lambda: datetime.now(timezone.utc) + timedelta(seconds=later[0]))
    stack.storage.reserve_id()  # primes the id pool while Google is healthy
    stack.manager.initialise_structure()
    now[0] += 4000  # the access token has expired
    stack.fake.fail_next(10**6, 503, "backendError", match=lambda r: r.url.host == "oauth2.googleapis.com")
    content = pdf_bytes("deferred")
    with pytest.raises(StorageUploadError) as first:
        stack.manager.store(db, kind=KIND, content=content, original_filename="d.pdf")
    record = first.value.record
    assert first.value.retryable and record.state == sr.UPLOADING and record.attempts == 0 and "DriveAuthTransientError" in record.last_error
    assert stack.manager._spooled(record) == content  # the bytes are kept
    for _ in range(5):  # the same file again, and reconciliation: nothing is lost, nothing is failed, no retry budget is used
        # (3A.2-B corrections: a resubmission waits for the record's schedule like the automatic retry, so it no longer makes a Google call of its own)
        again = stack.manager.store(db, kind=KIND, content=content, original_filename="d.pdf")
        assert again.duplicate and not again.created and again.record.state == sr.UPLOADING
        stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.UPLOADING and record.attempts == 0
    deferred = db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id, StorageEvent.event_type == "UPLOAD_DEFERRED_AUTH")).scalars().all()
    assert len(deferred) == 1  # noted once, not once per pass
    assert not db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id, StorageEvent.event_type == "UPLOAD_ATTEMPT_FAILED")).scalars().all()
    assert record.deferrals == 1 and record.failure_class == "auth_deferral" and record.next_attempt_at is not None  # one deferral: the resubmissions and the reconciles all waited for the schedule
    stack.fake.faults.clear()  # Google is back
    stack.tokens.invalidate()
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.UPLOADING  # ... but the deferral's schedule has not passed yet, so reconcile leaves the record alone
    later[0] = 5000.0
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.STORED and stack.fake.file_count() == 1 and record.deferrals == 0 and record.failure_class is None


def test_a_transient_sign_in_failure_before_anything_is_recorded_is_reported_as_retryable(tmp_path, db):
    stack = make_stack(tmp_path)
    stack.fake.fail_next(10**6, 503, "backendError", match=lambda r: r.url.host == "oauth2.googleapis.com")
    with pytest.raises(StorageUploadError) as caught:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("early"), original_filename="e.pdf")
    assert caught.value.record is None and caught.value.retryable is True  # it used to say False: "do not retry"


def test_a_permanent_sign_in_failure_is_not_treated_as_transient(tmp_path, db):
    stack = make_stack(tmp_path)
    stack.fake.fail_next(10**6, 400, "invalid_grant", match=lambda r: r.url.host == "oauth2.googleapis.com")
    with pytest.raises(StorageUploadError) as caught:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("perm"), original_filename="p.pdf")
    assert caught.value.retryable is False  # unchanged: a revoked authorization needs a person (3A.2-B refines what happens to the record)


# ====================================================================================================================== secrets


def test_no_secret_reaches_logs_error_messages_or_reprs_in_any_failure_mode(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    sim, endpoint = SimTime(), Endpoint()
    provider, store = user_oauth(tmp_path, endpoint, sim=sim)
    sa = service_account(tmp_path, endpoint, sim=sim)
    issued = provider.get_token()
    messages = []
    for step in (timeout, fail(503), fail(400, "invalid_grant"), fail(401, "invalid_client"), lambda r: httpx.Response(200, content=b"{not json SECRET-BODY")):
        for p in (provider, sa):
            p.invalidate()
            endpoint.script = [step] * 4
            try:
                p.get_token()
            except DriveAuthError as exc:
                messages.append(str(exc))
                messages.append(repr(exc))
    sim.advance(100)
    secrets_to_hide = [SYNTHETIC_CLIENT_SECRET, REFRESH, issued, "SECRET-DETAIL", "SECRET-BODY", "BEGIN PRIVATE KEY"]
    surfaces = ["\n".join(messages), repr(provider), repr(sa), str(provider), caplog.text]
    for secret in secrets_to_hide:
        for surface in surfaces:
            assert secret not in surface
    assert messages and not [r for r in caplog.records if r.name.startswith("app")]  # our modules log nothing in these flows (as before)
    assert "Bearer" not in "\n".join(messages)


def test_the_client_never_puts_a_token_in_an_error_when_the_token_endpoint_fails(tmp_path):
    stack = make_stack(tmp_path)
    stack.fake.fail_next(10**6, 503, "backendError", match=lambda r: r.url.host == "oauth2.googleapis.com")
    with pytest.raises(DriveAuthTransientError) as caught:
        stack.client.get_file(stack.config.root_folder_id)
    text = str(caught.value) + repr(caught.value) + repr(stack.client) + repr(stack.tokens)
    assert "fake-token" not in text and "BEGIN" not in text and "Bearer" not in text
