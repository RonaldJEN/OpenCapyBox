"""Durable replacement histories used by the model-context projector."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from src.api.utils.timezone import now_naive

from .database import Base


class ContextCheckpoint(Base):
    """Immutable compacted history for one session.

    The original rounds/messages/events remain the audit source of truth.  A
    checkpoint is only a bounded projection used to rebuild provider context.
    """

    __tablename__ = "context_checkpoints"
    __table_args__ = (
        UniqueConstraint("session_id", "generation", name="uq_context_checkpoint_session_generation"),
        Index("idx_context_checkpoints_session_generation", "session_id", "generation"),
    )

    checkpoint_id = Column(String(36), primary_key=True)
    session_id = Column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation = Column(Integer, nullable=False)
    source_round_id = Column(
        String(36),
        ForeignKey("rounds.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    source_message_sequence = Column(Integer, nullable=False, default=0)
    source_event_sequence = Column(Integer, nullable=False, default=0)
    trigger_phase = Column(String(30), nullable=False, default="pre_turn")
    summary_text = Column(Text, nullable=False, default="")
    schema_version = Column(Integer, nullable=False, default=4)
    replacement_messages_json = Column(Text, nullable=False)
    source_token_count = Column(Integer, nullable=True)
    replacement_token_count = Column(Integer, nullable=True)
    replacement_sha256 = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_naive, index=True)
