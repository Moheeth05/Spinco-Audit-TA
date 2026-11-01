"""Diagnostics of Drive API refusals: the ORIGINAL Google status / reason / message are captured (sanitised), a Shared Drive root is read with the
right request, and nothing that looks like a credential or an e-mail address survives. Everything runs against the fake Drive: it proves OUR code,
not Google's behaviour (the exact JSON shapes below are modelled on Google's documented error format)."""

import pytest

from app.services.drive.client import DriveConflict, DriveError, DriveNotFound, DrivePermissionError, DriveTransientError, safe_text
from drive_testkit import make_stack

SHARED_DRIVE_ID = "0AC6JZ6F0PQY-Uk9PVA"
PROJECT_NUMBER = "123456789012"  # the Google Cloud project number is NOT a credential and is exactly what a person needs to fix a disabled API


def google_error(status, reason, google_status, *, message="Request refused.", detail_reason=None):
    error = {"code": status, "message": message, "errors": [{"message": message, "domain": "global", "reason": reason}], "status": google_status}
    if detail_reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": detail_reason, "domain": "googleapis.com", "metadata": {"service": "drive.googleapis.com"}}]
    return {"error": error}


LEAKY_MESSAGE = (
    f"Google Drive API has not been used in project {PROJECT_NUMBER} before or it is disabled. Enable it at "
    f"https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project={PROJECT_NUMBER} then retry. "
    "Signed in as ops@spinco.example, Authorization: Bearer ya29.A0AfH6SMBsecretsecretsecretsecretsecret refresh 1//0gSECRETSECRETSECRETSECRET"
)


@pytest.fixture()
def stack(tmp_path):
    return make_stack(tmp_path, drive_id=SHARED_DRIVE_ID)


def root_of(stack):
    return stack.config.root_folder_id


# --------------------------------------------------------------------------------------------------------- Google's answer is captured


def test_a_permission_refusal_carries_googles_status_reason_and_a_safe_message(stack):
    body = google_error(403, "accessNotConfigured", "PERMISSION_DENIED", message=LEAKY_MESSAGE, detail_reason="SERVICE_DISABLED")
    stack.fake.fail_next(1, 403, "accessNotConfigured", body=body)
    with pytest.raises(DrivePermissionError) as caught:
        stack.client.get_raw(root_of(stack), "id")
    exc = caught.value
    assert (exc.status, exc.reason, exc.google_status, exc.detail_reason) == (403, "accessNotConfigured", "PERMISSION_DENIED", "SERVICE_DISABLED")
    assert str(exc) == "Drive API error 403 (accessNotConfigured)"  # the pre-existing message is unchanged
    assert PROJECT_NUMBER in exc.detail and "has not been used" in exc.detail  # the useful part survives ...
    everything = str(exc) + repr(vars(exc))
    for leak in ("ops@", "spinco.example", "Bearer", "ya29", "SECRETSECRET", "1//", "fake-token"):  # ... the credential-shaped parts do not
        assert leak not in everything, leak


def test_not_found_is_reported_with_googles_own_status(stack):
    stack.fake.fail_next(1, 404, "notFound", body=google_error(404, "notFound", "NOT_FOUND", message=f"File not found: {SHARED_DRIVE_ID}."))
    with pytest.raises(DriveNotFound) as caught:
        stack.client.get_raw(root_of(stack), "id")
    assert (caught.value.status, caught.value.reason, caught.value.google_status) == (404, "notFound", "NOT_FOUND") and "File not found" in caught.value.detail


def test_an_invalid_id_is_a_plain_drive_error_with_its_400_reason(stack):
    stack.fake.fail_next(1, 400, "invalid", body=google_error(400, "invalid", "INVALID_ARGUMENT", message="Invalid value for file id."))
    with pytest.raises(DriveError) as caught:
        stack.client.get_raw("not a real id", "id")
    assert type(caught.value) is DriveError and (caught.value.status, caught.value.reason, caught.value.google_status) == (400, "invalid", "INVALID_ARGUMENT")


def test_a_401_that_survives_the_token_refresh_is_reported_as_401_not_as_a_missing_permission(stack):
    stack.fake.fail_next(2, 401, "authError", body=google_error(401, "authError", "UNAUTHENTICATED", message="Invalid Credentials"))
    with pytest.raises(DrivePermissionError) as caught:
        stack.client.get_raw(root_of(stack), "id")
    assert (caught.value.status, caught.value.google_status) == (401, "UNAUTHENTICATED")


def test_a_conflict_and_a_retried_outage_keep_googles_details_too(stack):
    stack.fake.fail_next(1, 409, "conflict", body=google_error(409, "conflict", "ALREADY_EXISTS", message="A file with the given ID already exists."))
    with pytest.raises(DriveConflict) as conflict:
        stack.client.get_raw(root_of(stack), "id")
    assert conflict.value.google_status == "ALREADY_EXISTS"
    stack.fake.fail_next(50, 503, "backendError", body=google_error(503, "backendError", "UNAVAILABLE", message="Backend error."))
    with pytest.raises(DriveTransientError) as outage:
        stack.client.get_raw(root_of(stack), "id")  # all attempts fail
    assert outage.value.google_status == "UNAVAILABLE" and outage.value.status == 503


@pytest.mark.parametrize("body", ["plain text", ["a", "list"], {}, {"error": "just text"}, {"error": {"message": None, "details": "oops"}}, {"error": {"details": [1, None, {"reason": None}]}}])
def test_an_unexpected_error_body_never_breaks_the_diagnostics(stack, body):
    stack.fake.fail_next(1, 403, "forbidden", body=body)
    with pytest.raises(DrivePermissionError) as caught:
        stack.client.get_raw(root_of(stack), "id")
    assert caught.value.status == 403 and isinstance(caught.value.detail, str) and isinstance(caught.value.google_status, str)


@pytest.mark.parametrize(
    ("raw", "must_not_contain", "must_contain"),
    [
        ("mail ops@spinco.example now", "ops@", "mail"),
        ("Authorization: Bearer abc.DEF-123_xyz", "abc.DEF", "[masked]"),
        ("token ya29.A0AfH6SMB-abc_def", "ya29", "token"),
        ("refresh 1//0gAbCdEfGhIjKlMnOp", "1//0g", "refresh"),
        ("jwt eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJ4In0.c2ln done", "eyJhbG", "done"),
        ("client_secret=GOCSPX-abcdef", "GOCSPX", "[masked]"),
        ("opaque " + "A" * 60, "A" * 40, "opaque"),
        (f"project {PROJECT_NUMBER} and folder {SHARED_DRIVE_ID}", "[masked]", PROJECT_NUMBER),  # ids and the project number are kept
        (None, "None", ""),
    ],
)
def test_safe_text_masks_credentials_and_addresses_but_keeps_what_helps(raw, must_not_contain, must_contain):
    cleaned = safe_text(raw)
    assert must_not_contain not in cleaned and must_contain in cleaned


def test_safe_text_caps_the_length():
    assert len(safe_text("word " * 500, 300)) == 300 and len(safe_text("x" * 10, 4)) == 4


# --------------------------------------------------------------------------------------------------------------- the Shared Drive requests


def request_to(stack, path):
    return next(r for r in stack.fake.requests if r["path"] == path)


def test_the_root_is_read_with_files_get_and_supports_all_drives_only(stack):
    stack.client.get_raw(root_of(stack), "id,driveId")
    sent = request_to(stack, f"/drive/v3/files/{root_of(stack)}")
    assert sent["method"] == "GET" and sent["query"]["supportsAllDrives"] == "true" and sent["query"]["fields"] == "id,driveId"
    # files.get has no use for the list-only parameters, and sending a driveId or corpora to it would be wrong
    for list_only in ("corpora", "includeItemsFromAllDrives", "driveId", "q"):
        assert list_only not in sent["query"], list_only
    assert sent["auth"].startswith("Bearer ")  # sent to Google, but never reported (see the harness tests)


def test_listing_children_carries_the_shared_drive_list_parameters(stack):
    stack.client.find_children(root_of(stack))
    sent = next(r for r in stack.fake.requests if r["path"] == "/drive/v3/files")
    assert sent["query"]["supportsAllDrives"] == "true" and sent["query"]["includeItemsFromAllDrives"] == "true" and sent["query"]["corpora"] == "allDrives"


def test_drives_get_asks_about_the_shared_drive_itself(stack):
    info = stack.client.get_shared_drive(SHARED_DRIVE_ID)
    sent = request_to(stack, f"/drive/v3/drives/{SHARED_DRIVE_ID}")
    assert sent["method"] == "GET" and set(sent["query"]) == {"fields"} and info["id"] == SHARED_DRIVE_ID and info["capabilities"]["canAddChildren"] is True


def test_drives_get_on_an_id_that_is_not_a_shared_drive_is_not_found(tmp_path):
    my_drive = make_stack(tmp_path)  # a My Drive style fake: it has no Shared Drive at all
    with pytest.raises(DriveNotFound):
        my_drive.client.get_shared_drive(SHARED_DRIVE_ID)
    (tmp_path / "other").mkdir()
    other = make_stack(tmp_path / "other", drive_id="0AOTHERDRIVE000001")  # a different Shared Drive
    with pytest.raises(DriveNotFound):
        other.client.get_shared_drive(SHARED_DRIVE_ID)
