"""Encodes a type + primary key into the opaque document id used by the
generic /api/documents/* endpoints, so callers don't need to know which
underlying table (expense_reports vs trip_reports) a document lives in."""

from dataclasses import dataclass

EXPENSE = "expense"
TRIP = "trip"


@dataclass
class DocumentRef:
    kind: str  # "expense" | "trip"
    pk: int

    def encode(self) -> str:
        return f"{self.kind}:{self.pk}"


class InvalidDocumentId(Exception):
    pass


def decode_document_id(document_id: str) -> DocumentRef:
    parts = document_id.split(":", 1)
    if len(parts) != 2 or parts[0] not in (EXPENSE, TRIP) or not parts[1].isdigit():
        raise InvalidDocumentId(f"'{document_id}' is not a valid document id.")
    return DocumentRef(kind=parts[0], pk=int(parts[1]))
