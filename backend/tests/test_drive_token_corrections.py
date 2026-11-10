"""Phase 3A.2-A.1 - corrections to the token code found by the independent review of 3A.2-A.

  R-1  the refresh flag could stay set forever if an exception happened between electing the refresher and committing its result;
  R-2  an unexpected (non-auth) exception escaped raw and was neither classified nor remembered, so every caller retried at once;
  R-3  with no usable token a request thread could wait 5 s: now 2 s;
  +    a burst of 401s with the same rejected token forced one refresh per thread; 408/425 were permanent; `expires_in` was not clamped.

Everything runs against mocked token endpoints / scripted refreshers. No call to Google, no real credential or token file."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.services.drive.auth import (
    INVALID_LIFETIME_SECONDS,
    MAX_TOKEN_DEADLINE_SECONDS,
    MAX_TOKEN_LIFETIME_SECONDS,
    DriveAuthError,
    DriveAuthPermanentError,
    DriveAuthTransientError,
    ServiceAccountCredentials,
    SingleFlightToken,
    TokenProvider,
    TokenSettings,
)
from app.services.drive.config import DEFAULT_TOKEN_WAIT_SECONDS

from drive_testkit import make_stack, write_service_account_file
from test_drive_token_hardening import KINDS, Endpoint, SimTime, fail, make_provider, service_account, user_oauth


class Abort(BaseException):
    """Stands in for KeyboardInterrupt / SystemExit: not an Exception, so only a `finally` can clean up after it."""


class Scripted(SingleFlightToken):
    """A refresher driven by a script: each step is a callable returning (token, lifetime) or raising; when the script is empty a fresh token is issued."""

    def __init__(self, steps=(), *, settings=None, clock=time.time, monotonic=time.monotonic):
        super().__init__(settings, clock, monotonic)
        self.steps = list(steps)
        self.calls = 0
        self._count = threading.Lock()

    def _refresh(self, budget):
        with self._count:
            self.calls += 1
            number = self.calls
            step = self.steps.pop(0) if self.steps else None
        return step() if step else (f"tok-{number}", 3600.0)


def raising(exc):
    def step():
        raise exc

    return step


def returning(token, lifetime):
    return lambda: (token, lifetime)


def blocking(entered, gate, then=None):
    def step():
        entered.set()
        assert gate.wait(timeout=20), "never released"
        return then() if then else ("after-block", 3600.0)

    return step


def run_threads(n, target, timeout=20):
    results, threads = [], []

    def wrapped(i):
        try:
            results.append(("ok", target(i)))
        except BaseException as exc:  # noqa: BLE001
            results.append((type(exc).__name__, str(exc)))

    for i in range(n):
        threads.append(threading.Thread(target=wrapped, args=(i,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    assert not any(t.is_alive() for t in threads), "a thread is stranded"
    return results


# ====================================================================================================================== R-3: the 2 second wait


def test_the_default_wait_without_a_usable_token_is_two_seconds_everywhere():
    assert TokenSettings().wait_seconds == 2.0 and DEFAULT_TOKEN_WAIT_SECONDS == 2.0


def test_threads_without_a_token_wait_about_two_seconds_not_five():
    entered, gate = threading.Event(), threading.Event()
    p = Scripted([blocking(entered, gate)], settings=TokenSettings())  # the DEFAULT settings
    refresher = threading.Thread(target=p.get_token)
    refresher.start()
    assert entered.wait(10)
    began = time.monotonic()
    results = run_threads(6, lambda i: p.get_token())
    elapsed = time.monotonic() - began
    assert all(kind == "DriveAuthTransientError" for kind, _ in results) and 1.7 <= elapsed < 3.5  # not 5 s
    gate.set()
    refresher.join(10)


def test_the_overall_refresh_deadline_is_still_at_most_fifteen_seconds():
    assert MAX_TOKEN_DEADLINE_SECONDS == 15.0 and TokenSettings().deadline_seconds == 15.0
    with pytest.raises(ValueError):
        TokenSettings(deadline_seconds=15.01)


# ====================================================================================================================== R-1: exception safety


@pytest.mark.parametrize(
    "bad",
    [("tok", None), ("tok", "abc"), ("tok", True), ("tok", object()), (None, 3600.0), ("", 3600.0), ("   ", 3600.0), (12345, 3600.0), (b"bytes", 3600.0)],
    ids=["no lifetime", "text lifetime", "bool lifetime", "object lifetime", "no token", "empty token", "blank token", "numeric token", "bytes token"],
)
def test_a_malformed_refresh_result_is_a_transient_failure_not_a_wedge(bad):
    sim = SimTime()
    p = Scripted([returning(*bad)], settings=TokenSettings(wait_seconds=0.3), clock=lambda: sim.wall, monotonic=lambda: sim.mono)
    with pytest.raises(DriveAuthTransientError, match="unexpectedly"):
        p.get_token()
    assert p._refreshing is False  # the flag is clear (it used to stay set forever: every later caller then failed after the wait)
    sim.advance(11)  # the remembered failure expires
    assert p.get_token() == "tok-2" and p.calls == 2  # and the provider simply works again


def test_a_refresher_that_raises_before_the_result_stage_releases_everything():
    sim = SimTime()
    p = Scripted([raising(RuntimeError("boom SECRET-DETAIL"))], clock=lambda: sim.wall, monotonic=lambda: sim.mono)
    with pytest.raises(DriveAuthTransientError) as caught:
        p.get_token()
    assert p._refreshing is False and "SECRET-DETAIL" not in str(caught.value) + repr(caught.value) and caught.value.__cause__ is None
    sim.advance(11)
    assert p.get_token() == "tok-2"


def test_an_exception_while_committing_the_result_cannot_leave_the_flag_set():
    sim = SimTime()
    p = Scripted(clock=lambda: sim.wall, monotonic=lambda: sim.mono)
    real_commit = p._commit

    def exploding_commit(access, expires_at):
        raise RuntimeError("commit failed")

    p._commit = exploding_commit
    with pytest.raises(RuntimeError):  # raised AFTER the result was accepted: it is a bug of the commit itself, and it reaches the caller ...
        p.get_token()
    assert p._refreshing is False  # ... but the flag is released by the `finally`, so nobody is stranded
    p._commit = real_commit
    assert p.get_token() == "tok-2"


class RaisingNotify(threading.Condition):
    def notify_all(self):
        raise RuntimeError("wake-up failed")


def test_a_failing_wake_up_after_the_commit_does_not_fail_the_refresh_or_strand_a_waiter():
    p = Scripted(settings=TokenSettings(wait_seconds=1.0))
    p._cond = RaisingNotify(threading.Lock())
    entered, gate = threading.Event(), threading.Event()
    p.steps = [blocking(entered, gate)]
    out = {}
    refresher = threading.Thread(target=lambda: out.__setitem__("r", p.get_token()))
    refresher.start()
    assert entered.wait(10)
    waiters = []
    began = time.monotonic()
    waiter_threads = [threading.Thread(target=lambda: waiters.append(p.get_token())) for _ in range(4)]
    for t in waiter_threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    refresher.join(10)
    for t in waiter_threads:
        t.join(10)
    assert out["r"] == "after-block" and waiters == ["after-block"] * 4  # nobody was told, everybody found the token when their wait ended
    assert time.monotonic() - began < 3.0 and p._refreshing is False


@pytest.mark.parametrize("stage", ["the clock at election", "the clock inside the refresh", "recording the failure"])
def test_a_failing_clock_or_failure_recorder_never_leaves_the_flag_set(stage):
    sim = SimTime()
    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        if stage == "the clock at election" and calls["n"] == 1:
            raise RuntimeError("clock failed")
        if stage == "the clock inside the refresh" and calls["n"] == 2:
            raise RuntimeError("clock failed")
        return sim.wall

    p = Scripted(clock=clock, monotonic=lambda: sim.mono)
    if stage == "recording the failure":
        p.steps = [raising(RuntimeError("x"))]
        real = p._record_failure

        def flaky(exc):
            p._record_failure = real
            raise RuntimeError("recorder failed")

        p._record_failure = flaky
    with pytest.raises(Exception):  # whichever error it is, the caller gets one ...
        p.get_token()
    assert p._refreshing is False  # ... and the provider is not wedged
    sim.advance(11)
    assert p.get_token().startswith("tok-")


def test_an_abort_that_is_not_an_exception_still_releases_the_flag_and_wakes_the_waiters():
    entered, gate = threading.Event(), threading.Event()
    p = Scripted([blocking(entered, gate, then=raising(Abort()))], settings=TokenSettings(wait_seconds=5.0))
    results = []
    refresher = threading.Thread(target=lambda: results.append(_catch(p.get_token)))
    refresher.start()
    assert entered.wait(10)
    waiter_results = []
    waiters = [threading.Thread(target=lambda: waiter_results.append(_catch(p.get_token))) for _ in range(5)]
    for t in waiters:
        t.start()
    time.sleep(0.2)
    began = time.monotonic()
    gate.set()
    refresher.join(10)
    for t in waiters:
        t.join(10)
    assert results == ["Abort"]  # the abort reached the thread that was running the refresh ...
    assert all(r.startswith("tok-") or r == "after-block" for r in waiter_results) and len(waiter_results) == 5  # ... and none of the waiters was stranded
    assert time.monotonic() - began < 3.0 and p._refreshing is False


def _catch(fn):
    try:
        return fn()
    except BaseException as exc:  # noqa: BLE001
        return type(exc).__name__


# ====================================================================================================================== R-2: unexpected exceptions


def test_an_unexpected_exception_is_classified_transient_and_remembered_so_callers_do_not_retry_at_once():
    sim = SimTime()
    p = Scripted([raising(RuntimeError("unexpected internal failure"))] * 5, clock=lambda: sim.wall, monotonic=lambda: sim.mono)
    with pytest.raises(DriveAuthTransientError):
        p.get_token()
    for _ in range(60):  # the next sixty callers fail at once, without asking the refresher again
        with pytest.raises(DriveAuthTransientError):
            p.get_token()
    assert p.calls == 1
    sim.advance(11)
    with pytest.raises(DriveAuthTransientError):  # after the window ONE caller asks again
        p.get_token()
    assert p.calls == 2


def test_forty_concurrent_callers_cause_one_attempt_even_when_it_ends_in_an_unexpected_exception():
    entered, gate = threading.Event(), threading.Event()
    p = Scripted([blocking(entered, gate, then=raising(RuntimeError("late failure")))], settings=TokenSettings(wait_seconds=5.0))
    refresher = threading.Thread(target=lambda: _catch(p.get_token))
    refresher.start()
    assert entered.wait(10)
    outcomes = []
    threads = [threading.Thread(target=lambda: outcomes.append(_catch(p.get_token))) for _ in range(40)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    released = time.monotonic()
    gate.set()
    refresher.join(10)
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)
    assert outcomes == ["DriveAuthTransientError"] * 40 and p.calls == 1  # the waiters were woken by the failure and shared it: nobody tried again
    assert time.monotonic() - released < 2.0  # woken by the failure, not left to run out their 5 s wait


def test_an_unexpected_exception_does_not_take_a_still_usable_token_away():
    wall = [1_800_000_000.0]
    p = Scripted([returning("old", 3600.0), raising(RuntimeError("x"))], clock=lambda: wall[0])
    assert p.get_token() == "old"
    wall[0] += 3600 - 60  # inside the refresh margin, still usable
    assert p.get_token() == "old" and p.calls == 2


@pytest.mark.parametrize("kind", KINDS)
def test_a_closed_http_client_or_an_exploding_transport_is_a_transient_sign_in_failure(tmp_path, kind):
    sim, endpoint = SimTime(), Endpoint()

    def explode(request):
        raise RuntimeError("transport exploded SECRET-DETAIL")

    endpoint.script = [explode] * 20
    provider = make_provider(kind, tmp_path, endpoint, sim=sim)
    with pytest.raises(DriveAuthTransientError) as caught:
        provider.get_token()
    assert "SECRET-DETAIL" not in str(caught.value) and not isinstance(caught.value, DriveAuthPermanentError)
    calls = endpoint.calls
    with pytest.raises(DriveAuthTransientError):
        provider.get_token()
    assert endpoint.calls == calls  # remembered
    closed = httpx.Client(transport=httpx.MockTransport(Endpoint().handler))
    closed.close()
    broken = service_account(tmp_path, Endpoint(), sim=sim)
    broken._http = closed
    with pytest.raises(DriveAuthTransientError):
        broken.get_token()


def test_no_secret_reaches_logs_or_messages_when_an_unexpected_exception_happens(caplog):
    caplog.set_level(logging.DEBUG)
    p = Scripted([raising(RuntimeError("Bearer ya29.SECRET-ACCESS refresh SECRET-REFRESH client_secret SECRET-CLIENT"))])
    with pytest.raises(DriveAuthTransientError) as caught:
        p.get_token()
    text = str(caught.value) + repr(caught.value) + repr(p) + caplog.text
    assert "SECRET" not in text and "ya29" not in text and not [r for r in caplog.records if r.name.startswith("app")]


# ====================================================================================================================== no hammering


def test_a_burst_of_invalidations_with_the_same_rejected_token_causes_one_refresh(tmp_path):
    endpoint = Endpoint()
    provider = service_account(tmp_path, endpoint)
    stale = provider.get_token()
    assert endpoint.calls == 1

    def rejected_with_the_stale_token(i):
        provider.invalidate(stale)
        return provider.get_token()

    results = run_threads(40, rejected_with_the_stale_token)
    assert len({value for _, value in results}) == 1 and all(kind == "ok" for kind, _ in results)
    assert endpoint.calls == 2 and endpoint.max_in_flight == 1  # ONE new token for forty rejections


def test_invalidate_with_a_token_is_compare_and_drop_and_without_one_is_unconditional(tmp_path):
    endpoint = Endpoint()
    provider = service_account(tmp_path, endpoint)
    current = provider.get_token()
    provider.invalidate("some-other-token")  # not the cached one: nothing happens
    assert provider.get_token() == current and endpoint.calls == 1
    provider.invalidate(current)
    assert provider.get_token() != current and endpoint.calls == 2
    provider.invalidate()  # the old, unconditional form still works
    assert endpoint.calls == 2 and provider.get_token() and endpoint.calls == 3


def test_a_401_burst_through_the_drive_client_does_not_hammer_the_token_endpoint(tmp_path):
    """40 requests are all rejected (401) for carrying the same stale token. Their rejections are delivered one after the other over ~200 ms while a
    refresh takes ~50 ms, so most of them arrive AFTER the new token exists. Each rejected request invalidates 'the token it was rejected with': only the
    first can still match the cached token, so there is exactly ONE refresh (an unconditional invalidate() would drop the good new token again and again)."""
    stack = make_stack(tmp_path, rate=10000.0)
    root = stack.config.root_folder_id
    old = stack.tokens.get_token()
    warm = stack.fake.token_requests
    barrier, arrivals = threading.Barrier(40), iter(range(10**6))

    def slow_token_endpoint(request):
        if request.url.host == "oauth2.googleapis.com":
            time.sleep(0.05)
        return False

    def reject_the_stale_token(request):
        if request.url.host == "www.googleapis.com" and request.headers.get("authorization") == f"Bearer {old}":
            index = next(arrivals)
            barrier.wait(timeout=20)  # every request has been sent with the stale token ...
            time.sleep(index * 0.005)  # ... and the rejections come back one after the other
            return True
        return False

    stack.fake.faults.append({"times": 10**6, "kind": "none", "match": slow_token_endpoint})
    stack.fake.fail_next(10**6, 401, "authError", match=reject_the_stale_token)
    results = run_threads(40, lambda i: stack.client.get_file(root).id, timeout=60)
    assert all(kind == "ok" and value == root for kind, value in results), results
    assert stack.fake.token_requests - warm == 1  # ONE refresh for forty rejections


# ====================================================================================================================== 408 / 425, expires_in


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_gateway_and_rate_limit_statuses_are_transient(tmp_path, kind, status):
    endpoint = Endpoint()
    endpoint.script = [fail(status, "some_error")] * 10
    with pytest.raises(DriveAuthTransientError) as caught:
        make_provider(kind, tmp_path, endpoint, sim=SimTime()).get_token()
    assert type(caught.value) is DriveAuthTransientError


@pytest.mark.parametrize("status", [400, 401, 403, 404, 407, 409])
@pytest.mark.parametrize("kind", KINDS)
def test_other_client_errors_stay_permanent(tmp_path, kind, status):
    endpoint = Endpoint()
    endpoint.script = [fail(status, "some_error")] * 10
    with pytest.raises(DriveAuthPermanentError):
        make_provider(kind, tmp_path, endpoint, sim=SimTime()).get_token()


def test_the_oauth_provider_retries_a_408_like_any_other_transient_answer(tmp_path):
    sim, endpoint = SimTime(), Endpoint()
    endpoint.script = [fail(408, "request_timeout"), fail(425, "too_early")]
    provider = user_oauth(tmp_path, endpoint, sim=sim)[0]
    assert provider.get_token() and sim.sleeps == [1.0, 2.0] and endpoint.calls == 3


@pytest.mark.parametrize(
    ("body", "seconds"),
    [
        (b'{"access_token":"x","expires_in":3600}', 3600.0),
        (b'{"access_token":"x","expires_in":"3600"}', 3600.0),
        (b'{"access_token":"x"}', 3600.0),
        (b'{"access_token":"x","expires_in":1e12}', MAX_TOKEN_LIFETIME_SECONDS),
        (b'{"access_token":"x","expires_in":86401}', MAX_TOKEN_LIFETIME_SECONDS),
        (b'{"access_token":"x","expires_in":Infinity}', INVALID_LIFETIME_SECONDS),
        (b'{"access_token":"x","expires_in":NaN}', INVALID_LIFETIME_SECONDS),
        (b'{"access_token":"x","expires_in":-5}', INVALID_LIFETIME_SECONDS),
        (b'{"access_token":"x","expires_in":0}', INVALID_LIFETIME_SECONDS),
        (b'{"access_token":"x","expires_in":45}', 45.0),
    ],
)
def test_an_unreasonable_expires_in_is_clamped_safely(tmp_path, body, seconds):
    wall = [1_800_000_000.0]
    provider = TokenProvider(
        ServiceAccountCredentials.from_file(write_service_account_file(tmp_path)),
        "https://www.googleapis.com/auth/drive",
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))),
        clock=lambda: wall[0],
    )
    assert provider.get_token() == "x"
    assert provider._expires_at - wall[0] == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("lifetime", "expected"),
    [(3600, 3600.0), ("90", 90.0), (1e30, MAX_TOKEN_LIFETIME_SECONDS), (float("inf"), INVALID_LIFETIME_SECONDS), (float("nan"), INVALID_LIFETIME_SECONDS), (-1, INVALID_LIFETIME_SECONDS), (0, INVALID_LIFETIME_SECONDS)],
)
def test_the_clamp_itself(lifetime, expected):
    assert SingleFlightToken._checked_result("tok", lifetime) == expected
    for bad in (None, True, "abc", [1]):
        with pytest.raises((ValueError, TypeError)):
            SingleFlightToken._checked_result("tok", bad)


def test_a_clamped_lifetime_cannot_make_every_call_refresh_or_a_token_live_forever():
    wall = [1_800_000_000.0]
    p = Scripted([returning("a", float("nan")), returning("b", 1e12)], clock=lambda: wall[0])
    assert p.get_token() == "a" and p.get_token() == "a" and p.calls == 1  # 5 minutes, not 0: no refresh per call
    wall[0] += INVALID_LIFETIME_SECONDS  # five minutes later it IS refreshed
    assert p.get_token() == "b" and p.calls == 2
    wall[0] += 25 * 3600  # a day later the "1e12 seconds" token is refreshed too (it was cut to 24 h)
    assert p.get_token().startswith("tok-")


# ====================================================================================================================== the fast path is untouched


def test_a_valid_token_is_served_without_ever_touching_the_refresher():
    p = Scripted()
    assert p.get_token() == "tok-1"
    began = time.monotonic()
    assert {p.get_token() for _ in range(20000)} == {"tok-1"}
    assert p.calls == 1 and time.monotonic() - began < 2.0


def test_threads_keep_using_a_usable_token_while_one_refreshes_and_none_is_stranded():
    wall = [1_800_000_000.0]
    entered, gate = threading.Event(), threading.Event()
    p = Scripted([returning("old", 3600.0), blocking(entered, gate, then=raising(RuntimeError("fails after the others returned")))], clock=lambda: wall[0])
    assert p.get_token() == "old"
    wall[0] += 3600 - 60
    refresher = threading.Thread(target=lambda: _catch(p.get_token))
    refresher.start()
    assert entered.wait(10)
    began = time.monotonic()
    results = run_threads(30, lambda i: p.get_token())
    assert [v for _, v in results] == ["old"] * 30 and time.monotonic() - began < 1.0
    gate.set()
    refresher.join(10)
    assert p.get_token() == "old"  # the failed refresh did not take the usable token away


@pytest.mark.parametrize("kind", KINDS)
def test_the_real_providers_still_refresh_once_for_many_callers(tmp_path, kind):
    endpoint = Endpoint()
    provider = make_provider(kind, tmp_path, endpoint, settings=TokenSettings(wait_seconds=10.0))
    with ThreadPoolExecutor(max_workers=30) as pool:
        values = {f.result(timeout=30) for f in [pool.submit(provider.get_token) for _ in range(30)]}
    assert len(values) == 1 and endpoint.calls == 1


def test_every_failure_class_is_still_a_drive_auth_error():
    assert issubclass(DriveAuthTransientError, DriveAuthError) and issubclass(DriveAuthPermanentError, DriveAuthError)
