"""Cron 服务 — DB 驱动 + 任务执行

职责：
- 从 CronJob DB 表管理定时任务定义（CRUD：HTTP 路由与 Agent 工具共用）
- Runner：恢复用户沙箱 → 归一化 cron turn → TurnOrchestrator 执行 → 写 CronJobRun

注：调度由 `cron_worker` 去中心化执行，Cron 语义统一来自 `CronEngine`，
本模块暴露 Cron CRUD 与 `run_cron_job`；表达式语义统一由 `CronEngine` 提供。
"""

import json
import logging
import re
import shlex
import uuid
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.auth_user import AuthUser
from src.api.models.cron_fire import CronFire
from src.api.models.cron_job import CronJob
from src.api.models.user_memory import CronJobRun
from src.api.models.user_sandbox import UserSandbox
from src.api.models.workspace import WorkspaceChangeSet
from src.api.services.cron_engine import CronEngine, CronExpressionError
from src.api.services.cron_schedule import schedule_to_cron, ScheduleError
from src.api.utils.timezone import now_naive
from src.api.utils.sandbox_helpers import is_workspace_publish_scratch_path

logger = logging.getLogger(__name__)
settings = get_settings()

# 任务名格式：字母数字 _ -，1-100 字符
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class CronJobValidationError(ValueError):
    """CronJob CRUD 校验错误（用于路由层映射 400）"""


class CronJobNotFoundError(CronJobValidationError):
    """CronJob 不存在（用于路由层映射 404）"""


class CronJobBusyError(RuntimeError):
    """PostgreSQL 写冲突（死锁 / 序列化失败）。"""


def _is_db_busy_error(error: OperationalError) -> bool:
    pgcode = getattr(getattr(error, "orig", None), "pgcode", None)
    # 40001 = serialization_failure, 40P01 = deadlock_detected
    return pgcode in ("40001", "40P01")


def _mark_run_failed(
    run_id: str,
    output: str,
    run_workspace: str | None = None,
    *,
    error_code: str | None = None,
    claim_token: str | None = None,
) -> None:
    from src.api.models.database import SessionLocal

    with SessionLocal() as db:
        query = db.query(CronJobRun).filter(CronJobRun.id == run_id)
        if claim_token is not None:
            query = query.filter(
                CronJobRun.claim_token == claim_token,
                CronJobRun.claim_lease_expires_at > now_naive(),
            )
        record = query.first()
        if record and record.status in {"queued", "running"}:
            record.status = "failed"
            record.phase = "terminal"
            record.output = output
            record.error_code = error_code
            record.completed_at = now_naive()
            record.run_workspace = run_workspace
            record.claim_token = None
            record.claim_worker_id = None
            record.claim_lease_expires_at = None
            db.commit()


def _ensure_cron_session(
    *,
    db: DBSession,
    user_id: str,
    session_id: str,
    job_name: str,
    run_id: str,
    cron_expr: str,
    source: str,
    model_id: str | None,
) -> None:
    """Create the internal session/binding required by TurnOrchestrator."""
    from src.api.models.session import Session
    from src.api.schemas.turn import NoReplyRoute
    from src.api.services.channel_binding_service import get_channel_binding_service

    session = db.query(Session).filter(Session.id == session_id).first()
    if session is None:
        session = Session(
            id=session_id,
            user_id=user_id,
            title=f"Cron: {job_name}",
            status="active",
            model_id=model_id,
        )
        db.add(session)
        db.commit()
    elif session.user_id != user_id:
        raise RuntimeError(f"内部 cron session 冲突: {session_id}")

    get_channel_binding_service().get_or_create_binding(
        db,
        user_id=user_id,
        session_id=session_id,
        channel="cron",
        peer_kind="cron",
        peer_id=job_name,
        external_thread_id=run_id,
        reply_route=NoReplyRoute(),
        metadata={
            "job_name": job_name,
            "run_id": run_id,
            "cron_expr": cron_expr,
            "source": source,
        },
    )


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise CronJobValidationError(
            "任务名必须为 1-100 字符的字母/数字/下划线/连字符"
        )
    return name


def _validate_job_text(value: str | None, *, field: str, max_length: int) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise CronJobValidationError(f"{field} 必须是字符串")
    if len(value) > max_length:
        raise CronJobValidationError(f"{field} 最多 {max_length} 个字符")


def build_cron_definition_snapshot(job: CronJob) -> dict:
    """Freeze every execution-relevant field before a run is queued."""
    def _text(value: object, default: str = "") -> str:
        return value if isinstance(value, str) else default

    def _version(value: object) -> int:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 1

    return {
        "job_id": int(job.id),
        "job_name": _text(getattr(job, "name", None)),
        "cron_expr": _text(getattr(job, "cron_expr", None)),
        "rule_version": _version(getattr(job, "rule_version", 1)),
        "definition_version": _version(getattr(job, "definition_version", 1)),
        "description": _text(getattr(job, "description", None)),
        "content": _text(getattr(job, "content", None)),
    }


def _resolve_cron_expr(schedule: dict | None, cron_expr: str | None) -> tuple[str, str | None]:
    """根据 schedule / cron_expr 输入决定最终存储值。

    优先级：schedule 提供 → 由 schedule 派生 cron_expr，schedule_json 入库；
    否则使用直传 cron_expr，schedule_json 为 None（Agent 工具走该路径）。
    """
    if schedule is not None:
        try:
            expr = schedule_to_cron(schedule)
        except ScheduleError as e:
            raise CronJobValidationError(str(e)) from e
        schedule_json = json.dumps(schedule, ensure_ascii=False)
    else:
        if not cron_expr or not cron_expr.strip():
            raise CronJobValidationError("必须提供 schedule 或 cron_expr")
        expr = cron_expr.strip()
        try:
            expr = CronEngine.validate(expr)
        except CronExpressionError as e:
            raise CronJobValidationError(f"cron 表达式解析失败: {e}") from e
        schedule_json = None

    # 语法合法不代表计划真的可执行，例如“2 月 31 日”。创建和更新都必须
    # 在任何 DB 查询/写入前确认至少存在一次未来触发，避免保存永不执行的任务，
    # 也避免 Agent 在 create_job 已提交后生成回执时才失败。
    try:
        future_fires = CronEngine.next_fires(expr, count=1)
    except CronExpressionError as e:
        raise CronJobValidationError(
            f"cron 表达式无法产生未来执行时间: {expr!r}"
        ) from e
    if not future_fires:
        raise CronJobValidationError(
            f"cron 表达式无法产生未来执行时间: {expr!r}"
        )
    return expr, schedule_json


class CronTask:
    """Cron 任务数据对象（路由层序列化用）"""

    def __init__(
        self,
        name: str,
        cron_expr: str,
        description: str,
        enabled: bool,
        *,
        schedule: dict | None = None,
        content: str = "",
        job_id: int | None = None,
        rule_version: int = 1,
        definition_version: int = 1,
    ):
        self.id = job_id
        self.rule_version = rule_version
        self.definition_version = definition_version
        self.name = name
        self.cron_expr = cron_expr
        self.description = description
        self.enabled = enabled
        self.schedule = schedule
        self.content = content

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "cron_expr": self.cron_expr,
            "schedule": self.schedule,
            "description": self.description,
            "content": self.content,
            "enabled": self.enabled,
            "rule_version": self.rule_version,
            "definition_version": self.definition_version,
        }


def parse_cron_fields(cron_expr: str) -> dict | None:
    """将 5 字段 cron 表达式拆为命名字段。

    保留此只读辅助函数供现有调用方使用；它不负责校验或调度语义。

    Returns:
        {"minute": ..., "hour": ..., "day": ..., "month": ..., "day_of_week": ...}
        解析失败返回 None
    """
    parts = cron_expr.split()
    if len(parts) != 5:
        return None

    keys = ["minute", "hour", "day", "month", "day_of_week"]
    return dict(zip(keys, parts))


def _extract_command_stdout(result: object) -> str:
    """兼容 OpenSandbox 命令结果的两种 stdout 结构（薄封装，向后兼容）。

    实际实现统一在 ``src.api.utils.sandbox_helpers.extract_command_stdout``。
    """
    from src.api.utils.sandbox_helpers import extract_command_stdout
    return extract_command_stdout(result)


# ============================================================
# CronService — DB 驱动
# ============================================================

class CronService:
    """Cron 任务管理服务（DB 驱动）"""

    def __init__(self, db: DBSession):
        self.db = db

    @contextmanager
    def _busy_guard(self):
        """包裹写操作段：任何 OperationalError(locked/busy) 统一映射为 503。"""
        try:
            yield
        except OperationalError as e:
            self.db.rollback()
            if _is_db_busy_error(e):
                raise CronJobBusyError("数据库繁忙，请稍后重试") from e
            raise

    def get_jobs(self, user_id: str) -> list[CronTask]:
        """从 CronJob 表获取用户所有定时任务"""
        jobs = (
            self.db.query(CronJob)
            .filter(CronJob.user_id == user_id)
            .order_by(CronJob.created_at)
            .all()
        )
        result: list[CronTask] = []
        for j in jobs:
            schedule_obj: dict | None = None
            raw_schedule = getattr(j, "schedule", None)
            if raw_schedule:
                # JSON 解析失败让它崩；schedule 由后端写入，结构损坏属于数据事故。
                schedule_obj = json.loads(raw_schedule)
            result.append(
                CronTask(
                    name=j.name,
                    cron_expr=j.cron_expr,
                    description=j.description or "",
                    enabled=j.enabled,
                    schedule=schedule_obj,
                    content=getattr(j, "content", "") or "",
                    job_id=j.id,
                    rule_version=int(getattr(j, "rule_version", 1) or 1),
                    definition_version=int(getattr(j, "definition_version", 1) or 1),
                )
            )
        return result

    def get_tasks(self, user_id: str) -> list[CronTask]:
        """获取用户的所有定时任务（get_jobs 别名，保持向下兼容）"""
        return self.get_jobs(user_id)

    # ────────────────────────── CRUD（HTTP + Agent 工具共用） ──────────────────────────

    def create_job(
        self,
        user_id: str,
        *,
        name: str,
        description: str = "",
        content: str = "",
        schedule: dict | None = None,
        cron_expr: str | None = None,
        enabled: bool = True,
    ) -> CronJob:
        """新建 CronJob，schedule 与 cron_expr 二选一（schedule 优先）。"""
        name = _validate_name(name)
        _validate_job_text(description, field="description", max_length=500)
        _validate_job_text(content, field="content", max_length=8000)
        expr, schedule_json = _resolve_cron_expr(schedule, cron_expr)

        with self._busy_guard():
            existing = (
                self.db.query(CronJob)
                .filter(CronJob.user_id == user_id, CronJob.name == name)
                .first()
            )
        if existing:
            raise CronJobValidationError(f"任务 '{name}' 已存在")

        job = CronJob(
            user_id=user_id,
            name=name,
            cron_expr=expr,
            schedule=schedule_json,
            description=description or "",
            content=content or "",
            enabled=bool(enabled),
            definition_version=1,
        )
        self.db.add(job)
        try:
            with self._busy_guard():
                self.db.commit()
                self.db.refresh(job)
        except IntegrityError as e:
            self.db.rollback()
            raise CronJobValidationError(f"任务 '{name}' 已存在") from e
        return job

    def update_job(
        self,
        user_id: str,
        name: str,
        *,
        description: str | None = None,
        content: str | None = None,
        schedule: dict | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
    ) -> CronJob:
        """更新 CronJob。任意字段省略则不动；schedule/cron_expr 至少传一个时才会改时间。"""
        _validate_job_text(description, field="description", max_length=500)
        _validate_job_text(content, field="content", max_length=8000)
        with self._busy_guard():
            job = (
                self.db.query(CronJob)
                .filter(CronJob.user_id == user_id, CronJob.name == name)
                .with_for_update()
                .first()
            )
        if not job:
            raise CronJobNotFoundError(f"任务 '{name}' 不存在")

        definition_changed = False
        if schedule is not None or cron_expr is not None:
            expr, schedule_json = _resolve_cron_expr(schedule, cron_expr)
            if expr != job.cron_expr:
                job.rule_version = int(job.rule_version or 1) + 1
                definition_changed = True
            job.cron_expr = expr
            job.schedule = schedule_json

        if description is not None:
            definition_changed = definition_changed or description != (job.description or "")
            job.description = description
        if content is not None:
            definition_changed = definition_changed or content != (job.content or "")
            job.content = content
        if enabled is not None:
            job.enabled = bool(enabled)
        if definition_changed:
            job.definition_version = int(job.definition_version or 1) + 1

        with self._busy_guard():
            self.db.commit()
            self.db.refresh(job)
        return job

    def delete_job(self, user_id: str, name: str) -> None:
        with self._busy_guard():
            job = (
                self.db.query(CronJob)
                .filter(CronJob.user_id == user_id, CronJob.name == name)
                .first()
            )
        if not job:
            raise CronJobNotFoundError(f"任务 '{name}' 不存在")

        with self._busy_guard():
            # 允许清理去重键历史，确保删除在有外键约束时依然可用。
            self.db.query(CronFire).filter(CronFire.job_id == job.id).delete(synchronize_session=False)

            # CronJobRun 历史保留：用户仍可在执行记录中看到过往运行。
            self.db.delete(job)
            self.db.commit()

    def get_run_history(
        self, user_id: str, job_name: str | None = None, limit: int = 20, offset: int = 0,
    ) -> tuple[list[dict], int]:
        """获取执行历史（分页）

        Returns:
            (runs_list, total_count)
        """
        query = self.db.query(CronJobRun).filter(CronJobRun.user_id == user_id)
        if job_name:
            query = query.filter(CronJobRun.job_name == job_name)
        total = query.count()
        runs = query.order_by(CronJobRun.queued_at.desc(), CronJobRun.id.desc()).offset(offset).limit(limit).all()
        return [CronJobRun.to_dict(r) for r in runs], total


async def _get_renewed_cron_sandbox(sandbox_service, user_id: str, sandbox_id: str | None):
    """Renew the frozen Sandbox, never replacing an existing run generation."""
    if not sandbox_id:
        return await sandbox_service.get_or_resume_and_renew(user_id, None)

    sandbox = await sandbox_service.get_existing(user_id, sandbox_id)
    connected_id = getattr(sandbox, "id", None)
    if connected_id != sandbox_id:
        raise RuntimeError("Cron 连接到非冻结 Sandbox")
    if not await sandbox_service.renew(user_id):
        raise RuntimeError("Cron 冻结 Sandbox 续租失败")
    if sandbox_service.get_sandbox_id(user_id) != sandbox_id:
        raise RuntimeError("Cron Sandbox 在续租期间发生代际切换")
    return sandbox


def _decode_definition_snapshot(record: CronJobRun | None) -> dict | None:
    raw = getattr(record, "definition_snapshot", None) if record is not None else None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _set_run_phase(
    run_id: str,
    phase: str,
    *,
    claim_token: str | None = None,
    run_workspace: str | None = None,
) -> bool:
    """Advance a live run only while the caller still owns its claim."""
    from src.api.models.database import SessionLocal

    with SessionLocal() as db:
        query = db.query(CronJobRun).filter(
            CronJobRun.id == run_id,
            CronJobRun.status == "running",
        )
        if claim_token is not None:
            query = query.filter(
                CronJobRun.claim_token == claim_token,
                CronJobRun.claim_lease_expires_at > now_naive(),
            )
        record = query.first()
        if record is None:
            return False
        record.phase = phase
        if run_workspace is not None:
            record.run_workspace = run_workspace
        db.commit()
        return True


def _set_run_sandbox_id(
    run_id: str,
    sandbox_id: str,
    *,
    claim_token: str | None = None,
) -> bool:
    """Freeze the exact Sandbox ID before Agent dispatch under the run claim."""
    if not isinstance(sandbox_id, str) or not sandbox_id:
        return False
    from src.api.models.database import SessionLocal

    with SessionLocal() as db:
        query = db.query(CronJobRun).filter(
            CronJobRun.id == run_id,
            CronJobRun.status == "running",
        )
        if claim_token is not None:
            query = query.filter(
                CronJobRun.claim_token == claim_token,
                CronJobRun.claim_lease_expires_at > now_naive(),
            )
        record = query.with_for_update().first()
        if record is None:
            return False
        current = record.sandbox_id if isinstance(record.sandbox_id, str) else None
        if current and current != sandbox_id:
            return False
        record.sandbox_id = sandbox_id
        db.commit()
        return True


def assert_cron_workspace_lease(
    db: DBSession,
    *,
    user_id: str,
    run_id: str,
    claim_token: str,
    at: datetime | None = None,
    for_update: bool = False,
) -> CronJobRun:
    """Fence every Cron workspace tool call against the live execution claim."""
    if not claim_token:
        raise PermissionError("Cron workspace claim token 缺失")
    query = db.query(CronJobRun).filter(
        CronJobRun.id == run_id,
        CronJobRun.user_id == user_id,
        CronJobRun.status == "running",
        CronJobRun.claim_token == claim_token,
        CronJobRun.claim_lease_expires_at.isnot(None),
        CronJobRun.claim_lease_expires_at > (at or now_naive()),
    )
    if for_update:
        query = query.with_for_update()
    record = query.first()
    if record is None:
        raise PermissionError("Cron workspace execution lease 已失效")
    return record


def append_cron_workspace_change(
    db: DBSession,
    *,
    user_id: str,
    run_id: str,
    claim_token: str,
    change: dict,
) -> bool:
    """Append one idempotent WorkspaceService mutation to a live Cron run.

    The workspace tool/gateway owns authorization and mutation execution. This
    helper only links its authoritative result back to CronJobRun without
    conflating published workspace files with ephemeral run artifacts.
    """
    if not isinstance(change, dict):
        raise TypeError("workspace change must be an object")
    idempotency_key = change.get("idempotency_key") or change.get("mutation_id")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("workspace change requires idempotency_key or mutation_id")

    record = assert_cron_workspace_lease(
        db,
        user_id=user_id,
        run_id=run_id,
        claim_token=claim_token,
        for_update=True,
    )
    try:
        existing = json.loads(record.workspace_changes or "[]")
    except (TypeError, ValueError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    if any(
        isinstance(item, dict)
        and (item.get("idempotency_key") or item.get("mutation_id")) == idempotency_key
        for item in existing
    ):
        return True
    if len(existing) >= 100:
        raise ValueError("workspace_changes exceeds 100 entries")
    existing.append(change)
    encoded = json.dumps(existing, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("workspace_changes exceeds 64 KiB")
    record.workspace_changes = encoded
    db.commit()
    return True


def build_cron_workspace_change_set_summaries(
    db: DBSession,
    *,
    run_id: str,
) -> list[dict]:
    rows = (
        db.query(WorkspaceChangeSet)
        .filter(
            WorkspaceChangeSet.cron_run_id == run_id,
            WorkspaceChangeSet.status.in_(("proposed", "conflict", "needs_review")),
        )
        .order_by(WorkspaceChangeSet.created_at.asc())
        .limit(100)
        .all()
    )
    summaries: list[dict] = []
    for row in rows:
        try:
            details = json.loads(row.details_json or "{}")
        except (TypeError, json.JSONDecodeError):
            details = {}
        summaries.append({
            "change_set_id": row.change_set_id,
            "entry_id": row.entry_id,
            "operation": row.operation,
            "status": row.status,
            "base_version_id": row.base_version_id,
            "proposed_version_id": row.proposed_version_id,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "target_name": details.get("destination_name"),
            "target_path": details.get("destination_path"),
        })
    return summaries


async def run_cron_job(
    user_id: str,
    job_name: str,
    run_id: str | None = None,
    *,
    expected_job_id: int | None = None,
    expected_rule_version: int | None = None,
    scheduled_at: datetime | None = None,
    trigger_source: str | None = None,
    claim_token: str | None = None,
) -> str | None:
    """执行单个 Cron 任务（从 CronJob DB 查任务定义）

    流程：查 DB 任务 → 恢复沙箱 → 创建内部 cron session → 统一 orchestrator 执行 → 记录结果

    Args:
        user_id: 用户 ID
        job_name: 任务名（CronJob.name）
        run_id: 可选，预创建的执行记录 ID（手动触发时由路由层预写入）

    Returns:
        执行结果摘要
    """
    from src.api.models.database import SessionLocal

    source = trigger_source or ("manual" if run_id else "scheduled")
    if not run_id:
        run_id = str(uuid.uuid4())

    run_workspace: str | None = None
    frozen_snapshot: dict | None = None
    current_snapshot: dict | None = None
    effective_job_id: int | None = None
    frozen_sandbox_id: str | None = None

    # 从 CronJob 表复核任务仍可执行；prompt 来自 queued run 的冻结快照。
    with SessionLocal() as db:
        auth_user = db.query(AuthUser).filter(AuthUser.user_id == user_id).first()
        if not auth_user or not auth_user.enabled:
            logger.warning("Cron 用户不存在或已禁用 (user=%s, job=%s)", user_id, job_name)
            _mark_run_failed(
                run_id,
                "用户不存在或已禁用",
                error_code="user_disabled",
                claim_token=claim_token,
            )
            return None

        job = (
            db.query(CronJob)
            .filter(CronJob.user_id == user_id, CronJob.name == job_name)
            .first()
        )
        if not job or (expected_job_id is not None and job.id != expected_job_id):
            logger.warning("Cron 任务不存在 (user=%s, job=%s)", user_id, job_name)
            _mark_run_failed(
                run_id,
                "任务不存在",
                error_code="job_missing",
                claim_token=claim_token,
            )
            return None
        if source == "scheduled" and not bool(job.enabled):
            _mark_run_failed(
                run_id,
                "任务已暂停",
                error_code="job_disabled",
                claim_token=claim_token,
            )
            return None
        rule_version = int(job.rule_version or 1)
        effective_job_id = int(job.id)
        if expected_rule_version is not None and rule_version != expected_rule_version:
            logger.info(
                "Cron 丢弃旧规则触发 (user=%s, job=%s, expected=%s, actual=%s)",
                user_id,
                job_name,
                expected_rule_version,
                rule_version,
            )
            _mark_run_failed(
                run_id,
                "任务调度规则已修改，请重新执行",
                error_code="stale_rule_version",
                claim_token=claim_token,
            )
            return None
        current_snapshot = build_cron_definition_snapshot(job)

    # Legacy/direct callers may not have pre-created a durable run. Preserve
    # compatibility while every worker/manual path now queues before execution.
    with SessionLocal() as db:
        existing = db.query(CronJobRun).filter(CronJobRun.id == run_id).first()
        if claim_token is not None and (
            existing is None
            or existing.status != "running"
            or existing.claim_token != claim_token
            or existing.claim_lease_expires_at is None
            or existing.claim_lease_expires_at <= now_naive()
        ):
            logger.warning("Cron claim 已丢失，拒绝启动 (run=%s)", run_id)
            return None
        candidate_sandbox_id = getattr(existing, "sandbox_id", None)
        if isinstance(candidate_sandbox_id, str) and candidate_sandbox_id:
            frozen_sandbox_id = candidate_sandbox_id
        frozen_snapshot = _decode_definition_snapshot(existing) or current_snapshot
        if frozen_snapshot is None:
            raise RuntimeError("Cron 定义快照不可用")
        task_description = (
            str(frozen_snapshot.get("content") or "").strip()
            or str(frozen_snapshot.get("description") or job_name)
        )
        cron_expr = str(frozen_snapshot.get("cron_expr") or current_snapshot["cron_expr"])
        definition_version = int(
            frozen_snapshot.get("definition_version")
            or current_snapshot["definition_version"]
            or 1
        )
        if not existing:
            run_record = CronJobRun(
                id=run_id,
                user_id=user_id,
                job_id=effective_job_id,
                job_name=job_name,
                cron_expr=cron_expr,
                rule_version=rule_version,
                definition_version=definition_version,
                definition_snapshot=json.dumps(
                    frozen_snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                scheduled_at=scheduled_at,
                trigger_source=source,
                status="running",
                phase="preparing",
                started_at=now_naive(),
                is_read=False,
            )
            db.add(run_record)
            db.commit()
        elif claim_token is None and existing.status == "queued":
            existing.status = "running"
            existing.phase = "preparing"
            existing.started_at = existing.started_at or now_naive()
            existing.attempt_count = int(existing.attempt_count or 0) + 1
            db.commit()

    try:
        # 恢复用户沙箱
        from src.api.services.sandbox_service import get_sandbox_service

        sandbox_service = get_sandbox_service()

        sandbox_id = frozen_sandbox_id
        if not sandbox_id:
            with SessionLocal() as db:
                user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                candidate_sandbox_id = user_sandbox.sandbox_id if user_sandbox else None
                sandbox_id = (
                    candidate_sandbox_id
                    if isinstance(candidate_sandbox_id, str) and candidate_sandbox_id
                    else None
                )

        sandbox = await _get_renewed_cron_sandbox(
            sandbox_service,
            user_id,
            sandbox_id,
        )
        connected_sandbox_id = getattr(sandbox, "id", None)
        latest_sandbox_id = (
            connected_sandbox_id
            if isinstance(connected_sandbox_id, str) and connected_sandbox_id
            else sandbox_service.get_sandbox_id(user_id)
        )
        if not isinstance(latest_sandbox_id, str) or not latest_sandbox_id:
            raise RuntimeError("Cron 无法确认冻结 Sandbox ID")
        if not _set_run_sandbox_id(
            run_id,
            latest_sandbox_id,
            claim_token=claim_token,
        ):
            raise RuntimeError("Cron Sandbox 绑定被 claim fence 拒绝")

        # 只有本轮开始时尚无 durable Sandbox 时才建立用户绑定；已有冻结 ID
        # 的运行不得把后来变化的 UserSandbox 指回旧代际。
        if frozen_sandbox_id is None:
            with SessionLocal() as db:
                user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
                runtime_config = sandbox_service.get_cached_runtime_config(user_id)
                if user_sandbox:
                    user_sandbox.sandbox_id = latest_sandbox_id
                    user_sandbox.status = "active"
                    if runtime_config:
                        user_sandbox.active_profile_id = runtime_config.profile_id
                        user_sandbox.active_profile_version = runtime_config.profile_version
                else:
                    import uuid

                    user_sandbox = UserSandbox(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        sandbox_id=latest_sandbox_id,
                        active_profile_id=runtime_config.profile_id if runtime_config else None,
                        active_profile_version=runtime_config.profile_version if runtime_config else None,
                        status="active",
                    )
                    db.add(user_sandbox)
                db.commit()

        try:
            from src.api.model_registry import get_model_registry
            from src.api.services.model_access_service import resolve_default_model_for_user

            registry = get_model_registry()
            with SessionLocal() as model_db:
                if getattr(registry, "source", "") == "db":
                    model_config = resolve_default_model_for_user(
                        model_db,
                        user_id,
                        kind="cron",
                        registry=registry,
                    )
                else:
                    model_config = registry.get_cron_default()
            cron_model_id = model_config.id
        except FileNotFoundError as e:
            raise RuntimeError(
                f"Model Registry 不可用: {e}. "
                "請修復 models.yaml 配置後重試。"
            ) from e
        except ValueError as e:
            raise RuntimeError(
                f"Model Registry 配置異常: {e}. "
                "請修復 models.yaml 或環境變數後重試。"
            ) from e

        # 创建与聊天 Agent 同源的工具集，但排除无人值守场景不应具备的交互、记忆、Cron 管理和 sub_agent 工具。
        mount = sandbox_service.get_mount_path(user_id)
        if not isinstance(mount, str):
            from src.api.services.sandbox_service import get_sandbox_mount_path

            mount = get_sandbox_mount_path()
        run_workspace = f"{mount}/cron/runs/{run_id}"
        # mount 均来自配置、run_id 为 UUID，但这里一律走 shlex.quote，
        # 避免未来 mount 含空格/特殊字符时静默断裂，与 cron-spec.md 明令保持一致。
        run_workspace_q = shlex.quote(run_workspace)

        # 确保 run 工作目录存在
        await sandbox.commands.run(f"mkdir -p {run_workspace_q}")

        from src.api.services.cron_channel_adapter import get_cron_channel_adapter
        from src.api.services.history_service import HistoryService
        from src.api.services.agent_service import AgentService
        from src.api.services.turn_orchestrator import get_turn_orchestrator
        from src.api.schemas.chat import TextContentBlock
        from src.agent.schema.agui_events import EventType

        cron_session_id = run_id
        with SessionLocal() as db:
            _ensure_cron_session(
                db=db,
                user_id=user_id,
                session_id=cron_session_id,
                job_name=job_name,
                run_id=run_id,
                cron_expr=cron_expr,
                source=source,
                model_id=cron_model_id,
            )

        cron_adapter = get_cron_channel_adapter()
        cron_turn = cron_adapter.normalize_run(
            user_id=user_id,
            session_id=cron_session_id,
            job_name=job_name,
            run_id=run_id,
            prompt=task_description,
            cron_expr=cron_expr,
            source=source,
            definition_version=definition_version,
            job_id=int(frozen_snapshot.get("job_id") or expected_job_id or effective_job_id),
        )
        task_prompt = cron_adapter.render_agent_prompt(cron_turn)
        orchestrated_turn = cron_turn.model_copy(
            update={"content": [TextContentBlock(type="text", text=task_prompt)]}
        )

        workspace_fence = None
        workspace_change_recorder = None
        if claim_token is not None:
            def workspace_fence(
                db,
                _user_id=user_id,
                _run_id=run_id,
                _claim=claim_token,
            ):
                assert_cron_workspace_lease(
                    db,
                    user_id=_user_id,
                    run_id=_run_id,
                    claim_token=_claim,
                )

            def workspace_change_recorder(
                db,
                change,
                _user_id=user_id,
                _run_id=run_id,
                _claim=claim_token,
            ):
                append_cron_workspace_change(
                    db,
                    user_id=_user_id,
                    run_id=_run_id,
                    claim_token=_claim,
                    change=change,
                )

        history_service = HistoryService(SessionLocal)
        agent_service = AgentService(
            sandbox=sandbox,
            history_service=history_service,
            session_id=cron_session_id,
            user_id=user_id,
            model_id=cron_model_id,
            tool_exclude={
                "AskUserQuestionTool",
                "SandboxSessionNoteTool",
                "SandboxRecallNoteTool",
                "UpdateLongTermMemoryTool",
                "SearchMemoryTool",
                "ReadUserProfileTool",
                "UpdateUserProfileTool",
                "ManageCronTool",
                "SubAgentTool",
            },
            system_prompt_override=(
                "You are a cron job executor. Complete the task efficiently.\n"
                f"所有普通运行产物必须保存在当前工作目录下（{run_workspace}）。\n"
                "仅当任务 prompt 要求操作用户持久工作区时，使用平台工作区工具完成读取、创建、更新、移动、重命名或永久删除；"
                "不得用 bash 或通用文件工具绕过工作区审计。"
            ),
            workspace_dir=run_workspace,
            workspace_access="manage",
            workspace_actor="cron",
            workspace_fence=workspace_fence,
            workspace_change_recorder=workspace_change_recorder,
            workspace_context={
                "cron_job_id": str(effective_job_id),
                "cron_run_id": run_id,
            },
            allow_human_interrupts=False,
        )
        await agent_service.initialize_agent()

        final_response = None
        accumulated_content = ""
        output_messages: list[str] = []
        run_status = "success"

        try:
            if claim_token is not None and not _set_run_phase(
                run_id,
                "executing",
                claim_token=claim_token,
                run_workspace=run_workspace,
            ):
                raise RuntimeError("Cron execution claim 已丢失")
            execution = await get_turn_orchestrator().submit_turn(
                orchestrated_turn,
                agent_service=agent_service,
            )
            async for event in execution.event_source:
                event_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
                if hasattr(event_type, "value"):
                    event_type = event_type.value

                if event_type == EventType.TEXT_MESSAGE_CONTENT.value:
                    delta = event.get("delta") if isinstance(event, dict) else getattr(event, "delta", "")
                    accumulated_content += delta or ""
                elif event_type == EventType.TEXT_MESSAGE_END.value:
                    final_response = accumulated_content or final_response
                    if accumulated_content:
                        output_messages.append(accumulated_content)
                    accumulated_content = ""
                elif event_type == EventType.RUN_FINISHED.value:
                    result = event.get("result") if isinstance(event, dict) else getattr(event, "result", None)
                    if isinstance(result, dict):
                        terminal_text = result.get("finalResponse") or result.get("final_response")
                        if isinstance(terminal_text, str) and terminal_text:
                            final_response = terminal_text
                    outcome = event.get("outcome") if isinstance(event, dict) else getattr(event, "outcome", None)
                    if outcome != "success":
                        run_status = "failed"
                elif event_type == EventType.RUN_ERROR.value:
                    run_status = "failed"
                    message = event.get("message") if isinstance(event, dict) else getattr(event, "message", None)
                    if isinstance(message, str) and message:
                        final_response = message

            if execution.task is not None:
                await execution.task
        finally:
            history_service.close()

        if final_response is None and accumulated_content:
            final_response = accumulated_content

        output = "\n\n".join(output_messages) or final_response or "Task completed (no output)"

        if claim_token is not None and not _set_run_phase(
            run_id,
            "publishing",
            claim_token=claim_token,
            run_workspace=run_workspace,
        ):
            raise RuntimeError("Cron publishing claim 已丢失")

        # 扫描产物文件
        artifacts_json = await _scan_run_artifacts(sandbox, run_workspace)

        # 更新执行记录
        with SessionLocal() as db:
            query = db.query(CronJobRun).filter(
                CronJobRun.id == run_id,
                CronJobRun.status == "running",
            )
            if claim_token is not None:
                query = query.filter(
                    CronJobRun.claim_token == claim_token,
                    CronJobRun.claim_lease_expires_at > now_naive(),
                )
            record = query.first()
            if record:
                record.workspace_change_sets = json.dumps(
                    build_cron_workspace_change_set_summaries(db, run_id=run_id),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                record.status = run_status
                record.phase = "terminal"
                record.completed_at = now_naive()
                record.output = output[:10000]  # 截断
                record.run_workspace = run_workspace
                record.artifacts = artifacts_json
                record.error_code = None if run_status == "success" else "agent_failed"
                record.claim_token = None
                record.claim_worker_id = None
                record.claim_lease_expires_at = None
                db.commit()
            else:
                logger.warning("Cron 终态写入被 claim fence 拒绝 (run=%s)", run_id)
                return None

        if run_status != "success":
            logger.warning("Cron 任务失败 (user=%s, job=%s, status=%s)", user_id, job_name, run_status)
            return None

        logger.info("Cron 任务完成 (user=%s, job=%s)", user_id, job_name)

        return output

    except Exception as e:
        logger.error("Cron 任务失败 (user=%s, job=%s): %s", user_id, job_name, e, exc_info=True)
        _mark_run_failed(
            run_id,
            f"Error: {e}",
            run_workspace,
            error_code="execution_error",
            claim_token=claim_token,
        )

        return None


async def _scan_run_artifacts(sandbox, run_workspace: str) -> str | None:
    """扫描 run_workspace 下的产物文件，返回 JSON 字符串。

    限制：最多 200 个文件，单路径最长 500 字符，总 JSON ≤ 64KB。
    扫描失败不阻塞主流程。
    """
    import json

    workspace_q = shlex.quote(run_workspace)
    public_files = f"find {workspace_q} -maxdepth 3 -type f -not -path '*/.workspace-change-sets/*'"

    try:
        # find -printf 是 GNU findutils 内置，不依赖外部 stat 命令
        # %p = 完整路径, %s = 文件大小(bytes)
        cmd = (
            f"{public_files} "
            f"-printf '%p\\t%s\\n' 2>/dev/null | head -200"
        )
        result = await sandbox.commands.run(cmd)
        stdout = _extract_command_stdout(result)

        # fallback: stat -c（兼容 BusyBox 等不支持 find -printf 的环境）
        if not stdout.strip():
            cmd = (
                f"{public_files} "
                f"-exec stat -c '%n\\t%s' {{}} \\; 2>/dev/null | head -200"
            )
            result = await sandbox.commands.run(cmd)
            stdout = _extract_command_stdout(result)

        # fallback: 仅路径（最大兼容），size 置 0
        path_only_mode = False
        if not stdout.strip():
            cmd = f"{public_files} 2>/dev/null | head -200"
            result = await sandbox.commands.run(cmd)
            stdout = _extract_command_stdout(result)
            path_only_mode = True

        if not stdout.strip():
            return None

        artifacts = []
        for line in stdout.strip().split("\n"):
            if path_only_mode:
                full_path = line
                size_str = "0"
            else:
                parts = line.split("\t", 1)
                if len(parts) != 2:
                    continue
                full_path, size_str = parts
            full_path = full_path.strip()
            if not full_path or len(full_path) > 500:
                continue
            # 相对路径
            rel_path = full_path
            if full_path.startswith(run_workspace + "/"):
                rel_path = full_path[len(run_workspace) + 1:]
            elif full_path.startswith(run_workspace):
                rel_path = full_path[len(run_workspace):]
            if is_workspace_publish_scratch_path(rel_path):
                continue
            name = rel_path.rsplit("/", 1)[-1] if "/" in rel_path else rel_path
            ext = name.rsplit(".", 1)[-1] if "." in name else ""
            try:
                size = int(size_str)
            except ValueError:
                size = 0
            artifacts.append({
                "name": name,
                "path": rel_path,
                "size": size,
                "type": ext,
            })

        if not artifacts:
            return None

        json_str = json.dumps(artifacts, ensure_ascii=False)
        if len(json_str) > 65536:
            # 截断到 64KB 限制内
            artifacts = artifacts[:100]
            json_str = json.dumps(artifacts, ensure_ascii=False)
        return json_str

    except Exception as e:
        logger.warning("扫描 Cron 产物失败 (workspace=%s): %s", run_workspace, e)
        return None
