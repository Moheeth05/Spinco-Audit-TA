"""Regression tests for the live-spike HARNESS itself (backend/drive_spike/run_live_spike.py), added after the first live run exposed two defects:

  * run under the wrong Python (one that lacks pypdf/argon2) it produced a dozen confusing failures instead of one clear message;
  * a failure in one test surfaced as `AttributeError: 'Context' object has no attribute 'uploaded'` in the tests that build on it.

These tests contact nothing: no Drive, no network."""

import importlib.util
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

HARNESS_PATH = Path(__file__).resolve().parents[1] / "drive_spike" / "run_live_spike.py"


@pytest.fixture()
def harness():
    spec = importlib.util.spec_from_file_location("run_live_spike_under_test", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------------------------------------------------ interpreter check


def test_the_environment_check_passes_in_the_project_environment(harness):
    info = harness.check_environment()
    assert info["missing"] == [] and harness.environment_problem(info) is None
    assert {"pypdf", "argon2-cffi", "httpx", "cryptography", "fastapi", "sqlalchemy"} <= set(info["packages"])


def test_a_wrong_interpreter_is_named_with_the_fix_and_nothing_is_to_be_installed(harness, monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None if name in ("pypdf", "argon2") else real(name, *a, **k))
    info = harness.check_environment()
    assert info["missing"] == ["pypdf", "argon2-cffi"]
    problem = harness.environment_problem(info)
    assert info["interpreter"] in problem and str(harness.PROJECT_PYTHON) in problem
    assert "requirements.txt" in problem and "nothing needs installing" in problem and "Do NOT pip install" in problem


def test_the_project_venv_is_where_the_harness_expects_it(harness):
    assert harness.PROJECT_PYTHON.name == "python.exe" and harness.PROJECT_PYTHON.parent.name == "Scripts" and harness.PROJECT_PYTHON.parent.parent.name == ".venv"


# ------------------------------------------------------------------------------------------------------------ prerequisite state


def test_shared_state_always_exists_so_a_missing_attribute_is_impossible(harness, tmp_path):
    ctx = harness.Context("FAKE", tmp_path)
    assert ctx.uploaded is None and ctx.upload_content is None and ctx.archived_record_id is None and ctx.DocumentStorageRecord is None
    assert ctx.environment == {} and ctx.blocked_kind == "credentials"


@pytest.mark.parametrize("test_name", ["t03_metadata_checksum", "t04_download_sha256", "t16_drive_type_behaviour"])
def test_a_failed_upload_is_reported_as_that_failure_never_as_a_missing_attribute(harness, tmp_path, test_name):
    ctx = harness.Context("FAKE", tmp_path)

    def failing_upload(*args, **kwargs):
        raise RuntimeError("synthetic upload failure")

    # Complete stubs: Python evaluates `ctx.storage.info` / `ctx.client.get_raw` BEFORE their arguments, so a stub missing them would raise an
    # AttributeError of its own and test the wrong thing. Only `upload` fails, exactly like a real upload failure.
    ctx.storage = types.SimpleNamespace(upload=failing_upload, inspect_unchecked=lambda location: None, retrieve=lambda location: b"", open_stream=lambda *a, **k: None)
    ctx.client = types.SimpleNamespace(get_raw=lambda *a, **k: {}, get_about=lambda: {})
    ctx.folders = types.SimpleNamespace(active=lambda kind: "folder")
    ctx.root_meta = {}
    with pytest.raises(RuntimeError, match="synthetic upload failure"):  # the REAL error, not AttributeError
        getattr(harness, test_name)(ctx, harness.Result(0, test_name))
    assert ctx.uploaded is None  # and nothing was recorded as uploaded


def test_the_restore_test_does_not_assume_the_archive_test_ran(harness, tmp_path, monkeypatch):
    ctx = harness.Context("FAKE", tmp_path)

    @contextmanager
    def fake_session(_ctx):
        yield types.SimpleNamespace(get=lambda *a, **k: None)  # `db.get` is looked up before ensure_archived() is evaluated

    monkeypatch.setattr(harness, "new_db", fake_session)
    monkeypatch.setattr(harness, "_record_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic archive failure")))
    with pytest.raises(RuntimeError, match="synthetic archive failure"):
        harness.t08_restore(ctx, harness.Result(8, "restore"))
    assert ctx.archived_record_id is None


def test_the_upload_prerequisite_is_performed_once_and_reused(harness, tmp_path):
    ctx = harness.Context("FAKE", tmp_path)
    calls = []

    class Outcome:
        location = "gdrive:fakeid000000000001"

    def upload(kind, sha, name, content):
        calls.append(sha)
        return Outcome()

    ctx.storage = types.SimpleNamespace(upload=upload)
    first = harness.ensure_uploaded(ctx)
    second = harness.ensure_uploaded(ctx)
    assert first is second and len(calls) == 1 and ctx.upload_content


# ------------------------------------------------------------------------------------------------------------ safety rails


@pytest.mark.parametrize("bad", ["D:/Bills/docs/spike-output", "C:/Windows/Temp/spike-output", "D:/SpincoTA-artifacts/scratch/spike-output"])
def test_output_is_refused_anywhere_but_the_test_runs_root(harness, bad):
    with pytest.raises(SystemExit):
        harness.validate_out_dir(bad)
    assert not Path(bad).exists()  # refusing never creates the folder


def test_the_harness_has_no_delete_capability(harness):
    source = HARNESS_PATH.read_text(encoding="utf-8")
    for forbidden in ('"DELETE"', "permanent_delete", "emptyTrash", ".unlink(", "rmtree", "os.remove"):
        assert forbidden not in source, forbidden


# ------------------------------------------------------------------------------------------------------------ failure classification


def test_environment_and_quota_refusals_are_blocked_everything_else_is_a_failure(harness):
    class Refused(Exception):
        def __init__(self, reason):
            super().__init__("Drive API error 403")
            self.reason = reason

    status, detail = harness.classify_failure(ModuleNotFoundError("No module named 'pypdf'"))
    assert status == "BLOCKED" and "pypdf" in detail
    for reason in ("storageQuotaExceeded", "quotaExceeded"):
        status, detail = harness.classify_failure(Refused(reason))
        assert status == "BLOCKED" and "storage quota" in detail
    for failure in (RuntimeError("boom"), AttributeError("x"), Refused("insufficientFilePermissions"), AssertionError("mismatch")):
        assert harness.classify_failure(failure)[0] == "FAIL"  # a real defect is never softened into BLOCKED
    assert "Shared Drive" in harness.QUOTA_NOTE and "BLOCKED, not failed" in harness.QUOTA_NOTE

# ------------------------------------------------------------------------------------------------------------ write-approval guard
# `--live` writes to the real Drive, so it must refuse unless `--approve-writes` is also given - BEFORE the output folder, the isolated
# environment, the app or any network client exists. Nothing below creates a folder, changes the environment or contacts anything.


class Reached(Exception):
    """Raised by a stand-in for the first real step of main(): proves the guard let the run through (and nothing past that point ran)."""


def test_live_without_write_approval_refuses_before_anything_is_created_changed_or_contacted(harness, tmp_path, monkeypatch, capsys):
    import os

    import httpx

    def must_not_run(*args, **kwargs):
        raise AssertionError("something ran that must come AFTER the approval guard")

    for name in ("validate_out_dir", "check_environment", "build"):
        monkeypatch.setattr(harness, name, must_not_run)
    monkeypatch.setattr(httpx, "Client", must_not_run)
    environment_before = dict(os.environ)
    out = tmp_path / "never-created"
    assert harness.main(["--live", "--out-dir", str(out)]) == 5
    printed = capsys.readouterr().out
    assert "REFUSED" in printed and "--approve-writes" in printed
    assert not out.exists() and not list(tmp_path.iterdir())
    assert dict(os.environ) == environment_before  # the isolated SPINCO_* environment was not even set up


def test_the_refusal_message_names_the_flag_and_says_nothing_was_written(harness, tmp_path, capsys):
    assert harness.main(["--live", "--out-dir", str(tmp_path / "x")]) == 5
    text = capsys.readouterr().out
    assert "--approve-writes" in text and "Nothing was contacted or written" in text


def test_live_with_write_approval_passes_the_guard(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "validate_out_dir", lambda raw: (_ for _ in ()).throw(Reached()))
    with pytest.raises(Reached):  # the very next real step: control got past the guard, and only because the flag was given
        harness.main(["--live", "--approve-writes", "--out-dir", str(tmp_path / "x")])


@pytest.mark.parametrize("flag", ["--fake", "--fake-shared-drive"])
def test_the_fake_modes_never_need_the_flag(harness, tmp_path, monkeypatch, flag):
    monkeypatch.setattr(harness, "validate_out_dir", lambda raw: (_ for _ in ()).throw(Reached()))
    with pytest.raises(Reached):
        harness.main([flag, "--out-dir", str(tmp_path / "x")])


def test_the_environment_check_needs_no_flag_and_contacts_nothing(harness, monkeypatch, capsys):
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a network client was created")))
    assert harness.main(["--check-env"]) in (0, 4)


@pytest.mark.parametrize("flags", [["--fake"], ["--fake-shared-drive"], ["--check-env"]])
def test_the_approval_flag_is_rejected_outside_live_mode(harness, tmp_path, flags):
    with pytest.raises(SystemExit) as caught:
        harness.main([*flags, "--approve-writes", "--out-dir", str(tmp_path / "x")])
    assert caught.value.code == 2
    assert not (tmp_path / "x").exists()


def test_build_itself_refuses_a_live_run_that_was_not_approved(harness, tmp_path, monkeypatch):
    """Defence in depth: even a caller that bypasses main() cannot start a live run without the approval being recorded on the context."""
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a network client was created")))
    ctx = harness.Context("LIVE", tmp_path)
    assert ctx.writes_approved is False
    assert harness.build(ctx) is False
    assert ctx.blocked_kind == "approval" and "not approved" in ctx.blocked_reason and "Nothing was contacted or written" in ctx.blocked_reason
    assert not list(tmp_path.iterdir())  # not even the isolated database or the fake working folder was touched


def test_the_fake_context_is_never_marked_as_approved_for_live_writes(harness, tmp_path):
    assert harness.Context("FAKE", tmp_path).writes_approved is False and harness.Context("FAKE-SHARED", tmp_path).writes_approved is False


def test_the_guard_is_checked_before_the_output_folder_is_validated_in_main(harness):
    import inspect

    source = inspect.getsource(harness.main)
    assert source.index("REFUSED") < source.index("validate_out_dir(") < source.index("os.environ.update(")


def test_the_documented_live_command_includes_the_approval_flag(harness):
    assert "--live --approve-writes" in harness.__doc__ and "--live --approve-writes" in harness.BLOCK_HELP


# ------------------------------------------------------------------------------------------------------------ the secrets-folder path


def test_no_stale_secrets_folder_name_remains_in_the_harness_or_the_env_example():
    for path in (HARNESS_PATH, HARNESS_PATH.parents[1] / ".env.example"):
        text = path.read_text(encoding="utf-8")
        assert "SpincoTA-secrets" not in text, path.name
    assert r"D:\\SPINCO-SECRETS\\spinco-drive-test-sa.json" in HARNESS_PATH.read_text(encoding="utf-8")
    assert r"D:\SPINCO-SECRETS\spinco-drive-test-sa.json" in (HARNESS_PATH.parents[1] / ".env.example").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------------------ dependency declaration


def test_cryptography_is_declared_with_an_exact_pin_like_every_other_requirement():
    lines = [line.strip() for line in (HARNESS_PATH.parents[1] / "requirements.txt").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    pins = [line for line in lines if line.lower().startswith("cryptography")]
    assert len(pins) == 1 and pins[0].count("==") == 1, pins
    assert all("==" in line for line in lines), [line for line in lines if "==" not in line]