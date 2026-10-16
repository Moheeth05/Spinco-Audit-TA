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

# Post-UAT correctness fix: separates merchant/vendor from category (see
# models/expense_report.py's Expense.merchant) for pre-existing databases
# that already have an `expenses` table without this column.
_EXPENSE_COLUMNS = [
    ("expenses", "merchant", "VARCHAR(255)"),
    # Candidate-vs-confirmed bill matching split (see models/expense_report.py's
    # Expense.candidate_bill_id) - new nullable column on the pre-existing
    # `expenses` table for a pre-existing database that predates this feature.
    ("expenses", "candidate_bill_id", "INTEGER"),
    # P0 follow-up: separates a "Ref# : <value>" expense-row prefix line from
    # category (see models/expense_report.py's Expense.reference_id) for
    # pre-existing databases that already have an `expenses` table without
    # this column.
    ("expenses", "reference_id", "VARCHAR(100)"),
]

# Post-UAT correction 2: manual "Verify" workflow (see
# models/validation.py's ExceptionRecord.verified_by_id/verified_at/
# verification_note and the new ExceptionVerification table, which
# create_all() creates fine on its own since it's a brand-new table - only
# these new columns on the pre-existing `exceptions` table need migrating).
_EXCEPTION_COLUMNS = [
    ("exceptions", "verified_by_id", "INTEGER"),
    ("exceptions", "verified_at", "DATETIME"),
    ("exceptions", "verification_note", "VARCHAR(1000)"),
]

# Supporting-document classifier fallback (see extraction/document_classifier.py
# and bill_pipeline.py) - new nullable columns on the pre-existing `bills`
# table for a pre-existing database that predates this feature.
_BILL_COLUMNS = [
    ("bills", "document_type", "VARCHAR(30)"),
    ("bills", "document_type_confidence", "FLOAT"),
    ("bills", "transaction_reference", "VARCHAR(100)"),
    ("bills", "catering_amount", "NUMERIC(12, 2)"),
    # Multi-page bill grouping (see models/bill.py's Bill.page_number_end and
    # extraction/bill_grouping.py) - new nullable column on the pre-existing
    # `bills` table for a pre-existing database that predates this feature.
    ("bills", "page_number_end", "INTEGER"),
]

_ALL_COLUMNS = _USER_COLUMNS + _EXPENSE_COLUMNS + _EXCEPTION_COLUMNS + _BILL_COLUMNS


def run_light_migrations(engine: Engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, column, ddl_type in _ALL_COLUMNS:
            if table not in existing_tables:
                continue  # brand-new DB - create_all() will build the full table correctly
            existing_columns = {c["name"] for c in inspector.get_columns(table)}
            if column in existing_columns:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
