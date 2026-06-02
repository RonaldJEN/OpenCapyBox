"""认证登录审计事件模型。"""

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from .database import Base
from src.api.utils.timezone import now_naive


class AuthLoginEvent(Base):
    """Web 登录历史记录。"""

    __tablename__ = "auth_login_events"
    __table_args__ = (
        Index("ix_auth_login_events_user_id_login_at", "user_id", "login_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    username = Column(String(100), nullable=False)
    auth_type = Column(String(20), nullable=False, default="simple")
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(Text, nullable=True)
    login_at = Column(DateTime, default=now_naive, nullable=False, index=True)
