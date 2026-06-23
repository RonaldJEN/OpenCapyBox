"""OpenSandbox backend profile model."""

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from .database import Base
from src.api.utils.timezone import now_naive


class SandboxProfile(Base):
    """Admin-managed OpenSandbox backend configuration.

    A profile represents one OpenSandbox VM/backend. Users without an explicit
    profile assignment use the single default profile.
    """

    __tablename__ = "sandbox_profiles"

    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    department = Column(String(100), nullable=True)
    domain = Column(String(255), nullable=False)
    protocol = Column(String(10), nullable=False, default="http")
    api_key = Column(Text, nullable=True)
    use_server_proxy = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False, index=True)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
