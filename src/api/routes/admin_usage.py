"""管理员使用报表查询与完整 Excel 导出。"""

from datetime import date, datetime
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_admin_user
from src.api.models.database import get_db
from src.api.services.admin_operation_audit import AdminAuditRoute, admin_audit_action, enrich_admin_audit
from src.api.services.admin_usage_report import build_usage_report, export_usage_workbook, report_rows, resolve_period

router = APIRouter(route_class=AdminAuditRoute)


@router.get("/usage-report")
@admin_audit_action("usage_report.read", target_type="usage_report")
def get_usage_report(
    start_date: date | None = None,
    end_date: date | None = None,
    as_of: datetime | None = None,
    view: Literal["accounts", "models", "details"] = "accounts",
    sort_by: str = Query("total_tokens", max_length=40),
    direction: Literal["asc", "desc"] = "desc",
    account_filter: str = Query("", max_length=100),
    model_filter: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _admin: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    report = build_usage_report(db, resolve_period(start_date, end_date, as_of))
    rows = report_rows(report, view, sort_by, direction, account_filter.strip(), model_filter.strip())
    page = min(page, max(1, (len(rows) + page_size - 1) // page_size))
    return {"period": report["period"], "note": report["note"], "summary": report["summary"],
            "view": view, "sort_by": sort_by, "direction": direction,
            "account_filter": account_filter.strip(), "model_filter": model_filter.strip(),
            "page": page, "page_size": page_size, "total": len(rows),
            "rows": rows[(page - 1) * page_size:page * page_size]}


@router.get("/usage-report/export")
@admin_audit_action("usage_report.export", target_type="usage_report")
def export_usage_report(
    request: Request,
    start_date: date,
    end_date: date,
    as_of: datetime | None = None,
    _admin: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    report = build_usage_report(db, resolve_period(start_date, end_date, as_of))
    content = export_usage_workbook(report)
    enrich_admin_audit(request, details={"exported_count": sum(len(report[key]) for key in ("accounts", "models", "details"))})
    filename = f"OpenCapyBox使用数据报表_{start_date:%Y%m%d}_{end_date:%Y%m%d}.xlsx"
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                             "Cache-Control": "no-store"})
