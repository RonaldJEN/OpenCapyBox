"""Runtime-neutral human interaction state for one logical Round."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, text

from src.api.utils.timezone import now_naive

from .database import Base


class AgentInteraction(Base):
    """Durable request/answer pair that suspends and resumes one Round."""

    __tablename__ = "agent_interactions"
    __table_args__ = (
        Index("idx_agent_interactions_session_status", "session_id", "status"),
        Index("idx_agent_interactions_round_created", "round_id", "created_at"),
        Index(
            "uq_agent_interactions_pending_round",
            "round_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id = Column(String(36), primary_key=True)
    session_id = Column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    round_id = Column(
        String(36),
        ForeignKey("rounds.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind = Column(String(32), nullable=False, default="user_input")
    tool_call_id = Column(String(64), nullable=True, index=True)
    status = Column(
        String(20),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
        index=True,
    )
    request_payload = Column(Text, nullable=False)
    answer_payload = Column(Text, nullable=True)
    tool_result_content = Column(Text, nullable=True)
    external_request_id = Column(String(128), nullable=True, index=True)
    # An answered interaction stays ``pending`` while a continuation worker
    # owns this lease. The opaque token fences stale writes. Once
    # ``continuation_started_at`` is set in the same transaction as the durable
    # ``interaction_resolved`` event, expiry is no longer a reclaim boundary:
    # recovery must fail the Round instead of replaying the accepted answer.
    claim_token = Column(String(64), nullable=True)
    claim_lease_expires_at = Column(DateTime, nullable=True, index=True)
    continuation_started_at = Column(DateTime, nullable=True, index=True)
    requested_at = Column(DateTime, default=now_naive, nullable=False, index=True)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False, index=True)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
