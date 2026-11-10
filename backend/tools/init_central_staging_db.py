"""Creates a NEW, EMPTY database (SQLite file, or a PostgreSQL database) for the central backend, in its final schema, and stamps it at the current Alembic head.

Why this tool exists: the central modes refuse to start on a database whose schema is not already exactly right (the backend never creates or alters tables there). The
Alembic history alone does not yet build the complete current schema (a known, separately tracked gap), so a NEW database is built from the models in one explicit,
operator-run step, then stamped so that later revisions apply normally.

Safety: it only ever creates a database that does not exist yet (or an existing EMPTY file). It refuses an existing database that has any table, refuses the default
development database, and never touches another file. It prints no secret.

    python tools/init_central_staging_db.py --db D:\\SpincoTA-artifacts\\central-staging\\run1\\central.db
    python tools/init_central_staging_db.py --database-url-file D:\\SPINCO-SECRETS\\spinco-db-url.txt      # PostgreSQL: an EMPTY database that already exists
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _init_postgres(url_file: Path) -> int:
    os.environ.pop("SPINCO_DB_PATH", None)
    os.environ.pop("SPINCO_DATABASE_URL", None)
    os.environ["SPINCO_DATABASE_URL_FILE"] = str(url_file)  # must be set before app.database is imported
    sys.path.insert(0, str(BACKEND_DIR))
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from app import models  # noqa: F401  (registers every model)
    from app.database import IS_SQLITE, Base, DatabaseConfigError, engine

    if IS_SQLITE:
        print("Refused: the URL file did not describe a PostgreSQL database.", file=sys.stderr)
        return 2
    try:
        existing = inspect(engine).get_table_names()
    except Exception:  # noqa: BLE001 - never echo the driver text (it can contain the host and user)
        print("Refused: the database could not be reached with that URL.", file=sys.stderr)
        return 2
    if existing:
        print("Refused: the database already contains tables. Initialisation only ever runs on an empty database.", file=sys.stderr)
        return 2
    Base.metadata.create_all(bind=engine)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.stamp(config, "head")
    engine.dispose()
    print("Created the PostgreSQL schema and stamped it at the current Alembic head.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--db", help="path of the NEW SQLite database file (must not exist, or be an empty file)")
    group.add_argument("--database-url-file", help="a file holding the PostgreSQL URL of an EMPTY database (the URL is never printed)")
    args = parser.parse_args(argv)
    if args.database_url_file:
        return _init_postgres(Path(args.database_url_file))
    target = Path(args.db).resolve()
    if target == (BACKEND_DIR / "spinco_ta.db").resolve():
        print("Refused: that is the default development database.", file=sys.stderr)
        return 2
    if target.exists() and target.stat().st_size > 0:
        try:
            with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as con:
                tables = con.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
        except sqlite3.DatabaseError:
            tables = -1
        if tables != 0:
            print("Refused: the file already exists and is not an empty database. Choose a new path.", file=sys.stderr)
            return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    os.environ["SPINCO_DB_PATH"] = str(target)  # must be set before app.database is imported
    sys.path.insert(0, str(BACKEND_DIR))
    from alembic import command
    from alembic.config import Config

    from app import models  # noqa: F401  (registers every model)
    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.stamp(config, "head")
    engine.dispose()
    print(f"Created {target.name} at the current schema and stamped it at the Alembic head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
