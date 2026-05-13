"""认证用户模型。"""

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text, text

from .database import Base
from src.api.utils.timezone import now_naive


class AuthUser(Base):
    """运行时用户与权限事实源。"""

    __tablename__ = "auth_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), unique=True, nullable=False, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    auth_type = Column(String(20), nullable=False, default="simple", server_default=text("'simple'"))
    password_hash = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    is_admin = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    token_limit_per_month = Column(BigInteger, nullable=True)
    token_limit_per_week = Column(BigInteger, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    token_generation = Column(Integer, nullable=False, default=0, server_default=text("0"))
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
