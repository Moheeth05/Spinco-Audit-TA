"""Phase 3A.2-B corrections (review F7): a missing or unreadable OAuth token file is classified by the token-hardening rules and its error never carries the
file's path, an account name or the text of the underlying exception - these messages are PERSISTED (last_error, events) and shown to administrators.

  missing file                     -> permanent (parked: a person must authorize)
  not valid / open to other users  -> permanent
  cannot be accessed / read / its permissions cannot be checked right now (locked file, permission refusal, helper timeout) -> TRANSIENT (a deferral: no budget)
  the file becomes readable again  -> the next attempt succeeds (recovery, no manual step)

The file here is a synthetic JSON in a temp folder; nothing is a real credential, and the permission helper (icacls) is replaced where the test is about the READ."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import storage_record as sr
from app.models.logs import AuditLog
from app.models.storage_record import StorageEvent
from app.services.drive import failures as fl
from app.services.drive import oauth
from app.services.drive.auth import DriveAuthPermanentError, DriveAuthTransientError
from app.services.drive.lifecycle import StorageUploadError

from drive_testkit import make_stack, pdf_bytes

KIND = "expense_report"
SECRET = "SYNTHETIC-REFRESH-TOKEN-must-never-leak"


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def write_token(path, **overrides):
    body = {"version": oauth.TOKEN_FILE_VERSION, "type": "authorized_user", "client_id": "synthetic-client", "scope": "https://www.googleapis.com/auth/drive",
            "created_at": "2026-11-01T00:00:00Z", "refresh_token": SECRET}
    body.update(overrides)
    path.write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A token store whose permission check passes (the helper that reads Windows ACLs is its own tested unit)."""
    monkeypatch.setattr(oauth, "_permission_check", lambda path, subject, timeout: (None, False))
    return oauth.OAuthTokenStore(tmp_path / "private-folder-name" / "synthetic-token-file.json")


def clean(error, store):
    text = str(error) + repr(error)
    for fragment in (str(store.path), str(store.path.parent), store.path.name, store.path.parent.name, str(store.path.parent.parent), SECRET):
        assert fragment not in text, fragment
    return text


# ------------------------------------------------------------------------------------------------------------------------------ at the store


def test_a_missing_token_file_is_permanent_and_names_no_path(store):
    with pytest.raises(DriveAuthPermanentError) as caught:
        store.load()
    assert "No stored Google authorization" in clean(caught.value, store) and oauth.REAUTHORIZE_HINT in str(caught.value)


def test_a_token_file_that_cannot_even_be_looked_up_is_transient(store, monkeypatch):
    store.path.parent.mkdir()
    write_token(store.path)
    real = type(store.path).is_file

    def refuse(self):
        if self.name == store.path.name:
            raise PermissionError(13, "Access is denied", str(self))
        return real(self)

    monkeypatch.setattr(type(store.path), "is_file", refuse)
    with pytest.raises(DriveAuthTransientError) as caught:
        store.load()
    clean(caught.value, store)
    assert "Access is denied" not in str(caught.value)  # the text of the underlying exception is not repeated either


def test_a_refused_read_is_transient_and_the_file_becomes_usable_when_the_refusal_passes(store, monkeypatch):
    store.path.parent.mkdir()
    write_token(store.path)
    real = type(store.path).read_text
    state = {"refuse": True}

    def read_text(self, *args, **kwargs):
        if self.name == store.path.name and state["refuse"]:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(type(store.path), "read_text", read_text)
    with pytest.raises(DriveAuthTransientError) as caught:  # permission refusal / sharing violation: nothing is wrong with the CONTENT
        store.load()
    text = clean(caught.value, store)
    assert "being used by another process" not in text and "retried automatically" in text
    state["refuse"] = False  # the antivirus scan, the other process, the ACL hiccup is over
    token = store.load()  # recovery needs no manual step: the same store works again
    assert token.refresh_token == SECRET and token.client_id == "synthetic-client"


def test_a_file_removed_between_the_checks_and_the_read_is_permanent(store, monkeypatch):
    store.path.parent.mkdir()
    write_token(store.path)

    def vanish(self, *args, **kwargs):
        raise FileNotFoundError(2, "No such file", str(self))

    monkeypatch.setattr(type(store.path), "read_text", vanish)
    with pytest.raises(DriveAuthPermanentError) as caught:
        store.load()
    clean(caught.value, store)


def test_an_unreadable_permission_listing_is_transient_without_the_path(tmp_path, monkeypatch):
    store = oauth.OAuthTokenStore(tmp_path / "private-folder-name" / "synthetic-token-file.json")
    store.path.parent.mkdir()
    write_token(store.path)
    monkeypatch.setattr(oauth, "_permission_check", lambda path, subject, timeout: (f"The permissions of {path} could not be read.", True))
    with pytest.raises(DriveAuthTransientError) as caught:
        store.load()
    clean(caught.value, store)


def test_a_file_open_to_other_accounts_is_permanent_and_names_neither_path_nor_accounts(tmp_path, monkeypatch):
    store = oauth.OAuthTokenStore(tmp_path / "private-folder-name" / "synthetic-token-file.json")
    store.path.parent.mkdir()
    write_token(store.path)
    monkeypatch.setattr(oauth, "_permission_check", lambda path, subject, timeout: (f"{path} is accessible to other accounts (BUILTIN\\Users, Everyone); only ...", False))
    with pytest.raises(DriveAuthPermanentError) as caught:
        store.load()
    text = clean(caught.value, store)
    assert "Everyone" not in text and "BUILTIN" not in text and "other users" in text
    # the operator-facing detail stays available to the command-line tools, which call permission_problem() themselves (unchanged)


@pytest.mark.parametrize("content", ["{not json " + SECRET, json.dumps({"version": 99, "refresh_token": SECRET}), json.dumps(["list", SECRET]), ""])
def test_an_invalid_token_file_is_permanent_and_never_echoes_its_content(store, content):
    store.path.parent.mkdir()
    store.path.write_text(content, encoding="utf-8")
    with pytest.raises(DriveAuthPermanentError) as caught:
        store.load()
    clean(caught.value, store)


def test_writing_the_token_reports_without_paths(tmp_path):
    missing_folder = oauth.OAuthTokenStore(tmp_path / "no-such-folder-xyz" / "t.json")
    with pytest.raises(oauth.DriveAuthError) as caught:
        missing_folder.save_new(oauth.StoredToken(refresh_token=SECRET, client_id="c", scope="s", created_at="now"))
    assert "no-such-folder-xyz" not in str(caught.value) and str(tmp_path) not in str(caught.value)
    existing = oauth.OAuthTokenStore(tmp_path / "t2.json")
    existing.path.write_text("{}", encoding="utf-8")
    with pytest.raises(oauth.DriveAuthError) as caught:
        existing.save_new(oauth.StoredToken(refresh_token=SECRET, client_id="c", scope="s", created_at="now"))
    assert "t2.json" not in str(caught.value) and str(tmp_path) not in str(caught.value) and "never overwritten" in str(caught.value)


# ------------------------------------------------------------------------------------------------ through the storage manager (what is persisted)


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def blob_of(db, record):
    parts = [record.last_error or ""] + [e.detail or "" for e in db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id)).scalars()]
    parts += [a.details or "" for a in db.execute(select(AuditLog)).scalars()]
    return " ".join(parts)


def caught(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the exception is the thing being passed to the manager
        return exc
    raise AssertionError("expected an error")


def test_a_missing_token_file_parks_the_record_and_nothing_persisted_names_the_path(tmp_path, db, store):
    clock = Clock()
    stack = make_stack(tmp_path, now=clock, manager_options={"rng": lambda: 0.5})
    stack.manager.initialise_structure()
    stack.storage.upload = lambda *a, **k: (_ for _ in ()).throw(caught(store.load))  # the upload's sign-in step fails the way the real provider's does
    with pytest.raises(StorageUploadError) as first:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("tok"), original_filename="t.pdf")
    record = first.value.record
    assert record.failure_class == fl.AUTH_PERMANENT and record.attempts == 0 and record.next_attempt_at is None and not first.value.retryable
    blob = blob_of(db, record)
    for fragment in (str(tmp_path), "private-folder-name", "synthetic-token-file"):
        assert fragment not in blob, fragment


def test_a_refused_token_read_defers_without_budget_and_recovers_by_itself(tmp_path, db, store, monkeypatch):
    clock = Clock()
    stack = make_stack(tmp_path, now=clock, manager_options={"rng": lambda: 0.5})
    stack.manager.initialise_structure()
    store.path.parent.mkdir()
    write_token(store.path)
    real_read = type(store.path).read_text
    state = {"refuse": True}

    def read_text(self, *args, **kwargs):
        if self.name == store.path.name and state["refuse"]:
            raise PermissionError(13, "Access is denied", str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(type(store.path), "read_text", read_text)
    real_upload = stack.storage.upload

    def upload(*args, **kwargs):
        store.load()  # the sign-in step; raises while the file cannot be read
        return real_upload(*args, **kwargs)

    stack.storage.upload = upload
    with pytest.raises(StorageUploadError) as first:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("tok2"), original_filename="t.pdf")
    record = first.value.record
    assert first.value.retryable and record.state == sr.UPLOADING and record.attempts == 0 and record.deferrals == 1 and record.failure_class == fl.AUTH_DEFERRAL
    assert str(tmp_path) not in blob_of(db, record) and "Access is denied" not in blob_of(db, record) and "synthetic-token-file" not in blob_of(db, record)
    for _ in range(3):  # however long it lasts, it is a deferral: never FAILED, no budget
        clock.advance(4000)
        stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.UPLOADING and record.attempts == 0 and record.deferrals == 4
    state["refuse"] = False  # the refusal passes
    clock.advance(4000)
    stack.manager.reconcile(db, stale_after_seconds=0)
    db.refresh(record)
    assert record.state == sr.STORED and record.failure_class is None and stack.fake.file_count() == 1  # recovered with no manual step


# ======================================================================= Step 0 / N4: a token file with invalid encoding is a permanent, path-free error

INVALID_BYTES = {
    "lone continuation byte": b"\x80",
    "bom-less utf-16": '{"version": 1}'.encode("utf-16-le"),
    "truncated multibyte": b'{"refresh_token": "' + "é".encode("utf-8")[:1] + b'"}',
    "binary junk": bytes(range(128, 256)) + SECRET.encode(),
    "latin-1 text": '{"client_id": "caf\xe9"}'.encode("latin-1"),
}


@pytest.mark.parametrize("raw", list(INVALID_BYTES.values()), ids=list(INVALID_BYTES))
def test_a_token_file_with_invalid_encoding_is_permanent_and_leaks_nothing(store, raw):
    store.path.parent.mkdir()
    store.path.write_bytes(raw)
    with pytest.raises(DriveAuthPermanentError) as caught:  # NOT a UnicodeDecodeError, and NOT transient
        store.load()
    assert not isinstance(caught.value, DriveAuthTransientError)
    text = clean(caught.value, store)
    for fragment in ("utf-8", "codec", "decode", "byte 0x", "position", "invalid start", "0x80"):
        assert fragment.lower() not in text.lower(), fragment  # nothing of the underlying exception (it names bytes and offsets)
    assert "not valid" in text and oauth.REAUTHORIZE_HINT in text


def test_absurdly_nested_json_is_a_permanent_invalid_file_not_a_crash(store):
    store.path.parent.mkdir()
    store.path.write_text("[" * 200_000, encoding="utf-8")  # json.loads raises RecursionError on this
    with pytest.raises(DriveAuthPermanentError) as caught:
        store.load()
    clean(caught.value, store)


def test_a_valid_token_file_still_loads_including_non_ascii_text_in_optional_fields(store):
    store.path.parent.mkdir()
    body = {"version": oauth.TOKEN_FILE_VERSION, "type": "authorized_user", "client_id": "synthetic-client", "scope": "https://www.googleapis.com/auth/drive",
            "created_at": "2026-11-01T00:00:00Z \u00e9\u4e2d", "refresh_token": SECRET}
    store.path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")  # real non-ASCII UTF-8 bytes on disk: valid, must not be mistaken for invalid
    assert any(b > 127 for b in store.path.read_bytes())
    token = store.load()
    assert token.refresh_token == SECRET and token.client_id == "synthetic-client" and token.scope.endswith("/auth/drive")
    assert "SYNTHETIC" not in repr(token) and "<hidden>" in repr(token)  # the refresh token is never in a repr


def test_a_permission_helper_whose_output_cannot_be_decoded_is_transient_without_detail(tmp_path, monkeypatch):
    store = oauth.OAuthTokenStore(tmp_path / "private-folder-name" / "synthetic-token-file.json")
    store.path.parent.mkdir()
    write_token(store.path)
    monkeypatch.setattr(oauth, "_IS_WINDOWS", True)
    monkeypatch.setattr(oauth, "_windows_user", lambda timeout=30.0: ("DOMAIN\\someone", "S-1-5-21-0"))

    def undecodable(args, timeout=30.0):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(oauth, "_run", undecodable)
    problem, transient = oauth._permission_check(store.path, "an OAuth token file", 5.0)
    assert transient and problem  # unreadable now, not wrong
    with pytest.raises(DriveAuthTransientError) as caught:
        store.load()
    text = clean(caught.value, store)
    assert "invalid start byte" not in text and "retried automatically" in text


def test_an_invalid_encoding_parks_the_record_and_never_uses_the_retry_budget(tmp_path, db, store):
    clock = Clock()
    stack = make_stack(tmp_path, now=clock, max_upload_attempts=2, manager_options={"rng": lambda: 0.5})  # a budget of TWO
    stack.manager.initialise_structure()
    store.path.parent.mkdir()
    store.path.write_bytes(b"\xff\xfe\x00 not utf-8 \x80\x81 " + SECRET.encode())
    calls = []

    def upload(*args, **kwargs):
        calls.append(1)
        store.load()  # the sign-in step: raises the file's typed error every time it is asked
        raise AssertionError("the token file must not load")

    stack.storage.upload = upload
    with pytest.raises(StorageUploadError) as first:
        stack.manager.store(db, kind=KIND, content=pdf_bytes("badenc"), original_filename="b.pdf")
    record = first.value.record
    assert not first.value.retryable and record.state == sr.UPLOADING and record.failure_class == fl.AUTH_PERMANENT  # parked for an administrator
    assert record.attempts == 0 and record.lease_owner is None and record.next_attempt_at is None  # NO budget used, lease cleared
    for _ in range(6):  # any number of reconciles / resubmissions, far beyond the budget of two: nothing is retried, nothing is charged
        clock.advance(10**5)
        stack.manager.reconcile(db, stale_after_seconds=0)
        stack.manager.store(db, kind=KIND, content=pdf_bytes("badenc"), original_filename="b.pdf")
    db.refresh(record)
    assert len(calls) == 1 and record.state == sr.UPLOADING and record.attempts == 0 and record.failure_class == fl.AUTH_PERMANENT
    blob = blob_of(db, record)
    for fragment in (str(tmp_path), "private-folder-name", "synthetic-token-file", "codec", "utf-8", "0xff", "0x80", SECRET):
        assert fragment not in blob, fragment  # nothing persisted names the path, the bytes or the decoder's complaint
    assert len(db.execute(select(StorageEvent).where(StorageEvent.record_id == record.id, StorageEvent.event_type == "UPLOAD_PARKED_AUTH")).scalars().all()) == 1