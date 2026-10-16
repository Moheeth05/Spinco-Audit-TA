"""Tesseract availability must not be permanently disabled by one transient
failure.

Root cause (found when a loaded test run lost OCR for every scanned page and
every ER-39832 bill came back unmatched): TesseractOCREngine.is_available()
cached the result of its first `get_tesseract_version()` call - including a
FAILURE - for the life of the process. One transient failure of that first
probe (e.g. a subprocess spawn failing under load) therefore returned empty
OCR text for every later page, silently, until the process restarted.

These tests mock pytesseract and the clock entirely: no Tesseract install is
required and nothing here waits in real time.
"""

import threading
import time
from io import BytesIO
from unittest import mock

import pytesseract
import pytest
from PIL import Image

from app.services.extraction import ocr_engine
from app.services.extraction.ocr_engine import TesseractOCREngine


class _Probe:
    """Stands in for pytesseract.get_tesseract_version."""

    def __init__(self):
        self.calls = 0
        self.fail_next = 0
        self.always_fail = False
        self.error: Exception = OSError("simulated transient failure")
        self.real_delay = 0.0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            should_fail = self.always_fail or self.fail_next > 0
            if self.fail_next > 0:
                self.fail_next -= 1
        if self.real_delay:
            time.sleep(self.real_delay)  # real, so concurrent callers genuinely overlap
        if should_fail:
            raise self.error
        return "5.5.0"


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture()
def env(monkeypatch):
    probe, clock = _Probe(), _Clock()
    monkeypatch.setattr(pytesseract, "get_tesseract_version", probe)
    monkeypatch.setattr(ocr_engine, "_configure_tesseract", lambda: None)
    monkeypatch.setattr(ocr_engine, "_monotonic", clock.monotonic)
    monkeypatch.setattr(ocr_engine, "_sleep", clock.sleep)
    return probe, clock


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (40, 20), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _fake_image_to_data(image, output_type=None):
    return {
        "text": ["TOTAL", "100"],
        "conf": ["96", "95"],
        "left": [0, 50],
        "top": [0, 0],
        "width": [40, 30],
        "height": [10, 10],
        "block_num": [1, 1],
        "par_num": [1, 1],
        "line_num": [1, 1],
    }


EMPTY_RESULT = {"text": "", "confidence": 0.0, "words": [], "rotation_applied": 0}


# ---------- A successful check is cached ----------


def test_successful_check_is_cached_and_never_repeated(env, monkeypatch):
    probe, _ = env
    monkeypatch.setattr(pytesseract, "image_to_data", _fake_image_to_data)
    engine = TesseractOCREngine()

    assert all(engine.is_available() for _ in range(5))
    for _ in range(3):
        assert engine.extract_text(_png_bytes())["text"] == "TOTAL 100"

    assert probe.calls == 1  # not once per OCR operation


def test_availability_forced_true_is_respected_without_any_check(env):
    """An existing test (test_bills.py) pins availability this way."""
    probe, _ = env
    engine = TesseractOCREngine()
    engine._available = True
    assert engine.is_available() is True
    assert probe.calls == 0


# ---------- A failed check is not permanently cached ----------


def test_failed_check_is_retried_after_the_cooldown_not_cached_forever(env):
    probe, clock = env
    probe.always_fail = True
    engine = TesseractOCREngine()

    assert engine.is_available() is False
    first_round = probe.calls
    assert first_round == ocr_engine._AVAILABILITY_PROBE_ATTEMPTS

    # Still inside the cooldown: answered from memory, Tesseract not touched.
    clock.now += ocr_engine._AVAILABILITY_RETRY_COOLDOWN_SECONDS - 1
    assert engine.is_available() is False
    assert probe.calls == first_round

    # Tesseract recovers; once the cooldown has passed the engine notices.
    probe.always_fail = False
    clock.now += 2
    assert engine.is_available() is True
    # ...and that success is now cached.
    calls_after_recovery = probe.calls
    assert engine.is_available() is True
    assert probe.calls == calls_after_recovery


def test_transient_failure_inside_one_check_recovers_immediately(env):
    probe, clock = env
    probe.fail_next = 1
    engine = TesseractOCREngine()

    assert engine.is_available() is True
    assert probe.calls == 2
    assert clock.sleeps == [ocr_engine._AVAILABILITY_PROBE_RETRY_DELAY_SECONDS]
    # Recovered inside the same call, so there is no cooldown to wait out.
    assert engine._next_probe_at is None


# ---------- OCR resumes normally after an initial failure ----------


def test_ocr_resumes_normally_after_the_first_check_fails(env, monkeypatch):
    probe, clock = env
    monkeypatch.setattr(pytesseract, "image_to_data", _fake_image_to_data)
    probe.always_fail = True
    engine = TesseractOCREngine()

    # While Tesseract is unavailable OCR degrades to the normal empty result.
    assert engine.extract_text(_png_bytes()) == EMPTY_RESULT

    # Tesseract becomes healthy; after the cooldown the SAME engine works again.
    probe.always_fail = False
    clock.now += ocr_engine._AVAILABILITY_RETRY_COOLDOWN_SECONDS + 1
    result = engine.extract_text(_png_bytes())
    assert result["text"] == "TOTAL 100"
    assert result["confidence"] > 0.9
    assert result["rotation_applied"] == 0


# ---------- Repeated failures stay safe and bounded ----------


@pytest.mark.parametrize(
    "error",
    [OSError("spawn failed"), RuntimeError("boom"), pytesseract.TesseractNotFoundError()],
    ids=["OSError", "RuntimeError", "TesseractNotFoundError"],
)
def test_repeated_failures_are_handled_and_bounded(env, error):
    probe, clock = env
    probe.always_fail = True
    probe.error = error
    engine = TesseractOCREngine()

    # Hammering within one cooldown window: exactly one bounded round of checks.
    for _ in range(100):
        assert engine.is_available() is False
        assert engine.extract_text(_png_bytes()) == EMPTY_RESULT
    assert probe.calls == ocr_engine._AVAILABILITY_PROBE_ATTEMPTS

    # Each further cooldown allows exactly one more bounded round - never more.
    for round_number in range(2, 5):
        clock.now += ocr_engine._AVAILABILITY_RETRY_COOLDOWN_SECONDS + 1
        assert engine.is_available() is False
        assert probe.calls == ocr_engine._AVAILABILITY_PROBE_ATTEMPTS * round_number


# ---------- Concurrency ----------


def _run_concurrently(engine, n=16):
    barrier = threading.Barrier(n)
    results = []

    def worker():
        barrier.wait()
        results.append(engine.is_available())

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_callers_share_a_single_successful_check(env):
    probe, _ = env
    probe.real_delay = 0.05
    results = _run_concurrently(TesseractOCREngine())
    assert results == [True] * 16
    assert probe.calls == 1


def test_concurrent_callers_during_a_failure_do_not_multiply_checks(env):
    probe, _ = env
    probe.always_fail = True
    probe.real_delay = 0.02
    results = _run_concurrently(TesseractOCREngine())
    assert results == [False] * 16
    assert probe.calls == ocr_engine._AVAILABILITY_PROBE_ATTEMPTS  # one round, not one per thread


# ---------- Configuration is not skipped after a failed import ----------


def test_configuration_still_runs_on_retry_after_pytesseract_failed_to_import(monkeypatch):
    monkeypatch.setattr(ocr_engine, "_configured", False)
    monkeypatch.delenv("SPINCO_TESSERACT_CMD", raising=False)
    monkeypatch.delenv("SPINCO_TESSERACT_DIR", raising=False)

    with mock.patch.dict("sys.modules", {"pytesseract": None}):
        with pytest.raises(ImportError):
            ocr_engine._configure_tesseract()
    # A failed import must NOT mark configuration as done - otherwise the
    # retry would skip it and probe the wrong Tesseract forever.
    assert ocr_engine._configured is False

    ocr_engine._configure_tesseract()
    assert ocr_engine._configured is True
