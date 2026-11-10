"""Read-only inspection of the three Drive tables (Phase 3A.2-B). It NEVER changes the database: no DDL, no write, no migration, no repair. The schema
is changed only by an operator running Alembic (revision c3a9f0b17d42); this module only says whether the database is in the shape the code needs.

The code compares what the database REALLY has (SQLAlchemy reflection) with what the models declare - EXACTLY:
  columns            name, type family (integer / biginteger / smallinteger / datetime / datetime_tz / string / boolean ...), string length, nullability and
                     server default
  primary key        its columns
  indexes            by name: the indexed COLUMNS (in order) and whether the index is UNIQUE
  unique constraints by name: their columns
  foreign keys       (columns, referred table, referred columns)
  check constraints  none are expected
and answers one of:
  ok               everything matches exactly.
  needs_migration  the Drive tables are in the shape the application had BEFORE Phase 3A.2-B, or part-way to the new one: the ONLY differences are retry
                   columns that revision c3a9f0b17d42 adds and that are not there yet. The migration has not been applied.
  absent           none of the Drive tables exist yet (a database the application has not started on).
  drift            anything else: a missing, unexpected or differently defined column, index, unique constraint, foreign key or check constraint, or only
                   some of the tables present. Never guessed at and never repaired: the operator has to look at it.
  unknown          the inspection itself failed (for example a locked database): treated as not ready.
Only `ok` lets the Drive storage code run. The check is cheap but not free, so the application caches an answer for a short time.

The migration (migrations/versions/c3a9f0b17d42_*.py) carries its OWN literal copy of this expected shape and the same comparison, because a migration must
keep meaning the same after the models change. tests/test_drive_schema_migration.py proves, today, that the two agree on the shape and on their verdicts.
"""

import re
import threading
import time
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.database import Base

DRIVE_TABLES = ("document_storage", "storage_events", "drive_sync_state")
CENTRAL_TABLES = ("upload_attempts", "processing_jobs")  # central storage phase (migration f1d0c9e84a27): checked by the same exact comparison, by their own gate
RETRY_COLUMNS = ("failure_class", "deferrals", "deferred_since", "next_attempt_at", "last_attempt_at", "lease_owner", "lease_expires_at")
OK, NEEDS_MIGRATION, ABSENT, DRIFT, UNKNOWN = "ok", "needs_migration", "absent", "drift", "unknown"
MIGRATION_REVISION = "c3a9f0b17d42"

# What an ADMINISTRATOR is told when the gate refuses (it names the revision). Everyone else gets GENERIC_UNAVAILABLE_TEXT: schema details are diagnostics,
# not something to hand to a user who is not allowed to see them.
NOT_READY_TEXT = (
    "Drive storage is not available: the database schema for the storage tables is not up to date. "
    "An administrator must apply the database migration (revision " + MIGRATION_REVISION + ") before Drive storage can be used."
)
GENERIC_UNAVAILABLE_TEXT = "Drive storage is not available right now. Contact an administrator."


@dataclass(frozen=True)
class SchemaReport:
    status: str
    problems: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == OK


# ------------------------------------------------------------------------------------------------------------------------------ normalisation


def type_family(column_type) -> str:
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


def normalize_default(value) -> str | None:
    """A server default as text, comparable across dialects: SQLite reports `0` or `'0'`, PostgreSQL `0` or `'0'::integer`."""
    if value is None:
        return None
    text = str(getattr(value, "text", value)).strip()
    text = re.sub(r"::[\w\s\[\]\"]+$", "", text)
    while len(text) >= 2 and text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    return text


def _column_spec(family: str, length, nullable, default) -> tuple:
    return family, (length if family == "string" else None), bool(nullable), normalize_default(default)


def model_shape(name: str) -> dict:
    """The expected shape of one Drive table, derived from the MODELS (so it cannot drift from them)."""
    table = Base.metadata.tables[name]
    columns = {}
    for column in table.columns:
        default = column.server_default.arg if column.server_default is not None else None
        columns[column.name] = _column_spec(type_family(column.type), getattr(column.type, "length", None), column.nullable, default)
    return {
        "columns": columns,
        "pk": tuple(c.name for c in table.primary_key.columns),
        "indexes": {ix.name: (tuple(c.name for c in ix.columns), bool(ix.unique)) for ix in table.indexes},
        "uniques": {c.name: tuple(col.name for col in c.columns) for c in table.constraints if isinstance(c, sa.UniqueConstraint)},
        "fks": {(tuple(e.parent.name for e in c.elements), c.elements[0].column.table.name, tuple(e.column.name for e in c.elements)) for c in table.foreign_key_constraints},
        "checks": 0,
    }


def expected_shape() -> dict[str, dict]:
    return {name: model_shape(name) for name in DRIVE_TABLES}


def reflect_shape(inspector, name: str) -> dict:
    """The same canonical description, read from the database."""
    columns = {}
    for c in inspector.get_columns(name):
        family = type_family(c["type"])
        default = c.get("default")
        if c.get("identity") or (isinstance(default, str) and default.strip().lower().startswith("nextval(")):
            default = None  # PostgreSQL reports an auto-increment primary key as a sequence default (SERIAL) or an IDENTITY column: that is the model's plain Integer PK, not a drift
        columns[c["name"]] = _column_spec(family, getattr(c["type"], "length", None), c["nullable"], default)
    indexes = {i["name"]: (tuple(i["column_names"]), bool(i.get("unique"))) for i in inspector.get_indexes(name) if not i.get("duplicates_constraint")}
    uniques = {(u.get("name") or f"<unnamed {tuple(u['column_names'])}>"): tuple(u["column_names"]) for u in inspector.get_unique_constraints(name)}
    fks = {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"])) for f in inspector.get_foreign_keys(name)}
    return {
        "columns": columns,
        "pk": tuple(inspector.get_pk_constraint(name).get("constrained_columns") or ()),
        "indexes": indexes,
        "uniques": uniques,
        "fks": fks,
        "checks": len(inspector.get_check_constraints(name)),
    }


def diff_table(name: str, want: dict, have: dict) -> tuple[list[str], list[str]]:
    """(problems, pending). `pending` = retry columns of document_storage that are simply not there yet (the migration has not run)."""
    problems: list[str] = []
    pending: list[str] = []
    for column, spec in want["columns"].items():
        if column not in have["columns"]:
            (pending if name == "document_storage" and column in RETRY_COLUMNS else problems).append(
                f"{name}.{column} is not added yet" if name == "document_storage" and column in RETRY_COLUMNS else f"{name}.{column} is missing")
        elif have["columns"][column] != spec:
            problems.append(f"{name}.{column} is defined as {have['columns'][column]} instead of {spec}")
    problems += [f"{name}.{column} is not expected" for column in have["columns"] if column not in want["columns"]]
    if have["pk"] != want["pk"]:
        problems.append(f"{name}: primary key is {have['pk']} instead of {want['pk']}")
    for kind, label in (("indexes", "index"), ("uniques", "unique constraint")):
        for key, spec in want[kind].items():
            if key not in have[kind]:
                problems.append(f"{name}: {label} {key} is missing")
            elif have[kind][key] != spec:
                problems.append(f"{name}: {label} {key} is {have[kind][key]} instead of {spec}")
        problems += [f"{name}: {label} {key} is not expected" for key in have[kind] if key not in want[kind]]
    problems += [f"{name}: foreign key {fk} is missing" for fk in sorted(want["fks"] - have["fks"])]
    problems += [f"{name}: foreign key {fk} is not expected" for fk in sorted(have["fks"] - want["fks"])]
    if have["checks"] != want["checks"]:
        problems.append(f"{name}: {have['checks']} check constraint(s) found, none expected")
    return problems, pending


def inspect_drive_schema(bind, tables: tuple[str, ...] = DRIVE_TABLES) -> SchemaReport:
    """`bind` is an Engine or a Connection. Read-only. `tables` defaults to the Drive tables; the central-storage tables use the same comparison."""
    inspector = sa.inspect(bind)
    existing = [t for t in tables if inspector.has_table(t)]
    if not existing:
        return SchemaReport(ABSENT, ("none of the expected tables exist",))
    if len(existing) != len(tables):
        return SchemaReport(DRIFT, (f"only some of the expected tables exist: {', '.join(existing)}",))
    problems: list[str] = []
    pending: list[str] = []
    for name in tables:
        found, waiting = diff_table(name, model_shape(name), reflect_shape(inspector, name))
        problems += found
        pending += waiting
    if problems:
        return SchemaReport(DRIFT, tuple(problems + pending))
    if pending:
        return SchemaReport(NEEDS_MIGRATION, tuple(pending))
    return SchemaReport(OK)


# ---------------------------------------------------------------------------------------------------------------------------- the gate

_cache_lock = threading.Lock()
_cached: dict = {}
POSITIVE_TTL_SECONDS = 60.0  # a ready schema is not re-inspected on every request
NEGATIVE_TTL_SECONDS = 5.0  # a refusal is re-checked soon, so applying the migration is noticed without a restart


def schema_report(bind, *, clock=time.monotonic) -> SchemaReport:
    """The (briefly cached) read-only answer the Drive endpoints use. The cache is per Engine, so a test database never sees another's answer.
    An inspection that itself fails (a locked database, a dropped connection) is reported as `unknown` - not ready, never an unhandled error."""
    key = id(getattr(bind, "engine", bind))
    now = clock()
    with _cache_lock:
        entry = _cached.get(key)
        if entry is not None and now < entry[0]:
            return entry[1]
    try:
        report = inspect_drive_schema(bind)  # outside the lock: it is a read of the database and may take a moment
    except SQLAlchemyError:
        report = SchemaReport(UNKNOWN, ("the schema could not be inspected",))
    with _cache_lock:
        _cached[key] = (now + (POSITIVE_TTL_SECONDS if report.ready else NEGATIVE_TTL_SECONDS), report)
    return report


def reset_schema_cache() -> None:
    with _cache_lock:
        _cached.clear()


def central_schema_report(bind, *, clock=time.monotonic) -> SchemaReport:
    """The cached, read-only answer for the central-storage tables (upload_attempts, processing_jobs). Same rules as `schema_report`."""
    key = ("central", id(getattr(bind, "engine", bind)))
    now = clock()
    with _cache_lock:
        entry = _cached.get(key)
        if entry is not None and now < entry[0]:
            return entry[1]
    try:
        report = inspect_drive_schema(bind, CENTRAL_TABLES)
    except SQLAlchemyError:
        report = SchemaReport(UNKNOWN, ("the schema could not be inspected",))
    with _cache_lock:
        _cached[key] = (now + (POSITIVE_TTL_SECONDS if report.ready else NEGATIVE_TTL_SECONDS), report)
    return report
