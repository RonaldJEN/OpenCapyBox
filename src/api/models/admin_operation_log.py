"""Append-only audit trail for authenticated administrator operations."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)

from .database import Base
from src.api.utils.timezone import now_naive


class AdminOperationLog(Base):
    """One durable audit row per authenticated admin HTTP request.

    The table deliberately has no foreign keys: deleting an application user,
    conversation, or LLM record must never erase the corresponding audit
    evidence.
    """

    __tablename__ = "admin_operation_logs"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('started', 'succeeded', 'failed')",
            name="ck_admin_operation_logs_outcome",
        ),
        Index(
            "ix_admin_operation_logs_started_id",
            "started_at",
            "id",
        ),
        Index(
            "ix_admin_operation_logs_actor_started",
            "actor_user_id",
            "started_at",
        ),
        Index(
            "ix_admin_operation_logs_action_started",
            "action",
            "started_at",
        ),
        Index(
            "ix_admin_operation_logs_target_user_started",
            "target_user_id",
            "started_at",
        ),
        Index(
            "ix_admin_operation_logs_session_started",
            "session_id",
            "started_at",
        ),
    )

    # SQLite only auto-increments an exact INTEGER PRIMARY KEY. Production
    # keeps the wider PostgreSQL type while tests remain representative.
    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    request_id = Column(String(36), nullable=False, unique=True, index=True)
    actor_user_id = Column(String(100), nullable=False)
    action = Column(String(100), nullable=False)

    target_type = Column(String(50), nullable=True)
    target_id = Column(String(255), nullable=True)
    target_user_id = Column(String(100), nullable=True)
    session_id = Column(String(36), nullable=True)
    step_record_id = Column(Integer, nullable=True)

    outcome = Column(
        String(20),
        nullable=False,
        default="started",
        server_default="started",
        index=True,
    )
    http_method = Column(String(10), nullable=False)
    route_template = Column(String(255), nullable=False)
    status_code = Column(Integer, nullable=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(Text, nullable=True)

    # JSON is stored as text so the schema behaves identically on PostgreSQL
    # and SQLite. Serialization and redaction are centralized in the service.
    changed_fields = Column(Text, nullable=True)
    details_json = Column(Text, nullable=True)

    started_at = Column(DateTime, default=now_naive, nullable=False, index=True)
    completed_at = Column(DateTime, nullable=True)
