"""用户运行锁模型（数据库并发控制）

用于限制同一用户同一时刻最多 N 个运行中的 Agent。
通过 user_id + slot 唯一约束保证数据库层的原子配额。
"""

import uuid

from sqlalchemy import Column, String, DateTime, Integer, UniqueConstraint

from .database import Base
from src.api.utils.timezone import now_naive


class UserRunLock(Base):
    """用户运行锁（短生命周期）"""

    __tablename__ = "user_run_locks"
    __table_args__ = (
        UniqueConstraint("user_id", "slot", name="uq_user_run_lock_user_slot"),
        UniqueConstraint("user_id", "session_id", name="uq_user_run_lock_user_session"),
    )

    # 每次运行一行：lock_id 用于 owner 校验释放，slot 用于 per-user 并发配额。
    lock_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(100), nullable=False, index=True)
    session_id = Column(String(36), nullable=False, index=True)
    slot = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
