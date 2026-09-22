from datetime import date

from pydantic import BaseModel, ConfigDict


class TripCandidateSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trip_id: str | None
    is_embedded: bool
    employee_name: str | None
    employee_code: str | None
    start_date: date | None
    end_date: date | None
    destination: str | None
    extraction_status: str
    embedded_source_page: int | None


class TripMatchCandidateOut(BaseModel):
    trip_report_id: int
    rank: int
    score: int
    reasons: str | None
    trip: TripCandidateSummaryOut | None = None


class ExpenseTripMatchOut(BaseModel):
    expense_id: int
    s_no: int | None
    category: str | None
    expense_date: date | None
    claimed_amount: float | None
    trip_match_status: str
    trip_match_score: int | None
    trip_match_confidence: str | None
    trip_match_reasons: str | None
    trip_employee_mismatch: bool
    matched_trip: TripCandidateSummaryOut | None
    candidates: list[TripMatchCandidateOut]


class BattaMatchListItem(BaseModel):
    expense_id: int
    expense_report_id: int
    s_no: int | None
    expense_date: date | None
    claimed_amount: float | None
    is_matched_here: bool  # True if this trip is the winning match, False if only a considered candidate
    trip_match_status: str
    score: int | None
    reasons: str | None
