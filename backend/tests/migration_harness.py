"""Helpers for the schema-migration tests (Phase 3A.2-B). EVERY database touched through this module is a DISPOSABLE SQLite file under one approved folder.

Safety rules enforced in code (each has a test):
  * a database path outside the approved disposable root - or inside the repository, or the developer database - is refused BEFORE any process starts;
  * SPINCO_DB_PATH is set explicitly for every Alembic / Python subprocess, so Alembic can never fall back to its default (the developer database);
  * the migration is only ever run against a database whose Alembic revision is EXACTLY 8268b29c4fb1 (and only ever downgraded from c3a9f0b17d42);
  * a backup is only trusted after it has been verified (integrity, row counts, content fingerprint, read-only open) and a restore rehearsal worked.
Nothing here reads credentials, calls the network, or touches a real database.
"""

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

from drive_legacy_schema import LEGACY_DRIVE_DDL

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DISPOSABLE_ROOT = Path(r"D:\SpincoTA-artifacts\phase-3A\disposable-db-tests")
ROOT_ENV = "SPINCO_DISPOSABLE_DB_ROOT"  # only for running the suite on another machine; the default is the folder approved for this work
BASE_REVISION = "8268b29c4fb1"
NEW_REVISION = "c3a9f0b17d42"
DRIVE_TABLES = ("document_storage", "storage_events", "drive_sync_state")


class _Db:
    """A SQLite connection that is COMMITTED and CLOSED when the `with` block ends (sqlite3's own context manager only commits, which leaves the file
    locked on Windows until garbage collection)."""

    def __init__(self, *args, **kwargs):
        self.con = sqlite3.connect(*args, **kwargs)

    def __enter__(self):
        return self.con

    def __exit__(self, kind, value, trace):
        try:
            self.con.commit() if kind is None else self.con.rollback()
        finally:
            self.con.close()
        return False


def connect(*args, **kwargs) -> _Db:
    return _Db(*args, **kwargs)


class UnsafeDatabasePath(Exception):
    """A database path that is not a disposable test database. Raised before anything is opened or run."""


class PreconditionFailed(Exception):
    """The database is not in the state the operation requires (for example: its Alembic revision is not the required one)."""


def disposable_root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_DISPOSABLE_ROOT)


def root_available() -> bool:
    root = disposable_root()
    return root.exists() or (bool(root.anchor) and Path(root.anchor).exists())


def _check_root(root: Path) -> None:
    """The approved folder itself must be sane: not a drive root, not the repository, not inside it and not an ancestor of it (an override such as D:\\
    would otherwise make the whole disk 'disposable')."""
    repo = BACKEND_DIR.resolve().parent
    if root == Path(root.anchor) or repo.is_relative_to(root) or root.is_relative_to(repo):
        raise UnsafeDatabasePath("the disposable-database root must not be a drive root, the repository, inside it, or a folder that contains it")


def assert_disposable(path) -> Path:
    raw = Path(path)
    if ":" in raw.name:  # an NTFS alternate data stream (name.db:stream) is another way to name a file
        raise UnsafeDatabasePath(f"{raw.name}: not a plain file name")
    resolved = raw.resolve()
    root = disposable_root().resolve()
    _check_root(root)
    if not resolved.is_relative_to(root):
        raise UnsafeDatabasePath(f"{resolved.name}: not under the approved disposable-database folder")
    if resolved.is_relative_to(BACKEND_DIR.resolve()) or resolved.name.lower().rstrip(". ") == "spinco_ta.db":
        raise UnsafeDatabasePath(f"{resolved.name}: the repository and the application database file name are never disposable (any case, any trailing dot)")
    try:
        if resolved.is_file() and os.stat(resolved).st_nlink > 1:  # a hard link inside the folder can lead to a file outside it
            raise UnsafeDatabasePath(f"{resolved.name}: has more than one hard link")
    except OSError:
        raise UnsafeDatabasePath(f"{resolved.name}: could not be examined") from None
    return resolved


def new_run_dir(prefix: str = "pytest") -> Path:
    root = disposable_root()
    _check_root(root.resolve())
    root.mkdir(parents=True, exist_ok=True)
    run = root / f"{prefix}-{uuid.uuid4().hex[:10]}"
    run.mkdir()
    return run


def remove_run_dir(run: Path) -> None:
    """Deletes a folder THIS module created (same root, `pytest-` / `template-` prefix); refuses anything else."""
    resolved = Path(run).resolve()
    if resolved.parent != disposable_root().resolve() or not resolved.name.startswith(("pytest-", "template-")):
        raise UnsafeDatabasePath(f"refusing to delete {resolved.name}")
    shutil.rmtree(resolved, ignore_errors=True)


def _env(db: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("SPINCO_DB_PATH", "DATABASE_URL", "SPINCO_DATABASE_URL", "SPINCO_DATABASE_URL_FILE", "SPINCO_TEST_DATABASE_URL_FILE")}  # a SQLite migration test must never inherit a PostgreSQL URL
    env["SPINCO_DB_PATH"] = str(db)  # ALWAYS explicit
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def alembic(db, *args) -> subprocess.CompletedProcess:
    path = assert_disposable(db)
    return subprocess.run([sys.executable, "-B", "-m", "alembic", *args], cwd=BACKEND_DIR, env=_env(path), capture_output=True, text=True, timeout=300)


def python(db, source: str) -> subprocess.CompletedProcess:
    path = assert_disposable(db)
    return subprocess.run([sys.executable, "-B", "-c", source], cwd=BACKEND_DIR, env=_env(path), capture_output=True, text=True, timeout=300)


def spawn_python(db, source: str, *args: str) -> subprocess.Popen:
    """Starts (does not wait for) a Python subprocess bound to a disposable database: for tests that race several OS processes."""
    path = assert_disposable(db)
    return subprocess.Popen([sys.executable, "-B", "-c", source, *args], cwd=BACKEND_DIR, env=_env(path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def output(result: subprocess.CompletedProcess) -> str:
    return "\n".join(x for x in (result.stdout.strip(), result.stderr.strip()) if x)


def current_revision(db) -> str | None:
    path = assert_disposable(db)
    with connect(str(path)) as con:
        try:
            rows = [r[0] for r in con.execute("select version_num from alembic_version")]
        except sqlite3.OperationalError:
            return None
    return rows[0] if len(rows) == 1 else (None if not rows else ",".join(rows))


def require_revision(db, revision: str) -> None:
    found = current_revision(db)
    if found != revision:
        raise PreconditionFailed(f"Alembic revision is {found!r}; it must be exactly {revision!r} before this operation")


def upgrade_to_new(db) -> subprocess.CompletedProcess:
    """The ONLY way these tests apply the migration: refuses any database that is not at exactly 8268b29c4fb1."""
    require_revision(db, BASE_REVISION)
    return alembic(db, "upgrade", NEW_REVISION)


def downgrade_to_base(db) -> subprocess.CompletedProcess:
    require_revision(db, NEW_REVISION)
    return alembic(db, "downgrade", BASE_REVISION)


def build_base_database(db) -> None:
    """A fresh database built ONLY by the existing migrations, at exactly 8268b29c4fb1 (18 tables, no Drive tables)."""
    result = alembic(db, "upgrade", BASE_REVISION)
    assert result.returncode == 0, output(result)
    require_revision(db, BASE_REVISION)


def add_legacy_drive_tables(db) -> None:
    """The three Drive tables in the shape every existing installation has (the frozen pre-3A.2-B DDL)."""
    with connect(str(assert_disposable(db))) as con:
        con.executescript(LEGACY_DRIVE_DDL)


def seed_legacy_rows(db) -> None:
    """Synthetic rows only: five storage records in every state, four events, one change-token row."""
    rows = [
        ("expense_report", "a" * 64, "one.pdf", 100, "1" * 32, "gdrive:FILEID0000000001", "STORED", None, 0),
        ("expense_report", "b" * 64, "two.pdf", 200, "2" * 32, "gdrive:FILEID0000000002", "UPLOADING", "DriveTransientError: Drive API error 503", 3),
        ("trip_report", "c" * 64, "three.pdf", 300, "3" * 32, "gdrive:FILEID0000000003", "FAILED", "Upload abandoned after 10 failed attempts (limit 10)", 10),
        ("expense_report", "d" * 64, "four.pdf", 400, "4" * 32, "gdrive:FILEID0000000004", "ARCHIVED", None, 1),
        ("excel", "e" * 64, "five.xlsx", 500, "5" * 32, "gdrive:FILEID0000000005", "MISSING_EXTERNAL", None, 0),
    ]
    with connect(str(assert_disposable(db))) as con:
        for kind, sha, name, size, md5, location, state, error, attempts in rows:
            con.execute(
                "insert into document_storage (kind, sha256, original_filename, size, md5, location, state, last_error, attempts, created_at, updated_at) "
                "values (?,?,?,?,?,?,?,?,?, '2026-11-01 10:00:00', '2026-11-01 10:00:00')",
                (kind, sha, name, size, md5, location, state, error, attempts),
            )
        for record_id in range(1, 5):
            con.execute("insert into storage_events (record_id, event_type, detail, created_at) values (?, 'STORED', 'synthetic', '2026-11-01 10:00:00')", (record_id,))
        con.execute("insert into drive_sync_state (name, page_token, updated_at) values ('changes:abcdef0123456789', '42~driveid', '2026-11-01 10:00:00')")


def create_all_with_current_models(db) -> subprocess.CompletedProcess:
    """What the application does at start-up (Base.metadata.create_all), against a disposable database."""
    return python(db, "from app.database import Base, engine\nimport app.models\nBase.metadata.create_all(engine)\nprint('create_all done')")


# ------------------------------------------------------------------------------------------------------------------------------ inspection


def columns(db, table: str) -> list[tuple]:
    with connect(str(assert_disposable(db))) as con:
        return [(r[1], r[2], bool(r[3]), r[4]) for r in con.execute(f"pragma table_info({table})")]  # name, type, notnull, default


def index_names(db, table: str) -> set[str]:
    with connect(str(assert_disposable(db))) as con:
        return {r[1] for r in con.execute(f"pragma index_list({table})")}


def unique_column_sets(db, table: str) -> set[tuple]:
    """The column tuples that carry a UNIQUE guarantee (SQLite lists a named UNIQUE constraint under an automatic index name)."""
    with connect(str(assert_disposable(db))) as con:
        names = [r[1] for r in con.execute(f"pragma index_list({table})") if r[2]]
        return {tuple(c[2] for c in con.execute(f"pragma index_info({name})")) for name in names}


def count(db, table: str) -> int:
    with connect(str(assert_disposable(db))) as con:
        return con.execute(f"select count(*) from {table}").fetchone()[0]


def table_names(db) -> set[str]:
    with connect(str(assert_disposable(db))) as con:
        return {r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")}


def schema_text(db, tables: tuple[str, ...] | None = None) -> str:
    """Every CREATE statement in the database (or only those of `tables`), sorted: two databases with equal text have the same schema."""
    with connect(str(assert_disposable(db))) as con:
        rows = [(r[0], r[1]) for r in con.execute("select sql, tbl_name from sqlite_master where sql is not null")]
    return "\n".join(sorted(sql for sql, table in rows if tables is None or table in tables))


def integrity(db) -> tuple[str, list]:
    with connect(str(assert_disposable(db))) as con:
        return con.execute("pragma integrity_check").fetchone()[0], con.execute("pragma foreign_key_check").fetchall()


def fingerprint(db, only_columns: dict[str, tuple] | None = None) -> str:
    """SHA-256 over every row of the given tables (default: the three Drive tables, all columns), in a fixed order. With `only_columns` the
    fingerprint covers just those columns, so the OLD columns of a migrated table can be compared with their pre-migration values."""
    path = assert_disposable(db)
    digest = hashlib.sha256()
    with connect(f"file:{path.as_posix()}?mode=ro", uri=True) as con:
        for table in DRIVE_TABLES:
            available = [r[1] for r in con.execute(f"pragma table_info({table})")]
            wanted = [c for c in available if only_columns is None or c in only_columns.get(table, ())]
            digest.update(f"{table}|{','.join(wanted)}\n".encode())
            for row in con.execute(f"select {','.join(wanted)} from {table} order by rowid"):
                digest.update(repr(row).encode() + b"\n")
    return digest.hexdigest()


# ------------------------------------------------------------------------------------------------------------------- backup, verify, restore


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def backup(source, target) -> Path:
    """A consistent copy through SQLite's online-backup API (safe even if the source is in use). Both paths must be disposable."""
    src, dst = assert_disposable(source), assert_disposable(target)
    if dst.exists():
        raise PreconditionFailed("the backup target already exists; a backup never overwrites anything")
    with connect(f"file:{src.as_posix()}?mode=ro", uri=True) as source_con, connect(str(dst)) as target_con:
        source_con.backup(target_con)
    return dst


def verify_backup(source, backup_path) -> dict:
    """Raises PreconditionFailed unless the backup is trustworthy. Checks: integrity of the BACKUP, equal row counts for every table, equal content
    fingerprint, the file opens read-only and answers a query. Returns what was checked (and the file hash to record next to the backup)."""
    src, bak = assert_disposable(source), assert_disposable(backup_path)
    result, foreign = integrity(bak)
    if result != "ok" or foreign:
        raise PreconditionFailed("the backup fails SQLite's integrity check")
    for table in sorted(table_names(src)):
        if count(src, table) != count(bak, table):
            raise PreconditionFailed(f"the backup has a different row count for {table}")
    if table_names(src) != table_names(bak) or schema_text(src) != schema_text(bak):
        raise PreconditionFailed("the backup has a different schema")
    if fingerprint(src) != fingerprint(bak):
        raise PreconditionFailed("the backup content differs from the source")
    with connect(f"file:{bak.as_posix()}?mode=ro", uri=True) as con:
        con.execute("select count(*) from sqlite_master").fetchone()
    return {"integrity": result, "tables": len(table_names(bak)), "fingerprint": fingerprint(bak), "sha256": sha256_file(bak), "revision": current_revision(bak)}


def restore_rehearsal(backup_path, scratch_target) -> dict:
    """Restores the backup into a throw-away path and proves it reproduces the backup exactly. The ORIGINAL is never touched."""
    bak, dst = assert_disposable(backup_path), assert_disposable(scratch_target)
    if dst.exists():
        raise PreconditionFailed("the rehearsal target already exists")
    shutil.copy2(bak, dst)
    return verify_backup(bak, dst)
