from typing import Any

from pydantic import BaseModel

from app.schemas.common import UTCDateTime


class ExcelReportOut(BaseModel):
    report_month: str
    report_month_label: str
    filename: str
    total_reports: int
    total_expenses: int
    total_claimed: float | None
    status: str
    last_updated: UTCDateTime


class ExcelSheetInfo(BaseModel):
    name: str
    row_count: int


class ExcelSheetsOut(BaseModel):
    filename: str
    last_updated: UTCDateTime
    sheets: list[ExcelSheetInfo]


class ExcelSheetPreviewOut(BaseModel):
    sheet: str
    headers: list[str]
    rows: list[list[Any]]
    page: int
    page_size: int
    total_rows: int
    total_pages: int
