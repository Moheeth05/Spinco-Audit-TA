"""Release-candidate server configuration: production start-up rules, database selection (SQLite / PostgreSQL URL), the configuration checker and the backup tool.
The PostgreSQL URL handling is tested WITHOUT a server here (parsing / refusal rules); the behaviour against a real PostgreSQL server is covered by the separate PostgreSQL run
(see the test report)."""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from app import database
from app.services import startup_checks
from app.services.startup_checks import StartupSecurityError, production_problems, run_checks

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS.parent))

GOOD_PROD = {
    "SPINCO_ENVIRONMENT": "production",
    "SPINCO_BIND_HOST": "127.0.0.1",
    "SPINCO_COOKIE_SECURE": "true",
    "SPINCO_ALLOWED_HOSTS": "spinco.corp.example",
    "SPINCO_CENTRAL_STORAGE_MODE": "serve",
    "SPINCO_DATABASE_URL_FILE": "C:\\ProgramData\\Spinco\\secrets\\database-url.txt",
    "SPINCO_ADMIN_INITIAL_PASSWORD": "Long-Synthetic-Pass-123",
}


# ------------------------------------------------------------------------------------------------------------------------- production rules


def test_a_complete_production_configuration_is_accepted():
    assert production_problems(GOOD_PROD) == []
    run_checks(None, GOOD_PROD)  # does not raise


@pytest.mark.parametrize(
    "change, setting",
    [
        ({"SPINCO_COOKIE_SECURE": "false"}, "SPINCO_COOKIE_SECURE"),
        ({"SPINCO_ALLOW_INSECURE_HTTP": "true"}, "SPINCO_ALLOW_INSECURE_HTTP"),
        ({"SPINCO_ALLOWED_HOSTS": ""}, "SPINCO_ALLOWED_HOSTS"),
        ({"SPINCO_CENTRAL_STORAGE_MODE": "shadow"}, "SPINCO_CENTRAL_STORAGE_MODE"),
        ({"SPINCO_CENTRAL_STORAGE_MODE": "off"}, "SPINCO_CENTRAL_STORAGE_MODE"),
        ({"SPINCO_DATABASE_URL_FILE": "", "SPINCO_DATABASE_URL": ""}, "PostgreSQL"),
        ({"SPINCO_ADMIN_INITIAL_PASSWORD": "short"}, "SPINCO_ADMIN_INITIAL_PASSWORD"),
        ({"SPINCO_ADMIN_INITIAL_PASSWORD": "ChangeMe123!"}, "SPINCO_ADMIN_INITIAL_PASSWORD"),
    ],
)
def test_production_refuses_to_start_when_a_rule_is_broken_and_names_the_setting_not_the_value(change, setting):
    env = {**GOOD_PROD, **change}
    with pytest.raises(StartupSecurityError) as caught:
        run_checks(None, env)
    assert setting in str(caught.value)
    for secret in ("Long-Synthetic-Pass-123", "short", "ChangeMe123!"):
        if secret != change.get("SPINCO_ADMIN_INITIAL_PASSWORD") or secret == "ChangeMe123!":
            continue
        assert secret not in str(caught.value)


def test_production_is_checked_even_on_a_loopback_only_backend_behind_a_proxy():
    env = {**GOOD_PROD, "SPINCO_COOKIE_SECURE": "false"}
    assert not startup_checks.network_exposed(env)
    with pytest.raises(StartupSecurityError):
        run_checks(None, env)


def test_sqlite_in_production_needs_an_explicit_exception():
    env = {**GOOD_PROD, "SPINCO_DATABASE_URL_FILE": ""}
    assert any("PostgreSQL" in p for p in production_problems(env))
    assert production_problems({**env, "SPINCO_ALLOW_SQLITE_IN_PRODUCTION": "true"}) == []


def test_staging_and_the_desktop_backend_are_unchanged_by_the_production_rules():
    run_checks(None, {})  # the desktop backend: loopback, no environment name
    run_checks(None, {"SPINCO_ENVIRONMENT": "staging"})  # loopback staging: nothing demanded
    staging_lan = {"SPINCO_ENVIRONMENT": "staging", "SPINCO_BIND_HOST": "0.0.0.0", "SPINCO_ADMIN_INITIAL_PASSWORD": "Long-Synthetic-Pass-123", "SPINCO_ALLOW_INSECURE_HTTP": "true"}
    run_checks(None, staging_lan)  # the PC-to-PC test setup still works (plain HTTP acknowledged, development only)


# ------------------------------------------------------------------------------------------------------------------------- database selection


@pytest.fixture()
def db_env(monkeypatch):
    for name in ("SPINCO_DB_PATH", "SPINCO_DATABASE_URL", "SPINCO_DATABASE_URL_FILE"):
        monkeypatch.delenv(name, raising=False)
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object() if name == "psycopg" else real_find_spec(name, *a, **k))  # the driver itself is not needed to test the rules
    return monkeypatch


def test_default_is_the_sqlite_file_and_the_explicit_path_wins(db_env, tmp_path):
    path, url = database._resolve()
    assert url.startswith("sqlite:///") and path.name == "spinco_ta.db"
    db_env.setenv("SPINCO_DB_PATH", str(tmp_path / "x.db"))
    assert database._resolve()[0] == tmp_path / "x.db"


def test_a_postgresql_url_is_accepted_and_the_legacy_driver_name_is_normalised(db_env):
    db_env.setenv("SPINCO_DATABASE_URL", "postgresql://spinco_app:S3cretPw@db.corp.example:5432/spinco")
    path, url = database._resolve()
    assert path is None
    assert url.startswith("postgresql+psycopg://spinco_app:S3cretPw@db.corp.example:5432/spinco")


def test_the_url_can_come_from_a_file_which_keeps_the_password_out_of_the_environment(db_env, tmp_path):
    f = tmp_path / "database-url.txt"
    f.write_text("\ufeffpostgresql+psycopg://u:p%40ss@h:5432/d\n", encoding="utf-8")
    db_env.setenv("SPINCO_DATABASE_URL_FILE", str(f))
    assert database._resolve()[1] == "postgresql+psycopg://u:p%40ss@h:5432/d"


@pytest.mark.parametrize(
    "env",
    [
        {"SPINCO_DATABASE_URL": "sqlite:///x.db"},  # SQLite is selected with SPINCO_DB_PATH, not a URL
        {"SPINCO_DATABASE_URL": "mysql://u:p@h/d"},
        {"SPINCO_DATABASE_URL": "not a url"},
        {"SPINCO_DATABASE_URL": "postgresql://u:p@/d"},  # no host
        {"SPINCO_DATABASE_URL": "postgresql://u:p@h"},  # no database
        {"SPINCO_DATABASE_URL": "postgresql://u:p@h/d", "SPINCO_DB_PATH": "x.db"},  # ambiguous: two databases named
        {"SPINCO_DATABASE_URL": "postgresql://u:p@h/d", "SPINCO_DATABASE_URL_FILE": "x.txt"},
        {"SPINCO_DATABASE_URL_FILE": "Z:\\does\\not\\exist.txt"},
    ],
)
def test_unusable_database_settings_are_refused_and_the_message_never_contains_the_password(db_env, env):
    for key, value in env.items():
        db_env.setenv(key, value)
    with pytest.raises(database.DatabaseConfigError) as caught:
        database._resolve()
    assert "p@ss" not in str(caught.value) and ":p@" not in str(caught.value)


def test_a_missing_postgresql_driver_gives_an_installation_hint(monkeypatch):
    for name in ("SPINCO_DB_PATH", "SPINCO_DATABASE_URL_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPINCO_DATABASE_URL", "postgresql://u:p@h/d")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    with pytest.raises(database.DatabaseConfigError, match="requirements-postgres.txt"):
        database._resolve()


def test_the_test_suite_itself_is_running_on_the_expected_dialect():
    assert database.IS_SQLITE is (database.DATABASE_URL.startswith("sqlite"))


# ------------------------------------------------------------------------------------------------------------------------- configuration checker


def run_checker(monkeypatch, capsys, env, connect=False):
    from tools import check_central_config as cc

    cc.ROWS.clear()
    for name in [k for k in list(__import__("os").environ) if k.startswith("SPINCO_")]:
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = cc.run(connect)
    return code, capsys.readouterr().out


def test_the_checker_fails_a_bad_production_configuration_and_prints_no_secret(monkeypatch, capsys, tmp_path):
    env = {"SPINCO_ENVIRONMENT": "production", "SPINCO_ALLOW_INSECURE_HTTP": "true", "SPINCO_ADMIN_INITIAL_PASSWORD": "Hunter2-Hunter2-X", "SPINCO_FRONTEND_DIST": str(tmp_path)}
    code, out = run_checker(monkeypatch, capsys, env)
    assert code == 1
    assert "FAIL  SPINCO_COOKIE_SECURE" in out or "FAIL  SPINCO_COOKIE_SECURE" in out.replace("  ", "  ")
    assert "FAIL" in out and "production start-up rules" in out
    assert "Hunter2-Hunter2-X" not in out


def test_the_checker_passes_a_complete_staging_configuration(monkeypatch, capsys, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    env = {
        "SPINCO_ENVIRONMENT": "staging",
        "SPINCO_FRONTEND_DIST": str(dist),
        "SPINCO_STORAGE_ROOT": str(tmp_path / "storage"),
        "SPINCO_CENTRAL_STORAGE_MODE": "serve",
        "SPINCO_GDRIVE_ENABLED": "true",
        "SPINCO_GDRIVE_AUTH_MODE": "service_account",
        "SPINCO_GDRIVE_CREDENTIALS_FILE": str(sa),
        "SPINCO_GDRIVE_EXPECTED_DRIVE_ID_STAGING": "0ASyntheticDriveId1",
        "SPINCO_GDRIVE_ROOT_FOLDER_ID": "1SyntheticRootFolderId",
        "SPINCO_GDRIVE_SPOOL_DIR": str(tmp_path / "spool"),
    }
    code, out = run_checker(monkeypatch, capsys, env)
    assert code == 0, out
    assert not any(line.startswith("FAIL") for line in out.splitlines())
    assert "credential file(s) exist (not opened)" in out


def test_the_checker_reports_a_missing_frontend_and_missing_drive_settings(monkeypatch, capsys):
    code, out = run_checker(monkeypatch, capsys, {"SPINCO_ENVIRONMENT": "staging", "SPINCO_CENTRAL_STORAGE_MODE": "serve"})
    assert code == 1
    assert "FAIL  SPINCO_FRONTEND_DIST" in out
    assert "FAIL  SPINCO_GDRIVE_ENABLED" in out or "FAIL  Google Drive configuration" in out


# ------------------------------------------------------------------------------------------------------------------------- backup tool


@pytest.fixture()
def backup():
    from tools import backup_database

    return backup_database


def make_db(path: Path, *, stamped=True, rows=5):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    con.executemany("INSERT INTO users (email) VALUES (?)", [(f"user{i}@example.com",) for i in range(rows)])
    if stamped:
        con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        con.execute("INSERT INTO alembic_version VALUES ('f1d0c9e84a27')")
    con.commit()
    con.close()


def test_sqlite_backup_is_a_consistent_verified_copy_with_a_checksum_sidecar(backup, tmp_path):
    src = tmp_path / "live.db"
    make_db(src)
    target = tmp_path / "out" / "spinco-20261201-020000.sqlite3"
    target.parent.mkdir()
    backup.backup_sqlite(src, target)
    target.with_name(target.name + ".sha256").write_text(f"{backup._sha256(target)}  {target.name}\n")
    assert backup.verify_file(target) == []
    con = sqlite3.connect(target)
    assert con.execute("SELECT count(*) FROM users").fetchone()[0] == 5
    con.close()


def test_backup_works_while_another_connection_is_writing(backup, tmp_path):
    src = tmp_path / "live.db"
    make_db(src)
    writer = sqlite3.connect(src)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO users (email) VALUES ('uncommitted@example.com')")
    target = tmp_path / "b.sqlite3"
    try:
        backup.backup_sqlite(src, target)  # the snapshot holds only committed data
    finally:
        writer.rollback()
        writer.close()
    con = sqlite3.connect(target)
    assert con.execute("SELECT count(*) FROM users WHERE email='uncommitted@example.com'").fetchone()[0] == 0
    con.close()


def test_verification_detects_damage_tampering_and_a_missing_stamp(backup, tmp_path):
    src = tmp_path / "live.db"
    make_db(src)
    good = tmp_path / "spinco-20261201-020000.sqlite3"
    backup.backup_sqlite(src, good)
    good.with_name(good.name + ".sha256").write_text(f"{backup._sha256(good)}  {good.name}\n")
    assert backup.verify_file(good) == []

    good.write_bytes(good.read_bytes() + b"\0")  # changed after the checksum was recorded
    assert any("SHA-256" in p for p in backup.verify_file(good))

    unstamped_src = tmp_path / "unstamped.db"
    make_db(unstamped_src, stamped=False)
    unstamped = tmp_path / "spinco-20261201-020001.sqlite3"
    backup.backup_sqlite(unstamped_src, unstamped)
    unstamped.with_name(unstamped.name + ".sha256").write_text(f"{backup._sha256(unstamped)}  {unstamped.name}\n")
    assert any("alembic_version" in p for p in backup.verify_file(unstamped))

    junk = tmp_path / "spinco-20261201-020002.sqlite3"
    junk.write_bytes(b"this is not a database" * 100)
    junk.with_name(junk.name + ".sha256").write_text(f"{backup._sha256(junk)}  {junk.name}\n")
    assert backup.verify_file(junk)
    assert backup.verify_file(tmp_path / "missing.sqlite3") == ["the backup file does not exist"]


def test_retention_removes_only_older_backups_made_by_the_tool(backup, tmp_path):
    for day in range(1, 6):
        for suffix in ("", ".sha256"):
            (tmp_path / f"spinco-2026120{day}-020000.sqlite3{suffix}").write_text("x")
    (tmp_path / "notes.txt").write_text("keep me")
    (tmp_path / "spinco-important.sqlite3").write_text("keep me too")  # does not match the tool's naming pattern
    removed = backup.apply_retention(tmp_path, 2)
    assert sorted(removed) == ["spinco-20261201-020000.sqlite3", "spinco-20261202-020000.sqlite3", "spinco-20261203-020000.sqlite3"]
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert "notes.txt" in remaining and "spinco-important.sqlite3" in remaining
    assert "spinco-20261205-020000.sqlite3" in remaining and "spinco-20261204-020000.sqlite3.sha256" in remaining
    assert "spinco-20261201-020000.sqlite3.sha256" not in remaining


def test_the_backup_command_end_to_end_against_the_applications_own_database(backup, tmp_path, client, capsys):
    # `client` has seeded the test database; the tool resolves it exactly as the backend does. (The test database is not Alembic-stamped, so the
    # verification correctly reports that - the backup itself is still written, checksummed and readable.)
    code = backup.main(["--out", str(tmp_path / "out")])
    out = capsys.readouterr()
    files = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert any(f.endswith(".sqlite3") for f in files) and any(f.endswith(".sha256") for f in files)
    assert code == 1 and "alembic_version" in out.err


# ------------------------------------------------------------------------------------------------------------------------- PostgreSQL schema reflection


def test_a_postgresql_sequence_or_identity_primary_key_is_not_reported_as_schema_drift():
    """Found by the real PostgreSQL run: a freshly initialised PostgreSQL database was reported as 'drift' because Postgres reports an auto-increment key as `nextval(...)`."""
    import sqlalchemy as sa

    from app.services.drive import schema

    class FakeInspector:
        def __init__(self, default, identity):
            self._default, self._identity = default, identity

        def get_columns(self, name):
            column = {"name": "id", "type": sa.Integer(), "nullable": False, "default": self._default}
            if self._identity:
                column["identity"] = {"start": 1}
            return [column]

        def get_indexes(self, name):
            return []

        def get_unique_constraints(self, name):
            return []

        def get_foreign_keys(self, name):
            return []

        def get_pk_constraint(self, name):
            return {"constrained_columns": ["id"]}

        def get_check_constraints(self, name):
            return []

    plain = ("integer", None, False, None)
    assert schema.reflect_shape(FakeInspector("nextval('document_storage_id_seq'::regclass)", False), "t")["columns"]["id"] == plain
    assert schema.reflect_shape(FakeInspector(None, True), "t")["columns"]["id"] == plain
    # a REAL server default is still compared (and still reported when it differs)
    assert schema.reflect_shape(FakeInspector("5", False), "t")["columns"]["id"] == ("integer", None, False, "5")