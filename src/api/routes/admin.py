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
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.deps import get_current_admin_user
from src.api.models.auth_login_event import AuthLoginEvent
from src.api.models.auth_user import AuthUser
from src.api.models.database import get_db
from src.api.models.session import Session
from src.api.models.round import Round
from src.api.models.cron_job import CronJob
from src.api.models.user_sandbox import UserSandbox
from src.api.models.user_run_lock import UserRunLock
from src.api.models.user_memory import CronJobRun
from src.api.models.llm_call_record import LLMCallRecord
from src.api.models.subagent_run import SubagentRun
from src.api.services.agent_pool_service import get_agent_pool
from src.api.services.auth_service import (
    auth_user_to_payload,
    create_ldap_user,
    create_simple_user,
    delete_auth_user,
    reset_simple_user_password,
    update_user_admin,
    update_user_enabled,
    update_user_token_limits,
)
from src.api.services.sandbox_service import SandboxSessionService
from src.api.utils.timezone import now_naive

router = APIRouter()


class ManualReviewUpdatePayload(BaseModel):
    manual_review_status: str


class AdminCreateSimpleUserPayload(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=1024)
    enabled: bool = True
    is_admin: bool = False
    token_limit_per_week: int | None = Field(default=None, ge=0)
    token_limit_per_month: int | None = Field(default=None, ge=0)

    @field_validator("username", mode="before")
    @classmethod
    def _strip_username(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("password")
    @classmethod
    def _reject_blank_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("密码不能为空")
        return value


class AdminCreateLdapUserPayload(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=100)
    username: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool = True
    is_admin: bool = False
    token_limit_per_week: int | None = Field(default=None, ge=0)
    token_limit_per_month: int | None = Field(default=None, ge=0)

    @field_validator("user_id", "username", mode="before")
    @classmethod
    def _strip_identifiers(cls, value):
        return value.strip() if isinstance(value, str) else value


class AdminEnabledUpdatePayload(BaseModel):
    enabled: bool


class AdminFlagUpdatePayload(BaseModel):
    is_admin: bool


class AdminTokenLimitsUpdatePayload(BaseModel):
    token_limit_per_week: int | None = Field(default=None, ge=0)
    token_limit_per_month: int | None = Field(default=None, ge=0)


class AdminResetPasswordPayload(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)

    @field_validator("password")
    @classmethod
    def _reject_blank_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("密码不能为空")
        return value


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _auth_login_event_to_payload(event: AuthLoginEvent) -> dict[str, Any]:
    return {
        "id": int(event.id),
        "user_id": event.user_id,
        "username": event.username,
        "auth_type": event.auth_type,
        "ip_address": event.ip_address,
        "user_agent": event.user_agent,
        "login_at": _iso(event.login_at),
    }


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
    now = now_naive()
    since_24h = now - timedelta(hours=24)
    since_days = now - timedelta(days=days)

    users_total = db.query(func.count(AuthUser.id)).scalar()
    admins_total = db.query(func.count(AuthUser.id)).filter(AuthUser.is_admin.is_(True)).scalar()

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
            Round.parent_run_id,
            Round.user_message,
            Round.final_response,
        )
        .join(Session, Session.id == Round.session_id)
        .filter(Round.session_id.in_(session_ids))
        .order_by(Round.created_at.desc())
        .all()
    ) if session_ids else []

    round_ids = [row.id for row in round_rows]

    subagent_edges = (
        db.query(SubagentRun)
        .filter(
            or_(
                SubagentRun.child_run_id.in_(round_ids),
                SubagentRun.parent_run_id.in_(round_ids),
            )
        )
        .all()
    ) if round_ids else []
    subagent_by_child_run_id = {
        edge.child_run_id: edge
        for edge in subagent_edges
        if edge.child_run_id
    }
    subagent_child_count_by_parent: dict[str, int] = {}
    for edge in subagent_edges:
        subagent_child_count_by_parent[edge.parent_run_id] = (
            subagent_child_count_by_parent.get(edge.parent_run_id, 0) + 1
        )

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
        subagent_edge = subagent_by_child_run_id.get(row.id)
        is_subagent_round = subagent_edge is not None
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
        subagent_prompt = subagent_edge.prompt if subagent_edge else None
        subagent_description = subagent_edge.description if subagent_edge else None

        round_item = {
            "round_id": row.id,
            "session_id": row.session_id,
            "user_id": row.user_id,
            "session_title": row.session_title,
            "run_kind": "subagent" if is_subagent_round else "main",
            "parent_run_id": subagent_edge.parent_run_id if subagent_edge else row.parent_run_id,
            "root_run_id": subagent_edge.root_run_id if subagent_edge else row.id,
            "subagent_edge_id": subagent_edge.id if subagent_edge else None,
            "subagent_type": subagent_edge.agent_type if subagent_edge else None,
            "subagent_description": subagent_description,
            "subagent_prompt_preview": (subagent_prompt or "")[:180],
            "subagent_child_count": int(subagent_child_count_by_parent.get(row.id, 0)),
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
        if any(flag == "running" for flag in status_flags):
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
    now = now_naive()
    since_24h = now - timedelta(hours=24)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    lock_cutoff = now - timedelta(seconds=get_settings().sse_subscribe_timeout)

    auth_user_rows = db.query(AuthUser).order_by(AuthUser.username).all()
    user_ids = [user.user_id for user in auth_user_rows]
    if not user_ids:
        return {
            "summary": {
                "users_total": 0,
                "admins_total": 0,
                "active_total": 0,
                "running_total": 0,
            },
            "users": [],
        }

    session_rows = (
        db.query(
            Session.user_id,
            func.count(Session.id).label("sessions_count"),
            func.max(Session.updated_at).label("last_session_at"),
        )
        .filter(Session.user_id.in_(user_ids))
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
        .filter(Session.user_id.in_(user_ids))
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
            func.coalesce(
                func.sum(case((LLMCallRecord.created_at >= week_start, LLMCallRecord.usage_total_tokens), else_=0)),
                0,
            ).label("weekly_tokens"),
            func.coalesce(
                func.sum(case((LLMCallRecord.created_at >= month_start, LLMCallRecord.usage_total_tokens), else_=0)),
                0,
            ).label("monthly_tokens"),
        )
        .join(LLMCallRecord, LLMCallRecord.session_id == Session.id)
        .filter(Session.user_id.in_(user_ids))
        .group_by(Session.user_id)
        .all()
    )
    token_map = {
        row.user_id: {
            "total_tokens": int(row.total_tokens),
            "weekly_tokens": int(row.weekly_tokens),
            "monthly_tokens": int(row.monthly_tokens),
        }
        for row in token_rows
    }

    cron_rows = (
        db.query(
            CronJob.user_id,
            func.count(CronJob.id).label("cron_jobs_total"),
            func.coalesce(
                func.sum(case((CronJob.enabled.is_(True), 1), else_=0)),
                0,
            ).label("cron_jobs_enabled"),
        )
        .filter(CronJob.user_id.in_(user_ids))
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
            CronJobRun.user_id.in_(user_ids),
        )
        .group_by(CronJobRun.user_id)
        .all()
    )
    cron_failed_map = {row.user_id: int(row.cron_failed_24h) for row in cron_failed_rows}

    lock_rows = (
        db.query(
            UserRunLock.user_id,
            func.count(UserRunLock.user_id).label("running_locks"),
            func.max(UserRunLock.updated_at).label("last_lock_at"),
        )
        .filter(
            UserRunLock.user_id.in_(user_ids),
            UserRunLock.updated_at >= lock_cutoff,
        )
        .group_by(UserRunLock.user_id)
        .all()
    )
    lock_map = {
        row.user_id: {
            "running_locks": int(row.running_locks),
            "last_lock_at": row.last_lock_at,
        }
        for row in lock_rows
    }

    latest_login_subq = (
        db.query(
            AuthLoginEvent.user_id,
            func.max(AuthLoginEvent.login_at).label("last_login_event_at"),
        )
        .filter(AuthLoginEvent.user_id.in_(user_ids))
        .group_by(AuthLoginEvent.user_id)
        .subquery()
    )
    latest_login_rows = (
        db.query(
            AuthLoginEvent.user_id,
            AuthLoginEvent.ip_address,
            AuthLoginEvent.login_at,
        )
        .join(
            latest_login_subq,
            (AuthLoginEvent.user_id == latest_login_subq.c.user_id)
            & (AuthLoginEvent.login_at == latest_login_subq.c.last_login_event_at),
        )
        .all()
    )
    latest_login_map = {
        row.user_id: {
            "ip_address": row.ip_address,
            "login_at": row.login_at,
        }
        for row in latest_login_rows
    }

    users: list[dict[str, Any]] = []
    for auth_user in auth_user_rows:
        user_id = auth_user.user_id
        session_info = session_map.get(user_id, {})
        round_info = round_map.get(user_id, {})
        cron_info = cron_map.get(user_id, {})
        token_info = token_map.get(user_id, {})
        lock_info = lock_map.get(user_id, {})

        last_active_candidates = [
            session_info.get("last_session_at"),
            round_info.get("last_round_at"),
            lock_info.get("last_lock_at"),
        ]
        last_active = max((dt for dt in last_active_candidates if dt is not None), default=None)

        running_rounds = max(
            int(round_info.get("running_rounds", 0)),
            int(lock_info.get("running_locks", 0)),
        )
        if running_rounds > 0:
            status = "running"
        elif last_active and (now - last_active) <= timedelta(days=7):
            status = "active"
        else:
            status = "idle"

        is_admin = bool(auth_user.is_admin)
        users.append(
            {
                "user_id": user_id,
                "username": auth_user.username,
                "auth_type": auth_user.auth_type,
                "enabled": bool(auth_user.enabled),
                "role": "admin" if is_admin else "user",
                "is_admin": is_admin,
                "status": status,
                "sessions_count": int(session_info.get("sessions_count", 0)),
                "rounds_count": int(round_info.get("rounds_count", 0)),
                "running_rounds": running_rounds,
                "total_tokens": int(token_info.get("total_tokens", 0)),
                "weekly_tokens_used": int(token_info.get("weekly_tokens", 0)),
                "monthly_tokens_used": int(token_info.get("monthly_tokens", 0)),
                "token_limit_per_week": auth_user.token_limit_per_week,
                "token_limit_per_month": auth_user.token_limit_per_month,
                "cron_jobs_total": int(cron_info.get("cron_jobs_total", 0)),
                "cron_jobs_enabled": int(cron_info.get("cron_jobs_enabled", 0)),
                "cron_failed_24h": int(cron_failed_map.get(user_id, 0)),
                "last_active_at": _iso(last_active),
                "last_login_at": _iso(auth_user.last_login_at),
                "last_login_ip": latest_login_map.get(user_id, {}).get("ip_address"),
                "created_by": auth_user.created_by,
                "created_at": _iso(auth_user.created_at),
                "updated_at": _iso(auth_user.updated_at),
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


def _build_user_login_events_payload(db: DBSession, *, user_id: str, limit: int) -> dict[str, Any]:
    user = db.query(AuthUser).filter(AuthUser.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    events = (
        db.query(AuthLoginEvent)
        .filter(AuthLoginEvent.user_id == user_id)
        .order_by(AuthLoginEvent.login_at.desc(), AuthLoginEvent.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "user_id": user_id,
        "events": [_auth_login_event_to_payload(event) for event in events],
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
    status: str = Query(
        "all",
        description="all|running|completed|failed|interrupted|resumed|cancelled|max_steps_reached",
    ),
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


@router.get("/users/{user_id}/login-events")
async def get_admin_user_login_events(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """查看指定用户的登录审计历史。"""
    return _build_user_login_events_payload(db, user_id=user_id, limit=limit)


@router.post("/users/simple")
async def create_admin_simple_user(
    payload: AdminCreateSimpleUserPayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """创建本地 simple 用户。"""
    user = create_simple_user(
        db,
        username=payload.username,
        password=payload.password,
        enabled=payload.enabled,
        is_admin=payload.is_admin,
        token_limit_per_week=payload.token_limit_per_week,
        token_limit_per_month=payload.token_limit_per_month,
        created_by=admin_user_id,
    )
    return auth_user_to_payload(user)


@router.post("/users/ldap")
async def create_admin_ldap_user(
    payload: AdminCreateLdapUserPayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """创建 LDAP 目录账号用户。"""
    user = create_ldap_user(
        db,
        user_id=payload.user_id,
        username=payload.username,
        enabled=payload.enabled,
        is_admin=payload.is_admin,
        token_limit_per_week=payload.token_limit_per_week,
        token_limit_per_month=payload.token_limit_per_month,
        created_by=admin_user_id,
    )
    return auth_user_to_payload(user)


@router.patch("/users/{user_id}/enabled")
async def update_admin_user_enabled(
    user_id: str,
    payload: AdminEnabledUpdatePayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """启用或禁用用户。"""
    if user_id == admin_user_id and not payload.enabled:
        raise HTTPException(status_code=400, detail="不能禁用当前管理员账号")
    user = update_user_enabled(db, user_id=user_id, enabled=payload.enabled)
    return auth_user_to_payload(user)


@router.patch("/users/{user_id}/admin")
async def update_admin_user_admin_flag(
    user_id: str,
    payload: AdminFlagUpdatePayload,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """设置或取消管理员权限。"""
    if user_id == admin_user_id and not payload.is_admin:
        raise HTTPException(status_code=400, detail="不能取消自己的管理员权限")
    user = update_user_admin(db, user_id=user_id, is_admin=payload.is_admin)
    return auth_user_to_payload(user)


@router.patch("/users/{user_id}/token-limits")
async def update_admin_user_token_limits(
    user_id: str,
    payload: AdminTokenLimitsUpdatePayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """更新用户 token 周/月限额。"""
    user = update_user_token_limits(
        db,
        user_id=user_id,
        token_limit_per_week=payload.token_limit_per_week,
        token_limit_per_month=payload.token_limit_per_month,
    )
    return auth_user_to_payload(user)


@router.post("/users/{user_id}/reset-password")
async def reset_admin_simple_user_password(
    user_id: str,
    payload: AdminResetPasswordPayload,
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """重置 simple 用户密码。"""
    user = reset_simple_user_password(db, user_id=user_id, password=payload.password)
    return auth_user_to_payload(user)


@router.delete("/users/{user_id}")
async def delete_admin_auth_user(
    user_id: str,
    admin_user_id: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """硬删除认证用户账号及其用户态数据。"""
    if user_id == admin_user_id:
        raise HTTPException(status_code=400, detail="不能删除当前管理员账号")

    target_user = db.query(AuthUser).filter(AuthUser.user_id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="用户不存在")

    lock_cutoff = now_naive() - timedelta(seconds=get_settings().sse_subscribe_timeout)
    active_lock = (
        db.query(UserRunLock)
        .filter(UserRunLock.user_id == user_id, UserRunLock.updated_at >= lock_cutoff)
        .first()
    )
    if active_lock:
        raise HTTPException(status_code=409, detail="用户当前有正在运行的任务，无法删除")

    running_cron = (
        db.query(CronJobRun)
        .filter(CronJobRun.user_id == user_id, CronJobRun.status == "running")
        .first()
    )
    if running_cron:
        raise HTTPException(status_code=409, detail="用户当前有正在运行的定时任务，无法删除")

    await get_agent_pool().invalidate_user_async(user_id, preserve_running=False)

    sandbox_service = SandboxSessionService()
    user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
    sandbox_id = user_sandbox.sandbox_id if user_sandbox else None
    if sandbox_id or sandbox_service.get_cached(user_id):
        sandbox_deleted = await sandbox_service.kill(user_id, sandbox_id)
        if not sandbox_deleted:
            raise HTTPException(status_code=409, detail="沙箱清理失败，用户未删除")

    deleted_user_id = delete_auth_user(db, user_id=user_id)
    return {"user_id": deleted_user_id, "deleted": True}


@router.get("/system")
async def get_admin_system(
    hours: int = Query(24, ge=1, le=168),
    _: str = Depends(get_current_admin_user),
    db: DBSession = Depends(get_db),
):
    """管理端系统监控聚合指标。"""
    return _build_system_payload(db, hours)
