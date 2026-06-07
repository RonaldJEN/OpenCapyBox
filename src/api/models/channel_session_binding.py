"""Channel identity to internal session binding."""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, UniqueConstraint

from src.api.models.database import Base
from src.api.utils.timezone import now_naive


class ChannelSessionBinding(Base):
    """Maps an external channel peer to an internal user/session."""

    __tablename__ = "channel_session_bindings"
    __table_args__ = (
        UniqueConstraint("user_id", "binding_key", name="uq_channel_session_bindings_user_binding_key"),
        Index("idx_channel_session_bindings_session_id", "session_id"),
        Index("idx_channel_session_bindings_channel_peer", "channel", "account_id", "peer_kind", "peer_id"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(100), ForeignKey("auth_users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(String(50), nullable=False, index=True)
    account_id = Column(String(100), nullable=True, index=True)
    peer_kind = Column(String(20), nullable=False)
    peer_id = Column(String(255), nullable=False)
    external_thread_id = Column(String(255), nullable=True)
    binding_key = Column(String(64), nullable=False, index=True)
    reply_route_json = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)
