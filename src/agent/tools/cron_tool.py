"""manage_cron 工具 — 供 Agent 管理 Cron 定时任务

通过 DB 直接增删改查 CronJob，并实时同步 APScheduler。
"""

import logging
from typing import Any

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)


def _validate_cron_expr(cron_expr: str) -> str | None:
    """校验 5 字段 cron 表达式，返回错误信息或 None"""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return f"cron 表达式必须是 5 个字段（分 时 日 月 周），当前 {len(parts)} 个字段: '{cron_expr}'"
    return None


class ManageCronTool(Tool):
    """管理 Cron 定时任务（增删改查 + 执行历史）"""

    def __init__(self, db_session_factory, user_id: str, scheduler=None):
        self._db_factory = db_session_factory
        self._user_id = user_id
        self._scheduler = scheduler

    @property
    def name(self) -> str:
        return "manage_cron"

    @property
    def description(self) -> str:
        return (
            "Manage cron scheduled tasks. Actions:\n"
            "- add: Create a new cron job (requires name, cron, description)\n"
            "- remove: Delete a cron job by name\n"
            "- list: List all cron jobs for this user\n"
            "- toggle: Enable/disable a cron job by name\n"
            "- history: View recent execution history\n\n"
            "Cron expression uses 5 fields: minute hour day month day_of_week\n"
            "Examples: '0 9 * * *' (daily 9am), '0 9 * * 1' (Monday 9am), "
            "'*/30 * * * *' (every 30 min)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list", "toggle", "history"],
                    "description": "Action to perform",
                },
                "name": {
                    "type": "string",
                    "description": "Job name (required for add/remove/toggle/history)",
                },
                "cron": {
                    "type": "string",
                    "description": "5-field cron expression (required for add). Format: minute hour day month day_of_week",
                },
                "description": {
                    "type": "string",
                    "description": "Job description - what the agent should do when triggered (required for add)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        name: str = "",
        cron: str = "",
        description: str = "",
    ) -> ToolResult:
        try:
            if action == "add":
                return self._do_add(name, cron, description)
            elif action == "remove":
                return self._do_remove(name)
            elif action == "list":
                return self._do_list()
            elif action == "toggle":
                return self._do_toggle(name)
            elif action == "history":
                return self._do_history(name)
            else:
                return ToolResult(success=False, error=f"未知 action: {action}")
        except Exception as e:
            logger.error("manage_cron 执行失败: %s", e, exc_info=True)
            return ToolResult(success=False, error=str(e))

    def _do_add(self, name: str, cron_expr: str, description: str) -> ToolResult:
        if not name or not name.strip():
            return ToolResult(success=False, error="任务名 name 不能为空")
        if not cron_expr or not cron_expr.strip():
            return ToolResult(success=False, error="cron 表达式不能为空")
        if not description or not description.strip():
            return ToolResult(success=False, error="任务描述 description 不能为空")

        name = name.strip()
        cron_expr = cron_expr.strip()
        description = description.strip()

        err = _validate_cron_expr(cron_expr)
        if err:
            return ToolResult(success=False, error=err)

        from src.api.models.cron_job import CronJob

        db = self._db_factory()
        try:
            existing = (
                db.query(CronJob)
                .filter(CronJob.user_id == self._user_id, CronJob.name == name)
                .first()
            )
            if existing:
                return ToolResult(
                    success=False,
                    error=f"任务 '{name}' 已存在。如需修改，请先 remove 再 add。",
                )

            job = CronJob(
                user_id=self._user_id,
                name=name,
                cron_expr=cron_expr,
                description=description,
                enabled=True,
            )
            db.add(job)
            db.commit()

            # 实时注册到 APScheduler — 注册失败则标记为 disabled 并告警
            warning = ""
            try:
                self._register_to_scheduler(name, cron_expr)
            except Exception as e:
                logger.warning("任务 '%s' 已写入 DB 但 Scheduler 注册失败: %s", name, e)
                job.enabled = False
                db.commit()
                warning = f"（⚠️ Scheduler 注册失败，任务已暂停: {e}）"

            return ToolResult(
                success=True,
                content=f"已创建定时任务 '{name}' (cron: {cron_expr}): {description}{warning}",
            )
        finally:
            db.close()

    def _do_remove(self, name: str) -> ToolResult:
        if not name or not name.strip():
            return ToolResult(success=False, error="任务名 name 不能为空")

        name = name.strip()
        from src.api.models.cron_job import CronJob

        db = self._db_factory()
        try:
            job = (
                db.query(CronJob)
                .filter(CronJob.user_id == self._user_id, CronJob.name == name)
                .first()
            )
            if not job:
                return ToolResult(success=False, error=f"任务 '{name}' 不存在")

            db.delete(job)
            db.commit()

            # 从 APScheduler 注销
            self._unregister_from_scheduler(name)

            return ToolResult(success=True, content=f"已删除定时任务 '{name}'")
        finally:
            db.close()

    def _do_list(self) -> ToolResult:
        from src.api.models.cron_job import CronJob

        db = self._db_factory()
        try:
            jobs = (
                db.query(CronJob)
                .filter(CronJob.user_id == self._user_id)
                .order_by(CronJob.created_at)
                .all()
            )
            if not jobs:
                return ToolResult(success=True, content="当前没有定时任务。")

            lines = []
            for j in jobs:
                status = "✅ 启用" if j.enabled else "⏸️ 暂停"
                lines.append(f"- {j.name} | {j.cron_expr} | {status} | {j.description}")
            return ToolResult(success=True, content="\n".join(lines))
        finally:
            db.close()

    def _do_toggle(self, name: str) -> ToolResult:
        if not name or not name.strip():
            return ToolResult(success=False, error="任务名 name 不能为空")

        name = name.strip()
        from src.api.models.cron_job import CronJob

        db = self._db_factory()
        try:
            job = (
                db.query(CronJob)
                .filter(CronJob.user_id == self._user_id, CronJob.name == name)
                .first()
            )
            if not job:
                return ToolResult(success=False, error=f"任务 '{name}' 不存在")

            job.enabled = not job.enabled
            db.commit()

            # 同步 APScheduler
            if job.enabled:
                self._register_to_scheduler(name, job.cron_expr)
            else:
                self._unregister_from_scheduler(name)

            new_status = "启用" if job.enabled else "暂停"
            return ToolResult(
                success=True,
                content=f"任务 '{name}' 已切换为: {new_status}",
            )
        finally:
            db.close()

    def _do_history(self, name: str) -> ToolResult:
        from src.api.models.user_memory import CronJobRun

        db = self._db_factory()
        try:
            query = db.query(CronJobRun).filter(CronJobRun.user_id == self._user_id)
            if name and name.strip():
                query = query.filter(CronJobRun.job_name == name.strip())
            runs = query.order_by(CronJobRun.started_at.desc()).limit(10).all()

            if not runs:
                return ToolResult(success=True, content="暂无执行历史。")

            lines = []
            for r in runs:
                ts = r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "?"
                output_preview = (r.output or "")[:100]
                lines.append(f"- [{r.status}] {r.job_name} @ {ts}: {output_preview}")
            return ToolResult(success=True, content="\n".join(lines))
        finally:
            db.close()

    def _register_to_scheduler(self, job_name: str, cron_expr: str) -> None:
        """将任务注册到 APScheduler。

        Raises:
            Exception: 注册失败时向上传播，调用方决定如何处理。
        """
        if not self._scheduler:
            logger.debug("APScheduler 不可用，跳过注册")
            return

        from src.api.services.cron_service import parse_cron_fields, _run_cron_job_wrapper

        fields = parse_cron_fields(cron_expr)
        if not fields:
            raise ValueError(f"无法解析 cron 表达式: {cron_expr}")

        from apscheduler.triggers.cron import CronTrigger

        job_id = f"cron-{self._user_id}-{job_name}"
        self._scheduler.add_job(
            _run_cron_job_wrapper,
            CronTrigger(**fields),
            id=job_id,
            name=f"{self._user_id}/{job_name}",
            replace_existing=True,
            kwargs={
                "user_id": self._user_id,
                "job_name": job_name,
            },
        )

    def _unregister_from_scheduler(self, job_name: str) -> None:
        """从 APScheduler 注销任务（best-effort）"""
        try:
            if not self._scheduler:
                return

            job_id = f"cron-{self._user_id}-{job_name}"
            existing = self._scheduler.get_job(job_id)
            if existing:
                existing.remove()
        except Exception as e:
            logger.warning("注销 APScheduler 任务失败 (job=%s): %s", job_name, e)
