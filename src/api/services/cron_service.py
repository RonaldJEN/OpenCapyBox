"""Cron 服务 — DB 驱动 + APScheduler 动态注册 + 执行

职责：
- 从 CronJob DB 表管理定时任务定义
- 动态注册/注销 APScheduler CronTrigger 任务
- Runner：恢复用户沙箱 → 创建临时 Agent → 执行任务 → 写 CronJobRun
- 保留 parse_heartbeat_md() 以兼容旧测试
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.cron_job import CronJob
from src.api.models.user_memory import CronJobRun, UserMemory
from src.api.models.user_sandbox import UserSandbox
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)
settings = get_settings()


# ============================================================
# 旧格式解析（向下兼容，不再用于核心流程）
# ============================================================

# HEARTBEAT.md 任务行格式（仅用于旧数据兼容）:
_TASK_PATTERN = re.compile(
    r"^-\s*\[(?P<status>[xX ]?)\]\s+"
    r"(?P<name>\S+)\s+"
    r"(?P<cron>(?:\S+\s+){4}\S+)\s*"
    r"(?:-\s*(?P<desc>.+))?$"
)


class CronTask:
    """Cron 任务数据对象（兼容旧代码 + 新 DB 通用）"""

    def __init__(self, name: str, cron_expr: str, description: str, enabled: bool):
        self.name = name
        self.cron_expr = cron_expr
        self.description = description
        self.enabled = enabled

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "cron_expr": self.cron_expr,
            "description": self.description,
            "enabled": self.enabled,
        }


def parse_heartbeat_md(content: str) -> list[CronTask]:
    """解析 HEARTBEAT.md 内容为任务列表（旧格式兼容）

    格式规则：
    - [ ] task_name * * * * * - 描述 (启用)
    - [x] task_name * * * * * - 描述 (暂停)
    """
    tasks: list[CronTask] = []

    for line in content.splitlines():
        line = line.strip()
        match = _TASK_PATTERN.match(line)
        if not match:
            continue

        status = match.group("status").strip()
        name = match.group("name")
        cron_expr = match.group("cron").strip()
        desc = (match.group("desc") or "").strip()
        enabled = status != "x" and status != "X"

        tasks.append(CronTask(
            name=name,
            cron_expr=cron_expr,
            description=desc,
            enabled=enabled,
        ))

    return tasks


def parse_cron_fields(cron_expr: str) -> dict | None:
    """将 5 字段 cron 表达式解析为 APScheduler CronTrigger 参数

    Returns:
        {"minute": ..., "hour": ..., "day": ..., "month": ..., "day_of_week": ...}
        解析失败返回 None
    """
    parts = cron_expr.split()
    if len(parts) != 5:
        return None

    keys = ["minute", "hour", "day", "month", "day_of_week"]
    return dict(zip(keys, parts))


# ============================================================
# CronService — DB 驱动
# ============================================================

class CronService:
    """Cron 任务管理服务（DB 驱动）"""

    def __init__(self, db: DBSession):
        self.db = db

    def get_heartbeat_content(self, user_id: str) -> str:
        """从 DB 获取 HEARTBEAT.md 内容（仅用于 heartbeat 轮询）"""
        record = (
            self.db.query(UserMemory)
            .filter(UserMemory.user_id == user_id, UserMemory.file_type == "heartbeat_md")
            .first()
        )
        return record.content if record else ""

    def get_jobs(self, user_id: str) -> list[CronTask]:
        """从 CronJob 表获取用户所有定时任务"""
        jobs = (
            self.db.query(CronJob)
            .filter(CronJob.user_id == user_id)
            .order_by(CronJob.created_at)
            .all()
        )
        return [
            CronTask(
                name=j.name,
                cron_expr=j.cron_expr,
                description=j.description or "",
                enabled=j.enabled,
            )
            for j in jobs
        ]

    def get_tasks(self, user_id: str) -> list[CronTask]:
        """获取用户的所有定时任务（get_jobs 别名，保持向下兼容）"""
        return self.get_jobs(user_id)

    def get_run_history(
        self, user_id: str, job_name: str | None = None, limit: int = 20
    ) -> list[dict]:
        """获取执行历史"""
        query = self.db.query(CronJobRun).filter(CronJobRun.user_id == user_id)
        if job_name:
            query = query.filter(CronJobRun.job_name == job_name)
        runs = query.order_by(CronJobRun.started_at.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "job_name": r.job_name,
                "cron_expr": r.cron_expr,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "status": r.status,
                "output": r.output,
            }
            for r in runs
        ]


def register_user_jobs(db: DBSession, user_id: str, scheduler) -> int:
    """将用户 CronJob 表中的启用任务注册到 APScheduler

    Returns:
        注册的任务数
    """
    from apscheduler.triggers.cron import CronTrigger

    svc = CronService(db)
    tasks = svc.get_jobs(user_id)
    registered = 0

    for task in tasks:
        if not task.enabled:
            continue
        fields = parse_cron_fields(task.cron_expr)
        if not fields:
            logger.warning("Cron 表达式无效 (user=%s, job=%s): %s", user_id, task.name, task.cron_expr)
            continue

        job_id = f"cron-{user_id}-{task.name}"
        scheduler.add_job(
            _run_cron_job_wrapper,
            CronTrigger(**fields),
            id=job_id,
            name=f"{user_id}/{task.name}",
            replace_existing=True,
            kwargs={"user_id": user_id, "job_name": task.name},
        )
        registered += 1

    return registered


def reload_user_jobs(user_id: str, scheduler) -> int:
    """移除该用户的旧 job 并重新注册

    Returns:
        重新注册的任务数
    """
    # 先移除旧的
    prefix = f"cron-{user_id}-"
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            job.remove()

    from src.api.models.database import SessionLocal
    with SessionLocal() as db:
        return register_user_jobs(db, user_id, scheduler)


async def _run_cron_job_wrapper(user_id: str, job_name: str):
    """APScheduler AsyncIOScheduler 回调（直接 async，在主事件循环中执行）"""
    await run_cron_job(user_id, job_name)


async def run_cron_job(user_id: str, job_name: str) -> str | None:
    """执行单个 Cron 任务（从 CronJob DB 查任务定义）

    流程：查 DB 任务 → 恢复沙箱 → 创建临时 Agent → 执行任务 → 记录结果 → 注入聊天

    Args:
        user_id: 用户 ID
        job_name: 任务名（CronJob.name）

    Returns:
        执行结果摘要
    """
    from src.api.models.database import SessionLocal

    run_id = str(uuid.uuid4())

    # 从 CronJob 表查任务定义
    with SessionLocal() as db:
        job = (
            db.query(CronJob)
            .filter(CronJob.user_id == user_id, CronJob.name == job_name)
            .first()
        )
        if not job:
            logger.warning("Cron 任务不存在 (user=%s, job=%s)", user_id, job_name)
            return None
        task_description = job.description or job_name
        cron_expr = job.cron_expr

    with SessionLocal() as db:
        run_record = CronJobRun(
            id=run_id,
            user_id=user_id,
            job_name=job_name,
            cron_expr=cron_expr,
            status="running",
        )
        db.add(run_record)
        db.commit()

    try:
        # 恢复用户沙箱
        from src.api.services.sandbox_service import get_sandbox_service

        sandbox_service = get_sandbox_service()

        with SessionLocal() as db:
            user_sandbox = db.query(UserSandbox).filter(UserSandbox.user_id == user_id).first()
            sandbox_id = user_sandbox.sandbox_id if user_sandbox else None

        if not sandbox_id:
            raise RuntimeError(f"用户 {user_id} 无沙箱")

        sandbox = await sandbox_service.get_or_resume(user_id, sandbox_id)

        # 创建临时 Agent 执行任务
        from src.agent.llm import LLMClient
        from src.agent.schema import Message as AgentMessage

        try:
            from src.api.model_registry import get_model_registry
            model_config = get_model_registry().get_default()
            llm_client = LLMClient.from_model_config(model_config)
        except Exception:
            from src.agent.schema import LLMProvider
            llm_client = LLMClient(
                api_key=settings.llm_api_key,
                api_base=settings.llm_api_base,
                provider=(
                    LLMProvider.OPENAI
                    if settings.llm_provider.lower() == "openai"
                    else LLMProvider.ANTHROPIC
                ),
                model=settings.llm_model,
            )

        # 简单的单次 LLM 调用（带工具）
        from src.agent.tools.sandbox_bash_tool import SandboxBashTool
        from src.agent.tools.sandbox_file_tools import SandboxReadTool, SandboxWriteTool
        from src.api.services.sandbox_service import get_sandbox_mount_path

        mount = get_sandbox_mount_path()
        tools = [
            SandboxBashTool(sandbox=sandbox, workspace_dir=mount),
            SandboxReadTool(sandbox=sandbox, workspace_dir=mount),
            SandboxWriteTool(sandbox=sandbox, workspace_dir=mount),
        ]

        task_prompt = (
            f"你是一个定时任务执行器。请执行以下任务：\n\n"
            f"任务名：{job_name}\n"
            f"描述：{task_description}\n\n"
            f"请执行任务并给出简洁的结果摘要。"
        )

        from src.agent.agent import Agent

        agent = Agent(
            llm_client=llm_client,
            system_prompt="You are a cron job executor. Complete the task efficiently.",
            tools=tools,
            max_steps=10,
            workspace_dir=f"{mount}/cron",
            token_limit=50000,
        )

        # 执行（不使用 run_agui，简单收集最终结果）
        agent.add_user_message(task_prompt)
        final_response = None
        async for event in agent.run_agui(
            thread_id=f"cron-{user_id}",
            run_id=run_id,
        ):
            from src.agent.schema.agui_events import EventType
            if event.type == EventType.TEXT_MESSAGE_CONTENT:
                if final_response is None:
                    final_response = ""
                final_response += event.delta

        output = final_response or "Task completed (no output)"

        # 更新执行记录
        with SessionLocal() as db:
            record = db.query(CronJobRun).filter(CronJobRun.id == run_id).first()
            if record:
                record.status = "success"
                record.completed_at = now_naive()
                record.output = output[:10000]  # 截断
                db.commit()

        logger.info("Cron 任务完成 (user=%s, job=%s)", user_id, job_name)

        # 注入结果到用户最近活跃的 Session
        await _inject_cron_result_to_chat(user_id, job_name, output)

        return output

    except Exception as e:
        logger.error("Cron 任务失败 (user=%s, job=%s): %s", user_id, job_name, e, exc_info=True)

        with SessionLocal() as db:
            record = db.query(CronJobRun).filter(CronJobRun.id == run_id).first()
            if record:
                record.status = "failed"
                record.completed_at = now_naive()
                record.output = f"Error: {e}"
                db.commit()

        return None


async def _inject_cron_result_to_chat(
    user_id: str, job_name: str, output: str
) -> None:
    """将 Cron 执行结果注入到用户最近活跃的 Session

    创建一个系统 Round，包含完整的 AG-UI 事件序列，
    前端可通过轮询发现新 round 并展示。
    """
    try:
        from src.api.models.database import SessionLocal
        from src.api.models.session import Session

        with SessionLocal() as db:
            # 查找用户最近活跃的 Session
            session = (
                db.query(Session)
                .filter(Session.user_id == user_id, Session.status == "active")
                .order_by(Session.updated_at.desc())
                .first()
            )
            if not session:
                logger.info("用户 %s 无活跃 Session，跳过 Cron 结果注入", user_id)
                return

            session_id = session.id

            # 更新 Session.updated_at 以便前端轮询检测
            session.updated_at = now_naive()
            db.commit()

        # 注入系统 Round
        from src.api.services.history_service import HistoryService

        with SessionLocal() as db:
            history_svc = HistoryService(db)
            history_svc.inject_system_round(
                session_id=session_id,
                content=f"⏰ **定时任务 `{job_name}` 执行完成**\n\n{output}",
                source=f"cron:{job_name}",
            )

        logger.info(
            "Cron 结果已注入 Session (user=%s, session=%s, job=%s)",
            user_id, session_id, job_name,
        )

    except Exception as e:
        logger.warning("Cron 结果注入失败 (user=%s, job=%s): %s", user_id, job_name, e)
