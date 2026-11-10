import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------------------------------------------------------------------ which database
#
#   SPINCO_DB_PATH        a SQLite file (the desktop app, development, the staging harness). Unchanged. Default: backend/spinco_ta.db.
#   SPINCO_DATABASE_URL   a PostgreSQL URL for the company server:  postgresql+psycopg://user:password@host:5432/spinco
#   SPINCO_DATABASE_URL_FILE
#                         path of a file that contains that URL (keeps the password out of the process environment). Use one of the two, never both.
#
# The URL (it contains a password) is never logged or echoed in an error message.


class DatabaseConfigError(RuntimeError):
    pass


def _resolve():
    url_text = os.environ.get("SPINCO_DATABASE_URL", "").strip()
    url_file = os.environ.get("SPINCO_DATABASE_URL_FILE", "").strip()
    if url_text and url_file:
        raise DatabaseConfigError("Set SPINCO_DATABASE_URL or SPINCO_DATABASE_URL_FILE, not both.")
    if url_file:
        try:
            url_text = Path(url_file).read_text(encoding="utf-8-sig").strip()
        except OSError:
            raise DatabaseConfigError("SPINCO_DATABASE_URL_FILE could not be read.") from None
    if not url_text:
        db_path = Path(os.environ["SPINCO_DB_PATH"]) if "SPINCO_DB_PATH" in os.environ else BACKEND_DIR / "spinco_ta.db"
        return db_path, f"sqlite:///{db_path}"
    if "SPINCO_DB_PATH" in os.environ:
        raise DatabaseConfigError("Set SPINCO_DATABASE_URL (PostgreSQL) or SPINCO_DB_PATH (SQLite file), not both.")
    try:
        parsed = make_url(url_text)
    except ArgumentError:
        raise DatabaseConfigError("The database URL is not valid.") from None
    if parsed.get_backend_name() != "postgresql":
        raise DatabaseConfigError("SPINCO_DATABASE_URL must be a PostgreSQL URL (postgresql+psycopg://...). For SQLite use SPINCO_DB_PATH.")
    if parsed.drivername == "postgresql":  # the project installs psycopg 3 (requirements-postgres.txt), not the legacy psycopg2
        parsed = parsed.set(drivername="postgresql+psycopg")
    if not parsed.database or not parsed.host:
        raise DatabaseConfigError("The PostgreSQL URL must name a host and a database.")
    import importlib.util

    if importlib.util.find_spec("psycopg") is None:
        raise DatabaseConfigError("The PostgreSQL driver is not installed in this Python environment: pip install -r requirements-postgres.txt")
    return None, parsed.render_as_string(hide_password=False)


DB_PATH, DATABASE_URL = _resolve()
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# CENTRAL backend on SQLite (shadow / serve): many users write at once, which the default SQLite settings handle badly (a measured burst of 25 simultaneous uploaders gave
# "database is locked" errors). So - ONLY in the central modes - connections wait up to 30 s for a lock instead of 5 s, and the database uses write-ahead logging
# (readers no longer block the writer). The default (off) mode - the desktop app and every existing database - is exactly as before. WAL needs a LOCAL disk (never a
# network share); it is a property of the database file, so a central database stays in WAL when it is opened later. (PostgreSQL has real concurrent writers and needs
# none of this.)
CENTRAL_SQLITE_TUNING = IS_SQLITE and os.environ.get("SPINCO_CENTRAL_STORAGE_MODE", "").strip().lower() in ("shadow", "serve")
BUSY_TIMEOUT_SECONDS = 30


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, "").strip() or default)))
    except ValueError:
        return default


if IS_SQLITE:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, **({"timeout": BUSY_TIMEOUT_SECONDS} if CENTRAL_SQLITE_TUNING else {})})
else:
    # Pool sizes are per backend process: SPINCO_DB_POOL_SIZE (default 10) + SPINCO_DB_MAX_OVERFLOW (default 20) must stay below the server's max_connections.
    engine = create_engine(
        DATABASE_URL,
        pool_size=_int_env("SPINCO_DB_POOL_SIZE", 10, 1, 200),
        max_overflow=_int_env("SPINCO_DB_MAX_OVERFLOW", 20, 0, 400),
        pool_pre_ping=True,  # a connection the server closed (restart, idle timeout) is replaced instead of failing a request
        pool_recycle=1800,
    )

if CENTRAL_SQLITE_TUNING:

    @event.listens_for(engine, "connect")
    def _central_sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_SECONDS * 1000}")
        finally:
            cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
