from datetime import datetime
from typing import Any

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


class ExcelSheetInfo(BaseModel):
    name: str
    row_count: int


class ExcelSheetsOut(BaseModel):
    filename: str
    last_updated: datetime
    sheets: list[ExcelSheetInfo]


class ExcelSheetPreviewOut(BaseModel):
    sheet: str
    headers: list[str]
    rows: list[list[Any]]
    page: int
    page_size: int
    total_rows: int
    total_pages: int
