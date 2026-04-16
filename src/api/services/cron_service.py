"""Cron 服务 — DB 驱动 + APScheduler 动态注册 + 执行

职责：
- 从 CronJob DB 表管理定时任务定义
- 动态注册/注销 APScheduler CronTrigger 任务
- Runner：恢复用户沙箱 → 创建临时 Agent → 执行任务 → 写 CronJobRun
"""

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.cron_job import CronJob
from src.api.models.user_memory import CronJobRun
from src.api.models.user_sandbox import UserSandbox
from src.api.utils.timezone import now_naive

logger = logging.getLogger(__name__)
settings = get_settings()


class CronTask:
    """Cron 任务数据对象"""

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


# re-export 以保持已有 import 不破坏
from src.api.utils.sandbox_helpers import extract_command_stdout  # noqa: F401
from src.api.utils.sandbox_helpers import extract_command_stdout as _extract_command_stdout  # noqa: F401


# ============================================================
# CronService — DB 驱动
# ============================================================

class CronService:
    """Cron 任务管理服务（DB 驱动）"""

    def __init__(self, db: DBSession):
        self.db = db

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
        runs = query.order_by(CronJobRun.started_at.desc()).offset(offset).limit(limit).all()
        return [r.to_dict() for r in runs], total


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


async def run_cron_job(user_id: str, job_name: str, run_id: str | None = None) -> str | None:
    """执行单个 Cron 任务（从 CronJob DB 查任务定义）

    流程：查 DB 任务 → 恢复沙箱 → 创建临时 Agent → 执行任务 → 记录结果 → 注入聊天

    Args:
        user_id: 用户 ID
        job_name: 任务名（CronJob.name）
        run_id: 可选，预创建的执行记录 ID（手动触发时由路由层预写入）

    Returns:
        执行结果摘要
    """
    from src.api.models.database import SessionLocal

    if not run_id:
        run_id = str(uuid.uuid4())

    run_workspace: str | None = None

    # 从 CronJob 表查任务定义
    with SessionLocal() as db:
        job = (
            db.query(CronJob)
            .filter(CronJob.user_id == user_id, CronJob.name == job_name)
            .first()
        )
        if not job:
            logger.warning("Cron 任务不存在 (user=%s, job=%s)", user_id, job_name)
            # 兜底：将预创建的 run 记录标记为 failed，避免永远停留在 running
            with SessionLocal() as db2:
                rec = db2.query(CronJobRun).filter(CronJobRun.id == run_id).first()
                if rec and rec.status == "running":
                    rec.status = "failed"
                    rec.output = "任务不存在"
                    rec.completed_at = now_naive()
                    db2.commit()
            return None
        task_description = job.description or job_name
        cron_expr = job.cron_expr

    # 如果 run_id 对应的记录已存在（手动触发预创建），跳过；否则新建
    with SessionLocal() as db:
        existing = db.query(CronJobRun).filter(CronJobRun.id == run_id).first()
        if not existing:
            run_record = CronJobRun(
                id=run_id,
                user_id=user_id,
                job_name=job_name,
                cron_expr=cron_expr,
                status="running",
                is_read=False,
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
            cron_token_limit = model_config.compute_token_limit()
            cron_context_window = model_config.context_window
            cron_max_output_tokens = model_config.max_tokens
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

        # 创建与聊天 Agent 相同的工具集（排除 AskUserQuestionTool，Cron 无人交互）
        from src.api.services.sandbox_service import get_sandbox_mount_path
        from src.api.services.tool_factory import create_agent_tools

        mount = get_sandbox_mount_path()
        run_workspace = f"{mount}/cron/runs/{run_id}"

        # 确保 run 工作目录存在
        await sandbox.commands.run(f"mkdir -p {run_workspace}")

        tools, _ = await create_agent_tools(
            sandbox=sandbox,
            workspace_dir=run_workspace,
            mount=mount,
            user_id=user_id,
            db_session_factory=SessionLocal,
            exclude={
                "AskUserQuestionTool",
                "SandboxSessionNoteTool",
                "SandboxRecallNoteTool",
                "RecordDailyLogTool",
                "UpdateLongTermMemoryTool",
                "SearchMemoryTool",
                "ReadUserProfileTool",
                "UpdateUserProfileTool",
                "ManageCronTool",
            },
        )

        task_prompt = (
            f"你是一个定时任务执行器。请执行以下任务：\n\n"
            f"任务名：{job_name}\n"
            f"描述：{task_description}\n\n"
            f"请执行任务并给出简洁的结果摘要。"
        )

        from src.agent.agent import Agent

        agent = Agent(
            llm_client=llm_client,
            system_prompt=(
                "You are a cron job executor. Complete the task efficiently.\n"
                f"所有产出文件必须保存在当前工作目录下（{run_workspace}），禁止写入其他路径。"
            ),
            tools=tools,
            max_steps=settings.agent_max_steps,
            workspace_dir=run_workspace,
            token_limit=cron_token_limit,
            context_window=cron_context_window,
            max_output_tokens=cron_max_output_tokens,
            tool_timeout=settings.agent_tool_timeout,
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

        # 扫描产物文件
        artifacts_json = await _scan_run_artifacts(sandbox, run_workspace)

        # 更新执行记录
        with SessionLocal() as db:
            record = db.query(CronJobRun).filter(CronJobRun.id == run_id).first()
            if record:
                record.status = "success"
                record.completed_at = now_naive()
                record.output = output[:10000]  # 截断
                record.run_workspace = run_workspace
                record.artifacts = artifacts_json
                db.commit()

        logger.info("Cron 任务完成 (user=%s, job=%s)", user_id, job_name)

        return output

    except Exception as e:
        logger.error("Cron 任务失败 (user=%s, job=%s): %s", user_id, job_name, e, exc_info=True)

        with SessionLocal() as db:
            record = db.query(CronJobRun).filter(CronJobRun.id == run_id).first()
            if record:
                record.status = "failed"
                record.completed_at = now_naive()
                record.output = f"Error: {e}"
                record.run_workspace = run_workspace
                db.commit()

        return None


def _build_artifact(full_path: str, run_workspace: str, size: int = 0) -> dict | None:
    """将完整路径转换为产物元数据 dict，不合法返回 None。"""
    full_path = full_path.strip()
    if not full_path or len(full_path) > 500:
        return None
    rel_path = full_path
    if full_path.startswith(run_workspace + "/"):
        rel_path = full_path[len(run_workspace) + 1:]
    elif full_path.startswith(run_workspace):
        rel_path = full_path[len(run_workspace):]
    if not rel_path:
        return None
    name = rel_path.rsplit("/", 1)[-1] if "/" in rel_path else rel_path
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return {"name": name, "path": rel_path, "size": size, "type": ext}


def _artifacts_to_json(artifacts: list[dict]) -> str | None:
    """将产物列表序列化为 JSON，超过 64KB 则截断到 100 条。"""
    import json
    if not artifacts:
        return None
    json_str = json.dumps(artifacts, ensure_ascii=False)
    if len(json_str) > 65536:
        artifacts = artifacts[:100]
        json_str = json.dumps(artifacts, ensure_ascii=False)
    return json_str


async def _scan_run_artifacts(sandbox, run_workspace: str) -> str | None:
    """扫描 run_workspace 下的产物文件，返回 JSON 字符串。

    限制：最多 200 个文件，单路径最长 500 字符，总 JSON ≤ 64KB。
    扫描失败不阻塞主流程。
    """
    try:
        # find -printf 是 GNU findutils 内置，不依赖外部 stat 命令
        # %p = 完整路径, %s = 文件大小(bytes)
        cmd = (
            f"find {run_workspace} -maxdepth 3 -type f "
            f"-printf '%p\\t%s\\n' 2>/dev/null | head -200"
        )
        result = await sandbox.commands.run(cmd)
        stdout = _extract_command_stdout(result)

        # fallback: stat -c（兼容 BusyBox 等不支持 find -printf 的环境）
        if not stdout.strip():
            cmd = (
                f"find {run_workspace} -maxdepth 3 -type f "
                f"-exec stat -c '%n\\t%s' {{}} \\; 2>/dev/null | head -200"
            )
            result = await sandbox.commands.run(cmd)
            stdout = _extract_command_stdout(result)

        # fallback: 仅路径（最大兼容），size 置 0
        if not stdout.strip():
            cmd = f"find {run_workspace} -maxdepth 3 -type f 2>/dev/null | head -200"
            result = await sandbox.commands.run(cmd)
            path_only = _extract_command_stdout(result)
            if path_only.strip():
                artifacts = [
                    a for line in path_only.strip().split("\n")
                    if (a := _build_artifact(line, run_workspace)) is not None
                ]
                return _artifacts_to_json(artifacts)

        if not stdout.strip():
            return None

        artifacts = []
        for line in stdout.strip().split("\n"):
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            full_path, size_str = parts
            try:
                size = int(size_str)
            except ValueError:
                size = 0
            a = _build_artifact(full_path, run_workspace, size)
            if a is not None:
                artifacts.append(a)

        return _artifacts_to_json(artifacts)

    except Exception as e:
        logger.warning("扫描 Cron 产物失败 (workspace=%s): %s", run_workspace, e)
        return None
