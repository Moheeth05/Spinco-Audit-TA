from datetime import datetime

from pydantic import BaseModel


class ExcelReportOut(BaseModel):
    report_month: str
    report_month_label: str
    filename: str
    total_reports: int
    total_expenses: int
    total_claimed: float | None
    status: str
    last_updated: datetime
