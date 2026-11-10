"""drive storage: retry budget, deferral tracking, scheduling and per-record lease columns (Phase 3A.2-B)

Revision ID: c3a9f0b17d42
Revises: 8268b29c4fb1
Create Date: 2026-11-05

What this does: adds seven columns to `document_storage` (failure_class, deferrals, deferred_since, next_attempt_at, last_attempt_at, lease_owner,
lease_expires_at). Nothing is dropped, rewritten or copied; no existing row changes (the new columns are NULL, `deferrals` is 0, for every row that
exists). There is deliberately NO index on them: the real retry sweep never chooses one (measured: same plan and timing with and without a composite
(state, next_attempt_at) index at 200 000 rows) while every claim and settle would pay to maintain it.

IDEMPOTENT ON PURPOSE. The three Drive tables were never part of the Alembic history: the application created them at start-up with `create_all()`.
A database can therefore be in one of three states when this runs, and all three must end in the same schema:
  1. the tables exist in their pre-3A.2-B shape (every database the Phase 2/3 code has started on)        -> the columns are ADDED;
  2. the tables do not exist (a database built only by Alembic, e.g. a future PostgreSQL)                   -> the tables are CREATED in their final shape;
  3. the tables already have the new shape (`create_all()` of the 3A.2-B models ran first)                  -> nothing is done.
A run that was interrupted part-way (some of the new columns already added, correctly) is completed by running it again.

FAILS CLOSED, EXACTLY. Before it changes anything, the migration compares the Drive tables with the shape it knows - the literal description below, not the
application's models, so the migration keeps meaning the same after the models change: every column (type, length, nullability, server default), the primary
key, every index (its columns and whether it is UNIQUE), every unique constraint (its columns), every foreign key, and the absence of check constraints.
Anything missing, unexpected or defined differently (an index on the wrong column, an index that became UNIQUE, a lost foreign key, a changed default, ...), or
only some of the three tables existing, stops it with an explanation and touches nothing. The application's read-only gate (app/services/drive/schema.py)
applies the same comparison to the models; tests prove the two agree.

DIALECTS. The statements are plain `ADD COLUMN` / `DROP COLUMN` and `CREATE TABLE`. The downgrade uses `batch_alter_table(recreate="auto")`: Alembic rebuilds
the table on SQLite (which cannot drop a column in place) and emits an ordinary `ALTER TABLE ... DROP COLUMN` on PostgreSQL. It must NEVER be "always": that
would copy, drop and rename the table on PostgreSQL. No live PostgreSQL database has been used to test this revision; tests render its SQL against the
PostgreSQL dialect only.

NOT supported: offline (`--sql`) mode, because the decision depends on what the database really contains.
Downgrade removes only what upgrade added to an existing table and NEVER drops a Drive table, so a downgrade cannot lose a record, event or sync token.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "c3a9f0b17d42"
down_revision: Union[str, None] = "8268b29c4fb1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "document_storage"
DRIVE_TABLES = ("document_storage", "storage_events", "drive_sync_state")


def _c(family: str, length=None, nullable=True, default=None) -> tuple:
    return (family, length, nullable, default)


_INT, _DT = "integer", "datetime"

# The shape every Drive table had BEFORE this revision ...
_OLD_COLUMNS = {
    "document_storage": {
        "id": _c(_INT, nullable=False), "kind": _c("string", 20, False), "sha256": _c("string", 64, False), "original_filename": _c("string", 500, False),
        "size": _c(_INT, nullable=False), "md5": _c("string", 32, False), "location": _c("string", 200, False), "state": _c("string", 20, False),
        "last_error": _c("string", 500), "attempts": _c(_INT, nullable=False), "document_kind": _c("string", 10), "document_pk": _c(_INT),
        "created_at": _c(_DT, nullable=False), "created_by_id": _c(_INT), "stored_at": _c(_DT), "archived_at": _c(_DT), "archived_by_id": _c(_INT),
        "restored_at": _c(_DT), "restored_by_id": _c(_INT), "missing_detected_at": _c(_DT), "last_verified_at": _c(_DT), "updated_at": _c(_DT, nullable=False),
    },
    "storage_events": {
        "id": _c(_INT, nullable=False), "record_id": _c(_INT), "event_type": _c("string", 60, False), "detail": _c("string", 1000),
        "actor_id": _c(_INT), "created_at": _c(_DT, nullable=False),
    },
    "drive_sync_state": {"name": _c("string", 40, False), "page_token": _c("string", 200), "updated_at": _c(_DT, nullable=False)},
}
# ... plus the columns this revision adds to document_storage.
_NEW_COLUMNS = {
    "failure_class": _c("string", 20), "deferrals": _c(_INT, nullable=False, default="0"), "deferred_since": _c(_DT), "next_attempt_at": _c(_DT),
    "last_attempt_at": _c(_DT), "lease_owner": _c("string", 40), "lease_expires_at": _c(_DT),
}
_PK = {"document_storage": ("id",), "storage_events": ("id",), "drive_sync_state": ("name",)}
_INDEXES = {  # name -> (columns, unique)
    "document_storage": {
        "ix_document_storage_document_pk": (("document_pk",), False), "ix_document_storage_kind": (("kind",), False),
        "ix_document_storage_sha256": (("sha256",), False), "ix_document_storage_state": (("state",), False),
    },
    "storage_events": {"ix_storage_events_event_type": (("event_type",), False), "ix_storage_events_record_id": (("record_id",), False)},
    "drive_sync_state": {},
}
_UNIQUES = {"document_storage": {"uq_document_storage_kind_sha256": ("kind", "sha256")}, "storage_events": {}, "drive_sync_state": {}}
_FKS = {  # (columns, referred table, referred columns)
    "document_storage": {(("created_by_id",), "users", ("id",)), (("archived_by_id",), "users", ("id",)), (("restored_by_id",), "users", ("id",))},
    "storage_events": {(("record_id",), "document_storage", ("id",)), (("actor_id",), "users", ("id",))},
    "drive_sync_state": set(),
}


def _new_columns() -> list[sa.Column]:
    return [
        sa.Column("failure_class", sa.String(length=20), nullable=True),
        sa.Column("deferrals", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deferred_since", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("lease_owner", sa.String(length=40), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
    ]


# ------------------------------------------------------------------------------------------------------------------------- exact comparison


def _family(column_type) -> str:
    if isinstance(column_type, sa.Boolean):
        return "boolean"
    if isinstance(column_type, sa.BigInteger):
        return "biginteger"
    if isinstance(column_type, sa.SmallInteger):
        return "smallinteger"
    if isinstance(column_type, sa.Integer):
        return "integer"
    if isinstance(column_type, sa.DateTime):
        return "datetime_tz" if getattr(column_type, "timezone", False) else "datetime"
    if isinstance(column_type, sa.String):
        return "string"
    return type(column_type).__name__.lower()


def _default(value):
    """A server default as comparable text (SQLite: `0` / `'0'`; PostgreSQL: `0` / `'0'::integer`)."""
    import re

    if value is None:
        return None
    text = str(getattr(value, "text", value)).strip()
    text = re.sub(r"::[\w\s\[\]\"]+$", "", text)
    while len(text) >= 2 and text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text


def _wanted(name: str) -> dict:
    columns = dict(_OLD_COLUMNS[name])
    if name == TABLE:
        columns.update(_NEW_COLUMNS)
    return {"columns": columns, "pk": _PK[name], "indexes": _INDEXES[name], "uniques": _UNIQUES[name], "fks": _FKS[name]}


def _reflect(inspector, name: str) -> dict:
    columns = {}
    for c in inspector.get_columns(name):
        family = _family(c["type"])
        columns[c["name"]] = (family, getattr(c["type"], "length", None) if family == "string" else None, bool(c["nullable"]), _default(c.get("default")))
    return {
        "columns": columns,
        "pk": tuple(inspector.get_pk_constraint(name).get("constrained_columns") or ()),
        "indexes": {i["name"]: (tuple(i["column_names"]), bool(i.get("unique"))) for i in inspector.get_indexes(name) if not i.get("duplicates_constraint")},
        "uniques": {(u.get("name") or f"<unnamed {tuple(u['column_names'])}>"): tuple(u["column_names"]) for u in inspector.get_unique_constraints(name)},
        "fks": {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"])) for f in inspector.get_foreign_keys(name)},
        "checks": len(inspector.get_check_constraints(name)),
    }


def _diff(name: str, inspector) -> list[str]:
    """Everything about an EXISTING Drive table that is not exactly (the old shape, plus possibly some of the new columns). Empty = safe to proceed."""
    want, have = _wanted(name), _reflect(inspector, name)
    problems = []
    for column, (family, length, nullable, default) in want["columns"].items():
        if column not in have["columns"]:
            if not (name == TABLE and column in _NEW_COLUMNS):  # a new column that is not there yet is what this migration adds
                problems.append(f"{name}.{column} is missing")
        elif have["columns"][column] != (family, length, nullable, _default(default)):
            problems.append(f"{name}.{column} is defined as {have['columns'][column]} instead of {(family, length, nullable, _default(default))}")
    problems += [f"{name}.{column} is not a column this migration knows" for column in have["columns"] if column not in want["columns"]]
    if have["pk"] != want["pk"]:
        problems.append(f"{name}: primary key is {have['pk']} instead of {want['pk']}")
    for kind, label in (("indexes", "index"), ("uniques", "unique constraint")):
        for key, spec in want[kind].items():
            if key not in have[kind]:
                problems.append(f"{name}: {label} {key} is missing")
            elif have[kind][key] != spec:
                problems.append(f"{name}: {label} {key} is {have[kind][key]} instead of {spec}")
        problems += [f"{name}: {label} {key} is not one this migration knows" for key in have[kind] if key not in want[kind]]
    problems += [f"{name}: foreign key {fk} is missing" for fk in sorted(want["fks"] - have["fks"])]
    problems += [f"{name}: foreign key {fk} is not one this migration knows" for fk in sorted(have["fks"] - want["fks"])]
    if have["checks"]:
        problems.append(f"{name}: {have['checks']} check constraint(s) found, none expected")
    return problems


# --------------------------------------------------------------------------------------------------------------------------------- the DDL


def _create_all_drive_tables() -> None:
    op.create_table(
        "document_storage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("md5", sa.String(length=32), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        *_new_columns(),
        sa.Column("document_kind", sa.String(length=10), nullable=True),
        sa.Column("document_pk", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("stored_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("archived_by_id", sa.Integer(), nullable=True),
        sa.Column("restored_at", sa.DateTime(), nullable=True),
        sa.Column("restored_by_id", sa.Integer(), nullable=True),
        sa.Column("missing_detected_at", sa.DateTime(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["archived_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["restored_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "sha256", name="uq_document_storage_kind_sha256"),
    )
    op.create_index("ix_document_storage_document_pk", "document_storage", ["document_pk"])
    op.create_index("ix_document_storage_kind", "document_storage", ["kind"])
    op.create_index("ix_document_storage_sha256", "document_storage", ["sha256"])
    op.create_index("ix_document_storage_state", "document_storage", ["state"])
    op.create_table(
        "storage_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("detail", sa.String(length=1000), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["record_id"], ["document_storage.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_storage_events_event_type", "storage_events", ["event_type"])
    op.create_index("ix_storage_events_record_id", "storage_events", ["record_id"])
    op.create_table(
        "drive_sync_state",
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("page_token", sa.String(length=200), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def _add_missing_columns(have: set[str]) -> None:
    for column in _new_columns():
        if column.name not in have:
            op.add_column(TABLE, column)  # a plain ALTER TABLE ... ADD COLUMN: no table rewrite, no existing row touched


def _drop_retry_columns(have: set[str]) -> None:
    # recreate="auto": Alembic rebuilds (copies) the table ONLY where the dialect cannot drop a column in place (SQLite) and emits a plain
    # `ALTER TABLE ... DROP COLUMN` otherwise (PostgreSQL). "always" would copy, drop and rename the table on every dialect.
    with op.batch_alter_table(TABLE, recreate="auto") as batch:
        for column in _new_columns():
            if column.name in have:
                batch.drop_column(column.name)


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Revision c3a9f0b17d42 cannot be rendered offline (--sql): it decides what to do from what the database really contains.")
    inspector = sa.inspect(op.get_bind())
    existing = [t for t in DRIVE_TABLES if inspector.has_table(t)]

    if not existing:  # state 2: a database without the Drive tables gets them in their final shape
        if not inspector.has_table("users"):
            raise RuntimeError("Refusing to create the Drive tables: the `users` table they refer to does not exist.")
        _create_all_drive_tables()
        return
    if len(existing) != len(DRIVE_TABLES):
        raise RuntimeError(f"Refusing to migrate: only some of the Drive tables exist ({', '.join(existing)}). The schema is in an unexpected state and must be examined first.")

    problems = [p for name in DRIVE_TABLES for p in _diff(name, inspector)]
    if problems:  # fail closed: nothing has been changed yet
        raise RuntimeError("Refusing to migrate: the Drive tables are not in a shape this migration knows. " + "; ".join(problems))

    _add_missing_columns({c["name"] for c in inspector.get_columns(TABLE)})  # states 1 and 3


def downgrade() -> None:
    """Removes only what upgrade() added to an EXISTING table. The Drive tables themselves are never dropped (this revision cannot know whether it
    created them), so a downgrade can never lose a stored record, event or sync token. Whatever the new columns held (leases, schedules, failure
    classes) is lost, which is the point of a downgrade."""
    if context.is_offline_mode():
        raise RuntimeError("Revision c3a9f0b17d42 cannot be rendered offline (--sql).")
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        return
    _drop_retry_columns({c["name"] for c in inspector.get_columns(TABLE)})
