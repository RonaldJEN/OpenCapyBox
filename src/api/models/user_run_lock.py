"""用户运行锁模型（跨 worker 并发控制）

用于限制同一用户同一时刻仅有一个运行中的 Agent。
通过 user_id 主键约束保证跨进程/跨 worker 的原子互斥。
"""

import uuid

from sqlalchemy import Column, String, DateTime

from .database import Base
from src.api.utils.timezone import now_naive


class UserRunLock(Base):
    """用户运行锁（短生命周期）"""

    __tablename__ = "user_run_locks"

    # 一用户一把锁：主键冲突即表示已有运行中的任务
    user_id = Column(String(100), primary_key=True)
    session_id = Column(String(36), nullable=False, index=True)
    lock_id = Column(String(36), nullable=False, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
