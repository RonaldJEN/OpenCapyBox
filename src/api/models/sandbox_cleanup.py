"""Durable cleanup intents for platform-owned Sandbox paths."""

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint

from .database import Base
from src.api.utils.timezone import now_naive


class SandboxCleanupJob(Base):
    __tablename__ = "sandbox_cleanup_jobs"
    __table_args__ = (
        UniqueConstraint(
            "owner_kind", "owner_id", "relative_path",
            name="uq_sandbox_cleanup_owner_path",
        ),
        Index("idx_sandbox_cleanup_due", "state", "next_attempt_at"),
        Index("idx_sandbox_cleanup_lease", "state", "lease_expires_at"),
    )

    cleanup_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False, index=True)
    owner_kind = Column(String(32), nullable=False)
    owner_id = Column(String(64), nullable=False)
    sandbox_id = Column(String(100), nullable=False)
    profile_id = Column(String(36), nullable=True)
    profile_version = Column(Integer, nullable=True)
    mount_path = Column(String(500), nullable=False)
    relative_path = Column(String(2000), nullable=False)
    state = Column(String(20), nullable=False, default="queued", index=True)
    owner_token = Column(String(64), nullable=True)
    generation = Column(BigInteger, nullable=False, default=0)
    lease_expires_at = Column(DateTime, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_naive)
    completed_at = Column(DateTime, nullable=True)


__all__ = ["SandboxCleanupJob"]
