"""Alembic environment. Deliberately imports the application's own
app.database.DATABASE_URL and app.models package (rather than duplicating
the SPINCO_DB_PATH resolution or hand-listing tables here), so:

  1. `alembic` and the running FastAPI app can never disagree about which
     database file they're pointed at - both go through the exact same
     SPINCO_DB_PATH env var resolution in app/database.py.
  2. `target_metadata` always reflects every model actually registered on
     Base (via app/models/__init__.py), so --autogenerate sees the true
     current schema, not a stale hand-maintained list.

See D:\\Bills\\docs\\CENTRALIZED_ARCHITECTURE.md and this directory's own
BASELINE.md for how this applies to an already-existing SQLite database
(dev or production) that was never previously managed by Alembic.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, DATABASE_URL  # noqa: E402
import app.models  # noqa: E402,F401 - populates Base.metadata with every table

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))  # configparser treats % specially (a URL-encoded password has them)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # render_as_batch=True is required for SQLite, which cannot ALTER
        # TABLE to add/drop a constraint (unique/FK) directly - Alembic's
        # batch mode transparently does the recreate-table-and-copy-data
        # dance SQLite needs. It's a documented no-op passthrough on
        # dialects that support ALTER TABLE ADD CONSTRAINT directly (e.g.
        # PostgreSQL), so this stays correct across both targets without
        # any dialect branching here.
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
