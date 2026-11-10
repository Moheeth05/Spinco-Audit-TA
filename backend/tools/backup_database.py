"""Backs up (and verifies) the application database - SQLite or PostgreSQL - to a folder, safely and without stopping the backend.

    python tools/backup_database.py --out D:\\Backups\\spinco                       # one new backup file + its .sha256, then verification
    python tools/backup_database.py --out D:\\Backups\\spinco --keep 30             # ... and delete OLDER backups made by this tool beyond the newest 30
    python tools/backup_database.py --verify-only D:\\Backups\\spinco\\spinco-20261201-020000.sqlite3

The database is chosen exactly as the backend chooses it (SPINCO_DATABASE_URL / SPINCO_DATABASE_URL_FILE for PostgreSQL, otherwise SPINCO_DB_PATH). Nothing is ever
written to the database, and the password in a PostgreSQL URL is never printed or put on a command line (it is handed to pg_dump through the child's environment).

  SQLite      the online backup API: a consistent snapshot even while the backend is running. Verified with PRAGMA integrity_check, the Alembic revision and a table count.
  PostgreSQL  `pg_dump --format=custom` (needs the PostgreSQL client tools: pg_dump / pg_restore on PATH, or --pg-bin <folder>). Verified with `pg_restore --list`.

What a database backup does NOT contain: the original PDFs and Excel exports in Google Drive (Drive keeps those - back them up with the company's Drive policy) and the
server's working-copy cache (rebuilt from Drive on demand). Keep backup folders on a different disk/machine than the database, and test a restore periodically.
Exit code 0 = backup written AND verified; anything else = it was not.
"""

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PREFIX = "spinco-"
NAME_RE = re.compile(r"^spinco-\d{8}-\d{6}\.(sqlite3|dump)$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_database():
    """('sqlite', Path) or ('postgresql', sqlalchemy URL) - never printed."""
    sys.path.insert(0, str(BACKEND_DIR))
    from sqlalchemy.engine import make_url

    from app.database import DATABASE_URL, DB_PATH, IS_SQLITE

    if IS_SQLITE:
        return "sqlite", Path(DB_PATH)
    return "postgresql", make_url(DATABASE_URL)


def _pg_tool(name: str, pg_bin: str | None) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    if pg_bin:
        candidate = Path(pg_bin) / exe
        if candidate.is_file():
            return str(candidate)
        raise SystemExit(f"{exe} was not found in --pg-bin.")
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} was not found. Install the PostgreSQL client tools or pass --pg-bin <folder>.")
    return found


def _pg_env(url) -> dict:
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password  # the child's environment only: never a command-line argument, never printed
    return env


def _pg_args(url) -> list[str]:
    args = []
    if url.host:
        args += ["--host", url.host]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    return args


def backup_sqlite(source: Path, target: Path) -> None:
    if not source.is_file():
        raise SystemExit("The SQLite database file does not exist.")
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)  # consistent snapshot, safe while the backend is writing
        finally:
            dst.close()
    finally:
        src.close()


def verify_sqlite(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return ["the file is not a readable SQLite database"]
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            problems.append("PRAGMA integrity_check did not return ok")
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            problems.append("the backup contains no tables")
        if "alembic_version" not in tables:
            problems.append("the backup has no alembic_version table (it is not a stamped application database)")
        else:
            versions = [row[0] for row in con.execute("SELECT version_num FROM alembic_version")]
            if len(versions) != 1:
                problems.append("alembic_version does not hold exactly one revision")
    except sqlite3.DatabaseError:
        problems.append("the backup could not be read")
    finally:
        con.close()
    return problems


def backup_postgres(url, target: Path, pg_bin: str | None) -> None:
    exe = _pg_tool("pg_dump", pg_bin)
    command = [exe, "--format=custom", "--no-owner", "--file", str(target), *_pg_args(url), "--dbname", url.database]
    result = subprocess.run(command, env=_pg_env(url), capture_output=True, text=True)
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        # pg_dump's own message can name the host/user but never the password; print only its first line.
        first = (result.stderr or "").strip().splitlines()[:1]
        raise SystemExit(f"pg_dump failed: {first[0] if first else 'unknown error'}")


def verify_postgres(path: Path, pg_bin: str | None) -> list[str]:
    exe = _pg_tool("pg_restore", pg_bin)
    result = subprocess.run([exe, "--list", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        return ["pg_restore could not read the backup"]
    listing = result.stdout
    problems = []
    if "TABLE DATA" not in listing:
        problems.append("the backup lists no table data")
    if "alembic_version" not in listing:
        problems.append("the backup has no alembic_version table (it is not a stamped application database)")
    return problems


def verify_file(path: Path, pg_bin: str | None = None) -> list[str]:
    if not path.is_file():
        return ["the backup file does not exist"]
    sidecar = path.with_name(path.name + ".sha256")
    problems: list[str] = []
    if sidecar.is_file():
        recorded = sidecar.read_text(encoding="utf-8").split()[0].lower()
        if recorded != _sha256(path):
            problems.append("the file no longer matches its recorded SHA-256 (it was changed or damaged)")
    else:
        problems.append("there is no .sha256 file next to the backup")
    problems += verify_sqlite(path) if path.suffix == ".sqlite3" else verify_postgres(path, pg_bin)
    return problems


def apply_retention(folder: Path, keep: int) -> list[str]:
    """Deletes the OLDEST backups beyond `keep`, only files whose names match this tool's pattern (and their .sha256). Never anything else."""
    backups = sorted((p for p in folder.iterdir() if p.is_file() and NAME_RE.match(p.name)), key=lambda p: p.name, reverse=True)
    removed = []
    for old in backups[keep:]:
        old.unlink()
        old.with_name(old.name + ".sha256").unlink(missing_ok=True)
        removed.append(old.name)
    return removed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="the backup folder (created if missing; keep it on another disk or machine)")
    parser.add_argument("--keep", type=int, default=0, help="keep only the newest N backups made by this tool (0 = keep all)")
    parser.add_argument("--pg-bin", help="folder with pg_dump / pg_restore (PostgreSQL only; default: PATH)")
    parser.add_argument("--verify-only", metavar="FILE", help="verify an existing backup file and exit")
    args = parser.parse_args(argv)

    if args.verify_only:
        problems = verify_file(Path(args.verify_only), args.pg_bin)
        print("VERIFIED" if not problems else "NOT VERIFIED: " + "; ".join(problems))
        return 0 if not problems else 1
    if not args.out:
        parser.error("--out is required (or use --verify-only)")

    kind, source = _resolve_database()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out / f"{PREFIX}{stamp}.{'sqlite3' if kind == 'sqlite' else 'dump'}"
    if target.exists():
        print("Refused: that backup file already exists (wait a second and retry).", file=sys.stderr)
        return 2
    if kind == "sqlite":
        backup_sqlite(source, target)
    else:
        backup_postgres(source, target, args.pg_bin)
    target.with_name(target.name + ".sha256").write_text(f"{_sha256(target)}  {target.name}\n", encoding="utf-8")
    problems = verify_file(target, args.pg_bin)
    if problems:
        print(f"Backup written to {target.name} but NOT VERIFIED: " + "; ".join(problems), file=sys.stderr)
        return 1
    removed = apply_retention(out, args.keep) if args.keep > 0 else []
    print(f"Backup {target.name} written and verified ({target.stat().st_size:,} bytes, SHA-256 {_sha256(target)[:16]}...)." + (f" Removed {len(removed)} older backup(s)." if removed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
