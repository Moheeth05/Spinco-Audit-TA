from pydantic import BaseModel


class ReportStatusSummary(BaseModel):
    total_reports: int
    account_verified_reports: int
    exception_reports: int
    review_required_reports: int
    total_claimed_amount: float
    total_expenses: int


class MonthlyVolumePoint(BaseModel):
    month: str
    month_label: str
    expense_reports: int
    trip_reports: int


class MonthlyClaimedPoint(BaseModel):
    month: str
    month_label: str
    claimed_amount: float


class CategoryAnalyticsPoint(BaseModel):
    category: str
    expense_count: int
    claimed_amount: float


class ExceptionTypePoint(BaseModel):
    exception_type: str
    severity: str
    count: int


class ExceptionMonthlyPoint(BaseModel):
    month: str
    month_label: str
    exception_count: int


class StatusCountPoint(BaseModel):
    status: str
    count: int


class BillMatchSummary(BaseModel):
    by_status: list[StatusCountPoint]


class TripMatchSummary(BaseModel):
    total_batta_expenses: int
    matched_batta_expenses: int
    unmatched_batta_expenses: int
    by_status: list[StatusCountPoint]


class TripMonthPoint(BaseModel):
    month: str
    month_label: str
    trip_count: int


class TripDestinationPoint(BaseModel):
    destination: str
    trip_count: int


class TripAnalyticsOut(BaseModel):
    total_trips: int
    batta_linked_trips: int
    by_month: list[TripMonthPoint]
    by_destination: list[TripDestinationPoint]


class PolicyLimitAnalysisOut(BaseModel):
    evaluated_fixed_limit: int
    passed_within_limit: int
    over_limit: int
    review_required: int
    not_applicable_actual_basis: int
    over_limit_claimed_amount: float


class EmployeeClaimedPoint(BaseModel):
    employee_name: str
    claimed_amount: float
