"""用户级沙箱绑定模型

一个用户对应一个持久化沙箱，所有对话共享同一工作空间。
"""
from sqlalchemy import Column, String, DateTime
from .database import Base
from src.api.utils.timezone import now_naive


class UserSandbox(Base):
    """用户级沙箱绑定表

    一个用户一个沙箱：
    - user_id → sandbox_id（唯一绑定）
    - 所有 Session 共享同一沙箱工作空间
    - 各 Session 在沙箱内使用 /home/user/sessions/{session_id}/ 子目录隔离
    """

    __tablename__ = "user_sandboxes"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, unique=True, index=True)
    sandbox_id = Column(String(100), nullable=True)
    # active / paused
    status = Column(String(20), default="active")
    created_at = Column(DateTime, default=now_naive)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive)
