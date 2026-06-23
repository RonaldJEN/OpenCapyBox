"""Per-user sandbox profile assignment model."""

from sqlalchemy import Column, DateTime, String

from .database import Base
from src.api.utils.timezone import now_naive


class UserSandboxConfig(Base):
    """Optional admin assignment of a user to a sandbox profile."""

    __tablename__ = "user_sandbox_configs"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, unique=True, index=True)
    sandbox_profile_id = Column(String(36), nullable=True, index=True)
    updated_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
