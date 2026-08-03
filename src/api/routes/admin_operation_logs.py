"""Read-only administrator operation-log APIs."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import or_
from sqlalchemy.orm import Query as SQLQuery
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_admin_user
from src.api.models.admin_operation_log import AdminOperationLog
from src.api.models.database import get_db
from src.api.services.admin_operation_audit import (
    AdminAuditRoute,
    HIGH_RISK_ACTIONS,
    admin_audit_action,
    enrich_admin_audit,
    get_admin_audit_request_id,
)
from src.api.utils.timezone import get_timezone, now_naive

router = APIRouter(route_class=AdminAuditRoute)

_OUTCOMES = frozenset({"started", "succeeded", "failed"})
_RISK_LEVELS = frozenset({"high", "normal"})
_EXPORT_LIMIT = 50_000


def _naive_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(get_timezone()).replace(tzinfo=None)


def _encode_cursor(row: AdminOperationLog) -> str:
    raw = json.dumps(
        {"started_at": row.started_at.isoformat(), "id": int(row.id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        started_at = datetime.fromisoformat(payload["started_at"])
        row_id = int(payload["id"])
        if row_id <= 0:
            raise ValueError("cursor id must be positive")
        return _naive_local(started_at), row_id  # type: ignore[return-value]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="无效的操作日志游标") from exc


def _load_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        # Legacy/corrupt metadata must not break access to the audit trail, and
        # returning the raw text could disclose data that bypassed sanitizers.
        return None


def _log_payload(row: AdminOperationLog) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "request_id": row.request_id,
        "actor_user_id": row.actor_user_id,
        "action": row.action,
        "risk_level": "high" if row.action in HIGH_RISK_ACTIONS else "normal",
        "target_type": row.target_type,
        "target_id": row.target_id,
        "target_user_id": row.target_user_id,
        "session_id": row.session_id,
        "step_record_id": row.step_record_id,
        "outcome": row.outcome,
        "http_method": row.http_method,
        "route_template": row.route_template,
        "status_code": row.status_code,
        "ip_address": row.ip_address,
        "user_agent": row.user_agent,
        "changed_fields": _load_json(row.changed_fields),
        "details": _load_json(row.details_json),
        "started_at": row.started_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def _filtered_query(
    db: DBSession,
    *,
    current_request_id: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
    action: str | None,
    target_user_id: str | None,
    session_id: str | None,
    outcome: str | None,
    risk_level: str | None,
) -> SQLQuery:
    effective_start = _naive_local(start_time) or (now_naive() - timedelta(hours=24))
    effective_end = _naive_local(end_time)
    if effective_end is not None and effective_end < effective_start:
        raise HTTPException(status_code=400, detail="结束时间不能早于开始时间")
    if outcome is not None and outcome not in _OUTCOMES:
        raise HTTPException(status_code=400, detail="无效的操作结果筛选值")
    if risk_level is not None and risk_level not in _RISK_LEVELS:
        raise HTTPException(status_code=400, detail="无效的风险级别筛选值")

    query = db.query(AdminOperationLog).filter(
        AdminOperationLog.started_at >= effective_start
    )
    if effective_end is not None:
        query = query.filter(AdminOperationLog.started_at <= effective_end)
    if action:
        query = query.filter(AdminOperationLog.action == action)
    if target_user_id:
        query = query.filter(AdminOperationLog.target_user_id == target_user_id)
    if session_id:
        query = query.filter(AdminOperationLog.session_id == session_id)
    if outcome:
        query = query.filter(AdminOperationLog.outcome == outcome)
    if risk_level == "high":
        query = query.filter(AdminOperationLog.action.in_(HIGH_RISK_ACTIONS))
    elif risk_level == "normal":
        query = query.filter(~AdminOperationLog.action.in_(HIGH_RISK_ACTIONS))
    if current_request_id:
        query = query.filter(AdminOperationLog.request_id != current_request_id)
    return query


@router.get("/operation-logs")
@admin_audit_action("audit_log.list", target_type="admin_operation_log")
async def list_admin_operation_logs(
    request: Request,
    start_time: datetime | None = Query(None, alias="from"),
    end_time: datetime | None = Query(None, alias="to"),
    action: str | None = Query(None, max_length=100),
    target_user_id: str | None = Query(None, max_length=100),
    session_id: str | None = Query(None, max_length=36),
    outcome: str | None = Query(None, max_length=20),
    risk_level: str | None = Query(None, max_length=20),
    cursor: str | None = Query(None, max_length=500),
    limit: int = Query(50, ge=1, le=200),
    _admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """List newest audit events using a stable ``(started_at, id)`` cursor."""

    query = _filtered_query(
        db,
        current_request_id=get_admin_audit_request_id(request),
        start_time=start_time,
        end_time=end_time,
        action=action,
        target_user_id=target_user_id,
        session_id=session_id,
        outcome=outcome,
        risk_level=risk_level,
    )
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        query = query.filter(
            or_(
                AdminOperationLog.started_at < cursor_time,
                (
                    (AdminOperationLog.started_at == cursor_time)
                    & (AdminOperationLog.id < cursor_id)
                ),
            )
        )

    rows = (
        query.order_by(
            AdminOperationLog.started_at.desc(),
            AdminOperationLog.id.desc(),
        )
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    enrich_admin_audit(
        request,
        details={
            "returned_count": len(page_rows),
            "limit": limit,
            "has_action_filter": bool(action),
            "has_user_filter": bool(target_user_id),
            "has_session_filter": bool(session_id),
            "has_status_filter": bool(outcome),
            "has_risk_filter": bool(risk_level),
            "risk_level": risk_level,
        },
    )
    return {
        "items": [_log_payload(row) for row in page_rows],
        "next_cursor": _encode_cursor(page_rows[-1]) if has_more and page_rows else None,
        "has_more": has_more,
    }


def _csv_safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    # Avoid spreadsheet formula execution for every user-controlled cell.
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@router.get("/operation-logs/export")
@admin_audit_action("audit_log.export", target_type="admin_operation_log")
async def export_admin_operation_logs(
    request: Request,
    start_time: datetime | None = Query(None, alias="from"),
    end_time: datetime | None = Query(None, alias="to"),
    action: str | None = Query(None, max_length=100),
    target_user_id: str | None = Query(None, max_length=100),
    session_id: str | None = Query(None, max_length=36),
    outcome: str | None = Query(None, max_length=20),
    risk_level: str | None = Query(None, max_length=20),
    _admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """Export the same safe, filtered audit view as UTF-8 CSV."""

    rows = (
        _filtered_query(
            db,
            current_request_id=get_admin_audit_request_id(request),
            start_time=start_time,
            end_time=end_time,
            action=action,
            target_user_id=target_user_id,
            session_id=session_id,
            outcome=outcome,
            risk_level=risk_level,
        )
        .order_by(
            AdminOperationLog.started_at.desc(),
            AdminOperationLog.id.desc(),
        )
        .limit(_EXPORT_LIMIT + 1)
        .all()
    )
    if len(rows) > _EXPORT_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"导出结果超过 {_EXPORT_LIMIT} 条，请缩小筛选范围",
        )
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    columns = [
        "started_at",
        "completed_at",
        "actor_user_id",
        "action",
        "risk_level",
        "target_type",
        "target_id",
        "target_user_id",
        "session_id",
        "step_record_id",
        "outcome",
        "status_code",
        "http_method",
        "route_template",
        "ip_address",
        "user_agent",
        "request_id",
        "changed_fields",
        "details",
    ]
    writer.writerow(columns)
    for row in rows:
        payload = _log_payload(row)
        writer.writerow([_csv_safe(payload[column]) for column in columns])

    enrich_admin_audit(
        request,
        details={
            "exported_count": len(rows),
            "has_risk_filter": bool(risk_level),
            "risk_level": risk_level,
        },
    )
    filename = f"admin-operation-logs-{now_naive():%Y%m%d-%H%M%S}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["HIGH_RISK_ACTIONS", "router"]
