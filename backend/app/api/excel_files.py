import math

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.excel import ExcelReport
from app.models.user import User
from app.schemas.excel import ExcelReportOut, ExcelSheetInfo, ExcelSheetPreviewOut, ExcelSheetsOut
from app.services import audit, central_storage
from app.services.excel_generator import SHEET_NAMES, WORKBOOK_FILENAME, generate_workbook
from app.services.permissions import DOWNLOAD_EXCEL, GENERATE_EXCEL, VIEW_EXCEL, require_permission
from app.services.storage import EXCEL_DIR

router = APIRouter(prefix="/api/excel-files", tags=["excel-files"])
_view = Depends(require_permission(VIEW_EXCEL))

MAX_PAGE_SIZE = 500


def _workbook_path(record: ExcelReport):
    from pathlib import Path

    # Always the application's one managed workbook path - never a
    # client-supplied filename/path, so this endpoint can't be used to read
    # arbitrary files (including uploaded PDFs).
    path = EXCEL_DIR / WORKBOOK_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="The workbook file could not be found. Try regenerating it.")
    return path


def _current_record(db: Session) -> ExcelReport:
    record = db.query(ExcelReport).first()
    if record is None:
        raise HTTPException(
            status_code=404, detail="No workbook has been generated yet. Call POST /api/excel-files/generate first."
        )
    return record


@router.get("", response_model=list[ExcelReportOut])
def list_excel_files(db: Session = Depends(get_db), _user: User = _view):
    return db.execute(select(ExcelReport)).scalars().all()


@router.get("/metadata", response_model=ExcelReportOut)
def get_metadata(db: Session = Depends(get_db), _user: User = _view):
    return _current_record(db)


@router.post("/generate", response_model=ExcelReportOut)
def generate(db: Session = Depends(get_db), user: User = Depends(require_permission(GENERATE_EXCEL))):
    try:
        record = generate_workbook(db)
    except Exception:  # noqa: BLE001 - never leak an internal stack trace to the client
        raise HTTPException(status_code=500, detail="Workbook generation failed. See server logs for detail.")
    audit.log_action(db, user_id=user.id, action=audit.EXCEL_GENERATED, entity_type="ExcelReport", entity_id=None)
    db.commit()  # log_action only flushes now - see docs/CENTRALIZED_ARCHITECTURE.md §16
    out = ExcelReportOut.model_validate(record, from_attributes=True)
    if central_storage.config().active:
        # Central modes: also keep a verified copy in the Drive folder "Excel Exports" (best effort; the workbook the users preview and download is this server's).
        try:
            content = _workbook_path(record).read_bytes()
        except (OSError, HTTPException):
            content = None
        out.central_copy = central_storage.store_export(db, content, record.filename, user.id) if content else central_storage.FAILED
    else:
        out.central_copy = central_storage.NOT_CENTRAL
    return out


@router.get("/sheets", response_model=ExcelSheetsOut)
def get_sheets(db: Session = Depends(get_db), _user: User = _view):
    record = _current_record(db)
    path = _workbook_path(record)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = []
        for name in SHEET_NAMES:
            if name not in wb.sheetnames:
                continue
            ws = wb[name]
            count = 0
            for row in ws.iter_rows(min_row=2, values_only=True):
                if any(v is not None for v in row):
                    count += 1
            sheets.append(ExcelSheetInfo(name=name, row_count=count))
    finally:
        wb.close()
    return ExcelSheetsOut(filename=record.filename, last_updated=record.last_updated, sheets=sheets)


@router.get("/sheets/{sheet_name}", response_model=ExcelSheetPreviewOut)
def get_sheet_preview(
    sheet_name: str,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    db: Session = Depends(get_db),
    _user: User = _view,
):
    if sheet_name not in SHEET_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown sheet '{sheet_name}'.")
    if page < 1:
        page = 1
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    record = _current_record(db)
    path = _workbook_path(record)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            raise HTTPException(status_code=404, detail=f"Sheet '{sheet_name}' is not present in the current workbook.")
        ws = wb[sheet_name]
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        headers = [h for h in header_row if h is not None]

        search_lower = search.strip().lower() if search else None
        start = (page - 1) * page_size
        end = start + page_size

        matched_rows: list[list] = []
        total = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            if all(v is None for v in row):
                continue  # skip blank spacer rows (e.g. Summary sheet's section breaks)
            if search_lower:
                row_text = " ".join(str(v) for v in row if v is not None).lower()
                if search_lower not in row_text:
                    continue
            if start <= total < end:
                matched_rows.append(list(row[: len(headers)]))
            total += 1
    finally:
        wb.close()

    total_pages = max(1, math.ceil(total / page_size))
    return ExcelSheetPreviewOut(
        sheet=sheet_name,
        headers=headers,
        rows=matched_rows,
        page=page,
        page_size=page_size,
        total_rows=total,
        total_pages=total_pages,
    )


@router.get("/download")
def download(db: Session = Depends(get_db), user: User = Depends(require_permission(DOWNLOAD_EXCEL))):
    record = _current_record(db)
    path = _workbook_path(record)
    audit.log_action(db, user_id=user.id, action=audit.EXCEL_DOWNLOADED, entity_type="ExcelReport", entity_id=None)
    db.commit()  # log_action only flushes now - see docs/CENTRALIZED_ARCHITECTURE.md §16
    return FileResponse(
        path=path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=record.filename,
        content_disposition_type="attachment",
    )
