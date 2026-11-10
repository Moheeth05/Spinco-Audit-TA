"""Revision f1d0c9e84a27 (upload_attempts, processing_jobs) and the read-only central schema gate, on DISPOSABLE SQLite files only (tests/migration_harness.py refuses
anything else). No real database, credential, Google call or company document is involved; every row is synthetic."""

import pytest
import sqlalchemy as sa

import migration_harness as h
from app.services.drive import schema as drive_schema

# Reuse the disposable-database fixtures of the previous revision's tests (they build the same template databases).
from test_drive_schema_migration import base_db, legacy_db, ok, report, run_dir, template_db  # noqa: F401

pytestmark = pytest.mark.skipif(not h.root_available(), reason="the approved disposable-database folder is not available on this machine")

CENTRAL_REVISION = "f1d0c9e84a27"


def central_report(path):
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            return drive_schema.inspect_drive_schema(connection, drive_schema.CENTRAL_TABLES)
    finally:
        engine.dispose()


def to_head(path):
    return h.alembic(path, "upgrade", "head")


def test_an_existing_installation_gets_both_tables_and_loses_nothing(legacy_db):
    ok(h.upgrade_to_new(legacy_db))
    assert central_report(legacy_db).status == drive_schema.ABSENT  # before the revision: the central tables are simply not there
    before = h.fingerprint(legacy_db)
    ok(to_head(legacy_db))
    assert h.current_revision(legacy_db) == CENTRAL_REVISION
    assert {"upload_attempts", "processing_jobs"} <= h.table_names(legacy_db) and central_report(legacy_db).status == drive_schema.OK
    assert h.fingerprint(legacy_db) == before  # every row of the three Drive tables is byte-for-byte unchanged
    assert h.integrity(legacy_db) == ("ok", []) and report(legacy_db).status == drive_schema.OK  # and the Drive gate still agrees


def test_an_alembic_only_database_reaches_the_same_final_shape_as_create_all(base_db, run_dir, template_db):
    import shutil

    ok(to_head(base_db))
    assert central_report(base_db).status == drive_schema.OK
    fresh = run_dir / "created.db"
    shutil.copy2(template_db, fresh)
    ok(h.create_all_with_current_models(fresh))
    for table in ("upload_attempts", "processing_jobs"):
        assert h.columns(fresh, table) == h.columns(base_db, table), table
        assert h.index_names(fresh, table) == h.index_names(base_db, table), table


def test_when_create_all_built_the_tables_first_the_revision_changes_nothing(base_db):
    ok(h.create_all_with_current_models(base_db))
    assert central_report(base_db).status == drive_schema.OK
    before = h.schema_text(base_db)
    ok(to_head(base_db))
    assert h.schema_text(base_db) == before and h.current_revision(base_db) == CENTRAL_REVISION


def test_the_revision_refuses_a_table_that_exists_with_other_columns_and_changes_nothing(legacy_db):
    ok(h.upgrade_to_new(legacy_db))
    with h.connect(str(legacy_db)) as con:
        con.execute("create table upload_attempts (id INTEGER NOT NULL PRIMARY KEY, surprise TEXT)")
    before = h.schema_text(legacy_db)
    result = to_head(legacy_db)
    assert result.returncode != 0 and "Refusing to migrate" in h.output(result)
    assert h.schema_text(legacy_db) == before and h.current_revision(legacy_db) == h.NEW_REVISION  # nothing created, the revision was not recorded
    assert central_report(legacy_db).status == drive_schema.DRIFT  # and the gate keeps refusing it


def test_the_downgrade_never_removes_upload_history(legacy_db):
    ok(to_head(legacy_db))
    with h.connect(str(legacy_db)) as con:
        con.execute("insert into upload_attempts (uploaded_at_utc, original_filename, file_size, document_type, result, retained_copy) "
                    "values ('2026-11-06 10:00:00', 'x.pdf', 10, 'EXPENSE_REPORT', 'DUPLICATE', 0)")
    ok(h.alembic(legacy_db, "downgrade", h.NEW_REVISION))
    assert h.current_revision(legacy_db) == h.NEW_REVISION
    assert h.count(legacy_db, "upload_attempts") == 1 and "processing_jobs" in h.table_names(legacy_db)  # history is never lost by a downgrade
    ok(to_head(legacy_db))  # and the revision can be applied again
    assert h.count(legacy_db, "upload_attempts") == 1 and central_report(legacy_db).status == drive_schema.OK


def test_the_central_gate_reports_every_kind_of_difference_and_never_repairs(legacy_db):
    ok(to_head(legacy_db))
    assert central_report(legacy_db).ready
    with h.connect(str(legacy_db)) as con:
        con.execute("alter table processing_jobs add column surprise VARCHAR(5)")
    broken = central_report(legacy_db)
    assert broken.status == drive_schema.DRIFT and any("surprise" in p for p in broken.problems)
    with h.connect(str(legacy_db)) as con:
        con.execute("drop index ix_upload_attempts_sha256")
    assert any("ix_upload_attempts_sha256" in p for p in central_report(legacy_db).problems)
    with h.connect(str(legacy_db)) as con:
        con.execute("drop table processing_jobs")
    partial = central_report(legacy_db)
    assert partial.status == drive_schema.DRIFT and "only some" in partial.problems[0]
    assert "processing_jobs" not in h.table_names(legacy_db)  # the inspector only reads: it did not recreate the table


def test_offline_sql_mode_is_refused_for_the_central_revision(run_dir):
    result = h.alembic(run_dir / "o.db", "upgrade", f"{h.NEW_REVISION}:{CENTRAL_REVISION}", "--sql")
    assert result.returncode != 0 and "offline" in h.output(result).lower()


def test_the_central_tables_have_the_columns_and_constraints_the_application_relies_on(base_db):
    ok(to_head(base_db))
    attempts = {c[0] for c in h.columns(base_db, "upload_attempts")}
    assert {"uploaded_at_utc", "original_filename", "uploaded_by_id", "file_size", "sha256", "document_type", "client_id", "result", "canonical_kind", "canonical_pk",
            "duplicate_reason", "storage_record_id", "retained_copy"} <= attempts
    assert ("document_kind", "document_pk", "job_type") in h.unique_column_sets(base_db, "processing_jobs")  # one job per document and kind of work
