from pydantic import BaseModel


class SearchResultItem(BaseModel):
    kind: str  # EXPENSE_REPORT | TRIP_REPORT | POLICY_CATEGORY | MERCHANT | EXCEPTION_TYPE
    title: str
    subtitle: str | None
    # Where clicking this result should navigate. A document_id (e.g. "expense:5")
    # takes the user straight to that document's viewer; a bare path (e.g.
    # "/policy-master") is used for results with no single owning document.
    document_id: str | None
    path: str | None
    matched_field: str


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultItem]
