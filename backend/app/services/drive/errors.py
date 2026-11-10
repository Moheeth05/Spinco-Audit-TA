"""The Drive error types, in one dependency-free module so that the client and the destination guard can both use them without importing each
other. app.services.drive.client re-exports every name here, so existing imports keep working."""


class DriveError(Exception):
    # What Google itself said, when the failure was an HTTP answer. Always passed through safe_text(), so it carries no token, e-mail address or key.
    detail = ""  # Google's error message
    google_status = ""  # e.g. PERMISSION_DENIED
    detail_reason = ""  # the machine reason inside `details`, e.g. SERVICE_DISABLED

    def __init__(self, message: str, *, status: int | None = None, reason: str = "", retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retryable = retryable


class DriveNotFound(DriveError): ...
class DrivePermissionError(DriveError): ...
class DriveQuotaError(DriveError): ...
class DriveConflict(DriveError): ...
class DriveTransientError(DriveError): ...
class DriveChangeTokenExpired(DriveError): ...


class DriveIntegrityError(DriveError):
    """Bytes in Drive do not match what was sent (checksum or size). Never retried blindly."""


class DriveIncompleteListing(DriveError):
    """Drive said a listing is incomplete (`incompleteSearch`). A partial answer is never trusted: acting on it could create a duplicate folder or
    miss a duplicate file, so the operation stops instead (fail closed). Retryable: the same listing may be complete next time."""

    def __init__(self):
        super().__init__("Drive reported an incomplete listing, so the result was not trusted.", reason="incomplete_search", retryable=True)


class DriveObjectTrashed(DriveError):
    """The file is in the Drive trash (someone outside the application put it there). Archiving never un-trashes it: only an explicit restore may."""

    def __init__(self):
        super().__init__("The document is in the Drive trash.", status=404, reason="trashed", retryable=False)


class DriveForeignObject(DriveNotFound):
    """The file exists but is no longer in the expected Shared Drive (it was moved out, or the id points at another drive). It is not served,
    archived or restored, and it is never touched: a DriveNotFound for every caller that treats 'not ours' as 'not there'."""

    def __init__(self):
        super().__init__("The document is not in the expected Shared Drive.", status=404, reason="foreign_drive", retryable=False)


class DriveNotADocument(DriveNotFound):
    """The location points at something that is not an ordinary stored document: a folder, a Drive shortcut or a Google Docs/Sheets/Slides item. It is
    never reported as available, served, archived or restored (a DriveNotFound for every caller that treats 'not a document' as 'not there')."""

    def __init__(self):
        super().__init__("The Drive item is not an ordinary document (it is a folder, a shortcut or a Google Docs item).", status=404, reason="not_a_document", retryable=False)


class DriveIdentityMismatch(DriveError):
    """Drive described a DIFFERENT file than the one that was asked for. Nothing it said is used."""

    def __init__(self):
        super().__init__("Drive answered with the description of a different file than the one requested.", reason="identity_mismatch", retryable=False)


class DriveDestinationMissing(DriveError):
    """A folder that an archive/restore needed (its destination) was not found - the document itself was read a moment earlier and is untouched. Retryable:
    the folder cache is dropped, so the next attempt looks the folder up (or creates it, as on first use) again."""

    def __init__(self):
        super().__init__("A Drive folder needed for this operation was not found.", status=404, reason="destination_missing", retryable=True)


class UnguardedClientRefused(Exception):
    """A DriveClient was asked to exist without the destination guard in a situation where that is not allowed. The message is secret-free."""


class DriveRootMismatch(ValueError):
    """The folder structure and the client's destination guard do not refer to the same configured root, so writes could be verified against one
    destination and sent to another."""
