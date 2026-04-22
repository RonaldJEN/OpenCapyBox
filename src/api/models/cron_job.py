"""Cron 定时任务数据模型

CronJob 表实现 Cron 任务定义的持久化管理。
"""
import json

from sqlalchemy import Column, String, Integer, Text, Boolean, DateTime, UniqueConstraint
from .database import Base
from src.api.utils.timezone import now_naive


class CronJob(Base):
    """Cron 定时任务定义

    由用户通过前端 SchedulePicker（/api/cron/jobs CRUD）或 Agent 通过 manage_cron
    工具操作；cron worker 每分钟扫描本表派发任务（去中心化，无主进程注册）。
    """

    __tablename__ = "cron_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_cronjob_user_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    # 任务名（同一用户下唯一）
    name = Column(String(100), nullable=False)
    # 5 字段 cron 表达式，如 "0 9 * * *"（由 schedule_to_cron 派生或 Agent 直传）
    cron_expr = Column(String(50), nullable=False)
    # 结构化时间配置（JSON），用于前端编辑回显；Agent 工具创建时为 NULL
    schedule = Column(Text, nullable=True)
    # 任务描述（人类可读）
    description = Column(Text, default="")
    # 执行内容：触发时投给 Agent 的 prompt（与 description 解耦）
    # 为空时回退到 description（兼容老数据）。
    content = Column(Text, nullable=False, default="")
    # 是否启用
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=now_naive)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)

    def to_dict(self) -> dict:
        schedule_obj: dict | None = None
        if self.schedule:
            schedule_obj = json.loads(self.schedule)
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "cron_expr": self.cron_expr,
            "schedule": schedule_obj,
            "description": self.description or "",
            "content": self.content or "",
            "enabled": bool(self.enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
