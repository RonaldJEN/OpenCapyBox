"""Generic tool permission, approval, and audit records.

The permission domain deliberately references MCP servers by stable UUID while
remaining usable by built-in tools.  Connection/authentication state belongs to
the MCP domain; this module only records whether a concrete tool call may run.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from .database import Base
from src.api.utils.timezone import now_naive


class ToolPermissionRule(Base):
    """ALLOW/ASK/DENY rule for a stable tool identity."""

    __tablename__ = "tool_permission_rules"
    __table_args__ = (
        Index(
            "idx_tool_permission_match",
            "scope_type",
            "scope_id",
            "provider",
            "server_id",
            "tool_name",
        ),
    )

    id = Column(String(36), primary_key=True)
    scope_type = Column(String(20), nullable=False, index=True)
    scope_id = Column(String(100), nullable=True, index=True)
    provider = Column(String(20), nullable=False, index=True)
    server_id = Column(
        String(36),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    tool_name = Column(String(255), nullable=False, default="*", server_default=text("'*'"))
    effect = Column(String(10), nullable=False)
    priority = Column(Integer, nullable=False, default=0, server_default=text("0"))
    managed = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    conditions_json = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    expires_at = Column(DateTime, nullable=True, index=True)
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)


class ToolApprovalRequest(Base):
    """Durable, single-execution record for an ASK decision."""

    __tablename__ = "tool_approval_requests"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uq_tool_approval_run_call"),
        Index("idx_tool_approval_pending_user", "user_id", "status", "requested_at"),
        Index(
            "idx_tool_approval_execution_lease",
            "status",
            "execution_lease_expires_at",
        ),
    )

    id = Column(String(36), primary_key=True)  # also the AG-UI interrupt id
    user_id = Column(String(100), nullable=False, index=True)
    session_id = Column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id = Column(
        String(36),
        ForeignKey("rounds.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_call_id = Column(String(64), nullable=False, index=True)
    provider = Column(String(20), nullable=False)
    # Keep the original identities as durable evidence even when a user or
    # administrator deletes the MCP catalog entry while approval is pending.
    # Runtime ownership/availability is rechecked separately before execution.
    server_id = Column(String(36), nullable=True, index=True)
    installation_id = Column(String(36), nullable=True, index=True)
    tool_name = Column(String(255), nullable=False)
    model_tool_name = Column(String(255), nullable=False)
    arguments_encrypted = Column(Text, nullable=False)
    arguments_hash = Column(String(64), nullable=False)
    schema_hash = Column(String(64), nullable=True)
    connection_fingerprint = Column(String(64), nullable=True)
    policy_version = Column(String(128), nullable=True)
    matched_rule_id = Column(
        String(36),
        ForeignKey("tool_permission_rules.id", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(String(20), nullable=False, default="requested", server_default=text("'requested'"))
    resolution = Column(String(20), nullable=True)
    result_encrypted = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    requested_at = Column(DateTime, default=now_naive, nullable=False, index=True)
    resolved_at = Column(DateTime, nullable=True)
    execution_started_at = Column(DateTime, nullable=True)
    # A claim token fences a stale worker from completing a newer execution
    # claim. The lease is only a liveness signal: expiry never authorizes a
    # retry because the remote side effect may already have happened.
    execution_claim_token = Column(String(64), nullable=True)
    execution_lease_expires_at = Column(DateTime, nullable=True, index=True)
    completed_at = Column(DateTime, nullable=True)


class ToolPermissionAudit(Base):
    """Append-only evidence for permission and execution outcomes."""

    __tablename__ = "tool_permission_audits"
    __table_args__ = (
        Index("idx_tool_permission_audit_session", "session_id", "created_at"),
        Index("idx_tool_permission_audit_user", "user_id", "created_at"),
    )

    # SQLite only auto-increments an exact ``INTEGER PRIMARY KEY``. Keep the
    # wider PostgreSQL type in production while preserving local/test audit
    # persistence instead of silently dropping every record.
    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id = Column(String(100), nullable=False, index=True)
    session_id = Column(String(36), nullable=True, index=True)
    run_id = Column(String(36), nullable=True, index=True)
    tool_call_id = Column(String(64), nullable=True, index=True)
    provider = Column(String(20), nullable=False)
    server_id = Column(String(36), nullable=True, index=True)
    tool_name = Column(String(255), nullable=False)
    effect = Column(String(10), nullable=False)
    matched_rule_id = Column(String(36), nullable=True)
    reason = Column(Text, nullable=True)
    arguments_hash = Column(String(64), nullable=True)
    outcome = Column(String(30), nullable=False)
    created_at = Column(DateTime, default=now_naive, nullable=False, index=True)
