from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.excel import ExcelReport
from app.schemas.excel import ExcelReportOut

router = APIRouter(prefix="/api/excel-files", tags=["excel-files"])


@router.get("", response_model=list[ExcelReportOut])
def list_excel_files(db: Session = Depends(get_db)):
    return db.execute(select(ExcelReport).order_by(ExcelReport.report_month.desc())).scalars().all()
