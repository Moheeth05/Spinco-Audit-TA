"""Phase 10 - safe, additive schema migration for pre-existing SQLite
database files. This project has no migration framework (no Alembic) -
`Base.metadata.create_all()` only creates tables that don't exist yet, it
never alters an existing table. Since Phase 1-9 already shipped a `users`
table without the new auth columns, a developer's existing spinco_ta.db
(with real extracted Expense/Trip Report data already in it) would crash
the moment app code reads `User.password_hash` unless that column is added
in place first.

This module runs a small set of idempotent `ALTER TABLE ... ADD COLUMN`
statements for exactly the columns Phase 10 adds, before `create_all()` is
called - never drops, never rewrites, never touches existing rows/tables
beyond adding new nullable columns with safe defaults. Brand-new databases
are unaffected (the table won't exist yet, so nothing to migrate - the
column is already correct in the fresh table `create_all()` builds)."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# (table, column, DDL type + default) - order matters only in that each
# statement is independent and additive.
_USER_COLUMNS = [
    ("users", "password_hash", "VARCHAR(255)"),
    ("users", "is_active", "BOOLEAN DEFAULT 1"),
    ("users", "must_change_password", "BOOLEAN DEFAULT 0"),
    ("users", "last_login_at", "DATETIME"),
    ("users", "updated_at", "DATETIME"),
]


def run_light_migrations(engine: Engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, column, ddl_type in _USER_COLUMNS:
            if table not in existing_tables:
                continue  # brand-new DB - create_all() will build the full table correctly
            existing_columns = {c["name"] for c in inspector.get_columns(table)}
            if column in existing_columns:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
