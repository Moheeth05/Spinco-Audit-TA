"""Phase 3A.2-B: the READ-ONLY schema gate. Drive storage runs only when the Drive tables are exactly in the shape the code needs; otherwise it answers
503 and says a migration is needed. The gate never creates, alters or repairs anything (the schema changes only when an operator runs Alembic), and the
application's start-up behaviour is untouched. FAKE/EMPTY databases only; no Drive and no network."""

import inspect
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

import app.main as app_main
from app.database import Base, engine
from app.services import db_migrations
from app.services.drive import runtime
from app.services.drive import schema as drive_schema

RETRY_COLUMNS = drive_schema.RETRY_COLUMNS


@pytest.fixture(autouse=True)
def _fresh_gate_cache():
    drive_schema.reset_schema_cache()
    yield
    drive_schema.reset_schema_cache()  # a negative answer must never leak into another test module
    runtime.reset_cache()


@pytest.fixture()
def migrated(client):
    """The test database as the application creates it (create_all of the current models): the retry columns exist."""
    return client


def make_unmigrated():
    """Takes the retry columns away: the shape every database had before revision c3a9f0b17d42."""
    with engine.begin() as connection:
        for column in RETRY_COLUMNS:
            connection.execute(sa.text(f"ALTER TABLE document_storage DROP COLUMN {column}"))
    engine.dispose()  # SQLite quirk: a pooled connection that did not run the DDL still holds the OLD schema, and a later ALTER on it is judged against that
    drive_schema.reset_schema_cache()


def execute(*statements: str):
    with engine.begin() as connection:
        for sql in statements:
            connection.execute(sa.text(sql))
    engine.dispose()  # (see make_unmigrated)
    drive_schema.reset_schema_cache()


def report():
    return drive_schema.inspect_drive_schema(engine)


# --------------------------------------------------------------------------------------------------------------------------- the inspector


def test_a_database_built_from_the_current_models_is_ready(migrated):
    assert report() == drive_schema.SchemaReport(drive_schema.OK) and report().ready


def test_the_pre_change_shape_needs_the_migration_and_is_not_ready(migrated):
    make_unmigrated()
    result = report()
    assert result.status == drive_schema.NEEDS_MIGRATION and not result.ready
    assert len(result.problems) == len(RETRY_COLUMNS) and all("not added yet" in p for p in result.problems)


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE document_storage ADD COLUMN surprise VARCHAR(5)",  # a column the code does not know
        "ALTER TABLE storage_events ADD COLUMN note VARCHAR(5)",
        "DROP INDEX ix_document_storage_kind",  # an index that should exist is gone
        "DROP INDEX ix_storage_events_record_id",
        "ALTER TABLE document_storage DROP COLUMN md5",  # a column that should exist is gone
        "DROP TABLE drive_sync_state",  # only some tables
    ],
    ids=["extra column", "extra events column", "missing index", "missing events index", "missing column", "missing table"],
)
def test_any_unexpected_difference_is_drift_and_is_not_ready(migrated, sql):
    execute(sql)
    result = report()
    assert result.status == drive_schema.DRIFT and not result.ready and result.problems


@pytest.mark.parametrize(
    "statements",
    [
        ("DROP INDEX ix_document_storage_kind", "CREATE INDEX ix_document_storage_kind ON document_storage (md5)"),  # an index on the WRONG COLUMN
        ("DROP INDEX ix_document_storage_state", "CREATE UNIQUE INDEX ix_document_storage_state ON document_storage (state)"),  # an unexpected UNIQUE index
        (  # MISSING FOREIGN KEYS
            "DROP INDEX ix_storage_events_event_type", "DROP INDEX ix_storage_events_record_id", "ALTER TABLE storage_events RENAME TO events_old",
            "CREATE TABLE storage_events (id INTEGER NOT NULL, record_id INTEGER, event_type VARCHAR(60) NOT NULL, detail VARCHAR(1000), actor_id INTEGER, "
            "created_at DATETIME NOT NULL, PRIMARY KEY (id))",
            "INSERT INTO storage_events SELECT * FROM events_old", "DROP TABLE events_old",
            "CREATE INDEX ix_storage_events_event_type ON storage_events (event_type)", "CREATE INDEX ix_storage_events_record_id ON storage_events (record_id)",
        ),
        ("ALTER TABLE document_storage DROP COLUMN deferrals", "ALTER TABLE document_storage ADD COLUMN deferrals INTEGER NOT NULL DEFAULT 5"),  # a wrong default
    ],
    ids=["index on the wrong column", "unexpected unique index", "missing foreign keys", "wrong default"],
)
def test_the_three_reproduced_review_cases_and_a_wrong_default_are_unhealthy_for_the_gate(migrated, statements):
    assert report().ready
    execute(*statements)
    result = report()
    assert result.status == drive_schema.DRIFT and not result.ready and result.problems
    with pytest.raises(HTTPException) as refused:
        runtime.schema_gate()
    assert refused.value.status_code == 503
    assert migrated.get("/api/storage/status").json()["schema_ok"] is False  # the administrator's diagnostic says so too


def test_a_retry_column_of_the_wrong_type_is_drift_not_a_pending_migration(migrated):
    make_unmigrated()
    execute("ALTER TABLE document_storage ADD COLUMN lease_owner INTEGER")
    assert report().status == drive_schema.DRIFT


def test_no_drive_tables_at_all_is_reported_as_absent_and_not_ready(migrated):
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE storage_events"))
        connection.execute(sa.text("DROP TABLE document_storage"))
        connection.execute(sa.text("DROP TABLE drive_sync_state"))
    assert report().status == drive_schema.ABSENT and not report().ready


def test_the_inspector_and_the_gate_only_ever_read(migrated):
    seen = []
    listener = lambda conn, cursor, statement, parameters, context, executemany: seen.append(statement)  # noqa: E731
    for prepare in (lambda: None, make_unmigrated):  # on a ready database and on one that is not
        prepare()  # (the test's own set-up DDL is NOT part of what is observed)
        drive_schema.reset_schema_cache()
        sa.event.listen(engine, "before_cursor_execute", listener)
        try:
            report()
            drive_schema.schema_report(engine)
            try:
                runtime.schema_gate()
            except HTTPException:
                pass
        finally:
            sa.event.remove(engine, "before_cursor_execute", listener)
    assert seen, "the inspector must have asked the database something"
    writes = [s for s in seen if s.lstrip().split(None, 1)[0].upper() not in ("SELECT", "PRAGMA")]
    assert writes == []  # no DDL, no INSERT/UPDATE/DELETE: the gate cannot create, alter or repair anything
    assert not set(RETRY_COLUMNS) & {c["name"] for c in sa.inspect(engine).get_columns("document_storage")}  # and the database is still un-migrated afterwards


def test_a_positive_answer_is_cached_briefly_and_a_refusal_is_rechecked_soon(migrated, monkeypatch):
    calls = []
    real = drive_schema.inspect_drive_schema
    monkeypatch.setattr(drive_schema, "inspect_drive_schema", lambda bind: calls.append(1) or real(bind))
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    assert drive_schema.schema_report(engine, clock=clock).ready and drive_schema.schema_report(engine, clock=clock).ready and len(calls) == 1  # cached
    now[0] += drive_schema.POSITIVE_TTL_SECONDS + 1
    assert drive_schema.schema_report(engine, clock=clock).ready and len(calls) == 2  # looked at again after the cache time
    make_unmigrated()
    now[0] += drive_schema.POSITIVE_TTL_SECONDS + 1
    assert not drive_schema.schema_report(engine, clock=clock).ready and len(calls) == 3
    assert not drive_schema.schema_report(engine, clock=clock).ready and len(calls) == 3  # the refusal is cached too, for a few seconds only
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)  # the schema is repaired (what applying the migration achieves)
    now[0] += drive_schema.NEGATIVE_TTL_SECONDS + 1
    assert drive_schema.schema_report(engine, clock=clock).ready  # picked up without a restart


# --------------------------------------------------------------------------------------------------------------------------- the endpoints


def test_status_says_whether_the_schema_is_ready_without_being_blocked_by_it(migrated):
    ok = migrated.get("/api/storage/status")
    assert ok.status_code == 200 and ok.json()["schema_ok"] is True and ok.json()["schema_status"] == "ok"
    make_unmigrated()
    behind = migrated.get("/api/storage/status")
    assert behind.status_code == 200 and behind.json()["schema_ok"] is False and behind.json()["schema_status"] == "needs_migration"
    assert set(behind.json()["counts"]) == {"UPLOADING", "STORED", "ARCHIVED", "MISSING_EXTERNAL", "FAILED"}  # the counts only need `state`: still available
    drive_schema.reset_schema_cache()
    execute("ALTER TABLE document_storage ADD COLUMN surprise VARCHAR(5)")
    assert migrated.get("/api/storage/status").json()["schema_status"] == "drift"


def test_the_record_listing_is_refused_with_a_clear_message_until_the_schema_is_ready(migrated):
    assert migrated.get("/api/storage/records").status_code == 200
    make_unmigrated()
    refused = migrated.get("/api/storage/records")  # the administrator (the fixture's user) is told what to do
    assert refused.status_code == 503 and refused.json()["detail"] == drive_schema.NOT_READY_TEXT
    assert "migration" in refused.json()["detail"] and "c3a9f0b17d42" in refused.json()["detail"]
    migrated.cookies.clear()
    assert migrated.get("/api/storage/records").status_code == 401  # authentication still comes first: a stranger learns nothing about the schema


def make_viewer(client):
    response = client.post("/api/users", json={"name": "Viewer", "email": "viewer.gate@example.com", "role": "VIEWER", "password": "SyntheticPass123!"})
    assert response.status_code == 201, response.text
    return "viewer.gate@example.com"


def login(client, email, password="SyntheticPass123!"):
    client.cookies.clear()
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    client.csrf_token = response.json()["csrf_token"]


def test_a_user_who_is_not_allowed_to_see_a_listing_gets_access_denied_never_schema_details(migrated):
    """F10: the order is authentication -> authorisation -> gate. With the schema behind, a viewer asking for administrator-only records is refused with
    403 (not the 503 that names the migration revision), and a permitted viewer gets a generic 503 without any migration detail."""
    email = make_viewer(migrated)
    make_unmigrated()
    login(migrated, email)
    denied = migrated.get("/api/storage/records?state=FAILED")
    assert denied.status_code == 403 and "c3a9f0b17d42" not in denied.text and "migration" not in denied.text.lower()
    assert migrated.get("/api/storage/records?state=all").status_code == 403
    assert migrated.get("/api/storage/records?state=NOT_A_STATE").status_code == 400  # (unchanged: validation before anything else)
    allowed = migrated.get("/api/storage/records?state=STORED")  # a viewer may list active documents: refused only because storage is unavailable
    assert allowed.status_code == 503 and allowed.json()["detail"] == drive_schema.GENERIC_UNAVAILABLE_TEXT
    assert "c3a9f0b17d42" not in allowed.text and "migration" not in allowed.text.lower() and "schema" not in allowed.text.lower()
    status = migrated.get("/api/storage/status")
    assert status.status_code == 403 and "schema" not in status.text.lower()  # the diagnostics endpoint is administrators only
    migrated.cookies.clear()
    assert migrated.get("/api/storage/records?state=FAILED").status_code == 401


def test_a_failed_inspection_is_a_clean_503_not_a_500(migrated, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def broken(_bind):
        raise OperationalError("select", {}, Exception("database is locked"))

    monkeypatch.setattr(drive_schema, "inspect_drive_schema", broken)
    drive_schema.reset_schema_cache()
    result = drive_schema.schema_report(engine)
    assert result.status == drive_schema.UNKNOWN and not result.ready and "locked" not in " ".join(result.problems)
    response = migrated.get("/api/storage/records")
    assert response.status_code == 503 and "locked" not in response.text
    with pytest.raises(HTTPException) as refused:
        runtime.schema_gate()
    assert refused.value.status_code == 503


def test_the_manager_dependency_refuses_before_anything_is_built_or_contacted(migrated, monkeypatch):
    monkeypatch.setenv("SPINCO_GDRIVE_ENABLED", "true")
    built = []
    monkeypatch.setattr(runtime, "build_manager", lambda *a, **k: built.append(1) or SimpleNamespace(drive=SimpleNamespace(client=SimpleNamespace(write_guard=SimpleNamespace(try_verify=lambda: None)))))
    make_unmigrated()
    with pytest.raises(HTTPException) as refused:
        runtime.get_storage_manager()
    assert refused.value.status_code == 503 and refused.value.detail == drive_schema.GENERIC_UNAVAILABLE_TEXT and built == []  # no manager, no credentials read, no Google contact
    assert "c3a9f0b17d42" not in refused.value.detail  # code that does not know who is asking gives no schema detail; an administrator reads /status
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    drive_schema.reset_schema_cache()
    assert runtime.get_storage_manager() is not None and built == [1]  # ready: the normal path


def test_when_drive_is_not_enabled_that_answer_still_comes_first(migrated, monkeypatch):
    monkeypatch.delenv("SPINCO_GDRIVE_ENABLED", raising=False)
    make_unmigrated()
    with pytest.raises(HTTPException) as refused:
        runtime.get_storage_manager()
    assert refused.value.detail == "Drive storage is not enabled."


# --------------------------------------------------------------------------------------------------------------- start-up behaviour is unchanged


def test_start_up_does_not_modify_the_schema_for_this_change():
    """The retry columns are added ONLY by the Alembic revision. The start-up helpers neither add them nor consult the gate."""
    assert not [c for c in RETRY_COLUMNS if c in inspect.getsource(db_migrations)]
    startup = inspect.getsource(app_main)
    assert "create_all" in startup and "run_light_migrations" in startup  # the existing start-up behaviour, as it was
    assert "services.drive.schema" not in startup and "alembic" not in startup.lower()
