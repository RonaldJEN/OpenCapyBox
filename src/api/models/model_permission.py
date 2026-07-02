"""Model permission package tables."""

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, text

from .database import Base
from src.api.utils.timezone import now_naive


class ModelPermissionGroup(Base):
    """A named package of models, such as default or a business-specific package."""

    __tablename__ = "model_permission_groups"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, nullable=False, default=False, server_default=text("false"), index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
    updated_at = Column(DateTime, default=now_naive, onupdate=now_naive, nullable=False)


class ModelPermissionGroupModel(Base):
    """Models contained in a permission group."""

    __tablename__ = "model_permission_group_models"
    __table_args__ = (
        UniqueConstraint("group_id", "model_id", name="uq_model_permission_group_model"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(String(36), ForeignKey("model_permission_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id = Column(String(100), ForeignKey("llm_models.model_id", ondelete="CASCADE"), nullable=False, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)


class UserModelPermissionGroup(Base):
    """Extra model permission groups assigned to a user."""

    __tablename__ = "user_model_permission_groups"
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", name="uq_user_model_permission_group"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), ForeignKey("auth_users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(String(36), ForeignKey("model_permission_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=now_naive, nullable=False)
