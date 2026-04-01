"""Cron 定时任务数据模型

CronJob 表替代 HEARTBEAT.md 中的 Markdown 行存储，
实现 Cron 任务定义的持久化管理。
"""
from sqlalchemy import Column, String, Integer, Text, Boolean, DateTime, UniqueConstraint
from .database import Base
from src.api.utils.timezone import now_naive


class CronJob(Base):
    """Cron 定时任务定义

    由 Agent 通过 manage_cron 工具直接操作（增删改查），
    CronService 从此表读取任务并注册到 APScheduler。
    """

    __tablename__ = "cron_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_cronjob_user_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    # 任务名（同一用户下唯一）
    name = Column(String(100), nullable=False)
    # 5 字段 cron 表达式，如 "0 9 * * *"
    cron_expr = Column(String(50), nullable=False)
    # 任务描述（Agent 执行时作为 prompt）
    description = Column(Text, default="")
    # 是否启用
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=now_naive)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)
