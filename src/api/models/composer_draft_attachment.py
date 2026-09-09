"""Durable ledger for files selected before a real Session exists."""

from sqlalchemy import Column, DateTime, Index, Integer, String

from .database import Base
from src.api.utils.timezone import now_naive


class ComposerDraftAttachment(Base):
    __tablename__ = "composer_draft_attachments"
    __table_args__ = (
        Index("idx_composer_draft_attachment_expiry", "state", "expires_at"),
        Index("idx_composer_draft_attachment_user_draft", "user_id", "draft_id"),
    )

    attachment_id = Column(String(36), primary_key=True)
    user_id = Column(String(100), nullable=False)
    draft_id = Column(String(64), nullable=False)
    name = Column(String(512), nullable=True)
    content_type = Column(String(255), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    content_sha256 = Column(String(64), nullable=True)
    sandbox_id = Column(String(100), nullable=True)
    mount_path = Column(String(500), nullable=True)
    state = Column(String(20), nullable=False, default="uploading")
    generation = Column(Integer, nullable=False, default=1)
    claimed_session_id = Column(String(36), nullable=True, index=True)
    claimed_path = Column(String(2000), nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_naive)
    updated_at = Column(DateTime, nullable=False, default=now_naive, onupdate=now_naive)
    expires_at = Column(DateTime, nullable=False, index=True)
    deleted_at = Column(DateTime, nullable=True)


__all__ = ["ComposerDraftAttachment"]
