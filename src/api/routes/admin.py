"""管理后台 API（管理员专用）。

提供以下能力：
- 概览
- rounds 监控
- 用户管理（管理员 / 普通用户）
- 系统监控
"""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.deps import get_current_admin_user
from src.api.models.database import get_db
from src.api.models.session import Session
from src.api.models.round import Round
from src.api.models.cron_job import CronJob
from src.api.models.user_memory import CronJobRun
from src.api.models.llm_call_record import LLMCallRecord
from src.api.utils.timezone import now_naive

router = APIRouter()


class ManualReviewUpdatePayload(BaseModel):
    manual_review_status: str


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None

    ordered = sorted(values)
    pos = (len(ordered) - 1) * p
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    value = ordered[low] * (1 - frac) + ordered[high] * frac
    return round(value, 3)


def _build_overview_payload(db: DBSession, days: int) -> dict[str, Any]:
    settings = get_settings()
    now = now_naive()
    since_24h = now - timedelta(hours=24)
    since_days = now - timedelta(days=days)

    users_total = len(settings.get_auth_users())
    admins_total = len(settings.get_admin_users())

    sessions_total = db.query(func.count(Session.id)).scalar()
    rounds_total = db.query(func.count(Round.id)).scalar()
    rounds_24h = db.query(func.count(Round.id)).filter(Round.created_at >= since_24h).scalar()
    rounds_running = db.query(func.count(Round.id)).filter(Round.status == "running").scalar()

    cron_jobs_total = db.query(func.count(CronJob.id)).scalar()
    cron_jobs_enabled = (
        db.query(func.count(CronJob.id))
        .filter(CronJob.enabled.is_(True))
        .scalar()
    )
    cron_failed_24h = (
        db.query(func.count(CronJobRun.id))
        .filter(CronJobRun.status == "failed", CronJobRun.started_at >= since_24h)
        .scalar()
    )

    llm_calls_24h = (
        db.query(func.count(LLMCallRecord.id))
        .filter(LLMCallRecord.created_at >= since_24h)
        .scalar()
    )
    tokens_24h = (
        db.query(func.coalesce(func.sum(LLMCallRecord.usage_total_tokens), 0))
        .filter(LLMCallRecord.created_at >= since_24h)
        .scalar()
    )
    avg_completion_latency_24h = (
        db.query(func.avg(LLMCallRecord.completion_latency_s))
        .filter(
            LLMCallRecord.created_at >= since_24h,
            LLMCallRecord.completion_latency_s.isnot(None),
        )
        .scalar()
    )

    day_labels = [(now.date() - timedelta(days=offset)).isoformat() for offset in reversed(range(days))]
    trend_map = {day: {"date": day, "rounds": 0, "tokens": 0} for day in day_labels}

    round_rows = (
        db.query(Round.created_at)
        .filter(Round.created_at >= since_days)
        .all()
    )
    for (created_at,) in round_rows:
        day = created_at.date().isoformat()
        if day in trend_map:
            trend_map[day]["rounds"] += 1

    token_rows = (
        db.query(LLMCallRecord.created_at, LLMCallRecord.usage_total_tokens)
        .filter(LLMCallRecord.created_at >= since_days)
        .all()
    )
    for created_at, token_count in token_rows:
        day = created_at.date().isoformat()
        if day in trend_map:
            trend_map[day]["tokens"] += int(token_count or 0)

    return {
        "window_days": days,
        "summary": {
            "users_total": users_total,
            "admins_total": admins_total,
            "sessions_total": sessions_total,
            "rounds_total": rounds_total,
            "rounds_24h": rounds_24h,
            "rounds_running": rounds_running,
            "cron_jobs_total": cron_jobs_total,
            "cron_jobs_enabled": cron_jobs_enabled,
            "cron_failed_24h": cron_failed_24h,
            "llm_calls_24h": llm_calls_24h,
            "tokens_24h": int(tokens_24h),
            "avg_completion_latency_24h": round(float(avg_completion_latency_24h), 3)
            if avg_completion_latency_24h is not None
            else None,
        },
        "trends": [trend_map[day] for day in day_labels],
    }

def _build_rounds_tree_payload(
    db: DBSession,
    *,
    limit: int,
    offset: int,
    status: str,
    user_id: str | None,
    search: str | None,
) -> dict[str, Any]:
    def _apply_filters(query):
        if status != "all":
            query = query.filter(Round.status == status)
        if user_id:
            query = query.filter(Session.user_id == user_id)
        if search:
            query = query.filter(
                or_(
                    Round.user_message.contains(search),
                    Round.final_response.contains(search),
                )
            )
        return query

    base_query = (
        db.query(Round.session_id)
        .join(Session, Session.id == Round.session_id)
    )
    base_query = _apply_filters(base_query)
    total_sessions = base_query.distinct(Round.session_id).count()

    session_rows = (
        db.query(
            Session.id.label("session_id"),
            Session.user_id,
            Session.title.label("session_title"),
            func.count(Round.id).label("rounds_count"),
            func.max(Round.created_at).label("last_round_at"),
        )
        .join(Round, Round.session_id == Session.id)
    )
    session_rows = _apply_filters(session_rows)
    session_rows = (
        session_rows
        .group_by(Session.id)
        .order_by(func.max(Round.created_at).desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    session_ids = [row.session_id for row in session_rows if row.session_id]

    round_rows = (
        db.query(
            Round.id,
            Round.session_id,
            Session.user_id,
            Session.title.label("session_title"),
            Round.status,
            Round.step_count,
            Round.created_at,
            Round.completed_at,
            Round.user_message,
            Round.final_response,
        )
        .join(Session, Session.id == Round.session_id)
        .filter(Round.session_id.in_(session_ids))
        .order_by(Round.created_at.desc())
        .all()
    ) if session_ids else []

    round_ids = [row.id for row in round_rows]

    usage_rows = (
        db.query(
            LLMCallRecord.round_id,
            func.coalesce(func.sum(LLMCallRecord.usage_total_tokens), 0).label("total_tokens"),
            func.count(LLMCallRecord.id).label("llm_calls"),
            func.coalesce(
                func.sum(case((LLMCallRecord.response_error.isnot(None), 1), else_=0)),
                0,
            ).label("error_calls"),
            func.coalesce(
                func.sum(case((LLMCallRecord.compaction_triggered.is_(True), 1), else_=0)),
                0,
            ).label("compaction_steps"),
        )
        .filter(LLMCallRecord.round_id.in_(round_ids))
        .group_by(LLMCallRecord.round_id)
        .all()
    ) if round_ids else []

    usage_map: dict[str, dict[str, int]] = {
        row.round_id: {
            "total_tokens": int(row.total_tokens),
            "llm_calls": int(row.llm_calls),
            "error_calls": int(row.error_calls),
            "compaction_steps": int(row.compaction_steps),
        }
        for row in usage_rows
    }

    step_rows = (
        db.query(LLMCallRecord)
        .filter(LLMCallRecord.round_id.in_(round_ids))
        .order_by(LLMCallRecord.step_index)
        .all()
    ) if round_ids else []

    step_map: dict[str, list[dict[str, Any]]] = {}
    for row in step_rows:
        step_map.setdefault(row.round_id, []).append(
            {
                    "llm_record_id": int(row.id),
                    "step_index": int(row.step_index),
                    "request_message_count": int(row.request_message_count or 0),
                    # 首屏列表返回轻量 step 信息；详细原文通过单条详情接口按需加载。
                    "request_messages": "",
                    "request_tools": "",
                    "finish_reason": row.finish_reason,
                    "response_error": row.response_error,
                    "response_content": "",
                    "response_thinking": "",
                    "response_tool_calls": "",
                    "response_preview": "",
                    "usage_prompt_tokens": int(row.usage_prompt_tokens or 0),
                    "usage_completion_tokens": int(row.usage_completion_tokens or 0),
                    "usage_total_tokens": int(row.usage_total_tokens or 0),
                    "first_token_latency_s": round(float(row.first_token_latency_s), 3)
                    if row.first_token_latency_s is not None
                    else None,
                    "completion_latency_s": round(float(row.completion_latency_s), 3)
                    if row.completion_latency_s is not None
                    else None,
                    "compaction_triggered": bool(row.compaction_triggered),
                    "compaction_pre_tokens": int(row.compaction_pre_tokens or 0),
                    "compaction_post_tokens": int(row.compaction_post_tokens or 0),
                    "compaction_tokens_saved": int(row.compaction_tokens_saved or 0),
                    "compaction_microcompact_compacted_messages": int(row.compaction_microcompact_compacted_messages or 0),
                    "compaction_summary_generated_count": int(row.compaction_summary_generated_count or 0),
                    "compaction_summary_reused_count": int(row.compaction_summary_reused_count or 0),
                    "compaction_summary_quality_repair_count": int(row.compaction_summary_quality_repair_count or 0),
                    "compaction_emergency_truncate_dropped_rounds": int(row.compaction_emergency_truncate_dropped_rounds or 0),
                    "manual_review_status": row.manual_review_status,
                    "created_at": _iso(row.created_at),
                }
            )

    now = now_naive()
    session_items_map: dict[str, dict[str, Any]] = {
        row.session_id: {
            "session_id": row.session_id,
            "user_id": row.user_id,
            "session_title": row.session_title,
            "rounds_count": int(row.rounds_count),
            "last_round_at": _iso(row.last_round_at),
            "sum_step_count": 0,
            "total_tokens": 0,
            "llm_calls": 0,
            "error_calls": 0,
            "compaction_steps": 0,
            "total_duration_s": 0.0,
            "status": "completed",
            "rounds": [],
            "_status_flags": set(),
        }
        for row in session_rows
        if row.session_id
    }

    for row in round_rows:
        usage = usage_map.get(
            row.id,
            {
                "total_tokens": 0,
                "llm_calls": 0,
                "error_calls": 0,
                "compaction_steps": 0,
            },
        )
        ended_at = row.completed_at or now
        duration_s = round((ended_at - row.created_at).total_seconds(), 3)
        steps = step_map.get(row.id, [])

        round_item = {
            "round_id": row.id,
            "session_id": row.session_id,
            "user_id": row.user_id,
            "session_title": row.session_title,
            "status": row.status,
            "step_count": int(row.step_count or 0),
            "started_at": _iso(row.created_at),
            "completed_at": _iso(row.completed_at),
            "duration_s": duration_s,
            "user_message_preview": (row.user_message or "")[:120],
            "final_response_preview": (row.final_response or "")[:180],
            **usage,
            "steps": steps,
        }

        session_item = session_items_map.get(row.session_id)
        if not session_item:
            continue

        session_item["sum_step_count"] += int(row.step_count or 0)
        session_item["total_tokens"] += int(usage["total_tokens"])
        session_item["llm_calls"] += int(usage["llm_calls"])
        session_item["error_calls"] += int(usage["error_calls"])
        session_item["compaction_steps"] += int(usage["compaction_steps"])
        session_item["total_duration_s"] += float(duration_s)
        session_item["rounds"].append(round_item)
        session_item["_status_flags"].add(row.status)

    ordered_sessions: list[dict[str, Any]] = []
    for row in session_rows:
        if not row.session_id:
            continue
        session_item = session_items_map.get(row.session_id)
        if not session_item:
            continue

        status_flags = session_item.pop("_status_flags")
        if any(flag in {"running", "resumed"} for flag in status_flags):
            session_item["status"] = "running"
        elif any(flag in {"failed", "cancelled", "interrupted"} for flag in status_flags):
            session_item["status"] = "error"
        else:
            session_item["status"] = "completed"

        session_item["total_duration_s"] = round(float(session_item["total_duration_s"]), 3)
        ordered_sessions.append(session_item)

    return {
        "total_sessions": total_sessions,
        "offset": offset,
        "limit": limit,
        "sessions": ordered_sessions,
    }


def _build_llm_record_detail_payload(db: DBSession, llm_record_id: int) -> dict[str, Any]:
    row = (
        db.query(
            LLMCallRecord.id,
            LLMCallRecord.round_id,
            LLMCallRecord.step_index,
            LLMCallRecord.request_message_count,
            LLMCallRecord.request_messages,
            LLMCallRecord.request_tools,
            LLMCallRecord.finish_reason,
            LLMCallRecord.response_error,
            LLMCallRecord.response_content,
            LLMCallRecord.response_thinking,
            LLMCallRecord.response_tool_calls,
            LLMCallRecord.usage_prompt_tokens,
            LLMCallRecord.usage_completion_tokens,
            LLMCallRecord.usage_total_tokens,
            LLMCallRecord.first_token_latency_s,
            LLMCallRecord.completion_latency_s,
            LLMCallRecord.compaction_triggered,
            LLMCallRecord.compaction_pre_tokens,
            LLMCallRecord.compaction_post_tokens,
            LLMCallRecord.compaction_tokens_saved,
            LLMCallRecord.compaction_microcompact_compacted_messages,
            LLMCallRecord.compaction_summary_generated_count,
            LLMCallRecord.compaction_summary_reused_count,
            LLMCallRecord.compaction_summary_quality_repair_count,
            LLMCallRecord.compaction_emergency_truncate_dropped_rounds,
            LLMCallRecord.manual_review_status,
            LLMCallRecord.created_at,
        )
        .filter(LLMCallRecord.id == llm_record_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="llm_call_record 不存在")

    return {
        "llm_record_id": int(row.id),
        "round_id": row.round_id,
        "step_index": int(row.step_index),
        "request_message_count": int(row.request_message_count or 0),
        "request_messages": row.request_messages or "",
        "request_tools": row.request_tools or "",
        "finish_reason": row.finish_reason,
        "response_error": row.response_error,
        "response_content": row.response_content or "",
        "response_thinking": row.response_thinking or "",
        "response_tool_calls": row.response_tool_calls or "",
        "response_preview": (row.response_content or "")[:180],
        "usage_prompt_tokens": int(row.usage_prompt_tokens or 0),
        "usage_completion_tokens": int(row.usage_completion_tokens or 0),
        "usage_total_tokens": int(row.usage_total_tokens or 0),
        "first_token_latency_s": round(float(row.first_token_latency_s), 3)
        if row.first_token_latency_s is not None
        else None,
        "completion_latency_s": round(float(row.completion_latency_s), 3)
        if row.completion_latency_s is not None
        else None,
        "compaction_triggered": bool(row.compaction_triggered),
        "compaction_pre_tokens": int(row.compaction_pre_tokens or 0),
        "compaction_post_tokens": int(row.compaction_post_tokens or 0),
        "compaction_tokens_saved": int(row.compaction_tokens_saved or 0),
        "compaction_microcompact_compacted_messages": int(row.compaction_microcompact_compacted_messages or 0),
        "compaction_summary_generated_count": int(row.compaction_summary_generated_count or 0),
        "compaction_summary_reused_count": int(row.compaction_summary_reused_count or 0),
        "compaction_summary_quality_repair_count": int(row.compaction_summary_quality_repair_count or 0),
        "compaction_emergency_truncate_dropped_rounds": int(row.compaction_emergency_truncate_dropped_rounds or 0),
        "manual_review_status": row.manual_review_status,
        "created_at": _iso(row.created_at),
    }


def _update_llm_record_review_status(
    db: DBSession,
    *,
    llm_record_id: int,
    manual_review_status: str,
) -> dict[str, Any]:
    if manual_review_status not in {"没问题", "有问题"}:
        raise HTTPException(status_code=400, detail="manual_review_status 仅支持 没问题/有问题")

    record = (
        db.query(LLMCallRecord)
        .filter(LLMCallRecord.id == llm_record_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="llm_call_record 不存在")

    record.manual_review_status = manual_review_status
    db.commit()

    return {
        "llm_record_id": int(record.id),
        "manual_review_status": record.manual_review_status,
    }


def _build_users_payload(db: DBSession) -> dict[str, Any]:
    settings = get_settings()
    now = now_naive()
    since_24h = now - timedelta(hours=24)

    auth_users = settings.get_auth_users()
    admin_users = settings.get_admin_users()

    session_rows = (
        db.query(
            Session.user_id,
            func.count(Session.id).label("sessions_count"),
            func.max(Session.updated_at).label("last_session_at"),
        )
        .group_by(Session.user_id)
        .all()
    )
    session_map = {
        row.user_id: {
            "sessions_count": int(row.sessions_count),
            "last_session_at": row.last_session_at,
        }
        for row in session_rows
    }

    round_rows = (
        db.query(
            Session.user_id,
            func.count(Round.id).label("rounds_count"),
            func.coalesce(
                func.sum(case((Round.status == "running", 1), else_=0)),
                0,
            ).label("running_rounds"),
            func.max(Round.created_at).label("last_round_at"),
        )
        .join(Round, Round.session_id == Session.id)
        .group_by(Session.user_id)
        .all()
    )
    round_map = {
        row.user_id: {
            "rounds_count": int(row.rounds_count),
            "running_rounds": int(row.running_rounds),
            "last_round_at": row.last_round_at,
        }
        for row in round_rows
    }

    token_rows = (
        db.query(
            Session.user_id,
            func.coalesce(func.sum(LLMCallRecord.usage_total_tokens), 0).label("total_tokens"),
        )
        .join(LLMCallRecord, LLMCallRecord.session_id == Session.id)
        .group_by(Session.user_id)
        .all()
    )
    token_map = {row.user_id: int(row.total_tokens) for row in token_rows}

    cron_rows = (
        db.query(
            CronJob.user_id,
            func.count(CronJob.id).label("cron_jobs_total"),
            func.coalesce(
                func.sum(case((CronJob.enabled.is_(True), 1), else_=0)),
                0,
            ).label("cron_jobs_enabled"),
        )
        .group_by(CronJob.user_id)
        .all()
    )
    cron_map = {
        row.user_id: {
            "cron_jobs_total": int(row.cron_jobs_total),
            "cron_jobs_enabled": int(row.cron_jobs_enabled),
        }
        for row in cron_rows
    }

    cron_failed_rows = (
        db.query(
            CronJobRun.user_id,
            func.count(CronJobRun.id).label("cron_failed_24h"),
        )
        .filter(
            CronJobRun.status == "failed",
            CronJobRun.started_at >= since_24h,
        )
        .group_by(CronJobRun.user_id)
        .all()
    )
    cron_failed_map = {row.user_id: int(row.cron_failed_24h) for row in cron_failed_rows}

    users: list[dict[str, Any]] = []
    for username in sorted(auth_users.keys()):
        session_info = session_map.get(username, {})
        round_info = round_map.get(username, {})
        cron_info = cron_map.get(username, {})

        last_active_candidates = [
            session_info.get("last_session_at"),
            round_info.get("last_round_at"),
        ]
        last_active = max((dt for dt in last_active_candidates if dt is not None), default=None)

        running_rounds = int(round_info.get("running_rounds", 0))
        if running_rounds > 0:
            status = "running"
        elif last_active and (now - last_active) <= timedelta(days=7):
            status = "active"
        else:
            status = "idle"

        is_admin = username in admin_users
        users.append(
            {
                "user_id": username,
                "role": "admin" if is_admin else "user",
                "is_admin": is_admin,
                "status": status,
                "sessions_count": int(session_info.get("sessions_count", 0)),
                "rounds_count": int(round_info.get("rounds_count", 0)),
                "running_rounds": running_rounds,
                "total_tokens": int(token_map.get(username, 0)),
                "cron_jobs_total": int(cron_info.get("cron_jobs_total", 0)),
                "cron_jobs_enabled": int(cron_info.get("cron_jobs_enabled", 0)),
                "cron_failed_24h": int(cron_failed_map.get(username, 0)),
                "last_active_at": _iso(last_active),
            }
        )

    return {
        "summary": {
            "users_total": len(users),
            "admins_total": len([item for item in users if item["is_admin"]]),
            "active_total": len([item for item in users if item["status"] in {"active", "running"}]),
            "running_total": len([item for item in users if item["status"] == "running"]),
        },
        "users": users,
    }


def _build_system_payload(db: DBSession, hours: int) -> dict[str, Any]:
    now = now_naive()
    since = now - timedelta(hours=hours)

    round_status_rows = (
        db.query(Round.status, func.count(Round.id))
        .filter(Round.created_at >= since)
        .group_by(Round.status)
        .all()
    )
    round_status_counts = {status: int(count) for status, count in round_status_rows}

    cron_status_rows = (
        db.query(CronJobRun.status, func.count(CronJobRun.id))
        .filter(CronJobRun.started_at >= since)
        .group_by(CronJobRun.status)
        .all()
    )
    cron_status_counts = {status: int(count) for status, count in cron_status_rows}

    completion_latency_values = [
        float(value)
        for (value,) in (
            db.query(LLMCallRecord.completion_latency_s)
            .filter(
                LLMCallRecord.created_at >= since,
                LLMCallRecord.completion_latency_s.isnot(None),
            )
            .all()
        )
    ]
    first_token_latency_values = [
        float(value)
        for (value,) in (
            db.query(LLMCallRecord.first_token_latency_s)
            .filter(
                LLMCallRecord.created_at >= since,
                LLMCallRecord.first_token_latency_s.isnot(None),
            )
            .all()
        )
    ]

    compaction_agg = (
        db.query(
            func.count(LLMCallRecord.id).label("llm_calls"),
            func.coalesce(
                func.sum(case((LLMCallRecord.compaction_triggered.is_(True), 1), else_=0)),
                0,
            ).label("compaction_calls"),
            func.coalesce(func.sum(LLMCallRecord.compaction_tokens_saved), 0).label("tokens_saved"),
            func.coalesce(
                func.sum(LLMCallRecord.compaction_summary_quality_repair_count),
                0,
            ).label("quality_repairs"),
            func.coalesce(
                func.sum(LLMCallRecord.compaction_emergency_truncate_dropped_rounds),
                0,
            ).label("emergency_drops"),
            func.coalesce(
                func.sum(case((LLMCallRecord.response_error.isnot(None), 1), else_=0)),
                0,
            ).label("response_errors"),
        )
        .filter(LLMCallRecord.created_at >= since)
        .one()
    )

    running_rounds = db.query(func.count(Round.id)).filter(Round.status == "running").scalar()
    active_sessions = (
        db.query(func.count(Session.id))
        .filter(Session.updated_at >= now - timedelta(minutes=30))
        .scalar()
    )

    return {
        "window_hours": hours,
        "summary": {
            "running_rounds": int(running_rounds),
            "active_sessions_30m": int(active_sessions),
            "round_status_counts": round_status_counts,
            "cron_status_counts": cron_status_counts,
            "avg_completion_latency_s": round(sum(completion_latency_values) / len(completion_latency_values), 3)
            if completion_latency_values
            else None,
            "p50_completion_latency_s": _percentile(completion_latency_values, 0.5),
            "p95_completion_latency_s": _percentile(completion_latency_values, 0.95),
            "avg_first_token_latency_s": round(sum(first_token_latency_values) / len(first_token_latency_values), 3)
            if first_token_latency_values
            else None,
            "llm_calls": int(compaction_agg.llm_calls),
            "compaction_calls": int(compaction_agg.compaction_calls),
            "compaction_tokens_saved": int(compaction_agg.tokens_saved),
            "compaction_quality_repairs": int(compaction_agg.quality_repairs),
            "compaction_emergency_drops": int(compaction_agg.emergency_drops),
            "llm_response_errors": int(compaction_agg.response_errors),
        },
    }


@router.get("/overview")
async def get_admin_overview(
    days: int = Query(7, ge=1, le=90),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """管理端概览。"""
    return _build_overview_payload(db, days)

@router.get("/rounds-tree")
async def get_admin_rounds_tree(
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str = Query("all", description="all|running|completed|failed|interrupted|resumed|cancelled"),
    user_id: str | None = Query(None),
    search: str | None = Query(None),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """按 Session 聚合的 rounds 监控树，含 round 内 step 级 LLM 调用明细。"""
    return _build_rounds_tree_payload(
        db,
        limit=limit,
        offset=offset,
        status=status,
        user_id=user_id,
        search=search,
    )


@router.put("/llm-call-records/{llm_record_id}/review")
async def update_admin_llm_call_review(
    llm_record_id: int,
    payload: ManualReviewUpdatePayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """更新 step 级 LLM 调用记录的人工审阅状态。"""
    return _update_llm_record_review_status(
        db,
        llm_record_id=llm_record_id,
        manual_review_status=payload.manual_review_status,
    )


@router.get("/llm-call-records/{llm_record_id}")
async def get_admin_llm_call_record_detail(
    llm_record_id: int,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """获取单条 step 级 LLM 调用详情（按需加载）。"""
    return _build_llm_record_detail_payload(db, llm_record_id)


@router.get("/users")
async def get_admin_users(
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """管理端用户列表与角色信息。"""
    return _build_users_payload(db)


@router.get("/system")
async def get_admin_system(
    hours: int = Query(24, ge=1, le=168),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """管理端系统监控聚合指标。"""
    return _build_system_payload(db, hours)
