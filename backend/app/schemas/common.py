"""Shared field types for API schemas.

UTCDateTime fixes a real timezone bug: every system-generated timestamp in
this app (ExpenseReport.uploaded_at, AuditLog.created_at, ExceptionRecord.
verified_at, etc.) is written with `datetime.now(timezone.utc)`, but SQLite
has no timezone-aware storage - SQLAlchemy round-trips the value back as a
*naive* datetime that is UTC in fact but carries no marker saying so. A
plain Pydantic `datetime` field then serializes that naive value to JSON
with no "Z"/offset suffix (e.g. "2026-09-24T09:31:54"), and a browser's
`new Date(...)` treats an offset-less ISO string as LOCAL time, not UTC -
so a 09:31 UTC audit event was rendered as if it were 09:31 IST instead of
the correct 15:01 IST. UTCDateTime attaches the UTC tzinfo that's true by
construction but was lost on the DB round-trip, so the JSON payload always
carries an explicit "+00:00"/"Z" the frontend can parse correctly.

Never applied to expense/bill/trip *business* dates (Expense.expense_date,
Bill.bill_date, TripReport.start_date, etc.) - those are plain calendar
dates the source document states verbatim and must never be timezone-
shifted; only genuinely timestamped system events use this type.
"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer


def _assume_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


UTCDateTime = Annotated[
    datetime,
    PlainSerializer(lambda v: _assume_utc(v).isoformat(), return_type=str, when_used="json"),
]
