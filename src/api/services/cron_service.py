"""Cron 服务 — DB 驱动 + 任务执行

职责：
- 从 CronJob DB 表管理定时任务定义（CRUD：HTTP 路由与 Agent 工具共用）
- Runner：恢复用户沙箱 → 创建临时 Agent → 执行任务 → 写 CronJobRun

注：调度由 `cron_worker` 去中心化执行（不再依赖 APScheduler 持久注册），
本模块仅暴露 `parse_cron_fields` 与 `run_cron_job` 供 worker 调用。
"""

import json
import logging
import re
import shlex
import uuid
from contextlib import contextmanager

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as DBSession

from src.api.config import get_settings
from src.api.models.auth_user import AuthUser
from src.api.models.cron_fire import CronFire
from src.api.models.cron_job import CronJob
from src.api.models.user_memory import CronJobRun
from src.api.models.user_sandbox import UserSandbox
from src.api.services.cron_schedule import schedule_to_cron, ScheduleError
from src.api.utils.timezone import get_timezone, now_naive

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


def _mark_run_failed(run_id: str, output: str, run_workspace: str | None = None) -> None:
    from src.api.models.database import SessionLocal

    with SessionLocal() as db:
        record = db.query(CronJobRun).filter(CronJobRun.id == run_id).first()
        if record and record.status == "running":
            record.status = "failed"
            record.output = output
            record.completed_at = now_naive()
            record.run_workspace = run_workspace
            db.commit()


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise CronJobValidationError(
            "任务名必须为 1-100 字符的字母/数字/下划线/连字符"
        )
    return name


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
        return expr, json.dumps(schedule, ensure_ascii=False)
    if not cron_expr or not cron_expr.strip():
        raise CronJobValidationError("必须提供 schedule 或 cron_expr")
    expr = cron_expr.strip()
    parts = expr.split()
    if len(parts) != 5:
        raise CronJobValidationError(
            f"cron 表达式必须是 5 个字段（分 时 日 月 周），当前 {len(parts)} 个: {cron_expr!r}"
        )
    try:
        CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            timezone=get_timezone(),
        )
    except Exception as e:
        raise CronJobValidationError(f"cron 表达式解析失败: {expr!r}: {e}") from e
    return expr, None


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
    ):
        self.id = job_id
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
        }


def parse_cron_fields(cron_expr: str) -> dict | None:
    """将 5 字段 cron 表达式解析为 CronTrigger 字段（用于单分钟匹配，非持久注册）。

    返回值仅供 `cron_worker` 在每分钟唤醒时构造一次性 CronTrigger 做匹配，
    不喂给任何 scheduler 主进程或 jobstore。

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
        with self._busy_guard():
            job = (
                self.db.query(CronJob)
                .filter(CronJob.user_id == user_id, CronJob.name == name)
                .first()
            )
        if not job:
            raise CronJobNotFoundError(f"任务 '{name}' 不存在")

        if schedule is not None or cron_expr is not None:
            expr, schedule_json = _resolve_cron_expr(schedule, cron_expr)
            job.cron_expr = expr
            job.schedule = schedule_json

        if description is not None:
            job.description = description
        if content is not None:
            job.content = content
        if enabled is not None:
            job.enabled = bool(enabled)

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
        import json as _json

        query = self.db.query(CronJobRun).filter(CronJobRun.user_id == user_id)
        if job_name:
            query = query.filter(CronJobRun.job_name == job_name)
        total = query.count()
        runs = query.order_by(CronJobRun.started_at.desc()).offset(offset).limit(limit).all()
        result = []
        for r in runs:
            artifacts = None
            if r.artifacts:
                try:
                    artifacts = _json.loads(r.artifacts)
                except (ValueError, TypeError):
                    artifacts = None
            result.append({
                "id": r.id,
                "job_name": r.job_name,
                "cron_expr": r.cron_expr,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "status": r.status,
                "output": r.output,
                "is_read": bool(getattr(r, 'is_read', True)),
                "artifacts": artifacts,
                "run_workspace": getattr(r, 'run_workspace', None),
            })
        return result, total


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
        auth_user = db.query(AuthUser).filter(AuthUser.user_id == user_id).first()
        if not auth_user or not auth_user.enabled:
            logger.warning("Cron 用户不存在或已禁用 (user=%s, job=%s)", user_id, job_name)
            _mark_run_failed(run_id, "用户不存在或已禁用")
            return None

        job = (
            db.query(CronJob)
            .filter(CronJob.user_id == user_id, CronJob.name == job_name)
            .first()
        )
        if not job:
            logger.warning("Cron 任务不存在 (user=%s, job=%s)", user_id, job_name)
            _mark_run_failed(run_id, "任务不存在")
            return None
        # content 是新表单的"执行内容"字段，优先作为 prompt；
        # 老数据 content 为空时回退到 description（与之前行为一致）。
        task_description = (job.content or "").strip() or (job.description or job_name)
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
            model_config = get_model_registry().get_cron_default()
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
        # mount 均来自配置、run_id 为 UUID，但这里一律走 shlex.quote，
        # 避免未来 mount 含空格/特殊字符时静默断裂，与 cron-spec.md 明令保持一致。
        run_workspace_q = shlex.quote(run_workspace)

        # 确保 run 工作目录存在
        await sandbox.commands.run(f"mkdir -p {run_workspace_q}")

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
        _mark_run_failed(run_id, f"Error: {e}", run_workspace)

        return None


async def _scan_run_artifacts(sandbox, run_workspace: str) -> str | None:
    """扫描 run_workspace 下的产物文件，返回 JSON 字符串。

    限制：最多 200 个文件，单路径最长 500 字符，总 JSON ≤ 64KB。
    扫描失败不阻塞主流程。
    """
    import json

    workspace_q = shlex.quote(run_workspace)

    try:
        # find -printf 是 GNU findutils 内置，不依赖外部 stat 命令
        # %p = 完整路径, %s = 文件大小(bytes)
        cmd = (
            f"find {workspace_q} -maxdepth 3 -type f "
            f"-printf '%p\\t%s\\n' 2>/dev/null | head -200"
        )
        result = await sandbox.commands.run(cmd)
        stdout = _extract_command_stdout(result)

        # fallback: stat -c（兼容 BusyBox 等不支持 find -printf 的环境）
        if not stdout.strip():
            cmd = (
                f"find {workspace_q} -maxdepth 3 -type f "
                f"-exec stat -c '%n\\t%s' {{}} \\; 2>/dev/null | head -200"
            )
            result = await sandbox.commands.run(cmd)
            stdout = _extract_command_stdout(result)

        # fallback: 仅路径（最大兼容），size 置 0
        path_only_mode = False
        if not stdout.strip():
            cmd = f"find {workspace_q} -maxdepth 3 -type f 2>/dev/null | head -200"
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
