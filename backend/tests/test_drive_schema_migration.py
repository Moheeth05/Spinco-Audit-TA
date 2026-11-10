"""Phase 3A.2-B: the schema revision c3a9f0b17d42 and the read-only schema gate's inspector, tested on DISPOSABLE SQLite files only.

Every database here lives under the approved disposable folder (D:\\SpincoTA-artifacts\\phase-3A\\disposable-db-tests\\ by default), is built by the
project's own migrations or by the frozen pre-3A.2-B DDL, and is reached through tests/migration_harness.py, which refuses anything else and always sets
SPINCO_DB_PATH explicitly. No real database, credential, Google call or company document is involved: all rows are synthetic."""

import importlib.util
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa

import migration_harness as h
from app.services.drive import schema as drive_schema
from drive_legacy_schema import LEGACY_DOCUMENT_STORAGE_COLUMNS

pytestmark = pytest.mark.skipif(not h.root_available(), reason="the approved disposable-database folder is not available on this machine")

MIGRATION_FILE = h.BACKEND_DIR / "migrations" / "versions" / "c3a9f0b17d42_drive_storage_retry_budget_lease_columns.py"
NEW_COLUMNS = drive_schema.RETRY_COLUMNS
LEGACY_INDEXES = {"ix_document_storage_document_pk", "ix_document_storage_kind", "ix_document_storage_sha256", "ix_document_storage_state"}


def real_indexes(path, table="document_storage"):
    return {n for n in h.index_names(path, table) if not n.startswith("sqlite_autoindex")}


OLD_COLUMN_FILTER = {"document_storage": LEGACY_DOCUMENT_STORAGE_COLUMNS, "storage_events": ("id", "record_id", "event_type", "detail", "actor_id", "created_at"),
                     "drive_sync_state": ("name", "page_token", "updated_at")}


@pytest.fixture(scope="module")
def template_db():
    """A database built only by the existing migrations, at exactly 8268b29c4fb1. Copied for every test, never modified."""
    run = h.new_run_dir("template")
    path = run / "base.db"
    h.build_base_database(path)
    yield path
    h.remove_run_dir(run)


@pytest.fixture()
def run_dir():
    run = h.new_run_dir()
    yield run
    h.remove_run_dir(run)


@pytest.fixture()
def base_db(template_db, run_dir):
    target = run_dir / "base.db"
    shutil.copy2(template_db, target)
    return target


@pytest.fixture()
def legacy_db(base_db):
    """State 1: the shape every existing installation has (stamped at 8268b29c4fb1, Drive tables in the pre-3A.2-B shape) with synthetic rows."""
    h.add_legacy_drive_tables(base_db)
    h.seed_legacy_rows(base_db)
    return base_db


def ok(result):
    assert result.returncode == 0, h.output(result)
    return result


def report(path):
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            return drive_schema.inspect_drive_schema(connection)
    finally:
        engine.dispose()


# ------------------------------------------------------------------------------------------------------------------------------ the safety rules


def test_paths_outside_the_approved_folder_are_refused_before_anything_runs(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: called.append(a))
    for unsafe in (h.BACKEND_DIR / "spinco_ta.db", h.BACKEND_DIR / "scratch.db", tmp_path / "x.db", Path.home() / "x.db", h.disposable_root().parent / "x.db"):
        with pytest.raises(h.UnsafeDatabasePath):
            h.alembic(unsafe, "current")
        with pytest.raises(h.UnsafeDatabasePath):
            h.python(unsafe, "print(1)")
    with pytest.raises(h.UnsafeDatabasePath):  # even inside the approved folder, the application's own file name is never disposable
        h.alembic(h.disposable_root() / "spinco_ta.db", "current")
    assert called == []  # no process was started for any of them


def test_the_path_guard_rejects_name_tricks_hard_links_streams_and_an_unsafe_root(monkeypatch, run_dir):
    import os

    for name in ("SPINCO_TA.DB", "spinco_ta.db.", "Spinco_Ta.Db "):  # the application's file name in any case, with a trailing dot or space
        with pytest.raises(h.UnsafeDatabasePath):
            h.assert_disposable(h.disposable_root() / name)
    with pytest.raises(h.UnsafeDatabasePath):  # an NTFS alternate data stream
        h.assert_disposable(run_dir / "a.db:evil")
    real = run_dir / "real.db"
    real.write_bytes(b"x")
    os.link(real, run_dir / "link.db")  # a hard link inside the folder could lead to a file outside it: both names are refused
    for name in ("real.db", "link.db"):
        with pytest.raises(h.UnsafeDatabasePath):
            h.assert_disposable(run_dir / name)
    repo = h.BACKEND_DIR.parent
    try:
        for bad in (Path(repo.anchor), repo, repo.parent, repo / "docs"):  # an override of the root itself must not make the disk or the repository "disposable"
            monkeypatch.setenv(h.ROOT_ENV, str(bad))
            with pytest.raises(h.UnsafeDatabasePath):
                h.assert_disposable(bad / "x.db")
            with pytest.raises(h.UnsafeDatabasePath):
                h.new_run_dir()
    finally:
        monkeypatch.delenv(h.ROOT_ENV, raising=False)  # even when this test FAILS: the folder fixture removes its run directory under the approved root afterwards


def test_the_subprocess_always_gets_an_explicit_database_path(run_dir, monkeypatch):
    seen = {}
    monkeypatch.setenv("SPINCO_DB_PATH", str(h.BACKEND_DIR / "spinco_ta.db"))  # even a poisoned environment cannot leak into the subprocess
    monkeypatch.setattr(h.subprocess, "run", lambda cmd, **kwargs: seen.update(env=kwargs["env"], cwd=kwargs["cwd"]) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    h.alembic(run_dir / "d.db", "current")
    assert seen["env"]["SPINCO_DB_PATH"] == str((run_dir / "d.db").resolve()) and "DATABASE_URL" not in seen["env"]


def test_the_migration_is_only_run_against_a_database_at_exactly_the_required_revision(template_db, run_dir):
    empty = run_dir / "empty.db"
    with pytest.raises(h.PreconditionFailed):  # no alembic_version at all (for example a create_all-only database): never migrated
        h.upgrade_to_new(empty)
    assert not empty.exists() or h.current_revision(empty) is None
    base = run_dir / "base.db"
    shutil.copy2(template_db, base)
    assert h.current_revision(base) == h.BASE_REVISION
    with pytest.raises(h.PreconditionFailed):
        h.downgrade_to_base(base)  # it is not at the new revision yet
    ok(h.upgrade_to_new(base))
    with pytest.raises(h.PreconditionFailed):
        h.upgrade_to_new(base)  # already applied: not at 8268b29c4fb1 any more


def test_the_template_database_has_the_expected_history_and_no_drive_tables(template_db):
    assert h.current_revision(template_db) == h.BASE_REVISION
    assert not set(h.DRIVE_TABLES) & h.table_names(template_db)  # the Drive tables were never part of the Alembic history
    assert len(h.table_names(template_db)) == 19  # 18 application tables + alembic_version


CENTRAL_REVISION = "f1d0c9e84a27"  # the central-storage revision, which sits on top of the retry-budget revision


def test_there_is_a_single_head_and_it_sits_directly_on_the_retry_budget_revision(run_dir):
    heads = ok(h.alembic(run_dir / "h.db", "heads"))
    assert heads.stdout.split()[0] == CENTRAL_REVISION and heads.stdout.count("(head)") == 1
    history = ok(h.alembic(run_dir / "h.db", "history")).stdout
    assert f"{h.NEW_REVISION} -> {CENTRAL_REVISION}" in history  # the retry-budget revision is the central revision's only parent


# ---------------------------------------------------------------------------------------------------------- state 1: existing legacy databases


def test_upgrade_adds_the_columns_and_changes_no_existing_row(legacy_db, run_dir):
    assert report(legacy_db).status == drive_schema.NEEDS_MIGRATION
    before_rows = {t: h.count(legacy_db, t) for t in h.DRIVE_TABLES}
    before_fingerprint = h.fingerprint(legacy_db, OLD_COLUMN_FILTER)
    verified = h.verify_backup(legacy_db, h.backup(legacy_db, run_dir / "backup.db"))  # the documented backup step, verified before migrating
    assert verified["integrity"] == "ok" and verified["revision"] == h.BASE_REVISION

    ok(h.upgrade_to_new(legacy_db))

    assert h.current_revision(legacy_db) == h.NEW_REVISION
    assert {t: h.count(legacy_db, t) for t in h.DRIVE_TABLES} == before_rows  # same number of rows
    assert h.fingerprint(legacy_db, OLD_COLUMN_FILTER) == before_fingerprint  # every old column of every row is byte-for-byte unchanged
    found = {name: (kind, notnull, default) for name, kind, notnull, default in h.columns(legacy_db, "document_storage")}
    assert found["failure_class"][:2] == ("VARCHAR(20)", False) and found["lease_owner"][:2] == ("VARCHAR(40)", False)
    assert found["deferrals"][:2] == ("INTEGER", True) and str(found["deferrals"][2]).strip("'") == "0"
    for name in ("deferred_since", "next_attempt_at", "last_attempt_at", "lease_expires_at"):
        assert found[name][:2] == ("DATETIME", False)
    assert real_indexes(legacy_db) == LEGACY_INDEXES  # exactly the old indexes: no index was added on the retry columns (it would never be used), none was lost
    assert ("kind", "sha256") in h.unique_column_sets(legacy_db, "document_storage")  # nothing that existed was lost (the duplicate guard included)
    with h.connect(str(legacy_db)) as con:
        assert con.execute("select count(*) from document_storage where deferrals != 0").fetchone()[0] == 0
        assert con.execute("select count(*) from document_storage where failure_class is not null or deferred_since is not null or next_attempt_at is not null "
                           "or last_attempt_at is not null or lease_owner is not null or lease_expires_at is not null").fetchone()[0] == 0
        # code that predates the change (it does not know the new columns) can still write a row
        con.execute("insert into document_storage (kind, sha256, original_filename, size, md5, location, state, attempts, created_at, updated_at) "
                    "values ('excel', ?, 'x', 1, 'm', 'gdrive:X', 'UPLOADING', 0, '2026-11-01', '2026-11-01')", ("f" * 64,))
    assert h.integrity(legacy_db) == ("ok", [])
    assert report(legacy_db).status == drive_schema.OK  # and the read-only inspector agrees


def test_downgrade_restores_the_old_shape_without_losing_a_row_and_the_upgrade_can_be_repeated(legacy_db):
    before_fingerprint = h.fingerprint(legacy_db, OLD_COLUMN_FILTER)
    ok(h.upgrade_to_new(legacy_db))
    ok(h.downgrade_to_base(legacy_db))
    assert h.current_revision(legacy_db) == h.BASE_REVISION
    assert [c[0] for c in h.columns(legacy_db, "document_storage")] == list(LEGACY_DOCUMENT_STORAGE_COLUMNS)  # the same columns, in the same order
    assert real_indexes(legacy_db) == LEGACY_INDEXES
    assert ("kind", "sha256") in h.unique_column_sets(legacy_db, "document_storage")
    assert h.fingerprint(legacy_db, OLD_COLUMN_FILTER) == before_fingerprint and h.count(legacy_db, "document_storage") == 5
    assert h.integrity(legacy_db) == ("ok", [])
    assert report(legacy_db).status == drive_schema.NEEDS_MIGRATION
    ok(h.upgrade_to_new(legacy_db))  # and up again
    assert report(legacy_db).status == drive_schema.OK and h.fingerprint(legacy_db, OLD_COLUMN_FILTER) == before_fingerprint
    to_head = ok(h.alembic(legacy_db, "upgrade", "head"))  # the central-storage revision comes next
    assert h.current_revision(legacy_db) == CENTRAL_REVISION and CENTRAL_REVISION in h.output(to_head)
    again = ok(h.alembic(legacy_db, "upgrade", "head"))  # a repeat at head changes nothing
    assert h.current_revision(legacy_db) == CENTRAL_REVISION and "Running upgrade" not in h.output(again)


def test_the_foreign_keys_between_the_drive_tables_survive_a_downgrade(legacy_db):
    ok(h.upgrade_to_new(legacy_db))
    ok(h.downgrade_to_base(legacy_db))
    with h.connect(str(legacy_db)) as con:
        references = {(r[2], r[3]) for r in con.execute("pragma foreign_key_list(document_storage)")} | {(r[2], r[3]) for r in con.execute("pragma foreign_key_list(storage_events)")}
    assert {("users", "created_by_id"), ("users", "archived_by_id"), ("users", "restored_by_id"), ("document_storage", "record_id"), ("users", "actor_id")} <= references


def test_a_failed_attempt_leaves_a_resumable_partial_state_that_a_rerun_completes(legacy_db):
    with h.connect(str(legacy_db)) as con:  # what an interrupted run could have left: some of the new columns already added, correctly
        con.execute("alter table document_storage add column failure_class VARCHAR(20)")
        con.execute("alter table document_storage add column deferrals INTEGER NOT NULL DEFAULT 0")
        con.execute("alter table document_storage add column lease_owner VARCHAR(40)")
    assert report(legacy_db).status == drive_schema.NEEDS_MIGRATION
    before = h.fingerprint(legacy_db, OLD_COLUMN_FILTER)
    ok(h.upgrade_to_new(legacy_db))
    assert {c[0] for c in h.columns(legacy_db, "document_storage")} >= set(NEW_COLUMNS)
    assert report(legacy_db).status == drive_schema.OK and h.fingerprint(legacy_db, OLD_COLUMN_FILTER) == before


# ------------------------------------------------------------------------------------------------------ states 2 and 3: other starting points


def test_a_database_built_only_by_alembic_gets_the_drive_tables_in_their_final_shape(base_db):
    assert not set(h.DRIVE_TABLES) & h.table_names(base_db) and report(base_db).status == drive_schema.ABSENT
    ok(h.upgrade_to_new(base_db))
    assert set(h.DRIVE_TABLES) <= h.table_names(base_db) and report(base_db).status == drive_schema.OK
    assert all(h.count(base_db, t) == 0 for t in h.DRIVE_TABLES)
    migrated = h.schema_text(base_db, h.DRIVE_TABLES)
    ok(h.create_all_with_current_models(base_db))  # the application starts afterwards: create_all finds the Drive tables in place and changes nothing there
    # (it does still add the application tables the old Alembic history never knew about: that pre-existing drift is outside this revision)
    assert h.schema_text(base_db, h.DRIVE_TABLES) == migrated and report(base_db).status == drive_schema.OK


def test_the_migrated_schema_is_identical_to_the_one_create_all_builds(base_db, run_dir, template_db):
    ok(h.upgrade_to_new(base_db))
    fresh = run_dir / "created.db"
    shutil.copy2(template_db, fresh)
    ok(h.create_all_with_current_models(fresh))  # a fresh install: create_all builds the Drive tables from the models
    for table in h.DRIVE_TABLES:
        assert h.columns(fresh, table) == h.columns(base_db, table), table
        assert h.index_names(fresh, table) == h.index_names(base_db, table), table


def test_when_create_all_ran_first_the_migration_finds_nothing_to_do(base_db):
    ok(h.create_all_with_current_models(base_db))  # the new models' tables already exist in the final shape, no alembic history for them
    assert report(base_db).status == drive_schema.OK
    before = h.schema_text(base_db)
    ok(h.upgrade_to_new(base_db))
    assert h.schema_text(base_db) == before and h.current_revision(base_db) == h.NEW_REVISION  # not one statement of the schema changed


def test_offline_sql_mode_is_refused(run_dir):
    result = h.alembic(run_dir / "o.db", "upgrade", f"{h.BASE_REVISION}:{h.NEW_REVISION}", "--sql")
    assert result.returncode != 0 and "offline" in h.output(result).lower()


# --------------------------------------------------------------------------------------------------------------------------- failing closed


def _break_extra_column(con):
    con.execute("alter table document_storage add column surprise VARCHAR(5)")


def _break_missing_column(con):
    con.execute("alter table document_storage drop column md5")


def _break_wrong_type(con):
    con.execute("alter table document_storage add column lease_owner INTEGER")


def _break_missing_table(con):
    con.execute("drop table drive_sync_state")


def _break_extra_index(con):
    con.execute("create index ix_surprise on storage_events (detail)")


def _break_missing_index(con):
    con.execute("drop index ix_document_storage_kind")


def _break_events_column(con):
    con.execute("alter table storage_events add column note VARCHAR(5)")


# --- the three cases the independent review reproduced (they used to be ACCEPTED), and the other things an exact comparison must catch
def _break_index_on_the_wrong_column(con):
    con.executescript("drop index ix_document_storage_kind; create index ix_document_storage_kind on document_storage (md5);")


def _break_unexpected_unique_index(con):
    con.executescript("drop index ix_document_storage_state; create unique index ix_document_storage_state on document_storage (state);")


def _break_missing_foreign_keys(con):
    con.executescript(
        "drop index ix_storage_events_event_type; drop index ix_storage_events_record_id; alter table storage_events rename to events_old; "
        "create table storage_events (id INTEGER NOT NULL, record_id INTEGER, event_type VARCHAR(60) NOT NULL, detail VARCHAR(1000), actor_id INTEGER, "
        "created_at DATETIME NOT NULL, PRIMARY KEY (id)); insert into storage_events select * from events_old; drop table events_old; "
        "create index ix_storage_events_event_type on storage_events (event_type); create index ix_storage_events_record_id on storage_events (record_id);"
    )


def _break_one_foreign_key_of_two(con):
    con.executescript(
        "drop index ix_storage_events_event_type; drop index ix_storage_events_record_id; alter table storage_events rename to events_old; "
        "create table storage_events (id INTEGER NOT NULL, record_id INTEGER, event_type VARCHAR(60) NOT NULL, detail VARCHAR(1000), actor_id INTEGER, "
        "created_at DATETIME NOT NULL, PRIMARY KEY (id), FOREIGN KEY(record_id) REFERENCES document_storage (id)); "
        "insert into storage_events select * from events_old; drop table events_old; "
        "create index ix_storage_events_event_type on storage_events (event_type); create index ix_storage_events_record_id on storage_events (record_id);"
    )


def _break_a_wrong_default_in_a_partial_state(con):
    con.execute("alter table document_storage add column deferrals INTEGER NOT NULL DEFAULT 5")


def _break_a_default_on_an_old_column(con):
    con.executescript(
        "drop index ix_storage_events_event_type; drop index ix_storage_events_record_id; alter table storage_events rename to events_old; "
        "create table storage_events (id INTEGER NOT NULL, record_id INTEGER, event_type VARCHAR(60) NOT NULL DEFAULT 'x', detail VARCHAR(1000), actor_id INTEGER, "
        "created_at DATETIME NOT NULL, PRIMARY KEY (id), FOREIGN KEY(record_id) REFERENCES document_storage (id), FOREIGN KEY(actor_id) REFERENCES users (id)); "
        "insert into storage_events select * from events_old; drop table events_old; "
        "create index ix_storage_events_event_type on storage_events (event_type); create index ix_storage_events_record_id on storage_events (record_id);"
    )


def _break_the_primary_key(con):
    con.executescript(
        "drop table drive_sync_state; create table drive_sync_state (name VARCHAR(40) NOT NULL, page_token VARCHAR(200), updated_at DATETIME NOT NULL);"
    )


def _break_a_check_constraint(con):
    con.executescript(
        "drop table drive_sync_state; create table drive_sync_state (name VARCHAR(40) NOT NULL, page_token VARCHAR(200), updated_at DATETIME NOT NULL, "
        "PRIMARY KEY (name), CHECK (length(name) > 0));"
    )


BREAKS = [
    _break_extra_column, _break_missing_column, _break_wrong_type, _break_missing_table, _break_extra_index, _break_missing_index, _break_events_column,
    _break_index_on_the_wrong_column, _break_unexpected_unique_index, _break_missing_foreign_keys, _break_one_foreign_key_of_two,
    _break_a_wrong_default_in_a_partial_state, _break_a_default_on_an_old_column, _break_the_primary_key, _break_a_check_constraint,
]


def migration_problems(path):
    """The MIGRATION's own verdict (its literal expected shape and comparison), for the cross-check with the application's gate."""
    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        inspector = sa.inspect(engine)
        existing = [t for t in h.DRIVE_TABLES if inspector.has_table(t)]
        if len(existing) != len(h.DRIVE_TABLES):
            return ["tables missing"]
        return [p for name in h.DRIVE_TABLES for p in migration._diff(name, inspector)]
    finally:
        engine.dispose()


@pytest.mark.parametrize("break_it", BREAKS, ids=[b.__name__.removeprefix("_break_") for b in BREAKS])
def test_unexpected_drift_stops_the_migration_before_anything_is_changed(legacy_db, break_it):
    with h.connect(str(legacy_db)) as con:
        break_it(con)
    schema_before = h.schema_text(legacy_db)
    rows_before = {t: h.count(legacy_db, t) for t in h.table_names(legacy_db) if t in h.DRIVE_TABLES}
    result = h.upgrade_to_new(legacy_db)
    assert result.returncode != 0 and "Refusing to migrate" in h.output(result)
    assert h.current_revision(legacy_db) == h.BASE_REVISION  # still at the old revision
    assert h.schema_text(legacy_db) == schema_before  # not one column or index was added
    assert {t: h.count(legacy_db, t) for t in h.table_names(legacy_db) if t in h.DRIVE_TABLES} == rows_before
    assert report(legacy_db).status == drive_schema.DRIFT and not report(legacy_db).ready  # and the application's gate calls it unhealthy, not "needs migration"
    assert migration_problems(legacy_db)  # and the migration's own comparison saw it too


def test_a_unique_constraint_on_other_columns_is_drift(base_db):
    from drive_legacy_schema import LEGACY_DRIVE_DDL

    with h.connect(str(base_db)) as con:
        con.executescript(LEGACY_DRIVE_DDL.replace("UNIQUE (kind, sha256)", "UNIQUE (kind, md5)"))
    assert report(base_db).status == drive_schema.DRIFT and migration_problems(base_db)
    result = h.upgrade_to_new(base_db)
    assert result.returncode != 0 and "Refusing to migrate" in h.output(result)


def test_the_gate_and_the_migration_agree_on_every_state(legacy_db, template_db, run_dir):
    """For every database below, the application's gate and the migration's own comparison give the same verdict about drift."""
    states = {"legacy": legacy_db}
    ok(h.upgrade_to_new(legacy_db))
    states["migrated"] = legacy_db
    for index, break_it in enumerate(BREAKS):
        path = run_dir / f"agree-{index}.db"
        shutil.copy2(template_db, path)
        h.add_legacy_drive_tables(path)
        with h.connect(str(path)) as con:
            break_it(con)
        states[break_it.__name__] = path
    for name, path in states.items():
        gate = report(path)
        assert (gate.status == drive_schema.DRIFT) == bool(migration_problems(path)), (name, gate.problems, migration_problems(path))


def test_each_reproduced_review_case_is_unhealthy_for_the_gate_after_a_successful_migration_too(legacy_db):
    ok(h.upgrade_to_new(legacy_db))  # a healthy, migrated database ...
    assert report(legacy_db).ready
    with h.connect(str(legacy_db)) as con:  # ... that later loses its duplicate guard's column set
        con.executescript("drop index ix_document_storage_kind; create index ix_document_storage_kind on document_storage (md5);")
    assert report(legacy_db).status == drive_schema.DRIFT

# ---------------------------------------------------------------------------------------------------------- the migration and the models agree


def _load_migration():
    spec = importlib.util.spec_from_file_location("drive_retry_migration", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migrations_literal_shape_equals_the_models_shape():
    """The migration deliberately does not import the models (it must keep meaning the same after they change). This is the check that, TODAY, the
    two describe exactly the same tables: columns (type, length, nullability, default), primary keys, indexes (columns, uniqueness), unique constraints,
    foreign keys. When a later revision changes a Drive model, this test is what tells its author to look at both."""
    migration = _load_migration()
    for table in h.DRIVE_TABLES:
        wanted, model = migration._wanted(table), drive_schema.model_shape(table)
        assert {k: (v[0], v[1], v[2], drive_schema.normalize_default(v[3])) for k, v in wanted["columns"].items()} == model["columns"], table
        assert wanted["pk"] == model["pk"] and wanted["indexes"] == model["indexes"] and wanted["uniques"] == model["uniques"] and wanted["fks"] == model["fks"], table
        assert model["checks"] == 0
    assert tuple(c.name for c in migration._new_columns()) == drive_schema.RETRY_COLUMNS == tuple(migration._NEW_COLUMNS)
    assert migration.revision == drive_schema.MIGRATION_REVISION == h.NEW_REVISION and migration.down_revision == h.BASE_REVISION
    assert not hasattr(migration, "INDEX")  # no index on the retry columns (see the benchmark in the correction report)


def test_a_claim_uses_the_primary_key(base_db):
    ok(h.upgrade_to_new(base_db))
    with h.connect(str(base_db)) as con:
        claim = " ".join(r[3] for r in con.execute("explain query plan update document_storage set lease_owner = 'x' where id = 1 and state = 'UPLOADING'"))
    assert "INTEGER PRIMARY KEY" in claim
    # (the sweep's plan is tested against the REAL query in test_drive_retry_budget.py)


# ------------------------------------------------------------------------------------------------ dialect awareness (SQL rendered for PostgreSQL)
# NO live PostgreSQL database was used anywhere. These tests render the migration's SQL against the PostgreSQL DIALECT (Alembic's as_sql mode) and check the
# statements; they prove the statements are dialect-appropriate, not that a PostgreSQL server accepts them.


def render_for(dialect, body):
    import io

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    buffer = io.StringIO()
    with Operations.context(MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": buffer})):
        body()
    return buffer.getvalue()


def test_on_postgresql_the_downgrade_drops_columns_in_place_and_never_rebuilds_the_table():
    migration = _load_migration()
    sql = render_for("postgresql", lambda: migration._drop_retry_columns(set(drive_schema.RETRY_COLUMNS)))
    for column in drive_schema.RETRY_COLUMNS:
        assert f"ALTER TABLE document_storage DROP COLUMN {column}" in sql, column
    for forbidden in ("CREATE TABLE", "DROP TABLE", "_alembic_tmp", "INSERT INTO", "RENAME"):
        assert forbidden not in sql, forbidden  # no copy / drop / rename of document_storage (its FK from storage_events would block it, and the PK/sequence would be lost)


def test_the_downgrade_only_drops_columns_that_exist():
    migration = _load_migration()
    sql = render_for("postgresql", lambda: migration._drop_retry_columns({"failure_class", "lease_owner"}))
    assert sql.count("DROP COLUMN") == 2 and "deferrals" not in sql


def test_on_postgresql_the_upgrade_adds_plain_columns_with_the_specified_types_and_default():
    migration = _load_migration()
    sql = render_for("postgresql", lambda: migration._add_missing_columns(set()))
    assert "ALTER TABLE document_storage ADD COLUMN deferrals INTEGER DEFAULT 0 NOT NULL" in sql
    assert "ADD COLUMN failure_class VARCHAR(20)" in sql and "ADD COLUMN lease_owner VARCHAR(40)" in sql and sql.count("TIMESTAMP WITHOUT TIME ZONE") == 4
    assert "CREATE" not in sql and "DROP" not in sql
    assert render_for("postgresql", lambda: migration._add_missing_columns(set(drive_schema.RETRY_COLUMNS))).strip() == ""  # all there: nothing to do (idempotent)


def test_on_postgresql_a_database_without_the_drive_tables_gets_them_in_the_final_shape_without_an_extra_index():
    migration = _load_migration()
    sql = render_for("postgresql", migration._create_all_drive_tables)
    for table in h.DRIVE_TABLES:
        assert f"CREATE TABLE {table} (" in sql
    assert "CONSTRAINT uq_document_storage_kind_sha256 UNIQUE (kind, sha256)" in sql and "deferrals INTEGER DEFAULT 0 NOT NULL" in sql
    assert "FOREIGN KEY(record_id) REFERENCES document_storage (id)" in sql
    assert "next_attempt" not in sql.split("CREATE INDEX")[-1] and sql.count("CREATE INDEX") == 6  # the six original indexes, nothing on the retry columns


def test_postgresql_style_reflection_is_read_the_same_way_as_sqlite_reflection():
    """A fake inspector that answers the way PostgreSQL's does (INTEGER/VARCHAR/TIMESTAMP types, `0` / `'0'::integer` defaults, a constraint-backed unique
    index flagged `duplicates_constraint`, named foreign keys) must describe every Drive table exactly as the models do - and a BIGINT or TIMESTAMPTZ must not."""
    from sqlalchemy.dialects import postgresql as pg

    def column(spec_name, spec):
        family, length, nullable, default = spec
        kind = {"string": pg.VARCHAR(length), "integer": pg.INTEGER(), "datetime": pg.TIMESTAMP(), "datetime_tz": pg.TIMESTAMP(timezone=True), "biginteger": pg.BIGINT()}[family]
        return {"name": spec_name, "type": kind, "nullable": nullable, "default": None if default is None else f"{default}"}

    class FakePostgres:
        def __init__(self, name, tweak=None):
            self.model, self.tweak = drive_schema.model_shape(name), tweak or {}

        def get_columns(self, _):
            return [column(n, self.tweak.get(n, s)) for n, s in self.model["columns"].items()]

        def get_pk_constraint(self, _):
            return {"constrained_columns": list(self.model["pk"]), "name": "pk"}

        def get_indexes(self, _):
            out = [{"name": n, "column_names": list(c), "unique": u} for n, (c, u) in self.model["indexes"].items()]
            out += [{"name": n, "column_names": list(c), "unique": True, "duplicates_constraint": n} for n, c in self.model["uniques"].items()]  # PostgreSQL lists these too
            return out

        def get_unique_constraints(self, _):
            return [{"name": n, "column_names": list(c)} for n, c in self.model["uniques"].items()]

        def get_foreign_keys(self, _):
            return [{"name": f"fk_{i}", "constrained_columns": list(c), "referred_table": t, "referred_columns": list(r)} for i, (c, t, r) in enumerate(sorted(self.model["fks"]))]

        def get_check_constraints(self, _):
            return []

    for table in h.DRIVE_TABLES:
        assert drive_schema.reflect_shape(FakePostgres(table), table) == drive_schema.model_shape(table), table
    wider = {"attempts": ("biginteger", None, False, None)}
    assert drive_schema.reflect_shape(FakePostgres("document_storage", wider), "document_storage") != drive_schema.model_shape("document_storage")
    aware = {"created_at": ("datetime_tz", None, False, None)}
    assert drive_schema.reflect_shape(FakePostgres("document_storage", aware), "document_storage") != drive_schema.model_shape("document_storage")
    assert drive_schema.normalize_default("'0'::integer") == drive_schema.normalize_default("0") == drive_schema.normalize_default("'0'") == drive_schema.normalize_default("(0)") == "0"
    assert drive_schema.normalize_default(None) is None


def test_the_downgrade_is_not_hardwired_to_rebuild_the_table():
    source = MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'recreate="auto"' in source and 'recreate="always"' not in source

# ------------------------------------------------------------------------------------------------------------------ backup, verify and restore


def test_a_backup_is_verified_and_a_restore_rehearsal_reproduces_it(legacy_db, run_dir):
    backup = h.backup(legacy_db, run_dir / "backup.db")
    checked = h.verify_backup(legacy_db, backup)
    assert checked["integrity"] == "ok" and checked["revision"] == h.BASE_REVISION and len(checked["sha256"]) == 64
    rehearsed = h.restore_rehearsal(backup, run_dir / "rehearsal.db")
    assert rehearsed["fingerprint"] == checked["fingerprint"]
    with pytest.raises(h.PreconditionFailed):  # a backup never overwrites anything
        h.backup(legacy_db, backup)


def test_a_backup_that_differs_from_its_source_is_not_trusted(legacy_db, run_dir):
    backup = h.backup(legacy_db, run_dir / "backup.db")
    with h.connect(str(backup)) as con:
        con.execute("update document_storage set attempts = attempts + 1 where id = 1")
    with pytest.raises(h.PreconditionFailed, match="differs"):
        h.verify_backup(legacy_db, backup)
    with h.connect(str(backup)) as con:
        con.execute("delete from storage_events")
    with pytest.raises(h.PreconditionFailed, match="row count"):
        h.verify_backup(legacy_db, backup)


def test_restoring_the_backup_after_a_migration_returns_the_old_state_exactly(legacy_db, run_dir):
    before = h.fingerprint(legacy_db)
    backup = h.backup(legacy_db, run_dir / "backup.db")
    h.verify_backup(legacy_db, backup)
    ok(h.upgrade_to_new(legacy_db))
    restored = run_dir / "restored.db"
    shutil.copy2(backup, restored)  # the recovery procedure: the verified backup goes back in place of the migrated file
    assert h.current_revision(restored) == h.BASE_REVISION and h.fingerprint(restored) == before and h.integrity(restored) == ("ok", [])
    assert report(restored).status == drive_schema.NEEDS_MIGRATION
    ok(h.upgrade_to_new(restored))  # and the migration can be tried again from the restored copy
    assert report(restored).status == drive_schema.OK


def test_the_schema_inspector_never_changes_a_database(legacy_db):
    before = legacy_db.read_bytes()
    for _ in range(3):
        report(legacy_db)
    assert legacy_db.read_bytes() == before  # not one byte: read-only means read-only
