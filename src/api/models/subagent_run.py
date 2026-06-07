"""Subagent run graph edge model."""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, UniqueConstraint

from src.api.models.database import Base
from src.api.utils.timezone import now_naive


class SubagentRun(Base):
    """A directed edge from a parent run to a spawned subagent run."""

    __tablename__ = "subagent_runs"
    __table_args__ = (
        UniqueConstraint("child_run_id", name="uq_subagent_runs_child_run_id"),
        Index("idx_subagent_runs_parent_status", "parent_run_id", "status"),
        Index("idx_subagent_runs_root_status", "root_run_id", "status"),
        Index("idx_subagent_runs_tool_call", "parent_run_id", "tool_call_id"),
    )

    REQUESTED = "requested"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STATUSES: frozenset[str] = frozenset({REQUESTED, RUNNING, COMPLETED, FAILED, CANCELLED})

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(100), ForeignKey("auth_users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    root_run_id = Column(String(36), ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_run_id = Column(String(36), ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    child_run_id = Column(String(36), ForeignKey("rounds.id", ondelete="SET NULL"), nullable=True, index=True)

    tool_call_id = Column(String(64), nullable=True, index=True)
    agent_name = Column(String(100), nullable=True)
    agent_type = Column(String(100), nullable=True, index=True)
    model_id = Column(String(100), nullable=True)
    description = Column(String(255), nullable=True)
    prompt = Column(Text, nullable=False)
    isolation = Column(String(40), nullable=True)
    worktree_path = Column(String(500), nullable=True)

    status = Column(String(20), nullable=False, default=REQUESTED, index=True)
    output = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)

    created_at = Column(DateTime, default=now_naive, nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
